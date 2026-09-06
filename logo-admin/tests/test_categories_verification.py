"""Category editor verification pass (2026-09-04): one test per finding of the
adversarial review, each against the stateful fake WordPress.

Requires the provisioned test database. Reuses the Phase-4 golden scenario.
"""

import importlib
import os

import psycopg2
import psycopg2.extras
import pytest

import categories_draft
import categories_mapping
import categories_planner
import categories_runs
import categories_service
from categories_draft import DraftConflict, DraftError
from db import database
from tests.conftest import TEST_ADMIN_DSN
from tests.fake_wp import FakeWordPress
from tests.test_categories_planner import _build_scenario, _read, _write
from tests.test_categories_runs import BrokerRecorder


@pytest.fixture(autouse=True)
def _fast_broker_polls(monkeypatch):
    monkeypatch.setattr(categories_runs, "JOB_POLL_SECONDS", 0)


@pytest.fixture
def ready(monkeypatch):
    nodes = _build_scenario()
    _write(categories_planner.set_acks, skus=["RESCUE-2", "ORPHAN-1"], note="test")
    fake = BrokerRecorder().install(monkeypatch)
    return nodes, fake


def _admin():
    return psycopg2.connect(TEST_ADMIN_DSN)


# ---------------------------------------------------------------- 1: live-state fence


def test_apply_refuses_when_wordpress_changed_since_the_snapshot(ready):
    """A product that gained a category after the preview must not lose it to
    a whole-set membership write; an unplanned term must not survive a run
    that reports success. Both stop before the first mutation."""
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    fake.blogs[1]["products"][100]["term_ids"].add(15)          # PANT-1 also in field-uniform now
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    result = failed["jobs"][0]["result"]
    assert "changed since snapshot" in result["error"] and "re-import" in result["error"]
    assert result["live_drift"]["memberships_added"] == 1
    assert [p for _, p, _ in fake.calls] == []                  # nothing was touched
    assert fake.blogs[1]["products"][100]["term_ids"] == {10, 11, 15}


def test_apply_carries_expected_blog_path_and_membership_identity(ready):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    categories_runs.process_run(run["run_id"], actor="tester")
    for _, path, payload in fake.calls:
        assert payload["expected_blog_path"] == "/", path
    rows = next(p for _, path, p in fake.calls if path == "/apply-memberships")["rows"]
    assert all("expected_term_ids" in r and "expected_sku" in r for r in rows)
    assert next(r for r in rows if r["product_id"] == 100)["expected_term_ids"] == [10, 11]


def test_wrong_blog_path_is_refused_by_the_broker(ready):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    fake.blogs[7]["path"] = "/somewhere-else/"
    with _admin() as connection, connection.cursor() as cursor:   # keep the fingerprint fence quiet
        cursor.execute("UPDATE catmgr.run_job SET payload = payload - 'snapshot_fingerprint' WHERE run_id = %s", (run["run_id"],))
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    assert "blog path" in failed["jobs"][0]["result"]["error"]


# ---------------------------------------------------------------- 2: survivor election


def test_second_reslug_updates_the_surviving_term_in_place(ready):
    _, fake = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 90, "slug": "ppe", "name": "PPE", "parent": 0}],
           products=[{"term_id": 90, "product_id": 900, "sku": "SAFE-1"}])
    fake.seed_from_snapshot("prod", 9)
    ppe = next(n for n in _read(categories_draft.list_nodes) if n["slug"] == "ppe")
    _write(categories_draft.update_node, ppe["node_id"], slug="safety-ppe")
    run = _write(categories_runs.create_run, env="prod", blog_ids=[9])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    assert fake.blogs[9]["terms"][90]["slug"] == "safety-ppe"
    # Second re-slug: the explicit primary still names 'ppe' (no longer live);
    # the carried identity must win and term 90 must be UPDATED, not replaced.
    _write(categories_draft.update_node, ppe["node_id"], slug="safety-gear-2")
    plan = _read(categories_planner.build_blog_plan, "prod", 9)
    assert [u["term_id"] for u in plan["terms"]["update"] if u["changed"].get("slug")] == [90]
    assert plan["terms"]["delete"] == [] and not any(c["slug"] == "safety-gear-2" for c in plan["terms"]["create"])
    primary = [m for m in _read(categories_mapping.mapping_status, "prod")["slugs"]
               if m["target_slug"] == "safety-gear-2" and m["is_primary"]]
    assert [m["old_slug"] for m in primary] == ["safety-ppe"]
    run2 = _write(categories_runs.create_run, env="prod", blog_ids=[9])
    done2 = categories_runs.process_run(run2["run_id"], actor="tester")
    assert done2["status"] == "completed", done2["jobs"][0]["result"]
    assert fake.blogs[9]["terms"][90]["slug"] == "safety-gear-2" and 90 in fake.blogs[9]["terms"]


def test_store_without_the_global_primary_keeps_its_own_term(ready):
    """Blog 9 only carries the alternate mapped slug: it survives in place."""
    nodes, _ = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 95, "slug": "pants-alt", "name": "Pants", "parent": 0}],
           products=[{"term_id": 95, "product_id": 950, "sku": "PANT-9"}])
    _write(categories_mapping.set_mapping, old_slug="pants-alt", action="map",
           target_node_id=nodes["mens"]["node_id"], is_primary=False)
    plan = _read(categories_planner.build_blog_plan, "prod", 9)
    update = next(u for u in plan["terms"]["update"] if u["term_id"] == 95)
    assert update["set"]["slug"] == "mens" and plan["terms"]["delete"] == []
    assert not any(c["slug"] == "mens" for c in plan["terms"]["create"])
    assert plan["memberships"] == []          # PANT-9 rides the term


# ---------------------------------------------------------------- 3: store extras


