"""Category editor Phase 5: runs, the job engine, gating, drift audit.

Requires a provisioned test database (TEST_DATABASE_DSN + TEST_DATABASE_ADMIN_DSN).
Reuses the Phase-4 golden scenario builder.
"""

import pytest

import categories_planner
import categories_runs
import categories_service
from categories_draft import DraftConflict
from db import database
from tests.fake_wp import FakeWordPress
from tests.test_categories_planner import _build_scenario, _read, _write


@pytest.fixture(autouse=True)
def _fast_broker_polls(monkeypatch):
    monkeypatch.setattr(categories_runs, "JOB_POLL_SECONDS", 0)


@pytest.fixture
def ready_scenario():
    nodes = _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"],
           note="test")
    return nodes


class BrokerRecorder(FakeWordPress):
    """The stateful fake WordPress (tests/fake_wp.py) under its historical
    name: `.calls` records every phase call, `fail_on` fails a path at the
    transport layer. Unlike the old recorder it APPLIES each phase to real
    per-blog state, so the engine's live-state fence, verification and
    restore run against something that behaves like WordPress."""

    def install(self, monkeypatch, env="prod", blog_ids=(1, 7)):
        for blog_id in blog_ids:
            self.seed_from_snapshot(env, blog_id)
        monkeypatch.setattr(categories_runs, "broker_call", self)
        monkeypatch.setattr(categories_service, "fetch_export", self.export)
        monkeypatch.setattr(categories_runs, "fetch_wp_status", self.status)
        return self


@pytest.fixture
def broker(monkeypatch, ready_scenario):
    return BrokerRecorder().install(monkeypatch)


def test_create_run_requires_clean_preview():
    _build_scenario()  # zero-category blockers present, no acks
    with pytest.raises(DraftConflict):
        _write(categories_runs.create_run, env="prod", blog_ids=None)


def test_create_and_process_run(broker):
    run = _write(categories_runs.create_run, env="prod", blog_ids=None)
    assert run["status"] == "queued"
    assert [j["blog_id"] for j in run["jobs"]] == [1, 7]   # blog 1 first
    assert run["jobs"][0]["seq"] == 1

    # a second run for the same env is refused while one is active
    with pytest.raises(DraftConflict):
        _write(categories_runs.create_run, env="prod", blog_ids=None)

    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed"
    assert all(j["status"] == "done" for j in done["jobs"])
    assert all(j["has_snapshot"] for j in done["jobs"])

    paths = [p for _, p, _ in broker.calls]
    # per blog: terms -> memberships (blog1 has 2 rows -> 1 page) -> finalize
    assert paths == ["/apply-terms", "/apply-memberships", "/finalize",
                     "/apply-terms", "/finalize"]
    blog1_finalize = broker.calls[2][2]
    assert blog1_finalize["blog_id"] == 1
    assert blog1_finalize["unspsc_renames"] == {"men-s": "mens"}
    assert len(blog1_finalize["redirects"]) == 5
    blog7_terms = broker.calls[3][2]
    assert blog7_terms["blog_id"] == 7

    with database.cursor() as cursor:
        cursor.execute(
            "SELECT status, count(*) AS n FROM catmgr.redirect"
            " WHERE run_id = %s GROUP BY status", (run["run_id"],),
        )
        assert {r["status"]: r["n"] for r in cursor.fetchall()} == {"created": 5}


def test_membership_paging_and_progress(broker, monkeypatch):
    monkeypatch.setattr(categories_runs, "MEMBERSHIP_PAGE_SIZE", 1)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed"
    membership_calls = [p for _, p, _ in broker.calls
                        if p == "/apply-memberships"]
    assert len(membership_calls) == 5  # blog1 has 5 membership rows, page=1
    job = done["jobs"][0]
    assert job["progress"]["membership_offset"] == 5
    assert job["result"]["memberships"] == {"applied": 5, "total": 5}


def test_failure_stops_run_and_retry_resumes(ready_scenario, monkeypatch):
    recorder = BrokerRecorder(fail_on={"/finalize"}).install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=None)
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    statuses = {j["blog_id"]: j["status"] for j in failed["jobs"]}
    assert statuses[1] == "failed"
    assert statuses[7] == "pending"   # stop_on_failure left it queued

    job1 = next(j for j in failed["jobs"] if j["blog_id"] == 1)
    with pytest.raises(DraftConflict):
        _write(categories_runs.skip_job, run["run_id"] + 999, job1["job_id"])

    recorder.fail_on = set()          # WP recovered; retry converges
    _write(categories_runs.retry_job, run["run_id"], job1["job_id"])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    retried = next(j for j in done["jobs"] if j["blog_id"] == 1)
    assert retried["attempt"] == 2
    # A retry is a new attempt with its own broker keys (a refused finalize
    # must actually run again), but progress survived: terms + memberships
    # were NOT re-run.
    assert retried["request_id"] == f"j{retried['job_id']}a2"
    term_calls = [c for c in recorder.calls if c[1] == "/apply-terms"
                  and c[2]["blog_id"] == 1]
    assert len(term_calls) == 1
    assert retried["result"]["verified"] is True


