"""Category editor hardening (2026-09-03): the holes the three audits found.

Requires the provisioned test database. Reuses the Phase-4 golden scenario.
"""

from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

import categories_draft
import categories_mapping
import categories_planner
import categories_runs
import categories_service
from categories_draft import DraftConflict, DraftError
from db import database
from tests.conftest import TEST_ADMIN_DSN
from tests.test_categories_planner import _build_scenario, _read, _write
from tests.test_categories_runs import BrokerRecorder


@pytest.fixture(autouse=True)
def _fast_broker_polls(monkeypatch):
    monkeypatch.setattr(categories_runs, "JOB_POLL_SECONDS", 0)


@pytest.fixture
def ready(monkeypatch):
    nodes = _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"], note="test")
    recorder = BrokerRecorder().install(monkeypatch)
    return nodes, recorder


def _rewind_after_terms(run_id, job_id):
    """Put a finished job back to 'terms landed, worker died' so recovery
    resumes it from its cursor against the (already converged) fake."""
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run_job SET status='running', finished_at=NULL, result=NULL, progress = %s WHERE job_id = %s",
                           ('{"snapshot_taken": true, "terms_done": true}', job_id))
            cursor.execute("UPDATE catmgr.run SET status='running', finished_at=NULL, worker_heartbeat_at = now() - interval '30 minutes' WHERE run_id = %s",
                           (run_id,))


def _set_heartbeat(run_id, minutes_ago):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE catmgr.run SET worker_heartbeat_at = now() - (%s || ' minutes')::interval WHERE run_id = %s",
                (str(minutes_ago), run_id),
            )


def _set_job_status(job_id, status):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run_job SET status = %s WHERE job_id = %s", (status, job_id))


# ---- run engine ------------------------------------------------------------

def test_stale_running_job_is_reclaimed_and_resumes_from_its_cursor(ready):
    _, recorder = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    job = categories_runs.process_run(run["run_id"], actor="tester")["jobs"][0]
    # Simulate a worker that died after apply-terms: job 'running', run 'running',
    # heartbeat old, cursors say terms are done.
    _rewind_after_terms(run["run_id"], job["job_id"])
    listed = _read(categories_runs.list_runs, "prod")
    assert listed[0]["worker_stale"] is True
    recorder.calls.clear()
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    paths = [p for _, p, _ in recorder.calls]
    assert "/apply-terms" not in paths          # terms_done cursor honoured
    assert paths[0] == "/apply-memberships" and paths[-1] == "/finalize"
    with database.cursor() as cursor:
        cursor.execute("SELECT count(*) AS n FROM catmgr.audit_log WHERE action = 'job_reclaimed'")
        assert cursor.fetchone()["n"] == 1


def test_resume_refuses_a_live_worker_but_recovers_a_dead_one(ready):
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run SET status='running', worker_heartbeat_at = now() WHERE run_id = %s", (run["run_id"],))
            cursor.execute("UPDATE catmgr.run_job SET status='running' WHERE run_id = %s", (run["run_id"],))
    with pytest.raises(DraftConflict):
        _write(categories_runs.resume, run["run_id"])
    _set_heartbeat(run["run_id"], 60)
    recovered = _write(categories_runs.resume, run["run_id"])
    assert recovered["status"] == "queued"
    assert all(j["status"] == "pending" for j in recovered["jobs"])


def test_one_active_run_per_env_holds_for_resume_and_retry(ready):
    first = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    categories_runs.process_run(first["run_id"], actor="tester")
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run SET status='failed' WHERE run_id = %s", (first["run_id"],))
            cursor.execute("UPDATE catmgr.run_job SET status='failed' WHERE run_id = %s", (first["run_id"],))
    second = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    assert second["status"] == "queued"
    with pytest.raises(DraftConflict):
        _write(categories_runs.resume, first["run_id"])
    with pytest.raises(DraftConflict):
        _write(categories_runs.retry_job, first["run_id"], first["jobs"][0]["job_id"])


def test_membership_fence_violation_fails_the_job_with_a_report(ready, monkeypatch):
    class Refusing(BrokerRecorder):
        def __call__(self, env, path, payload):
            if path == "/apply-memberships":
                self.calls.append((env, path, payload))
                return {"ok": False, "applied": 0,
                        "skipped": [{"product_id": 100, "reason": "sku_mismatch"}], "missing_slugs": []}
            return super().__call__(env, path, payload)
    recorder = Refusing().install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    job = failed["jobs"][0]
    assert job["status"] == "failed"
    assert job["result"]["membership_fence"]["skipped"][0]["reason"] == "sku_mismatch"
    assert "/finalize" not in [p for _, p, _ in recorder.calls]