def test_store_extra_changes_apply_in_place_and_lineage_follows_the_live_slug(ready):
    nodes, fake = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=7, blog_path="/isa/",
           terms=[{"term_id": 20, "slug": "men-s", "name": "Men's", "parent": 0},
                  {"term_id": 21, "slug": "saws", "name": "Saws", "parent": 0},
                  {"term_id": 22, "slug": "kendall-x", "name": "Kendall X", "parent": 0},
                  {"term_id": 23, "slug": "crew-shop", "name": "Crew Shop", "parent": 0, "sort_order": 3}],
           products=[{"term_id": 20, "product_id": 200, "sku": "PANT-1"},
                     {"term_id": 22, "product_id": 201, "sku": "KEND-1"},
                     {"term_id": 23, "product_id": 202, "sku": "CREW-1"}])
    fake.seed_from_snapshot("prod", 7)
    extra = next(o for o in _read(categories_draft.list_overrides, 7) if o["kind"] == "extra_node")
    # Only the name, parent and sort change: the live term must be updated.
    _write(categories_draft.set_override, blog_id=7, kind="extra_node", override_id=extra["override_id"],
           name="Crew Store", slug="crew-shop", parent_node_id=nodes["footwear"]["node_id"], sort_order=7)
    plan = _read(categories_planner.build_blog_plan, "prod", 7)
    update = next(u for u in plan["terms"]["update"] if u["term_id"] == 23)
    assert update["changed"] == {"name": True, "parent": True, "sort_order": True}
    assert update["set"] == {"slug": "crew-shop", "name": "Crew Store", "parent_slug": "footwear",
                             "description": "", "sort_order": 7}
    run = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    assert categories_runs.process_run(run["run_id"], actor="tester")["status"] == "completed"
    assert fake.blogs[7]["terms"][23]["name"] == "Crew Store"
    assert fake.blogs[7]["terms"][23]["parent"] == next(t for t, v in fake.blogs[7]["terms"].items() if v["slug"] == "footwear")
    # Converged: the drift audit re-plans it to nothing.
    assert categories_runs.drift_audit("prod", actor="tester", blog_ids=[7])["converged"] is True
    # Re-slug, apply, re-slug again: lineage always points at what is live.
    _write(categories_draft.set_override, blog_id=7, kind="extra_node", override_id=extra["override_id"],
           name="Crew Store", slug="crew-store", parent_node_id=nodes["footwear"]["node_id"], sort_order=7)
    saved = next(o for o in _read(categories_draft.list_overrides, 7) if o["override_id"] == extra["override_id"])
    assert saved["previous_slug"] == "crew-shop"
    run2 = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    assert categories_runs.process_run(run2["run_id"], actor="tester")["status"] == "completed"
    assert fake.blogs[7]["terms"][23]["slug"] == "crew-store"
    _write(categories_draft.set_override, blog_id=7, kind="extra_node", override_id=extra["override_id"],
           name="Crew Store", slug="crew-market", parent_node_id=nodes["footwear"]["node_id"], sort_order=7)
    saved = next(o for o in _read(categories_draft.list_overrides, 7) if o["override_id"] == extra["override_id"])
    assert saved["previous_slug"] == "crew-store"          # not the original crew-shop
    plan3 = _read(categories_planner.build_blog_plan, "prod", 7)
    assert next(u for u in plan3["terms"]["update"] if u["term_id"] == 23)["set"]["slug"] == "crew-market"
    assert plan3["terms"]["delete"] == [] and not any(c["slug"] == "crew-market" for c in plan3["terms"]["create"])


def test_same_extra_slug_may_exist_on_two_stores_but_never_as_a_global_node(ready):
    nodes, _ = ready
    seven = _write(categories_draft.set_override, blog_id=7, kind="extra_node", name="Field Uniform", slug="field-kit",
                   parent_node_id=nodes["clothing"]["node_id"])
    nine = _write(categories_draft.set_override, blog_id=9, kind="extra_node", name="Field Uniform", slug="field-kit",
                  parent_node_id=nodes["clothing"]["node_id"])
    assert seven["override_id"] != nine["override_id"]
    with pytest.raises(DraftConflict):
        _write(categories_draft.set_override, blog_id=9, kind="extra_node", name="Dup", slug="field-kit")
    with pytest.raises(DraftConflict):
        _write(categories_draft.create_node, parent_id=None, name="Field Kit", slug="field-kit")
    with pytest.raises(DraftConflict):
        _write(categories_draft.update_node, nodes["footwear"]["node_id"], slug="field-kit")


# ---------------------------------------------------------------- 5/6/7: restore


def test_restore_fails_when_the_broker_restores_fewer_terms_than_the_snapshot(ready, monkeypatch):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")

    class Lossy(FakeWordPress):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner
            self.calls = inner.calls

        def __call__(self, env, path, payload):
            result = self.inner(env, path, payload)
            if path == "/restore" and payload.get("phase") == "terms":
                result = {**result, "terms": result["terms"] - 1}
            return result

    monkeypatch.setattr(categories_runs, "broker_call", Lossy(fake))
    result = categories_runs.restore_blog(run["run_id"], done["jobs"][0]["job_id"], actor="tester", background=False)
    assert result["restore"]["status"] == "failed"
    assert "put back 5 of 6 categories" in result["restore"]["error"]
    phases = [p["phase"] for _, path, p in fake.calls if path == "/restore"]
    assert phases == ["terms"]          # stopped before memberships/finalize


def test_restore_is_verified_against_the_snapshot(ready, monkeypatch):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")

    class Liar(FakeWordPress):
        """Reports success without touching anything."""
        def __init__(self, inner):
            super().__init__()
            self.inner = inner
            self.calls = inner.calls

        def __call__(self, env, path, payload):
            if path == "/restore":
                self.calls.append((env, path, payload))
                terms = payload["snapshot"].get("terms") or []
                products = payload["snapshot"].get("products") or []
                return {"ok": True, "terms": len(terms), "products_restored": len({p["product_id"] for p in products}),
                        "terms_removed": 0, "failures": []}
            return self.inner(env, path, payload)

    monkeypatch.setattr(categories_runs, "broker_call", Liar(fake))
    result = categories_runs.restore_blog(run["run_id"], done["jobs"][0]["job_id"], actor="tester", background=False)
    assert result["restore"]["status"] == "failed"
    assert "did not converge" in result["restore"]["error"]
    assert result["restore"]["verification"]


