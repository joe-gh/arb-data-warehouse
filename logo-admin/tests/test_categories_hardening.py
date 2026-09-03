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


def _export_from_snapshot(env, blog_id):
    """A fake WordPress export that returns exactly what the warehouse snapshot
    holds, so the post-apply refresh re-imports the same (mapped) state."""
    with database.cursor() as cursor:
        cursor.execute("SELECT blog_path FROM catmgr.snapshot WHERE env=%s AND blog_id=%s", (env, blog_id))
        row = cursor.fetchone()
        cursor.execute(
            "SELECT term_id, slug, name, parent_term_id AS parent, description, sort_order"
            " FROM catmgr.wp_term WHERE env=%s AND blog_id=%s ORDER BY term_id", (env, blog_id))
        terms = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            "SELECT term_id, product_id, sku FROM catmgr.wp_term_product WHERE env=%s AND blog_id=%s",
            (env, blog_id))
        products = [dict(r) for r in cursor.fetchall()]
    return {"blog_path": row["blog_path"] if row else "/", "terms": terms, "products": products}


@pytest.fixture
def ready(monkeypatch):
    nodes = _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"], note="test")
    recorder = BrokerRecorder()
    monkeypatch.setattr(categories_runs, "broker_call", recorder)
    monkeypatch.setattr(categories_service, "fetch_export", _export_from_snapshot)
    return nodes, recorder


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
    job = run["jobs"][0]
    # Simulate a worker that died after apply-terms: job 'running', run 'running',
    # heartbeat old, cursors say terms are done.
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run_job SET status='running', progress = %s WHERE job_id = %s",
                           ('{"snapshot_taken": true, "terms_done": true}', job["job_id"]))
            cursor.execute("UPDATE catmgr.run SET status='running', worker_heartbeat_at = now() - interval '30 minutes' WHERE run_id = %s",
                           (run["run_id"],))
    listed = _read(categories_runs.list_runs, "prod")
    assert listed[0]["worker_stale"] is True
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed"
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
    recorder = Refusing()
    monkeypatch.setattr(categories_runs, "broker_call", recorder)
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
    monkeypatch.setattr(categories_runs, "broker_call", DriftingFinalize())
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
    assert done["status"] == "completed"
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
    monkeypatch.setattr(categories_runs, "broker_call", Refuse())
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catmgr.run SET status='completed' WHERE run_id = %s", (run["run_id"],))
    run2 = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    failed = categories_runs.process_run(run2["run_id"], actor="tester")
    assert failed["status"] == "failed" and "refused" in failed["jobs"][0]["result"]["error"]


def test_restore_is_paged_and_resumable(ready, monkeypatch):
    _, recorder = ready
    monkeypatch.setattr(categories_runs, "RESTORE_PAGE_SIZE", 1)
    monkeypatch.setattr(
        categories_service, "fetch_export",
        lambda env, blog_id: {"blog_path": "/", "terms": [{"term_id": 1, "slug": "live", "name": "Live", "parent": 0}],
                              "products": [{"term_id": 1, "product_id": 100, "sku": "A"},
                                           {"term_id": 1, "product_id": 101, "sku": "B"},
                                           {"term_id": 1, "product_id": 102, "sku": "C"}]},
    )
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    recorder.calls.clear()
    result = categories_runs.restore_blog(run["run_id"], job["job_id"], actor="tester", background=False)
    assert result["accepted"] is True and result["restore"]["status"] == "done"
    phases = [p["phase"] for _, path, p in recorder.calls if path == "/restore"]
    assert phases == ["terms", "memberships", "memberships", "memberships", "finalize"]
    offsets = [p["products_offset"] for _, path, p in recorder.calls if path == "/restore" and p["phase"] == "memberships"]
    assert offsets == [0, 1, 2]
    assert all(p["snapshot"]["blog_path"] == "/" for _, path, p in recorder.calls if path == "/restore")
    detail = _read(categories_runs.get_run, run["run_id"])["jobs"][0]["restore"]
    assert detail["offset"] == 3 and detail["phase"] == "done"


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
    assert not any(u["expected_slug"] == "crew-shop" for u in plan_before["terms"]["update"])
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