def test_finalize_delete_drift_fails_the_job(ready, monkeypatch):
    class DriftingFinalize(BrokerRecorder):
        def __call__(self, env, path, payload):
            result = super().__call__(env, path, payload)
            if path == "/finalize":
                result["delete_report"] = [{"term_id": 12, "status": "has_products", "count": 3}]
            return result
    DriftingFinalize().install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    assert "has_products" in failed["jobs"][0]["result"]["error"]


def test_retry_after_finalize_does_not_repeat_finalize(ready):
    _, recorder = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    assert job["progress"]["finalize_done"] is True and job["progress"]["snapshot_refreshed"] is True
    assert job["result"]["snapshot"]["version"] >= 2      # snapshot refreshed after apply
    # force a retry of the finished job: finalize must be replayed from the cursor, not re-sent
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run_job SET status='failed' WHERE job_id = %s", (job["job_id"],))
            cursor.execute("UPDATE catmgr.run SET status='failed' WHERE run_id = %s", (run["run_id"],))
    _write(categories_runs.retry_job, run["run_id"], job["job_id"])
    before = len([p for _, p, _ in recorder.calls if p == "/finalize"])
    categories_runs.process_run(run["run_id"], actor="tester")
    after = len([p for _, p, _ in recorder.calls if p == "/finalize"])
    assert after == before


def test_skip_continues_or_finishes_the_run(ready):
    run = _write(categories_runs.create_run, env="prod", blog_ids=None)   # blogs 1 and 7
    first, second = run["jobs"]
    _set_job_status(first["job_id"], "failed")
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run SET status='failed' WHERE run_id = %s", (run["run_id"],))
    after_skip = _write(categories_runs.skip_job, run["run_id"], first["job_id"])
    assert after_skip["status"] == "queued"                  # pending job remains -> continue
    done = categories_runs.process_run(run["run_id"], actor="tester")
    # A skipped blog was NOT migrated: the run says so instead of "completed".
    assert done["status"] == "completed_with_skips"
    assert {j["status"] for j in done["jobs"]} == {"skipped", "done"}


def test_apply_terms_sends_doomed_terms_and_refuses_ok_false(ready, monkeypatch):
    _, recorder = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    categories_runs.process_run(run["run_id"], actor="tester")
    terms_call = next(p for _, path, p in recorder.calls if path == "/apply-terms")
    assert {d["term_id"] for d in terms_call["doomed"]} == {d["term_id"] for d in terms_call["doomed"]}
    assert len(terms_call["doomed"]) == 4               # merge + 3 deletes on blog 1
    assert terms_call["request_id"]

    class Refuse(BrokerRecorder):
        def __call__(self, env, path, payload):
            if path == "/apply-terms":
                return {"ok": False, "code": "arb_catmgr_drift", "drift": [{"term_id": 10}]}
            return super().__call__(env, path, payload)
    Refuse().install(monkeypatch)
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run SET status='completed' WHERE run_id = %s", (run["run_id"],))
    run2 = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    failed = categories_runs.process_run(run2["run_id"], actor="tester")
    assert failed["status"] == "failed" and "refused" in failed["jobs"][0]["result"]["error"]


def test_restore_is_paged_and_resumable(ready, monkeypatch):
    _, recorder = ready
    monkeypatch.setattr(categories_runs, "RESTORE_PAGE_SIZE", 1)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    recorder.calls.clear()
    result = categories_runs.restore_blog(run["run_id"], job["job_id"], actor="tester", background=False)
    assert result["accepted"] is True and result["restore"]["status"] == "done", result
    phases = [p["phase"] for _, path, p in recorder.calls if path == "/restore"]
    # Blog 1's snapshot has 7 membership rows over 6 products; product 100
    # straddles two terms, so a 1-row page must still carry both of its rows.
    assert phases == ["terms"] + ["memberships"] * 6 + ["finalize"]
    pages = [p["snapshot"]["products"] for _, path, p in recorder.calls if path == "/restore" and p["phase"] == "memberships"]
    assert [[(r["product_id"], r["term_id"]) for r in page] for page in pages][0] == [(100, 10), (100, 11)]
    offsets = [p["products_offset"] for _, path, p in recorder.calls if path == "/restore" and p["phase"] == "memberships"]
    assert offsets == [0, 2, 3, 4, 5, 6]
    assert all(p["snapshot"]["blog_path"] == "/" for _, path, p in recorder.calls if path == "/restore")
    detail = _read(categories_runs.get_run, run["run_id"])["jobs"][0]["restore"]
    assert detail["offset"] == 7 and detail["phase"] == "done" and detail["verified"] is True