def test_restore_puts_the_unspsc_mapping_back(ready):
    _, fake = ready
    fake.site_options["unspsc_category_mapping"] = [
        {"category_slug": "men-s", "unspsc_code": "53100000"},
        {"category_slug": "men-s-bottoms", "unspsc_code": "53101500"},
        {"category_slug": "saws", "unspsc_code": "27110000"},
    ]
    original = [dict(e) for e in fake.site_options["unspsc_category_mapping"]]
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    slugs = [e["category_slug"] for e in fake.site_options["unspsc_category_mapping"]]
    assert slugs == ["mens", "saws"]            # re-keyed, merge source dropped (survivor has its own)
    terms_call = next(p for _, path, p in fake.calls if path == "/apply-terms")
    assert terms_call["unspsc_renames"] == {"men-s": "mens"}
    assert terms_call["unspsc_merges"] == {"men-s-bottoms": "mens"}
    categories_runs.restore_blog(run["run_id"], done["jobs"][0]["job_id"], actor="tester", background=False)
    assert fake.site_options["unspsc_category_mapping"] == original
    terms_phase = next(p for _, path, p in fake.calls if path == "/restore" and p["phase"] == "terms")
    assert terms_phase["snapshot"]["site_options"]["unspsc_category_mapping"] == original


def test_restore_is_exclusive_per_environment_and_orphans_are_marked_on_restart(ready):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    # A NEWER active run for the environment refuses the restore of an old one.
    newer = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    with pytest.raises(DraftConflict):
        categories_runs.restore_blog(run["run_id"], job["job_id"], actor="tester", background=False)
    _write(categories_runs.cancel, newer["run_id"])
    # Another restore still running (fresh heartbeat) in the env refuses too,
    # and blocks new runs; a stale one does neither.
    with _admin() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE catmgr.run_job SET progress = progress || %s WHERE job_id = %s",
                       ('{"restore": {"status": "running", "phase": "memberships", "heartbeat_at": "2099-01-01T00:00:00+00:00"}}',
                        done["jobs"][0]["job_id"]))
    with pytest.raises(DraftConflict):
        _write(categories_runs.create_run, env="prod", blog_ids=[7])
    # Startup recovery marks the (dead-process) restore failed with a reason.
    categories_runs.recover_runs()
    state = _read(categories_runs.get_run, run["run_id"])["jobs"][0]["restore"]
    assert state["status"] == "failed" and state["orphaned"] is True
    assert "restarted" in state["error"]
    # ... after which a new restore is accepted and converges.
    result = categories_runs.restore_blog(run["run_id"], job["job_id"], actor="tester", background=False)
    assert result["restore"]["status"] == "done" and result["restore"]["verified"] is True


# ---------------------------------------------------------------- 8: derived data


def test_reslugged_terms_queue_their_products_for_search(ready):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    categories_runs.process_run(run["run_id"], actor="tester")
    # men-s (term 20, product 200) was re-slugged with no membership change:
    # its product still lands in the ES queue via apply-terms.
    queued = {pid for blog_id, pids in fake.es_queue for pid in pids if blog_id == 7}
    assert 200 in queued
    terms_result = next(j for j in _read(categories_runs.get_run, run["run_id"])["jobs"])["result"]["terms"]
    assert terms_result["es_queued"] >= 1


# ---------------------------------------------------------------- 9: redirects


def test_missing_redirection_fails_the_blog_1_job(ready):
    _, fake = ready
    fake.redirection_available = False
    with pytest.raises(DraftConflict) as excinfo:          # readiness catches it first
        _write(categories_runs.create_run, env="prod", blog_ids=[1])
    assert "Redirection" in str(excinfo.value)
    fake.redirection_available = True
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    fake.redirection_available = False                     # goes away mid-run
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    result = failed["jobs"][0]["result"]
    assert "redirect" in result["error"] and result["finalize_attempt"]["redirects"]
    with database.cursor() as cursor:
        cursor.execute("SELECT status, detail FROM catmgr.redirect WHERE run_id = %s", (run["run_id"],))
        rows = cursor.fetchall()
    assert {r["status"] for r in rows} == {"failed"} and all("redirection_unavailable" in r["detail"] for r in rows)


def test_existing_wrong_redirect_is_corrected_and_exclusions_get_redirects(ready):
    nodes, fake = ready
    fake.redirects["/product-category/saws/"] = {"new_path": "/sale/", "code": 301, "enabled": True}
    _write(categories_draft.set_override, blog_id=1, kind="exclude", node_id=nodes["footwear"]["node_id"])
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=1, blog_path="/",
           terms=[{"term_id": 10, "slug": "men-s", "name": "Men's", "parent": 0},
                  {"term_id": 12, "slug": "saws", "name": "Saws", "parent": 0},
                  {"term_id": 16, "slug": "footwear", "name": "Footwear", "parent": 0}],
           products=[{"term_id": 10, "product_id": 100, "sku": "PANT-1"}])
    fake.seed_from_snapshot("prod", 1)
    plan = _read(categories_planner.build_blog_plan, "prod", 1)
    redirects = {r["old_path"]: r["new_path"] for r in plan["redirects"]}
    assert redirects["/product-category/footwear/"] == "/store/"      # excluded on blog 1 -> redirect
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    assert fake.redirects["/product-category/saws/"]["new_path"] == "/store/"
    created = done["jobs"][0]["result"]["finalize"]["redirects_created"]
    assert next(r for r in created if r["old_path"] == "/product-category/saws/")["updated"] is True


# ---------------------------------------------------------------- 10/11: finalize retry, keys, tokens


def test_refused_finalize_runs_again_on_retry(ready, monkeypatch):
    _, fake = ready

    class Sticky(FakeWordPress):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner
            self.calls = inner.calls
            self.refuse_once = True

        def __call__(self, env, path, payload):
            result = self.inner(env, path, payload)
            if path == "/finalize" and self.refuse_once:
                self.refuse_once = False
                refused = {**result, "ok": False,
                           "delete_report": [{"term_id": 12, "status": "has_products", "count": 1}]}
                # The broker files a body that reports ok=false as a FAILED job
                # row; the durable row has to say what the engine was told.
                return self.inner.record_job(path, payload, refused)
            return result

    sticky = Sticky(fake)
    monkeypatch.setattr(categories_runs, "broker_call", sticky)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    job = failed["jobs"][0]
    assert failed["status"] == "failed" and "has_products" in job["result"]["error"]
    assert "finalize_done" not in job["progress"]
    finalize_calls = len([p for _, p, _ in fake.calls if p == "/finalize"])
    _write(categories_runs.retry_job, run["run_id"], job["job_id"])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    assert len([p for _, p, _ in fake.calls if p == "/finalize"]) == finalize_calls + 1
    keys = [p["request_id"] for _, path, p in fake.calls if path == "/finalize"]
    assert keys == [f"j{job['job_id']}a1", f"j{job['job_id']}a2"]


