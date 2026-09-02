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
from tests.test_categories_planner import _build_scenario, _read, _write


@pytest.fixture
def ready_scenario():
    nodes = _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"],
           note="test")
    return nodes


class BrokerRecorder:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def __call__(self, env, path, payload):
        self.calls.append((env, path, payload))
        if path in self.fail_on:
            raise categories_service.BrokerError(f"boom on {path}", 503)
        if path == "/apply-terms":
            return {"ok": True, "updated": len(payload["updates"]),
                    "created": len(payload["creates"])}
        if path == "/apply-memberships":
            return {"ok": True, "applied": len(payload["rows"]),
                    "skipped": [], "missing_slugs": []}
        if path == "/finalize":
            return {"ok": True, "deleted": len(payload["deletes"]),
                    "redirects_created": [
                        {"old_path": r["old_path"], "new_path": r["new_path"]}
                        for r in payload["redirects"]
                    ],
                    "unspsc_rewritten": len(payload["unspsc_renames"])}
        if path == "/restore":
            return {"ok": True, "terms": len(payload["snapshot"]["terms"])}
        raise AssertionError(f"unexpected broker path {path}")


@pytest.fixture
def broker(monkeypatch, ready_scenario):
    recorder = BrokerRecorder()
    monkeypatch.setattr(categories_runs, "broker_call", recorder)
    monkeypatch.setattr(
        categories_service, "fetch_export",
        lambda env, blog_id: {"blog_path": "/", "terms": [
            {"term_id": 1, "slug": "live", "name": "Live", "parent": 0},
        ], "products": []},
    )
    return recorder


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
    assert len(membership_calls) == 2  # blog1 has 2 membership rows, page=1
    job = done["jobs"][0]
    assert job["progress"]["membership_offset"] == 2
    assert job["result"]["memberships"] == {"applied": 2, "total": 2}


def test_failure_stops_run_and_retry_resumes(ready_scenario, monkeypatch):
    recorder = BrokerRecorder(fail_on={"/finalize"})
    monkeypatch.setattr(categories_runs, "broker_call", recorder)
    monkeypatch.setattr(
        categories_service, "fetch_export",
        lambda env, blog_id: {"blog_path": "/", "terms": [], "products": []},
    )
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
    assert done["status"] == "completed"
    retried = next(j for j in done["jobs"] if j["blog_id"] == 1)
    assert retried["attempt"] == 2
    # progress survived: terms + memberships were NOT re-run on retry
    term_calls = [c for c in recorder.calls if c[1] == "/apply-terms"
                  and c[2]["blog_id"] == 1]
    assert len(term_calls) == 1


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
    result = categories_runs.restore_blog(run["run_id"], job["job_id"],
                                          actor="tester")
    assert result["ok"] is True
    restore_call = broker.calls[-1]
    assert restore_call[1] == "/restore"
    assert restore_call[2]["blog_id"] == 1
    assert restore_call[2]["snapshot"]["terms"][0]["slug"] == "live"


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
    recorder = BrokerRecorder()
    monkeypatch.setattr(categories_runs, "broker_call", recorder)
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
    categories_runs.process_run(run["run_id"], actor="tester")

    # Simulate WordPress now matching the target: live export == plan result.
    def converged_export(env, blog_id):
        with database.cursor() as cursor:
            plan_target = categories_planner.blog_target(cursor, blog_id)
        terms = []
        next_id = 1000
        slug_to_id = {}
        for node in plan_target["kept"].values():
            slug_to_id[node["slug"]] = next_id
            terms.append({"term_id": next_id, "slug": node["slug"],
                          "name": node["name"], "parent": 0,
                          "sort_order": node["sort_order"]})
            next_id += 1
        for term in terms:
            node = next(n for n in plan_target["kept"].values()
                        if n["slug"] == term["slug"])
            if node["parent_slug"]:
                term["parent"] = slug_to_id[node["parent_slug"]]
        for extra in plan_target["extras"]:
            terms.append({"term_id": next_id, "slug": extra["slug"],
                          "name": extra["name"],
                          "parent": slug_to_id.get(extra["parent_slug"], 0)})
            next_id += 1
        # store_custom live terms stay
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.term_id, t.slug, t.name FROM catmgr.wp_term t
                  JOIN catmgr.slug_map m ON m.old_slug = t.slug
                 WHERE t.env = 'prod' AND t.blog_id = %s
                   AND m.action = 'store_custom'
                """, (blog_id,),
            )
            for row in cursor.fetchall():
                terms.append({"term_id": row["term_id"], "slug": row["slug"],
                              "name": row["name"], "parent": 0})
        return {"blog_path": "/", "terms": terms, "products": []}

    monkeypatch.setattr(categories_service, "fetch_export", converged_export)
    outcome = categories_runs.drift_audit("prod", actor="tester")
    assert outcome["refreshed_blogs"] == 2
    assert outcome["converged"] is True
    assert outcome["pending"] == []