def test_restore_refuses_while_the_run_is_active(ready):
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run SET status='running', worker_heartbeat_at = now() WHERE run_id = %s", (run["run_id"],))
    with pytest.raises(DraftConflict):
        categories_runs.restore_blog(run["run_id"], job["job_id"], actor="tester", background=False)


def test_drift_audit_refused_during_an_active_run(ready):
    _write(categories_runs.create_run, env="prod", blog_ids=[1])   # queued
    with pytest.raises(DraftConflict):
        categories_runs.drift_audit("prod", actor="tester")


# ---- planner / draft ---------------------------------------------------------

def test_store_extra_reslug_converges_in_place_with_redirect():
    _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"], note="test")
    # blog 7 carries the extra's live term under its original slug
    with database.cursor(write=True, actor="seed") as cursor:
        categories_service.import_blog_snapshot(
            cursor, env="prod", blog_id=7, blog_path="/isa/",
            terms=[{"term_id": 20, "slug": "men-s", "name": "Men's", "parent": 0},
                   {"term_id": 21, "slug": "saws", "name": "Saws", "parent": 0},
                   {"term_id": 22, "slug": "kendall-x", "name": "Kendall X", "parent": 0},
                   {"term_id": 23, "slug": "crew-shop", "name": "Crew Shop", "parent": 0}],
            products=[{"term_id": 20, "product_id": 200, "sku": "PANT-1"},
                      {"term_id": 22, "product_id": 201, "sku": "KEND-1"},
                      {"term_id": 23, "product_id": 202, "sku": "CREW-1"}], actor="seed")
    overrides = _read(categories_draft.list_overrides, 7)
    extra = next(o for o in overrides if o["kind"] == "extra_node")
    plan_before = _read(categories_planner.build_blog_plan, "prod", 7)
    # The extra's own live term converges in place on its blog (here its
    # parent moves under Clothing) but its slug is not touched before the re-slug.
    before = next(u for u in plan_before["terms"]["update"] if u["expected_slug"] == "crew-shop")
    assert not before["changed"].get("slug") and before["set"]["slug"] == "crew-shop"
    _write(categories_draft.set_override, blog_id=7, kind="extra_node",
           override_id=extra["override_id"], name="Crew Store", slug="crew-store",
           parent_node_id=extra["parent_node_id"])
    saved = next(o for o in _read(categories_draft.list_overrides, 7) if o["override_id"] == extra["override_id"])
    assert saved["slug"] == "crew-store" and saved["previous_slug"] == "crew-shop"
    plan = _read(categories_planner.build_blog_plan, "prod", 7)
    update = next(u for u in plan["terms"]["update"] if u["expected_slug"] == "crew-shop")
    assert update["term_id"] == 23 and update["set"]["slug"] == "crew-store" and update["set"]["name"] == "Crew Store"
    assert not any(c["slug"] == "crew-store" for c in plan["terms"]["create"])
    assert not any(d["expected_slug"] == "crew-shop" for d in plan["terms"]["delete"])
    assert plan["stats"]["membership_changes"] == 0        # CREW-1 rides the term
    preview = _read(categories_planner.preview, "prod", [7])
    assert preview["ok"], preview["blockers"]
    # changing back clears previous_slug
    _write(categories_draft.set_override, blog_id=7, kind="extra_node",
           override_id=extra["override_id"], name="Crew Store", slug="crew-shop",
           parent_node_id=extra["parent_node_id"])
    again = next(o for o in _read(categories_draft.list_overrides, 7) if o["override_id"] == extra["override_id"])
    assert again["previous_slug"] is None


def test_slug_uniqueness_spans_nodes_and_store_extras_and_duplicates_are_409():
    nodes = _build_scenario()
    with pytest.raises(DraftConflict):
        _write(categories_draft.create_node, parent_id=None, name="Crew", slug="crew-shop")
    clothing = _read(categories_draft.list_nodes)[0]
    with pytest.raises(DraftConflict):
        _write(categories_draft.update_node, clothing["node_id"], slug="crew-shop")
    with pytest.raises(DraftConflict):
        _write(categories_draft.set_override, blog_id=7, kind="extra_node", name="Dup", slug="crew-shop")
    with pytest.raises(DraftConflict):      # unique index -> conflict, not 500
        _write(categories_draft.set_override, blog_id=7, kind="rename", node_id=clothing["node_id"], name="X")
        _write(categories_draft.set_override, blog_id=7, kind="rename", node_id=clothing["node_id"], name="Y")
    del nodes