def test_recovery_adopts_a_still_running_wordpress_job_instead_of_reposting(ready, monkeypatch):
    """After a restart the reclaimed worker must find the WordPress row of the
    attempt it continues (same key) and poll it, never POST a second copy."""
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    job_id = run["jobs"][0]["job_id"]
    key = f"j{job_id}a1:apply-terms"
    state = {"polls": 0, "posts": 0}

    class Adopting(FakeWordPress):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner
            self.calls = inner.calls

        def __call__(self, env, path, payload):
            if path == "/job" and payload["key"] == key:
                if state["posts"] == 0:      # nothing reached WordPress yet
                    raise categories_service.BrokerError("No job with that key.", 404)
                state["polls"] += 1
                if state["polls"] < 3:
                    return {"ok": True, "job": {"key": key, "status": "running", "heartbeat_age": 1, "stale": False}}
                # the WordPress worker finished the phase meanwhile: apply it for real
                terms_payload = next(p for _, pth, p in self.inner.calls if pth == "/apply-terms")
                result = self.inner._apply_terms(1, self.inner.blogs[1], terms_payload)
                return {"ok": True, "job": {"key": key, "status": "done"}, "result": result, "error": None}
            if path == "/apply-terms":
                state["posts"] += 1
                self.inner.calls.append((env, path, payload))          # record the request WP holds
                raise categories_service.BrokerError("upstream timed out", 504)
            return self.inner(env, path, payload)

    monkeypatch.setattr(categories_runs, "broker_call", Adopting(fake))
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    assert state["posts"] == 1 and state["polls"] >= 3


def test_a_superseded_worker_cannot_overwrite_newer_state(ready):
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    claimed = categories_runs._claim_next_job(run["run_id"])
    assert claimed["request_id"] == f"j{claimed['job_id']}a1" and claimed["worker_token"]
    with _admin() as connection, connection.cursor() as cursor:   # a newer worker reclaimed the job
        cursor.execute("UPDATE catmgr.run_job SET worker_token = 'newer' WHERE job_id = %s", (claimed["job_id"],))
    with pytest.raises(categories_runs.WorkerSuperseded):
        categories_runs._save_progress(claimed["job_id"], {"terms_done": True}, claimed["worker_token"])
    with pytest.raises(categories_runs.WorkerSuperseded):
        categories_runs._finish_job(claimed["job_id"], run["run_id"], "done", {}, claimed["worker_token"])
    detail = _read(categories_runs.get_run, run["run_id"])["jobs"][0]
    assert detail["progress"] == {} and detail["status"] == "running"


# ---------------------------------------------------------------- 12: readiness


def test_readiness_gates_run_creation_with_exact_failures(ready):
    _, fake = ready
    fake.status_overrides = {"broker_version": 1, "durable_jobs": False}
    with pytest.raises(DraftConflict) as excinfo:
        _write(categories_runs.create_run, env="prod", blog_ids=[7])
    message = str(excinfo.value)
    assert "version 1" in message and "in the background" in message
    fake.status_overrides = {"freeze": False}
    with pytest.raises(DraftConflict) as excinfo:
        _write(categories_runs.create_run, env="prod", blog_ids=[7])
    assert "not locked" in str(excinfo.value)
    ready_state = categories_runs.readiness("prod", [7])
    assert ready_state["ok"] is False and len(ready_state["failures"]) == 1
    fake.status_overrides = {}
    assert categories_runs.readiness("prod", [1])["ok"] is True


# ---------------------------------------------------------------- 13/14: product universe + identity


def test_uncategorized_products_can_be_rescued_by_rules_and_assignments(ready):
    nodes, fake = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 90, "slug": "ppe", "name": "PPE", "parent": 0}],
           products=[{"term_id": 90, "product_id": 900, "sku": "SAFE-1"}],
           uncategorized=[{"product_id": 901, "sku": "LOST-1"}, {"product_id": 902, "sku": ""}])
    with _admin() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO woo.store_product_state (fdm4_store, catalog_id, sku, kind, style_code, name, is_active, payload, content_hash)"
            " VALUES ('S_1', 'CAT', 'LOST-1', 'parent', 'LOST-1', 'Lost Helmet', true, '{}'::jsonb, '')")
    rule = _read(categories_mapping.evaluate_rule, "prod", {"field": "name", "op": "prefix", "value": "Lost"})
    assert rule["skus"] == ["LOST-1"]           # the uncategorized product is in the universe
    _write(categories_mapping.set_assignments, node_id=nodes["ppe"]["node_id"], skus=["LOST-1"], mode="add")
    plan = _read(categories_planner.build_blog_plan, "prod", 9)
    row = next(m for m in plan["memberships"] if m["product_id"] == 901)
    assert row == {"product_id": 901, "expected_sku": "LOST-1", "expected_term_ids": [], "final_slugs": ["ppe"]}
    assert plan["stats"]["uncategorized_total"] == 2 and plan["zero_category"] == []
    fake.seed_from_snapshot("prod", 9)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[9])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    assert fake.blogs[9]["products"][901]["term_ids"] == {90}


def test_blank_sku_products_block_and_fence_by_product_id(ready):
    nodes, fake = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 90, "slug": "ppe", "name": "PPE", "parent": 0},
                  {"term_id": 91, "slug": "doomed", "name": "Doomed", "parent": 0}],
           products=[{"term_id": 91, "product_id": 910, "sku": ""}])
    _write(categories_mapping.set_mapping, old_slug="doomed", action="delete")
    preview = _read(categories_planner.preview, "prod", [9])
    blocker = next(b for b in preview["blockers"] if b["kind"] == "zero_category_skus")
    assert blocker["skus"][0]["key"] == "PID:9:910" and blocker["skus"][0]["sku"] == ""
    _write(categories_planner.set_acks, skus=["PID:9:910"], note="no sku")
    assert _read(categories_planner.preview, "prod", [9])["ok"] is True
    plan = _read(categories_planner.build_blog_plan, "prod", 9)
    assert next(m for m in plan["memberships"] if m["product_id"] == 910)["expected_sku"] == ""
    fake.seed_from_snapshot("prod", 9)
    fake.blogs[9]["products"][910]["sku"] = "NOW-HAS-SKU"     # identity changed after the snapshot
    run = _write(categories_runs.create_run, env="prod", blog_ids=[9])
    with _admin() as connection, connection.cursor() as cursor:   # only the row fence should trip
        cursor.execute("UPDATE catmgr.run_job SET payload = payload - 'snapshot_fingerprint' WHERE run_id = %s", (run["run_id"],))
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    assert failed["jobs"][0]["result"]["membership_fence"]["skipped"][0]["reason"] == "sku_mismatch"