def test_pause_between_jobs_and_resume(broker):
    run = _write(categories_runs.create_run, env="prod", blog_ids=None)
    first = categories_runs.process_run(run["run_id"], actor="tester",
                                        max_jobs=1)
    assert {j["status"] for j in first["jobs"]} == {"done", "pending"}
    _write(categories_runs.request_pause, run["run_id"])
    paused = categories_runs.process_run(run["run_id"], actor="tester")
    assert paused["status"] == "paused"
    assert {j["blog_id"]: j["status"] for j in paused["jobs"]}[7] == "pending"

    _write(categories_runs.resume, run["run_id"])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed"

    with pytest.raises(DraftConflict):
        _write(categories_runs.cancel, run["run_id"])  # already completed


def test_stale_snapshot_fence(broker):
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    # a re-import bumps the snapshot version after the plan was frozen
    with database.cursor(write=True, actor="drift") as cursor:
        categories_service.import_blog_snapshot(
            cursor, env="prod", blog_id=1, blog_path="/",
            terms=[{"term_id": 1, "slug": "men-s", "name": "X", "parent": 0}],
            products=[], actor="drift",
        )
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    assert "re-plan required" in failed["jobs"][0]["result"]["error"]


def test_restore_uses_job_snapshot(broker):
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    before = broker.export("prod", 1)
    result = categories_runs.restore_blog(run["run_id"], job["job_id"],
                                          actor="tester", background=False)
    assert result["accepted"] is True and result["restore"]["status"] == "done", result
    assert result["restore"]["verified"] is True
    restore_calls = [c for c in broker.calls if c[1] == "/restore"]
    assert [c[2]["phase"] for c in restore_calls] == ["terms", "memberships", "finalize"]
    assert restore_calls[0][2]["blog_id"] == 1
    assert restore_calls[0][2]["expected_blog_path"] == "/"
    # the pre-apply snapshot (not the applied state) is what goes back
    assert {t["slug"] for t in restore_calls[0][2]["snapshot"]["terms"]} == {
        "men-s", "men-s-bottoms", "saws", "old-boots", "lonely", "field-uniform"}
    after = broker.export("prod", 1)
    assert {t["slug"] for t in after["terms"]} == {t["slug"] for t in restore_calls[0][2]["snapshot"]["terms"]}
    assert after["products"] != before["products"]


def test_apply_routes_gated_by_allowlist(client_as, monkeypatch, ready_scenario):
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    monkeypatch.setenv("CATMGR_PROD_URL", "https://prod.example.test/base")
    monkeypatch.setenv("CATMGR_PROD_USER", "svc")
    monkeypatch.setenv("CATMGR_PROD_APP_PASSWORD", "pw")
    from config import get_settings
    get_settings.cache_clear()
    client = client_as("admin-one")

    refused = client.post("/api/categories/runs",
                          json={"env": "prod", "start": False})
    assert refused.status_code == 403   # empty allowlist fails closed

    monkeypatch.setenv("CATMGR_APPLY_USERS", "ADMIN-ONE")
    get_settings.cache_clear()
    BrokerRecorder().install(monkeypatch)
    created = client.post("/api/categories/runs",
                          json={"env": "prod", "start": False})
    assert created.status_code == 200
    run = created.json()["run"]
    assert run["status"] == "queued"

    listing = client.get("/api/categories/runs", params={"env": "prod"})
    assert listing.status_code == 200
    assert listing.json()["runs"][0]["run_id"] == run["run_id"]
    get_settings.cache_clear()


def test_drift_audit_converges(broker, monkeypatch):
    run = _write(categories_runs.create_run, env="prod", blog_ids=None)
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", [j["result"] for j in done["jobs"]]
    # WordPress (the fake) now holds the applied tree; a fresh audit re-plans
    # it to zero changes on every blog.
    outcome = categories_runs.drift_audit("prod", actor="tester")
    assert outcome["refreshed_blogs"] == 2
    assert outcome["converged"] is True, outcome
    assert outcome["pending"] == []