def test_collision_with_a_kept_store_custom_slug_blocks_the_preview():
    _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"], note="test")
    footwear = next(n for n in _read(categories_draft.list_nodes) if n["slug"] == "footwear")
    _write(categories_draft.update_node, footwear["node_id"], slug="field-uniform")   # a live store_custom slug on blog 1
    preview = _read(categories_planner.preview, "prod", [1])
    assert not preview["ok"]
    collision = next(b for b in preview["blockers"] if b["kind"] == "slug_collisions")
    assert collision["blogs"][0]["slugs"] == ["field-uniform"]


def test_preview_rejects_unknown_blog_ids_and_lists_zero_category_skus():
    _build_scenario()
    with pytest.raises(DraftError):
        _read(categories_planner.preview, "prod", [1, 999])
    preview = _read(categories_planner.preview, "prod", [1])
    zero = next(b for b in preview["blockers"] if b["kind"] == "zero_category_skus")
    assert {z["sku"] for z in zero["skus"]} == {"RESCUE-2", "ORPHAN-1"}
    assert zero["skus"][0]["where"][0]["blog_id"] == 1


def test_move_node_closes_the_gap_in_the_source_siblings():
    _build_scenario()
    nodes = _read(categories_draft.list_nodes)
    roots = [n for n in nodes if n["parent_id"] is None]
    footwear = next(n for n in roots if n["slug"] == "footwear")
    clothing = next(n for n in roots if n["slug"] == "clothing")
    _write(categories_draft.move_node, footwear["node_id"], parent_id=clothing["node_id"], position=0)
    remaining = sorted((n["sort_order"] for n in _read(categories_draft.list_nodes) if n["parent_id"] is None))
    assert remaining == [10, 20]


# ---- transport ----------------------------------------------------------------

def test_wordpress_error_message_carries_the_drift_report():
    from auth import _wordpress_error_message
    body = b'{"ok": false, "code": "arb_catmgr_drift", "drift": [{"term_id": 10, "expected_slug": "a", "live": "b"}]}'
    text = _wordpress_error_message(body)
    assert "arb_catmgr_drift" in text and "term_id=10" in text and "live=b" in text


def test_export_pages_are_bounded():
    assert categories_service._EXPORT_PAGE_LIMIT <= 5000
    from auth import WORDPRESS_RESPONSE_CAP_BYTES
    assert WORDPRESS_RESPONSE_CAP_BYTES >= 8 * 1024 * 1024


# ---- HTTP gating ------------------------------------------------------------------

def _enable(monkeypatch, apply_users=""):
    from config import get_settings
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    monkeypatch.setenv("CATMGR_PROD_URL", "https://wp.example.test/wp-json/arb/v1/logo-admin/categories")
    monkeypatch.setenv("CATMGR_PROD_USER", "svc")
    monkeypatch.setenv("CATMGR_PROD_APP_PASSWORD", "pw")
    monkeypatch.setenv("CATMGR_APPLY_USERS", apply_users)
    monkeypatch.delenv("CATMGR_VIEW_USERS", raising=False)
    get_settings.cache_clear()


def test_every_apply_tier_route_is_refused_for_non_allowlisted_users(client_as, monkeypatch):
    _enable(monkeypatch, apply_users="someone-else")
    client = client_as()
    posts = [
        ("/api/categories/runs", {"env": "prod"}),
        ("/api/categories/runs/1/start", None), ("/api/categories/runs/1/pause", None),
        ("/api/categories/runs/1/resume", None), ("/api/categories/runs/1/cancel", None),
        ("/api/categories/runs/1/jobs/1/retry", None), ("/api/categories/runs/1/jobs/1/skip", None),
        ("/api/categories/runs/1/jobs/1/restore", None),
        ("/api/categories/freeze", {"env": "prod", "on": True}),
        ("/api/categories/drift-audit", {"env": "prod"}),
    ]
    for path, body in posts:
        response = client.post(path, json=body) if body is not None else client.post(path)
        assert response.status_code == 403, (path, response.status_code, response.text)
    page = client.get("/")
    assert 'data-apply-allowed="0"' in page.text
    from config import get_settings
    get_settings.cache_clear()


def test_uncategorized_ack_routes_and_mapping_envelope(client_as, monkeypatch):
    _enable(monkeypatch)
    _build_scenario()
    client = client_as()
    assert client.put("/api/categories/uncategorized-ack", json={"skus": ["orphan-1"], "note": "n"}).json() == {"count": 1}
    acks = client.get("/api/categories/uncategorized-ack").json()["acks"]
    assert acks[0]["sku"] == "ORPHAN-1"
    assert client.delete("/api/categories/uncategorized-ack/ORPHAN-1").json() == {"ok": True}
    assert client.delete("/api/categories/uncategorized-ack/ORPHAN-1").status_code == 422
    nodes = _read(categories_draft.list_nodes)
    ppe = next(n for n in nodes if n["slug"] == "ppe")
    single = client.put("/api/categories/mapping", json={"rows": [{"old_slug": "kendall-x", "action": "map", "target_node_id": ppe["node_id"]}]}).json()
    assert "mapping" in single and single["results"][0]["ok"] is True
    from config import get_settings
    get_settings.cache_clear()