# ---------------------------------------------------------------- 15: unplanned terms


def test_an_unplanned_term_under_a_planned_create_slug_refuses_the_apply(ready):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    with _admin() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE catmgr.run_job SET payload = payload - 'snapshot_fingerprint' WHERE run_id = %s", (run["run_id"],))
    fake.blogs[7]["terms"][777] = {"slug": "clothing", "name": "Someone's Clothing", "parent": 0, "description": "",
                                   "sort_order": 0, "thumbnail_id": 0, "name_locked": False, "parked_from": "",
                                   "created_by_run": None}
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed" and "refused" in failed["jobs"][0]["result"]["error"]
    assert fake.blogs[7]["terms"][777]["name"] == "Someone's Clothing"     # untouched


# ---------------------------------------------------------------- 16: export consistency


def test_export_is_repulled_when_pages_disagree_and_rejects_short_deliveries(monkeypatch):
    pages = {"calls": 0}

    def broker(env, path, method="GET", payload=None):
        pages["calls"] += 1
        rows = [{"term_id": 1, "product_id": p, "sku": f"P{p}"} for p in range(1, 7)]
        if payload.get("after_term_id") is None and payload.get("after_product_id") is None:
            return {"terms": [{"term_id": 1, "slug": "a", "name": "A"}], "products": rows[:3],
                    "products_total": 6, "next_after": {"term_id": 1, "product_id": 3}, "uncategorized": []}
        total = 5 if pages["calls"] <= 2 else 6       # first attempt: count moved underneath
        return {"products": rows[3:], "products_total": total, "next_after": None}

    monkeypatch.setattr(categories_service, "_broker", broker)
    monkeypatch.setattr(categories_service, "_EXPORT_PAGE_LIMIT", 3)
    export = categories_service.fetch_export("prod", 1)
    # 2 (inconsistent) + 2, then one re-read of page 1: this broker stamps no
    # export_generation, so a multi-page walk is checked by re-reading page 1.
    assert len(export["products"]) == 6 and pages["calls"] == 5

    def short(env, path, method="GET", payload=None):
        return {"terms": [], "products": [{"term_id": 1, "product_id": 1, "sku": "A"}],
                "products_total": 3, "next_after": None, "uncategorized": []}

    monkeypatch.setattr(categories_service, "_broker", short)
    with pytest.raises(categories_service.BrokerError) as excinfo:
        categories_service.fetch_export("prod", 1)
    assert "kept changing" in str(excinfo.value)


# ---------------------------------------------------------------- 17: suggestions


def test_ambiguous_name_matches_are_never_auto_mapped():
    _build_scenario()
    tops = _write(categories_draft.create_node, parent_id=None, name="Tops")
    _write(categories_draft.create_node, parent_id=tops["node_id"], name="T-Shirts", slug="tops-tshirts")
    _write(categories_draft.create_node, parent_id=None, name="T-Shirts", slug="womens-tshirts")
    _write(categories_draft.create_node, parent_id=None, name="Hats", slug="hats-2")
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 90, "slug": "tees", "name": "T-Shirts", "parent": 0},
                  {"term_id": 91, "slug": "caps", "name": "Hats", "parent": 0}], products=[])
    outcome = _read(categories_mapping.auto_suggest, "prod")
    assert [s["old_slug"] for s in outcome["suggestions"]] == ["caps"]
    assert outcome["ambiguous"][0]["old_slug"] == "tees"
    assert sorted(c["path"] for c in outcome["ambiguous"][0]["candidates"]) == ["T-Shirts", "Tops > T-Shirts"]


# ---------------------------------------------------------------- 18: state machine


def test_start_is_an_atomic_transition_and_imports_are_refused_for_locked_blogs(ready, client_as, monkeypatch):
    from config import get_settings
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    started = _write(categories_runs.start, run["run_id"])
    assert started["status"] == "queued"
    with _admin() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE catmgr.run SET status = 'failed' WHERE run_id = %s", (run["run_id"],))
    with pytest.raises(DraftConflict):
        _write(categories_runs.start, run["run_id"])
    # A failed run hands out no work even with pending jobs.
    assert categories_runs._claim_next_job(run["run_id"]) is None
    with _admin() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE catmgr.run SET status = 'queued' WHERE run_id = %s", (run["run_id"],))
    with database.cursor() as cursor:
        assert categories_runs.blogs_locked_by_runs(cursor, "prod") == {1: run["run_id"]}
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    monkeypatch.setenv("CATMGR_PROD_URL", "https://wp.example.test/wp-json/arb/v1/logo-admin/categories")
    monkeypatch.setenv("CATMGR_PROD_USER", "svc")
    monkeypatch.setenv("CATMGR_PROD_APP_PASSWORD", "pw")
    get_settings.cache_clear()
    response = client_as().post("/api/categories/snapshots/import", json={"env": "prod", "blog_ids": [1, 7]})
    results = {r["blog_id"]: r for r in response.json()["results"]}
    assert results[1]["ok"] is False and "active run" in results[1]["error"]
    assert results[7]["ok"] is True
    get_settings.cache_clear()


# ---------------------------------------------------------------- 19: outcomes


def test_unverifiable_apply_is_reported_not_hidden(ready, monkeypatch):
    _, fake = ready
    real_export = fake.export
    seen = {"exports": 0}

    def flaky_export(env, blog_id):
        seen["exports"] += 1
        if seen["exports"] == 2:        # the post-apply verification export
            raise categories_service.BrokerError("WordPress went away", 502)
        return real_export(env, blog_id)

    monkeypatch.setattr(categories_service, "fetch_export", flaky_export)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed_unverified"
    job = done["jobs"][0]
    assert job["status"] == "done" and job["result"]["verified"] is False
    assert "went away" in job["result"]["verification_error"]


