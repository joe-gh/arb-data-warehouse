#!/usr/bin/env python3
"""Category editor integration cycle against a REAL WordPress (the dev site).

Runs ON the warehouse box, in-process with the app (like mcp_server.py), so it
exercises the real planner, the real engine and the real arb-category-apply.php
broker end to end - the things the fake broker in the unit tests can only
imitate. Every step is idempotent and prints a JSON verdict; the orchestrator
(a shell on the operator's machine) interleaves the WordPress-side mutations
that the fence steps need (wp-cli on the dev box).

    sudo env $(sudo cat /etc/arb-logo-admin.env | xargs) ARB_MCP_OPERATOR=<login> \\
        /opt/arb-logo-admin-venv/bin/python tests/integration/dev_cycle.py <step> [--blog 9]

Steps, in order:
    prepare        import the blog, seed the draft from it, curate: re-slug +
                   rename one leaf, create a child under it, merge a second
                   leaf into the first. Prints the plan summary.
    apply          readiness -> preview -> run -> verify (in-process worker).
    replay         re-POST the finished apply-terms phase with its exact key:
                   WordPress must replay the stored result, not re-run.
    fence-arm      curate one more change and create (not start) a new run.
    fence-check    after the orchestrator changed a membership in WordPress:
                   the run must be refused before any mutation.
    fence-recover  after the orchestrator reverted it: re-import, re-plan, run.
    restore        restore the blog from the first run's pre-apply snapshot
                   and compare it with the export captured in `prepare`.
    cleanup        drop the scratch draft (snapshots and audit stay).

Refuses to run against prod: the target is always the dev environment.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import categories_draft          # noqa: E402
import categories_mapping        # noqa: E402
import categories_planner        # noqa: E402
import categories_runs           # noqa: E402
import categories_service        # noqa: E402
from db import database          # noqa: E402

ENV = "dev"
STATE_FILE = "/tmp/catmgr-dev-cycle.json"
TAG = "itest"


def out(**payload: Any) -> None:
    print(json.dumps(payload, default=str, indent=2))


def fail(message: str, **payload: Any) -> None:
    out(ok=False, error=message, **payload)
    sys.exit(1)


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as handle:
            return json.load(handle)
    return {}


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w") as handle:
        json.dump(state, handle, default=str)


def operator() -> str:
    from config import get_settings
    allowed = sorted(get_settings().catmgr_apply_users)
    login = (os.environ.get("ARB_MCP_OPERATOR") or "").strip().lower()
    if login and login in allowed:
        return login
    if not allowed:
        fail("CATMGR_APPLY_USERS is empty; the cycle needs an apply-tier operator")
    return allowed[0]


def write(fn, *args, **kwargs):
    with database.cursor(write=True, actor=ACTOR) as cursor:
        return fn(cursor, *args, **kwargs, actor=ACTOR)


def read(fn, *args, **kwargs):
    with database.cursor() as cursor:
        return fn(cursor, *args, **kwargs)


def export_shape(export: Dict[str, Any]) -> Dict[str, Any]:
    """The comparable shape of an export: terms by id and memberships."""
    terms = {t["term_id"]: (t["slug"], t["name"], t["parent"], t.get("sort_order", 0), t.get("description", ""))
             for t in export["terms"]}
    members = sorted((p["term_id"], p["product_id"]) for p in export["products"])
    return {"terms": terms, "memberships": members,
            "uncategorized": sorted(u["product_id"] for u in export.get("uncategorized") or [])}


def leaf_with_products(export: Dict[str, Any], exclude=()):
    """Two leaf terms with products (no children), most-populated first."""
    parents = {t["parent"] for t in export["terms"]}
    counts: Dict[int, int] = {}
    for row in export["products"]:
        counts[row["term_id"]] = counts.get(row["term_id"], 0) + 1
    leaves = [t for t in export["terms"] if t["term_id"] not in parents and counts.get(t["term_id"])
              and t["slug"] not in exclude and t["slug"] != "uncategorized"]
    leaves.sort(key=lambda t: (-counts[t["term_id"]], t["term_id"]))
    return leaves


def step_prepare(blog_id: int, solo: bool = False) -> None:
    state: Dict[str, Any] = {"blog_id": blog_id}
    if solo:
        # Every live slug of the environment needs a disposition before a
        # preview passes; stale snapshots of other dev blogs would block a
        # single-blog cycle. They are scratch (re-importable any time).
        with database.cursor(write=True, actor=ACTOR) as cursor:
            for table in ("wp_term_product", "wp_uncategorized_product", "wp_term", "snapshot"):
                cursor.execute(f"DELETE FROM catmgr.{table} WHERE env = %s AND blog_id <> %s", (ENV, blog_id))
    export = categories_service.fetch_export(ENV, blog_id)
    state["baseline"] = export_shape(export)
    state["baseline_raw"] = {"terms": export["terms"], "products": export["products"],
                             "uncategorized": export.get("uncategorized") or []}
    imported = write(categories_service.import_export, env=ENV, blog_id=blog_id, export=export)
    write(categories_draft.seed_from_snapshot, env=ENV, blog_id=blog_id, force=True)
    leaves = leaf_with_products(export)
    if len(leaves) < 2:
        fail("blog needs at least two populated leaf categories", blog_id=blog_id)
    first, second = leaves[0], leaves[1]
    nodes = {n["slug"]: n for n in read(categories_draft.list_nodes)}
    target = nodes[first["slug"]]
    victim = nodes[second["slug"]]
    new_slug = f"{first['slug']}-{TAG}"
    write(categories_draft.update_node, target["node_id"], name=f"{target['name']} {TAG.upper()}", slug=new_slug)
    child = write(categories_draft.create_node, parent_id=target["node_id"], name=f"Integration {TAG}",
                  slug=f"integration-{TAG}")
    # Merge the second leaf into the first: its products carry over, its term goes.
    write(categories_draft.delete_node, victim["node_id"])
    write(categories_mapping.set_mapping, old_slug=second["slug"], action="map",
          target_node_id=target["node_id"], is_primary=False)
    plan = read(categories_planner.build_blog_plan, ENV, blog_id)
    preview = read(categories_planner.preview, ENV, [blog_id])
    state.update({
        "target": {"term_id": first["term_id"], "old_slug": first["slug"], "new_slug": new_slug,
                   "node_id": target["node_id"]},
        "victim": {"term_id": second["term_id"], "slug": second["slug"]},
        "child_slug": child["slug"],
        "snapshot_version": imported["version"],
    })
    save_state(state)
    out(ok=preview["ok"], blockers=preview["blockers"], stats=plan["stats"],
        target=state["target"], victim=state["victim"], child=child["slug"],
        redirects=plan["redirects"], unspsc=plan["unspsc"])


def step_apply() -> None:
    state = load_state()
    blog_id = state["blog_id"]
    ready = categories_runs.readiness(ENV, [blog_id])
    if not ready["ok"]:
        fail("not ready", failures=ready["failures"])
    run = write(categories_runs.create_run, env=ENV, blog_ids=[blog_id])
    write(categories_runs.start, run["run_id"])
    done = categories_runs.process_run(run["run_id"], actor=ACTOR)
    job = done["jobs"][0]
    state["run_id"] = run["run_id"]
    state["job_id"] = job["job_id"]
    state["request_id"] = job["request_id"]
    save_state(state)
    export = categories_service.fetch_export(ENV, blog_id)
    by_id = {t["term_id"]: t for t in export["terms"]}
    by_slug = {t["slug"]: t for t in export["terms"]}
    target, victim = state["target"], state["victim"]
    checks = {
        "run_completed": done["status"] == "completed",
        "job_verified": (job.get("result") or {}).get("verified") is True,
        "target_term_id_survived": target["term_id"] in by_id and by_id[target["term_id"]]["slug"] == target["new_slug"],
        "child_created_under_target": by_slug.get(state["child_slug"], {}).get("parent") == target["term_id"],
        "victim_term_gone": victim["term_id"] not in by_id,
        "no_parked_terms": not any(t.get("parked_from") for t in export["terms"]),
        "victim_products_moved": all(
            any(p["term_id"] == target["term_id"] and p["product_id"] == pid for p in export["products"])
            for pid in {p["product_id"] for p in state["baseline_raw"]["products"] if p["term_id"] == victim["term_id"]}
        ),
    }
    out(ok=all(checks.values()), checks=checks, run_status=done["status"],
        result={k: v for k, v in (job.get("result") or {}).items() if k != "terms"} | {"terms": (job.get("result") or {}).get("terms")},
        readiness_warnings=ready["warnings"])


def step_replay() -> None:
    state = load_state()
    blog_id = state["blog_id"]
    with database.cursor() as cursor:
        cursor.execute("SELECT payload, blog_path FROM catmgr.run_job WHERE job_id = %s", (state["job_id"],))
        row = cursor.fetchone()
    payload = row["payload"]
    before = export_shape(categories_service.fetch_export(ENV, blog_id))
    response = categories_runs.broker_call(ENV, "/apply-terms", {
        "blog_id": blog_id, "run_id": state["run_id"], "request_id": state["request_id"],
        "expected_blog_path": row["blog_path"],
        "updates": payload["terms"]["update"], "creates": payload["terms"]["create"],
        "doomed": [{"term_id": d["term_id"], "expected_slug": d["expected_slug"]} for d in payload["terms"]["delete"]],
        "unspsc_renames": payload.get("unspsc", {}).get("renames") or {},
        "unspsc_merges": payload.get("unspsc", {}).get("merges") or {},
    })
    after = export_shape(categories_service.fetch_export(ENV, blog_id))
    job = (response or {}).get("job") or {}
    out(ok=bool(job.get("replayed")) and before == after, replayed=job.get("replayed"),
        status=job.get("status"), unchanged=before == after)


def step_fence_arm() -> None:
    state = load_state()
    blog_id = state["blog_id"]
    # One more curation change so the next run has real work.
    write(categories_draft.update_node, state["target"]["node_id"],
          name=f"{state['target']['old_slug'].replace('-', ' ').title()} {TAG.upper()} 2")
    export = categories_service.fetch_export(ENV, blog_id)
    write(categories_service.import_export, env=ENV, blog_id=blog_id, export=export)
    preview = read(categories_planner.preview, ENV, [blog_id])
    if not preview["ok"]:
        fail("preview blocked", blockers=preview["blockers"])
    run = write(categories_runs.create_run, env=ENV, blog_ids=[blog_id])
    state["fence_run_id"] = run["run_id"]
    # A product to move in WordPress, and a term to move it into.
    product_id = next(p["product_id"] for p in export["products"] if p["term_id"] == state["target"]["term_id"])
    current = {p["term_id"] for p in export["products"] if p["product_id"] == product_id}
    # A category the product is NOT in yet - otherwise the wp-cli "add" is a
    # no-op and proves nothing.
    other_term = next(t for t in export["terms"] if t["term_id"] not in current
                      and t["slug"] not in ("uncategorized", state["child_slug"]))
    state["fence_product"] = product_id
    state["fence_term_id"] = other_term["term_id"]
    state["fence_term_slug"] = other_term["slug"]
    save_state(state)
    out(ok=True, run_id=run["run_id"], fingerprint=run["jobs"][0].get("stats") and True,
        mutate={"product_id": product_id, "add_term_slug": other_term["slug"], "blog_id": blog_id})


def step_fence_check() -> None:
    state = load_state()
    write(categories_runs.start, state["fence_run_id"])
    done = categories_runs.process_run(state["fence_run_id"], actor=ACTOR)
    job = done["jobs"][0]
    result = job.get("result") or {}
    export = categories_service.fetch_export(ENV, state["blog_id"])
    still_there = any(p["product_id"] == state["fence_product"] and p["term_id"] == state["fence_term_id"]
                      for p in export["products"])
    out(ok=done["status"] == "failed" and bool(result.get("live_drift")) and still_there,
        run_status=done["status"], error=result.get("error"), live_drift=result.get("live_drift"),
        membership_untouched=still_there)


def step_fence_recover() -> None:
    state = load_state()
    blog_id = state["blog_id"]
    export = categories_service.fetch_export(ENV, blog_id)
    if any(p["product_id"] == state["fence_product"] and p["term_id"] == state["fence_term_id"] for p in export["products"]):
        fail("revert the WordPress-side membership first")
    write(categories_service.import_export, env=ENV, blog_id=blog_id, export=export)
    with database.cursor(write=True, actor=ACTOR) as cursor:
        cursor.execute("UPDATE catmgr.run SET status = 'cancelled', finished_at = now() WHERE run_id = %s",
                       (state["fence_run_id"],))
        cursor.execute("UPDATE catmgr.run_job SET status = 'cancelled' WHERE run_id = %s AND status <> 'done'",
                       (state["fence_run_id"],))
    run = write(categories_runs.create_run, env=ENV, blog_ids=[blog_id])
    write(categories_runs.start, run["run_id"])
    done = categories_runs.process_run(run["run_id"], actor=ACTOR)
    job = done["jobs"][0]
    state["recover_run_id"] = run["run_id"]
    save_state(state)
    out(ok=done["status"] == "completed" and (job.get("result") or {}).get("verified") is True,
        run_status=done["status"], result_error=(job.get("result") or {}).get("error"))


def step_restore() -> None:
    state = load_state()
    result = categories_runs.restore_blog(state["run_id"], state["job_id"], actor=ACTOR, background=False)
    restore = result["restore"]
    export = categories_service.fetch_export(ENV, state["blog_id"])
    now = export_shape(export)
    base = state["baseline"]
    base_terms = {int(k): tuple(v) for k, v in base["terms"].items()}
    now_terms = now["terms"]
    surviving = {tid for tid in base_terms if tid in now_terms}
    id_preserved = all(base_terms[tid] == now_terms[tid] for tid in surviving)
    by_slug_base = {v[0]: v[1:] for v in base_terms.values()}
    by_slug_now = {v[0]: v[1:] for v in now_terms.values()}
    checks = {
        "restore_done": restore.get("status") == "done",
        "restore_verified": restore.get("verified") is True,
        "same_slugs": set(by_slug_base) == set(by_slug_now),
        "same_attributes_by_slug": all(by_slug_base[s][:2] == by_slug_now[s][:2] for s in by_slug_base),
        "surviving_term_ids_identical": id_preserved,
        "same_memberships_by_slug": _memberships_by_slug(state["baseline_raw"]) == _memberships_by_slug(export),
        "same_uncategorized": [int(x) for x in base["uncategorized"]] == now["uncategorized"],
    }
    out(ok=all(checks.values()), checks=checks, restore={k: v for k, v in restore.items() if k != "snapshot"})


def _memberships_by_slug(export: Dict[str, Any]):
    slugs = {t["term_id"]: t["slug"] for t in export["terms"]}
    rows = {}
    for p in export["products"]:
        if p["term_id"] in slugs:
            rows.setdefault(p["product_id"], set()).add(slugs[p["term_id"]])
    return {pid: sorted(s) for pid, s in rows.items()}


def step_cleanup() -> None:
    state = load_state()
    with database.cursor(write=True, actor=ACTOR) as cursor:
        cursor.execute("DELETE FROM catmgr.product_assignment")
        cursor.execute("DELETE FROM catmgr.assignment_rule")
        cursor.execute("DELETE FROM catmgr.slug_map")
        cursor.execute("DELETE FROM catmgr.node_store_override")
        cursor.execute("DELETE FROM catmgr.node")
        cursor.execute("DELETE FROM catmgr.uncategorized_ack")
        cursor.execute("DELETE FROM catmgr.run")      # cascades jobs, snapshots, redirects
        categories_service.record_audit(cursor, actor=ACTOR, action="test_data_cleared", entity="draft",
                                        entity_key="integration", detail={"state": {k: v for k, v in state.items()
                                                                                    if k not in ("baseline", "baseline_raw")}})
    if state.get("blog_id"):
        export = categories_service.fetch_export(ENV, state["blog_id"])
        write(categories_service.import_export, env=ENV, blog_id=state["blog_id"], export=export)
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    out(ok=True)


STEPS = {
    "prepare": lambda args: step_prepare(args.blog, solo=args.solo),
    "apply": lambda args: step_apply(),
    "replay": lambda args: step_replay(),
    "fence-arm": lambda args: step_fence_arm(),
    "fence-check": lambda args: step_fence_check(),
    "fence-recover": lambda args: step_fence_recover(),
    "restore": lambda args: step_restore(),
    "cleanup": lambda args: step_cleanup(),
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=sorted(STEPS))
    parser.add_argument("--blog", type=int, default=9)
    parser.add_argument("--solo", action="store_true", help="drop the other dev blogs' snapshots first")
    arguments = parser.parse_args()
    from config import get_settings
    if ENV not in get_settings().catmgr_targets:
        fail("the dev target is not configured")
    ACTOR = operator()
    database.open()
    try:
        STEPS[arguments.step](arguments)
    finally:
        database.close()