def test_startup_recovery_reclaims_running_jobs_immediately(ready, monkeypatch):
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run SET status='running', worker_heartbeat_at = now() WHERE run_id = %s", (run["run_id"],))
            cursor.execute("UPDATE catmgr.run_job SET status='running' WHERE run_id = %s", (run["run_id"],))
    started = []
    monkeypatch.setattr(categories_runs, "start_run", lambda run_id, actor: started.append(run_id))
    assert categories_runs.recover_runs() == [run["run_id"]]
    assert started == [run["run_id"]]
    recovered = _read(categories_runs.get_run, run["run_id"])
    assert recovered["status"] == "queued" and recovered["jobs"][0]["status"] == "pending"


def test_mapping_into_a_node_with_a_live_identity_term_defaults_to_merge():
    _build_scenario()
    nodes = _read(categories_draft.list_nodes)
    footwear = next(n for n in nodes if n["slug"] == "footwear")
    # 'footwear' is not live anywhere in the scenario -> first mapping is primary
    first = _write(categories_mapping.set_mapping, old_slug="saws", action="map", target_node_id=footwear["node_id"])
    assert first["is_primary"] is True
    # A node whose own slug IS live (men-s? no: mens is the node slug; make 'ppe' live on blog 7)
    ppe = next(n for n in nodes if n["slug"] == "ppe")
    with database.cursor(write=True, actor="seed") as cursor:
        categories_service.import_blog_snapshot(
            cursor, env="prod", blog_id=9, blog_path="/nelson/",
            terms=[{"term_id": 90, "slug": "ppe", "name": "PPE", "parent": 0},
                   {"term_id": 91, "slug": "safety-gear", "name": "Safety Gear", "parent": 0}],
            products=[{"term_id": 91, "product_id": 900, "sku": "SAFE-1"}], actor="seed")
    merged = _write(categories_mapping.set_mapping, old_slug="safety-gear", action="map", target_node_id=ppe["node_id"])
    assert merged["is_primary"] is False          # the live 'ppe' term survives; safety-gear merges into it
    plan = _read(categories_planner.build_blog_plan, "prod", 9)
    assert [d["expected_slug"] for d in plan["terms"]["delete"] if d["reason"] == "merge"] == ["safety-gear"]
    assert not any(u["expected_slug"] == "safety-gear" for u in plan["terms"]["update"])


def test_reslugging_a_node_carries_its_live_identity_mapping():
    _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"], note="test")
    # 'saws' is live and explicitly mapped as delete in the scenario; use a node
    # whose slug is live only implicitly: seed one.
    with database.cursor(write=True, actor="seed") as cursor:
        categories_service.import_blog_snapshot(
            cursor, env="prod", blog_id=9, blog_path="/nelson/",
            terms=[{"term_id": 90, "slug": "ppe", "name": "PPE", "parent": 0}],
            products=[{"term_id": 90, "product_id": 900, "sku": "SAFE-1"}], actor="seed")
    ppe = next(n for n in _read(categories_draft.list_nodes) if n["slug"] == "ppe")
    _write(categories_draft.update_node, ppe["node_id"], slug="safety-ppe")
    status = _read(categories_mapping.mapping_status, "prod")
    row = next(s for s in status["slugs"] if s["old_slug"] == "ppe")
    assert row["action"] == "map" and row["is_primary"] is True and row["implicit"] is False
    plan = _read(categories_planner.build_blog_plan, "prod", 9)
    update = next(u for u in plan["terms"]["update"] if u["expected_slug"] == "ppe")
    assert update["term_id"] == 90 and update["set"]["slug"] == "safety-ppe"
    assert plan["stats"]["deletes"] == 0
    assert not any(c["slug"] == "safety-ppe" for c in plan["terms"]["create"])


# ---------------------------------------------------------------- durable broker jobs