def test_post_apply_verification_catches_a_blog_that_did_not_converge(ready, monkeypatch):
    _, fake = ready

    class Forgetful(FakeWordPress):
        """Says it applied the terms but leaves the tree alone."""
        def __init__(self, inner):
            super().__init__()
            self.inner = inner
            self.calls = inner.calls

        def __call__(self, env, path, payload):
            if path == "/apply-terms":
                self.calls.append((env, path, payload))
                return {"ok": True, "updated": len(payload["updates"]), "created": len(payload["creates"])}
            if path == "/apply-memberships":
                self.calls.append((env, path, payload))
                return {"ok": True, "applied": len(payload["rows"]), "skipped": [], "missing_slugs": []}
            if path == "/finalize":
                self.calls.append((env, path, payload))
                return {"ok": True, "deleted": 0, "delete_report": [], "redirects_created": [], "redirects_failed": []}
            return self.inner(env, path, payload)

    monkeypatch.setattr(categories_runs, "broker_call", Forgetful(fake))
    run = _write(categories_runs.create_run, env="prod", blog_ids=[7])
    failed = categories_runs.process_run(run["run_id"], actor="tester")
    assert failed["status"] == "failed"
    result = failed["jobs"][0]["result"]
    assert "did not converge" in result["error"]
    kinds = {p["kind"] for p in result["verification"]}
    assert {"update", "create", "delete"} <= kinds


# ---------------------------------------------------------------- 20: MCP identity


def test_mcp_identity_is_the_invoking_operator(monkeypatch):
    import mcp_server
    monkeypatch.setenv("ARB_MCP_OPERATOR", "Joseph")
    assert mcp_server._operator_login() == "joseph"
    monkeypatch.setenv("ARB_MCP_OPERATOR", "bad user!")
    monkeypatch.delenv("ARB_MCP_ACTOR", raising=False)
    assert mcp_server._operator_login() == "CLI connection"
    monkeypatch.delenv("ARB_MCP_OPERATOR", raising=False)
    assert mcp_server._operator_login() == "CLI connection"
    assert not categories_runs.apply_allowed("CLI connection")
    wrapper = open(os.path.join(os.path.dirname(mcp_server.__file__), "mcp-run.sh")).read()
    assert "ARB_MCP_OPERATOR" in wrapper and "SUDO_USER" in wrapper


# ---------------------------------------------------------------- 22: schema mirror


def test_blank_install_mirror_supports_every_category_query():
    """Provision a database from sql/logo_schema.sql alone (no migrations) and
    run the category queries that read the columns migrations added."""
    import re
    from pathlib import Path
    from urllib.parse import urlsplit

    mirror = (Path(__file__).resolve().parents[2] / "sql" / "logo_schema.sql").read_text()
    name = "catmgr_mirror_check"
    admin = psycopg2.connect(TEST_ADMIN_DSN)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS {name}")
        cursor.execute(f"CREATE DATABASE {name}")
    admin.close()
    dsn = TEST_ADMIN_DSN
    if dsn.startswith("postgres"):
        parts = urlsplit(dsn)
        dsn = dsn.replace(parts.path, f"/{name}", 1)
    else:
        dsn = re.sub(r"dbname=\S+", f"dbname={name}", dsn)
    connection = psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(mirror)
            cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='catmgr' AND table_name='node_store_override'")
            columns = {r["column_name"] for r in cursor.fetchall()}
            assert "previous_slug" in columns
            categories_service.import_blog_snapshot(
                cursor, env="prod", blog_id=1, blog_path="/",
                terms=[{"term_id": 1, "slug": "crew-shop", "name": "Crew", "parent": 0}],
                products=[{"term_id": 1, "product_id": 5, "sku": "A"}],
                uncategorized=[{"product_id": 6, "sku": "B"}], actor="mirror")
            node = categories_draft.create_node(cursor, parent_id=None, name="Clothing", actor="mirror")
            categories_draft.set_override(cursor, blog_id=1, kind="extra_node", name="Crew Shop", slug="crew-shop",
                                          parent_node_id=node["node_id"], actor="mirror")
            categories_draft.set_override(cursor, blog_id=1, kind="extra_node",
                                          override_id=categories_draft.list_overrides(cursor, 1)[0]["override_id"],
                                          name="Crew Store", slug="crew-store", parent_node_id=node["node_id"], actor="mirror")
            categories_planner.load_dispositions(cursor)
            categories_mapping.mapping_status(cursor, "prod")
            categories_mapping.auto_suggest(cursor, "prod")
            plan = categories_planner.build_blog_plan(cursor, "prod", 1)
            assert plan["terms"]["update"][0]["set"]["slug"] == "crew-store"
            cursor.execute("INSERT INTO catmgr.run (env, target_blogs, status) VALUES ('prod', '{1}', 'completed_with_skips') RETURNING run_id")
            cursor.execute("SELECT worker_token FROM catmgr.run_job LIMIT 0")
        connection.rollback()
    finally:
        connection.close()
        admin = psycopg2.connect(TEST_ADMIN_DSN)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {name}")
        admin.close()


# ---------------------------------------------------------------- 23: restore completeness (2026-09-05)


def test_restore_puts_a_rescued_product_back_to_no_category(ready):
    """A product that had NO category before the run and was assigned one by
    it must come back out: restore sends it as an explicit empty set."""
    nodes, fake = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 90, "slug": "ppe", "name": "PPE", "parent": 0}],
           products=[{"term_id": 90, "product_id": 900, "sku": "SAFE-1"}],
           uncategorized=[{"product_id": 901, "sku": "LOST-1"}])
    _write(categories_mapping.set_assignments, node_id=nodes["ppe"]["node_id"],
           skus=["LOST-1"], mode="add")
    fake.seed_from_snapshot("prod", 9)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[9])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    assert fake.blogs[9]["products"][901]["term_ids"] == {90}

    result = categories_runs.restore_blog(run["run_id"], done["jobs"][0]["job_id"],
                                          actor="tester", background=False)
    assert result["restore"]["status"] == "done", result["restore"]
    assert result["restore"]["verified"] is True
    assert fake.blogs[9]["products"][901]["term_ids"] == set()
    assert 901 in {row["product_id"] for row in fake.export("prod", 9)["uncategorized"]}
    pages = [p for _, path, p in fake.calls
             if path == "/restore" and p.get("phase") == "memberships"]
    carried = [p for p in pages if p["snapshot"].get("uncategorized")]
    assert [row["product_id"] for row in carried[0]["snapshot"]["uncategorized"]] == [901]
    assert carried[0]["snapshot"]["products"] == []