class AsyncBroker(BrokerRecorder):
    """A broker that detaches every mutating phase as a durable job (202 + key)
    and answers /job polls: running for `polls_before_done` polls, then the
    configured outcome."""

    def __init__(self, polls_before_done=1, outcome="done", status_hiccups=0):
        super().__init__()
        self.jobs = {}
        self.polls_before_done = polls_before_done
        self.outcome = outcome
        self.status_hiccups = status_hiccups

    def __call__(self, env, path, payload):
        if path == "/job":
            key = payload["key"]
            if key not in self.jobs:
                # The engine probes deterministic keys before posting; an
                # unknown key is a 404 like the real broker.
                raise categories_service.BrokerError("No job with that key.", 404)
            self.calls.append((env, path, payload))
            if self.status_hiccups > 0:
                self.status_hiccups -= 1
                raise categories_service.BrokerError("WordPress is currently unreachable", 502)
            job = self.jobs[key]
            job["polls"] += 1
            if job["polls"] < self.polls_before_done:
                return {"ok": True, "job": {"key": key, "status": "running", "heartbeat_age": 2,
                                            "stale": False, "progress": {"pass": "update", "updated": 25}}}
            if self.outcome == "done":
                return {"ok": True, "job": {"key": key, "status": "done"}, "result": job["result"], "error": None}
            if self.outcome == "failed":
                return {"ok": True, "job": {"key": key, "status": "failed"}, "result": None,
                        "error": "Update failed for term 9: nope"}
            if self.outcome == "dead":
                return {"ok": True, "job": {"key": key, "status": "running", "stale": True, "lock_free": True,
                                            "heartbeat_age": 40, "progress": {"pass": "update", "updated": 50}}}
            if self.outcome == "stale":
                return {"ok": True, "job": {"key": key, "status": "running", "stale": True,
                                            "heartbeat_age": 400, "progress": {"pass": "update"}}}
            raise AssertionError(self.outcome)
        result = super().__call__(env, path, payload)
        key = f"{payload['request_id']}:{path.strip('/')}" + (f":{payload['page']}" if "page" in payload else "")
        self.jobs[key] = {"polls": 0, "result": result}
        return {"ok": True, "async": True, "job": {"key": key, "status": "running", "heartbeat_age": 0}}


@pytest.fixture
def fast_polls(monkeypatch):
    monkeypatch.setattr(categories_runs, "JOB_POLL_SECONDS", 0)


def test_detached_broker_jobs_are_polled_to_completion(ready, monkeypatch, fast_polls):
    broker = AsyncBroker(polls_before_done=3).install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    job = done["jobs"][0]
    # The phase bodies the engine records are the polled results, same shape as before.
    assert job["result"]["terms"]["updated"] >= 1
    assert job["result"]["finalize"]["ok"] is True
    polls = [p["key"] for _, path, p in broker.calls if path == "/job"]
    phases = {k.split(":")[1] for k in polls}
    assert phases == {"apply-terms", "apply-memberships", "finalize"}
    assert len(polls) >= 3 * 3
    # Every job key carries the engine's request id; membership pages carry their page.
    terms_key = next(k for k in polls if k.endswith(":apply-terms"))
    assert terms_key.startswith(job["request_id"])
    membership_call = next(p for _, path, p in broker.calls if path == "/apply-memberships")
    assert membership_call["page"] == 0
    # Polling kept the run's heartbeat fresh, so the stale-job reclaim never fires mid-poll.
    detail = _read(categories_runs.get_run, run["run_id"])
    assert detail["worker_stale"] is False


def test_dead_broker_worker_fails_the_job_with_retry_guidance(ready, monkeypatch, fast_polls):
    AsyncBroker(outcome="dead").install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    result = failed["jobs"][0]["result"]
    assert "died mid-phase" in result["error"] and "Retry" in result["error"]
    assert result["wp_side_unknown"] is True
    # The snapshot was taken before the phase, so a retry can still fall back on it.
    assert failed["jobs"][0]["progress"]["snapshot_taken"] is True
    assert "terms_done" not in failed["jobs"][0]["progress"]


def test_stale_broker_job_fails_with_wait_then_retry(ready, monkeypatch, fast_polls):
    AsyncBroker(outcome="stale").install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    result = failed["jobs"][0]["result"]
    assert "no heartbeat for 400s" in result["error"] and "wait a minute, then Retry" in result["error"]
    assert result["wp_side_unknown"] is True


def test_broker_reported_failure_is_definite_not_unknown(ready, monkeypatch, fast_polls):
    AsyncBroker(outcome="failed").install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    result = failed["jobs"][0]["result"]
    assert "Update failed for term 9: nope" in result["error"]
    assert "wp_side_unknown" not in result


def test_status_poll_hiccups_are_tolerated(ready, monkeypatch, fast_polls):
    broker = AsyncBroker(polls_before_done=1, status_hiccups=2).install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]


def test_transport_failures_are_flagged_wp_side_unknown(ready, monkeypatch):
    BrokerRecorder(fail_on={"/apply-terms"}).install(monkeypatch)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    result = failed["jobs"][0]["result"]
    assert result["wp_side_unknown"] is True and "boom" in result["error"]