def test_restore_is_refused_against_a_site_that_cannot_empty_a_product(ready):
    nodes, fake = ready
    _write(categories_service.import_blog_snapshot, env="prod", blog_id=9, blog_path="/nelson/",
           terms=[{"term_id": 90, "slug": "ppe", "name": "PPE", "parent": 0}],
           products=[{"term_id": 90, "product_id": 900, "sku": "SAFE-1"}],
           uncategorized=[{"product_id": 901, "sku": "LOST-1"}])
    _write(categories_mapping.set_assignments, node_id=nodes["ppe"]["node_id"],
           skus=["LOST-1"], mode="add")
    fake.seed_from_snapshot("prod", 9)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[9])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job = done["jobs"][0]
    fake.calls.clear()
    fake.status_overrides = {"broker_version": 2}
    result = categories_runs.restore_blog(run["run_id"], job["job_id"],
                                          actor="tester", background=False)
    assert result["restore"]["status"] == "failed"
    assert "out of date" in result["restore"]["error"]
    assert [p for _, path, p in fake.calls if path == "/restore"] == []   # nothing was touched


def test_a_missing_term_in_a_restore_page_leaves_the_product_alone(ready):
    """The broker used to clear a product whose snapshot term did not come
    back, stripping the categories the restore HAD put back."""
    _, fake = ready
    before = set(fake.blogs[1]["products"][100]["term_ids"])
    assert len(before) > 1
    outcome = fake("prod", "/restore", {
        "blog_id": 1, "run_id": 0, "request_id": "probe", "phase": "memberships",
        "expected_blog_path": "/",
        "snapshot": {"terms": [{"term_id": 999, "slug": "vanished", "name": "Vanished"}],
                     "products": [{"product_id": 100, "term_id": 999}],
                     "blog_path": "/"},
    })
    assert outcome["ok"] is False
    assert outcome["failures"][0]["step"] == "membership_term_missing"
    assert fake.blogs[1]["products"][100]["term_ids"] == before


# ---------------------------------------------------------------- 24: redirects come back


def test_restore_puts_the_redirect_rules_back_exactly(ready):
    from copy import deepcopy
    _, fake = ready
    # One planned path already has a rule pointing somewhere else; the others
    # have none at all.
    fake.redirects["/product-category/men-s/"] = {"id": 77, "new_path": "/clearance/",
                                                  "code": 301, "enabled": True}
    before = deepcopy(fake.redirects)
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    assert done["status"] == "completed", done["jobs"][0]["result"]
    assert fake.redirects["/product-category/men-s/"]["new_path"] == "/product-category/mens/"
    assert len(fake.redirects) > len(before)          # the run created the rest

    with database.cursor() as cursor:
        cursor.execute("SELECT payload FROM catmgr.job_snapshot WHERE job_id = %s",
                       (done["jobs"][0]["job_id"],))
        journal = cursor.fetchone()["payload"]["redirects_prior"]
    assert len(journal) == 5 and all(r["group_id"] == 1 for r in journal)
    present = [r for r in journal if r["present"]]
    assert [r["old_path"] for r in present] == ["/product-category/men-s/"]
    assert present[0]["action_data"] == "/clearance/" and present[0]["id"] == 77

    result = categories_runs.restore_blog(run["run_id"], done["jobs"][0]["job_id"],
                                          actor="tester", background=False)
    assert result["restore"]["status"] == "done", result["restore"]
    finalize_call = next(p for _, path, p in fake.calls
                         if path == "/restore" and p.get("phase") == "finalize")
    assert len(finalize_call["snapshot"]["redirects"]) == len(journal)
    assert fake.redirects == before


# ---------------------------------------------------------------- 25: a restored blog is not migrated


def test_restore_is_refused_while_the_run_is_paused_and_makes_it_partial(ready):
    _, fake = ready
    run = _write(categories_runs.create_run, env="prod", blog_ids=None)
    categories_runs.process_run(run["run_id"], actor="tester", max_jobs=1)
    _write(categories_runs.request_pause, run["run_id"])
    paused = categories_runs.process_run(run["run_id"], actor="tester")
    assert paused["status"] == "paused"
    job = next(j for j in paused["jobs"] if j["blog_id"] == 1)
    with pytest.raises(DraftConflict) as excinfo:
        categories_runs.restore_blog(run["run_id"], job["job_id"], actor="tester", background=False)
    assert "paused" in str(excinfo.value)

    _write(categories_runs.cancel, run["run_id"])
    result = categories_runs.restore_blog(run["run_id"], job["job_id"],
                                          actor="tester", background=False)
    assert result["restore"]["status"] == "done", result["restore"]
    detail = _read(categories_runs.get_run, run["run_id"])["jobs"]
    assert next(j for j in detail if j["blog_id"] == 1)["result"]["restored"] is True
    with database.cursor(write=True, actor="tester") as cursor:
        assert categories_runs._finish_run(cursor, run["run_id"]) == "completed_with_skips"


# ---------------------------------------------------------------- 26: a long restore stays alive