def test_restore_pages_carry_one_request_id_and_their_page(ready, monkeypatch):
    _, recorder = ready
    monkeypatch.setattr(categories_runs, "RESTORE_PAGE_SIZE", 1)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    recorder.calls.clear()
    categories_runs.restore_blog(run["run_id"], done["jobs"][0]["job_id"], actor="tester", background=False)
    restores = [p for _, path, p in recorder.calls if path == "/restore"]
    ids = {p["request_id"] for p in restores}
    assert len(ids) == 1 and next(iter(ids)).startswith(f"restore-{done['jobs'][0]['job_id']}-")
    assert [p.get("page") for p in restores] == [None, 0, 1, None]   # blog 7: two single-row products


# ---------------------------------------------------------------- planner/broker delete contract


def test_parked_term_keeps_its_original_disposition_and_merge_destination(ready):
    """A term the broker parked on catmgrtmp-<id> reports its original slug
    (parked_from). The Mapping tab groups it under that slug, its
    disposition (here: merge into mens) still applies on a re-plan, every
    product on it carries to the merge destination, and blog 1 redirects the
    ORIGINAL public URL. A slug that merely starts with catmgrtmp- and has no
    lineage is an ordinary unmapped slug."""
    nodes, _ = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=7, blog_path="/isa/",
           terms=[{"term_id": 20, "slug": "men-s", "name": "Men's", "parent": 0},
                  {"term_id": 31, "slug": "catmgrtmp-31", "name": "Women's Bargain", "parent": 0,
                   "parked_from": "women-s-bargain"}],
           products=[{"term_id": 20, "product_id": 200, "sku": "PANT-1"},
                     {"term_id": 31, "product_id": 200, "sku": "PANT-1"},
                     {"term_id": 31, "product_id": 300, "sku": "ONLY-PARKED"}])
    # The mapping is keyed by the ORIGINAL slug, which the parked term still counts as live under.
    _write(categories_mapping.set_mapping, old_slug="women-s-bargain", action="map",
           target_node_id=nodes["mens"]["node_id"], is_primary=False)
    status = _read(categories_mapping.mapping_status, "prod")
    row = next(s for s in status["slugs"] if s["old_slug"] == "women-s-bargain")
    assert row["action"] == "map" and row["parked"] is True
    assert not any(s["old_slug"].startswith("catmgrtmp-") for s in status["slugs"])
    plan = _read(categories_planner.build_blog_plan, "prod", 7)
    delete = next(d for d in plan["terms"]["delete"] if d["term_id"] == 31)
    assert delete["reason"] == "merge" and delete["expected_slug"] == "catmgrtmp-31"
    assert delete["public_slug"] == "women-s-bargain"
    rows = {m["product_id"]: m for m in plan["memberships"]}
    assert rows[200]["final_slugs"] == ["mens"] and rows[200]["expected_term_ids"] == [20, 31]
    assert rows[300]["final_slugs"] == ["mens"]        # carried to the merge destination, not dropped
    assert plan["zero_category"] == []
    # Blog 1: the redirect comes from the original public slug.
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=1, blog_path="/",
           terms=[{"term_id": 10, "slug": "men-s", "name": "Men's", "parent": 0},
                  {"term_id": 41, "slug": "catmgrtmp-41", "name": "Parked", "parent": 0,
                   "parked_from": "women-s-bargain"}],
           products=[{"term_id": 10, "product_id": 100, "sku": "PANT-1"},
                     {"term_id": 41, "product_id": 100, "sku": "PANT-1"}])
    plan1 = _read(categories_planner.build_blog_plan, "prod", 1)
    assert {r["old_path"]: r["new_path"] for r in plan1["redirects"]}["/product-category/women-s-bargain/"] == "/product-category/mens/"
    assert plan1["unspsc"]["merges"] == {"women-s-bargain": "mens", "men-s-bottoms": "mens"} or \
        plan1["unspsc"]["merges"].get("women-s-bargain") == "mens"
    # No lineage = ordinary slug: unmapped until the operator decides.
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 90, "slug": "catmgrtmp-99", "name": "Odd", "parent": 0}], products=[])
    with pytest.raises(DraftError):
        _read(categories_planner.build_blog_plan, "prod", 9)