def test_a_long_restore_keeps_its_liveness_fence_while_polling(ready, monkeypatch):
    import time as clock
    _, fake = ready
    monkeypatch.setattr(categories_runs, "RESTORE_STALE_MINUTES", 0.002)     # 120 ms
    run = _write(categories_runs.create_run, env="prod", blog_ids=[1])
    done = categories_runs.process_run(run["run_id"], actor="tester")
    job_id = done["jobs"][0]["job_id"]
    observed = {"alive": [], "beats": [], "refused": 0}

    class SlowJobs(FakeWordPress):
        """Every restore phase is detached as a durable job whose polls take
        longer than the stale window."""

        def __init__(self, inner):
            super().__init__()
            self.inner = inner
            self.calls = inner.calls
            self.pending = {}

        def __call__(self, env, path, payload):
            if path == "/job":
                job = self.pending.get(payload["key"])
                if job is None:
                    raise categories_service.BrokerError("No job with that key.", 404)
                job["polls"] += 1
                with database.cursor() as cursor:
                    observed["alive"].append(len(categories_runs._running_restores(cursor, "prod")))
                    observed["beats"].append(categories_runs._job_restore_state(cursor, job_id)["heartbeat_at"])
                if job["polls"] == 2:
                    try:
                        _write(categories_runs.create_run, env="prod", blog_ids=[7])
                    except DraftConflict:
                        observed["refused"] += 1
                clock.sleep(0.15)               # longer than the stale window
                if job["polls"] < 2:
                    return {"ok": True, "job": {"key": payload["key"], "status": "running",
                                                "heartbeat_age": 1, "stale": False}}
                return {"ok": True, "job": {"key": payload["key"], "status": "done"},
                        "result": job["result"], "error": None}
            if path == "/restore":
                result = self.inner(env, path, payload)
                key = categories_runs.job_key(path, payload)
                self.pending[key] = {"polls": 0, "result": result}
                return {"ok": True, "async": True,
                        "job": {"key": key, "status": "running", "heartbeat_age": 0}}
            return self.inner(env, path, payload)

    monkeypatch.setattr(categories_runs, "broker_call", SlowJobs(fake))
    reserved = categories_runs.restore_blog(run["run_id"], job_id, actor="tester",
                                            background=False)
    assert reserved["restore"]["status"] == "done", reserved["restore"]
    assert observed["alive"] and all(n == 1 for n in observed["alive"])
    assert observed["beats"] == sorted(observed["beats"])
    assert observed["beats"][0] < observed["beats"][-1]      # the fence was renewed
    assert observed["refused"] >= 1

    # A heartbeat from a worker that no longer owns the restore changes nothing,
    # and a restore whose worker died does go stale.
    with _admin() as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE catmgr.run_job SET progress = progress || %s WHERE job_id = %s",
            ('{"restore": {"status": "running", "phase": "terms", "request_id": "mine",'
             ' "heartbeat_at": "2020-01-01T00:00:00+00:00"}}', job_id))
    categories_runs._touch_restore_heartbeat(job_id, "someone-else")
    with database.cursor() as cursor:
        state = categories_runs._job_restore_state(cursor, job_id)
        assert state["heartbeat_at"].startswith("2020-01-01")
        assert categories_runs._running_restores(cursor, "prod") == []


# ---------------------------------------------------------------- 27: one export, one live state


def test_pages_of_one_export_must_come_from_one_live_state(ready, monkeypatch):
    _, fake = ready
    monkeypatch.setattr(categories_service, "_broker", fake.broker_export)
    monkeypatch.setattr(categories_service, "_EXPORT_PAGE_LIMIT", 3)

    def move_a_membership(page):
        if page == 2:
            # One membership moves between two terms: the row COUNT is the same.
            fake.blogs[1]["products"][105]["term_ids"] = {14}

    fake.on_export_page = move_a_membership
    fake.export_pages = 0
    with pytest.raises(categories_service.ExportInconsistent):
        categories_service._fetch_export_once("prod", 1)

    def rename_a_term(page):
        if page == 2:
            fake.blogs[1]["terms"][10]["name"] = "Renamed Mid-Export"

    fake.on_export_page = rename_a_term
    fake.export_pages = 0
    with pytest.raises(categories_service.ExportInconsistent):
        categories_service._fetch_export_once("prod", 1)

    # A stable multi-page export costs no extra call: the generation is the check.
    fake.on_export_page = None
    fake.export_pages = 0
    export = categories_service._fetch_export_once("prod", 1)
    assert len(export["products"]) == export["products_total"] == 7
    assert fake.export_pages == 3


def test_a_broker_without_a_generation_is_checked_by_re_reading_page_one(monkeypatch):
    rows = [{"term_id": 1, "product_id": p, "sku": f"P{p}"} for p in range(1, 5)]
    calls = {"n": 0}

    def paged(env, path, method="GET", payload=None):
        calls["n"] += 1
        if payload.get("after_product_id") is None:
            return {"terms": [{"term_id": 1, "slug": "a", "name": "A"}], "products": rows[:2],
                    "products_total": 4, "next_after": {"term_id": 1, "product_id": 2},
                    "uncategorized": []}
        return {"products": rows[2:], "products_total": 4, "next_after": None,
                "uncategorized": []}

    monkeypatch.setattr(categories_service, "_broker", paged)
    monkeypatch.setattr(categories_service, "_EXPORT_PAGE_LIMIT", 2)
    assert len(categories_service.fetch_export("prod", 1)["products"]) == 4
    assert calls["n"] == 3          # two pages + one re-read of page 1

    def single(env, path, method="GET", payload=None):
        calls["n"] += 1
        return {"terms": [{"term_id": 1, "slug": "a", "name": "A"}], "products": rows,
                "products_total": 4, "next_after": None, "uncategorized": []}

    calls["n"] = 0
    monkeypatch.setattr(categories_service, "_broker", single)
    monkeypatch.setattr(categories_service, "_EXPORT_PAGE_LIMIT", 10)
    assert len(categories_service.fetch_export("prod", 1)["products"]) == 4
    assert calls["n"] == 1          # nothing to stitch, nothing to re-read


def test_products_without_a_category_are_paged_or_the_import_is_refused(ready, monkeypatch):
    _, fake = ready
    monkeypatch.setattr(categories_service, "_broker", fake.broker_export)
    fake.seed(9, "/nelson/", terms=[{"term_id": 90, "slug": "ppe", "name": "PPE"}],
              products=[], uncategorized=[{"product_id": 901, "sku": "A"},
                                          {"product_id": 902, "sku": "B"},
                                          {"product_id": 903, "sku": "C"}])
    fake.uncategorized_page_size = 1
    fake.export_pages = 0
    export = categories_service._fetch_export_once("prod", 9)
    assert [r["product_id"] for r in export["uncategorized"]] == [901, 902, 903]
    assert fake.export_pages >= 3

    # An older broker cuts the set off and offers no cursor: refuse rather than
    # import a copy that would plan and restore only part of the store.
    fake.uncategorized_pageable = False
    with pytest.raises(categories_service.BrokerError) as excinfo:
        categories_service._fetch_export_once("prod", 9)
    message = str(excinfo.value)
    assert "1 of 3" in message and "no category" in message
    assert not isinstance(excinfo.value, categories_service.ExportInconsistent)