def test_preview_blocks_a_slug_deleted_and_recreated_in_one_run(ready):
    """Mapping a live slug to delete while a draft node still carries that slug
    would swap the term's identity (delete + create): a blocker, not a plan."""
    _write(categories_draft.create_node, parent_id=None, name="Saws", slug="saws")
    plan = _read(categories_planner.build_blog_plan, "prod", 1)
    assert any(d["expected_slug"] == "saws" for d in plan["terms"]["delete"])
    assert any(c["slug"] == "saws" for c in plan["terms"]["create"])
    preview = _read(categories_planner.preview, "prod", [1])
    blocker = next(b for b in preview["blockers"] if b["kind"] == "recreated_slugs")
    assert blocker["blogs"] == [{"blog_id": 1, "slugs": ["saws"]}]
    assert "term_id" in blocker["message"]
    assert preview["ok"] is False


def test_drift_audit_can_be_scoped_and_is_audited(ready):
    result = categories_runs.drift_audit("prod", actor="tester", blog_ids=[7])
    assert result["scope"] == [7] and result["refreshed_blogs"] == 1
    assert {b["blog_id"] for b in result["pending"]} <= {7}
    with database.cursor() as cursor:
        cursor.execute("SELECT detail FROM catmgr.audit_log WHERE action = 'drift_audit' ORDER BY id DESC LIMIT 1")
        detail = cursor.fetchone()["detail"]
    assert detail["scope"] == [7] and detail["refreshed"] == 1 and "converged" in detail
    with pytest.raises(DraftError):
        categories_runs.drift_audit("prod", actor="tester", blog_ids=[999])
    everything = categories_runs.drift_audit("prod", actor="tester")
    assert everything["scope"] is None and everything["refreshed_blogs"] == 2


def test_deleting_a_node_reports_the_mappings_it_drops(ready):
    """slug_map rows cascade away with their target node; the operator is
    told which live slugs just lost their disposition."""
    nodes, _ = ready
    footwear = nodes["footwear"] if "footwear" in nodes else None
    target = footwear or next(iter(nodes.values()))
    _write(categories_mapping.set_mapping, old_slug="old-boots", action="map",
           target_node_id=target["node_id"], is_primary=False)
    result = _write(categories_draft.delete_node, target["node_id"], cascade=True)
    assert "old-boots" in result["unmapped_slugs"]
    with database.cursor() as cursor:
        cursor.execute("SELECT detail FROM catmgr.audit_log WHERE action = 'node_deleted' ORDER BY id DESC LIMIT 1")
        assert "old-boots" in cursor.fetchone()["detail"]["unmapped_slugs"]
    status = _read(categories_mapping.mapping_status, "prod")
    assert next(s for s in status["slugs"] if s["old_slug"] == "old-boots")["action"] is None


def test_recovery_of_a_job_that_finished_before_its_status_flip(ready, monkeypatch):
    """A worker that died between the post-apply snapshot refresh and the
    status flip leaves a 'running' job whose cursors say everything landed.
    Recovery marks it done without calling WordPress again - and without
    tripping the version fence on the job's own refresh."""
    _, recorder = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    assert job["progress"]["snapshot_refreshed"] is True
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run_job SET status='running', finished_at=NULL WHERE job_id=%s", (job["job_id"],))
            cursor.execute("UPDATE catmgr.run SET status='running', finished_at=NULL WHERE run_id=%s", (run["run_id"],))
    _set_heartbeat(run["run_id"], 30)
    recorder.calls.clear()
    categories_runs.process_run(run["run_id"], actor="tester")   # reclaims the stale job, then resumes it
    detail = _read(categories_runs.get_run, run["run_id"])
    assert detail["jobs"][0]["status"] == "done"
    assert detail["jobs"][0]["result"]["replayed"] is True
    assert recorder.calls == []                      # WordPress was not touched again


def test_in_flight_job_continues_past_a_bumped_snapshot_version(ready, monkeypatch):
    """Once apply-terms has landed, a snapshot re-import must not strand the
    blog behind 're-plan required': the remaining phases run under the
    broker's own fences."""
    _, recorder = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE catmgr.run_job SET status='pending', finished_at=NULL, result=NULL,"
                " progress = '{\"snapshot_taken\": true, \"terms_done\": true}'::jsonb WHERE job_id=%s",
                (job["job_id"],))
            cursor.execute("UPDATE catmgr.run SET status='queued', finished_at=NULL WHERE run_id=%s", (run["run_id"],))
            cursor.execute("UPDATE catmgr.snapshot SET version = version + 1 WHERE env='prod' AND blog_id=1")
    recorder.calls.clear()
    categories_runs.process_run(run["run_id"], actor="tester")
    detail = _read(categories_runs.get_run, run["run_id"])
    assert detail["jobs"][0]["status"] == "done", detail["jobs"][0]["result"]
    paths = [p for _, p, _ in recorder.calls]
    assert "/apply-terms" not in paths and "/finalize" in paths
    fence = detail["jobs"][0]["result"]["resumed_past_version_fence"]
    assert fence["snapshot"] > fence["plan"]
