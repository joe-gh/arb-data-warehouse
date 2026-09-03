"""Category editor: apply runs and the sequential job worker.

A run freezes one declarative plan per target blog (blog 1 first). The worker
converges blogs one at a time through the WP broker in three fenced,
idempotent, resumable phases (apply-terms -> paged apply-memberships ->
finalize). Failures stop the run by default; jobs are individually retryable
and the whole run resumes across app restarts because progress cursors live in
catmgr.run_job.progress.

Every WordPress-crossing job writes apply_requested / apply_succeeded /
apply_failed audit rows (the sync-intent pattern).
"""

import secrets
import threading
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

import categories_planner
import categories_service
from categories_draft import DraftConflict, DraftError
from categories_service import record_audit
from db import database


MEMBERSHIP_PAGE_SIZE = 400
RESTORE_PAGE_SIZE = 400
ACTIVE_STATUSES = ("queued", "running", "paused")
# A job still marked running whose run has not heartbeat for this long has
# lost its worker (app restart, crash): it is reclaimed and its progress
# cursors resume it.
STALE_JOB_MINUTES = 15


def broker_call(env: str, path: str, payload: Dict[str, Any]) -> Any:
    """POST one broker call. Module-level so tests can monkeypatch it."""

    return categories_service._broker(env, path, method="POST", payload=payload)


# ---------------------------------------------------------------- gating


def apply_allowed(user_login: str) -> bool:
    from config import get_settings

    allowed = get_settings().catmgr_apply_users
    return bool(allowed) and user_login.strip().lower() in allowed


def _assert_env_exclusive(cursor, env: str, run_id: Optional[int]) -> None:
    """One active run per environment - checked at create AND whenever an
    older run is woken again (resume, retry), never only at creation."""
    cursor.execute(
        "SELECT run_id, status FROM catmgr.run"
        " WHERE env = %s AND status IN %s AND run_id IS DISTINCT FROM %s"
        " ORDER BY run_id LIMIT 1",
        (env, ACTIVE_STATUSES, run_id),
    )
    other = cursor.fetchone()
    if other:
        raise DraftConflict(
            f"run {other['run_id']} is {other['status']} for {env};"
            " finish or cancel it first"
        )


# ---------------------------------------------------------------- create


def create_run(cursor, *, env: str, blog_ids: Optional[List[int]],
               stop_on_failure: bool = True, actor: str) -> Dict[str, Any]:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtext('catmgr_run_' || %s))", (env,)
    )
    _assert_env_exclusive(cursor, env, None)

    preview = categories_planner.preview(cursor, env, blog_ids)
    if not preview["ok"]:
        kinds = ", ".join(b["kind"] for b in preview["blockers"])
        raise DraftConflict(f"preview has blockers: {kinds}")
    if not preview["blogs"]:
        raise DraftError("no blogs to apply")

    ordered = sorted(preview["blogs"],
                     key=lambda b: (b["blog_id"] != 1, b["blog_id"]))
    dispositions = categories_planner.load_dispositions(cursor)
    extra_memberships = categories_planner.load_extra_memberships(cursor, env)

    cursor.execute(
        """
        INSERT INTO catmgr.run
            (env, target_blogs, status, plan_totals, snapshot_versions,
             stop_on_failure, created_by)
        VALUES (%s, %s, 'queued', %s, %s, %s, %s)
        RETURNING run_id
        """,
        (env, [b["blog_id"] for b in ordered], Json(preview["totals"]),
         Json({str(b["blog_id"]): b["snapshot_version"] for b in ordered}),
         stop_on_failure, actor[:100]),
    )
    run_id = cursor.fetchone()["run_id"]

    for seq, blog in enumerate(ordered, start=1):
        plan = categories_planner.build_blog_plan(
            cursor, env, blog["blog_id"],
            dispositions=dispositions, extra_memberships=extra_memberships,
        )
        cursor.execute(
            """
            INSERT INTO catmgr.run_job
                (run_id, blog_id, blog_path, seq, payload)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING job_id
            """,
            (run_id, plan["blog_id"], plan["blog_path"], seq, Json(plan)),
        )
        job_id = cursor.fetchone()["job_id"]
        for redirect in plan["redirects"]:
            cursor.execute(
                """
                INSERT INTO catmgr.redirect (run_id, blog_id, old_path, new_path)
                VALUES (%s, %s, %s, %s)
                """,
                (run_id, plan["blog_id"], redirect["old_path"],
                 redirect["new_path"]),
            )
        del job_id
    record_audit(cursor, actor=actor, action="run_created", entity="run",
                 entity_key=str(run_id),
                 detail={"env": env, "blogs": len(ordered),
                         "totals": preview["totals"]})
    return get_run(cursor, run_id)


# ---------------------------------------------------------------- reads


def list_runs(cursor, env: Optional[str] = None) -> List[Dict[str, Any]]:
    restoring_sql = """
        EXISTS (SELECT 1 FROM catmgr.run_job j
                 WHERE j.run_id = catmgr.run.run_id
                   AND j.progress -> 'restore' ->> 'status' = 'running') AS restoring
    """
    if env:
        cursor.execute(
            f"SELECT *, {restoring_sql} FROM catmgr.run WHERE env = %s ORDER BY run_id DESC LIMIT 50",
            (env,),
        )
    else:
        cursor.execute(f"SELECT *, {restoring_sql} FROM catmgr.run ORDER BY run_id DESC LIMIT 50")
    runs = [dict(row) for row in cursor.fetchall()]
    for run in runs:
        run["worker_stale"] = run["status"] == "running" and _stale(run)
    return runs


def get_run(cursor, run_id: int) -> Dict[str, Any]:
    cursor.execute("SELECT * FROM catmgr.run WHERE run_id = %s", (run_id,))
    run = cursor.fetchone()
    if run is None:
        raise DraftError(f"unknown run: {run_id}")
    run = dict(run)
    cursor.execute(
        """
        SELECT job_id, blog_id, blog_path, seq, status, progress, result,
               attempt, request_id, started_at, finished_at,
               (payload -> 'stats') AS stats,
               EXISTS (SELECT 1 FROM catmgr.job_snapshot s
                        WHERE s.job_id = catmgr.run_job.job_id) AS has_snapshot
          FROM catmgr.run_job WHERE run_id = %s ORDER BY seq
        """,
        (run_id,),
    )
    run["jobs"] = [dict(row) for row in cursor.fetchall()]
    for job in run["jobs"]:
        job["restore"] = (job.get("progress") or {}).get("restore") or None
    run["worker_stale"] = run["status"] == "running" and _stale(run)
    return run


# ---------------------------------------------------------------- controls


def _set_run_status(cursor, run_id: int, status: str, *,
                    finished: bool = False) -> None:
    cursor.execute(
        "UPDATE catmgr.run SET status = %s"
        + (", finished_at = now()" if finished else "")
        + " WHERE run_id = %s",
        (status, run_id),
    )


def request_pause(cursor, run_id: int, *, actor: str) -> Dict[str, Any]:
    run = get_run(cursor, run_id)
    if run["status"] not in ("queued", "running"):
        raise DraftConflict(f"run is {run['status']}")
    cursor.execute(
        "UPDATE catmgr.run SET cancel_requested = true WHERE run_id = %s",
        (run_id,),
    )
    record_audit(cursor, actor=actor, action="run_pause_requested",
                 entity="run", entity_key=str(run_id), detail={})
    return get_run(cursor, run_id)


def _stale(run: Dict[str, Any]) -> bool:
    """A running run whose worker has not heartbeat for STALE_JOB_MINUTES."""
    cursor_time = run.get("worker_heartbeat_at")
    if cursor_time is None:
        return True
    from datetime import datetime, timedelta, timezone
    return cursor_time < datetime.now(timezone.utc) - timedelta(minutes=STALE_JOB_MINUTES)


def _reclaim_stale_jobs(cursor, run_id: int, *, actor: str) -> int:
    """Return running jobs of a run with a dead worker to pending. Their
    progress cursors (snapshot taken, terms done, membership offset, finalize
    done) make the re-run converge instead of repeat."""
    cursor.execute(
        "UPDATE catmgr.run_job SET status = 'pending'"
        " WHERE run_id = %s AND status = 'running' RETURNING job_id, blog_id",
        (run_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    for row in rows:
        record_audit(cursor, actor=actor, action="job_reclaimed", entity="job",
                     entity_key=str(row["job_id"]),
                     detail={"run_id": run_id, "blog_id": row["blog_id"],
                             "reason": "worker heartbeat stale"})
    return len(rows)


def resume(cursor, run_id: int, *, actor: str) -> Dict[str, Any]:
    run = get_run(cursor, run_id)
    if run["status"] == "running":
        if not _stale(run):
            raise DraftConflict("run is running and its worker is alive")
        _reclaim_stale_jobs(cursor, run_id, actor=actor)
    elif run["status"] not in ("paused", "failed", "queued"):
        raise DraftConflict(f"run is {run['status']}")
    _assert_env_exclusive(cursor, run["env"], run_id)
    cursor.execute(
        "UPDATE catmgr.run SET cancel_requested = false, status = 'queued',"
        " finished_at = NULL WHERE run_id = %s",
        (run_id,),
    )
    record_audit(cursor, actor=actor, action="run_resumed", entity="run",
                 entity_key=str(run_id), detail={})
    return get_run(cursor, run_id)


def cancel(cursor, run_id: int, *, actor: str) -> Dict[str, Any]:
    run = get_run(cursor, run_id)
    if run["status"] in ("completed", "cancelled"):
        raise DraftConflict(f"run is already {run['status']}")
    if run["status"] == "running":
        raise DraftConflict("pause the run first; the current job must finish")
    cursor.execute(
        "UPDATE catmgr.run SET status = 'cancelled', finished_at = now()"
        " WHERE run_id = %s",
        (run_id,),
    )
    cursor.execute(
        "UPDATE catmgr.run_job SET status = 'cancelled'"
        " WHERE run_id = %s AND status = 'pending'",
        (run_id,),
    )
    record_audit(cursor, actor=actor, action="run_cancelled", entity="run",
                 entity_key=str(run_id), detail={})
    return get_run(cursor, run_id)


def retry_job(cursor, run_id: int, job_id: int, *, actor: str) -> Dict[str, Any]:
    run = get_run(cursor, run_id)
    if run["status"] == "running" and not _stale(run):
        raise DraftConflict("run is running; wait for the current job to finish")
    _assert_env_exclusive(cursor, run["env"], run_id)
    cursor.execute(
        "UPDATE catmgr.run_job SET status = 'pending'"
        " WHERE run_id = %s AND job_id = %s AND status IN ('failed', 'skipped')"
        " RETURNING blog_id",
        (run_id, job_id),
    )
    if cursor.fetchone() is None:
        raise DraftConflict("job is not failed/skipped")
    cursor.execute(
        "UPDATE catmgr.run SET status = 'queued', cancel_requested = false,"
        " finished_at = NULL WHERE run_id = %s",
        (run_id,),
    )
    record_audit(cursor, actor=actor, action="job_retried", entity="job",
                 entity_key=str(job_id), detail={"run_id": run_id})
    return get_run(cursor, run_id)


def skip_job(cursor, run_id: int, job_id: int, *, actor: str) -> Dict[str, Any]:
    cursor.execute(
        "UPDATE catmgr.run_job SET status = 'skipped', finished_at = now()"
        " WHERE run_id = %s AND job_id = %s AND status = 'failed'"
        " RETURNING blog_id",
        (run_id, job_id),
    )
    if cursor.fetchone() is None:
        raise DraftConflict("only failed jobs can be skipped")
    record_audit(cursor, actor=actor, action="job_skipped", entity="job",
                 entity_key=str(job_id), detail={"run_id": run_id})
    # Skipping must let the run continue (or finish) - it used to strand the
    # run in 'failed' with pending jobs nobody could reach.
    cursor.execute(
        "SELECT count(*) AS n FROM catmgr.run_job"
        " WHERE run_id = %s AND status IN ('pending', 'running')",
        (run_id,),
    )
    if cursor.fetchone()["n"]:
        _assert_env_exclusive(cursor, get_run(cursor, run_id)["env"], run_id)
        cursor.execute(
            "UPDATE catmgr.run SET status = 'queued', cancel_requested = false,"
            " finished_at = NULL WHERE run_id = %s",
            (run_id,),
        )
    else:
        cursor.execute(
            """
            UPDATE catmgr.run
               SET status = CASE WHEN EXISTS (
                       SELECT 1 FROM catmgr.run_job
                        WHERE run_id = %s AND status = 'failed')
                   THEN 'failed' ELSE 'completed' END,
                   finished_at = now()
             WHERE run_id = %s
            """,
            (run_id, run_id),
        )
    return get_run(cursor, run_id)


# ---------------------------------------------------------------- engine


def _claim_next_job(run_id: int) -> Optional[Dict[str, Any]]:
    with database.cursor(write=True, actor="worker") as cursor:
        cursor.execute(
            "SELECT cancel_requested, status FROM catmgr.run WHERE run_id = %s"
            " FOR UPDATE",
            (run_id,),
        )
        run = cursor.fetchone()
        if run is None:
            return None
        # A job left 'running' by a dead worker (app restart mid-job) blocks
        # the run forever unless reclaimed; the heartbeat says whether the
        # worker is really gone.
        cursor.execute(
            "SELECT worker_heartbeat_at FROM catmgr.run WHERE run_id = %s", (run_id,),
        )
        heartbeat = cursor.fetchone()
        if _stale({"worker_heartbeat_at": heartbeat["worker_heartbeat_at"]}):
            _reclaim_stale_jobs(cursor, run_id, actor="worker")
        if run["cancel_requested"]:
            cursor.execute(
                "UPDATE catmgr.run SET status = 'paused',"
                " cancel_requested = false WHERE run_id = %s",
                (run_id,),
            )
            return None
        cursor.execute(
            """
            SELECT job_id, blog_id, payload, progress, attempt
              FROM catmgr.run_job
             WHERE run_id = %s AND status = 'pending'
             ORDER BY seq LIMIT 1 FOR UPDATE SKIP LOCKED
            """,
            (run_id,),
        )
        job = cursor.fetchone()
        if job is None:
            # Another worker may still be mid-job: the run is only finished
            # when nothing is pending AND nothing is running.
            cursor.execute(
                "SELECT count(*) AS n FROM catmgr.run_job"
                " WHERE run_id = %s AND status = 'running'",
                (run_id,),
            )
            if cursor.fetchone()["n"]:
                return None
            cursor.execute(
                """
                UPDATE catmgr.run
                   SET status = CASE WHEN EXISTS (
                           SELECT 1 FROM catmgr.run_job
                            WHERE run_id = %s AND status = 'failed')
                       THEN 'failed' ELSE 'completed' END,
                       finished_at = now()
                 WHERE run_id = %s
                """,
                (run_id, run_id),
            )
            return None
        request_id = secrets.token_hex(16)
        cursor.execute(
            """
            UPDATE catmgr.run_job
               SET status = 'running', attempt = attempt + 1,
                   started_at = now(), request_id = %s
             WHERE job_id = %s
            """,
            (request_id, job["job_id"]),
        )
        cursor.execute(
            "UPDATE catmgr.run SET status = 'running',"
            " started_at = COALESCE(started_at, now()),"
            " worker_heartbeat_at = now() WHERE run_id = %s",
            (run_id,),
        )
        return {**dict(job), "request_id": request_id}


def _finish_job(job_id: int, run_id: int, status: str,
                result: Dict[str, Any]) -> None:
    with database.cursor(write=True, actor="worker") as cursor:
        cursor.execute(
            "UPDATE catmgr.run_job SET status = %s, result = %s,"
            " finished_at = now() WHERE job_id = %s",
            (status, Json(result), job_id),
        )
        cursor.execute(
            "UPDATE catmgr.run SET worker_heartbeat_at = now()"
            " WHERE run_id = %s",
            (run_id,),
        )


def _execute_job(env: str, run_id: int, job: Dict[str, Any], actor: str) -> None:
    job_id = job["job_id"]
    payload = job["payload"]
    blog_id = payload["blog_id"]
    progress = dict(job["progress"] or {})
    result: Dict[str, Any] = {"request_id": job["request_id"]}

    with database.cursor(write=True, actor=actor) as cursor:
        # Staleness fence: the plan must match the CURRENT snapshot version.
        cursor.execute(
            "SELECT version FROM catmgr.snapshot WHERE env = %s AND blog_id = %s",
            (env, blog_id),
        )
        row = cursor.fetchone()
        current_version = row["version"] if row else None
        if current_version != payload["snapshot_version"]:
            raise DraftConflict(
                f"snapshot for blog {blog_id} is v{current_version}, plan was"
                f" built on v{payload['snapshot_version']}; re-plan required"
            )
        record_audit(cursor, actor=actor, action="apply_requested",
                     entity="job", entity_key=str(job_id),
                     detail={"run_id": run_id, "blog_id": blog_id,
                             "request_id": job["request_id"],
                             "stats": payload["stats"]})

    # Pre-apply live capture for emergency restore (once per job).
    if not progress.get("snapshot_taken"):
        live = categories_service.fetch_export(env, blog_id)
        with database.cursor(write=True, actor=actor) as cursor:
            cursor.execute(
                """
                INSERT INTO catmgr.job_snapshot (job_id, payload)
                VALUES (%s, %s)
                ON CONFLICT (job_id) DO UPDATE
                   SET payload = EXCLUDED.payload, taken_at = now()
                """,
                (job_id, Json({"terms": live.get("terms") or [],
                               "products": live.get("products") or [],
                               "blog_path": live.get("blog_path") or ""})),
            )
        progress["snapshot_taken"] = True
        _save_progress(job_id, progress)

    if not progress.get("terms_done"):
        result["terms"] = broker_call(env, "/apply-terms", {
            "blog_id": blog_id,
            "run_id": run_id,
            "request_id": job["request_id"],
            "updates": payload["terms"]["update"],
            "creates": payload["terms"]["create"],
            # Terms finalize will delete: the broker parks them on temp slugs
            # in the same pass so a merge target can take their slug.
            "doomed": [{"term_id": d["term_id"], "expected_slug": d["expected_slug"]}
                       for d in payload["terms"]["delete"]],
        })
        if not isinstance(result["terms"], dict) or result["terms"].get("ok") is False:
            raise DraftConflict(
                f"apply-terms was refused for blog {blog_id}: {result['terms']}"
            )
        progress["terms_done"] = True
        _save_progress(job_id, progress)

    memberships = payload["memberships"]
    offset = int(progress.get("membership_offset") or 0)
    applied = int(progress.get("membership_applied") or 0)
    while offset < len(memberships):
        page = memberships[offset:offset + MEMBERSHIP_PAGE_SIZE]
        outcome = broker_call(env, "/apply-memberships", {
            "blog_id": blog_id,
            "run_id": run_id,
            "request_id": job["request_id"],
            "rows": page,
        })
        fence = _fence_report(outcome, ("skipped", "missing_slugs"))
        if fence:
            # Rows the broker refused (product gone, SKU changed, target slug
            # missing) mean WordPress drifted from the snapshot: stop here with
            # the report - the cursor keeps the page so a re-plan + retry can
            # resume - rather than finishing a job WordPress did not apply.
            result["memberships"] = {"applied": applied, "total": len(memberships),
                                     "offset": offset, "fence": fence}
            _save_progress(job_id, {**progress, "membership_fence": fence})
            raise DraftConflict(
                f"apply-memberships refused rows for blog {blog_id}: {fence}"
            )
        applied += int(outcome.get("applied") or 0)
        offset += len(page)
        progress["membership_offset"] = offset
        progress["membership_applied"] = applied
        _save_progress(job_id, progress)
    result["memberships"] = {"applied": applied, "total": len(memberships)}

    unspsc_renames = {}
    if blog_id == 1:
        unspsc_renames = {
            u["expected_slug"]: u["set"]["slug"]
            for u in payload["terms"]["update"]
            if u.get("changed", {}).get("slug")
        }
    if progress.get("finalize_done"):
        # A retry after finalize already ran must not repeat it (Redirection
        # rows would duplicate); the recorded outcome stands.
        result["finalize"] = progress.get("finalize_result") or {"ok": True, "replayed": True}
    else:
        result["finalize"] = broker_call(env, "/finalize", {
            "blog_id": blog_id,
            "run_id": run_id,
            "request_id": job["request_id"],
            "deletes": payload["terms"]["delete"],
            "redirects": payload["redirects"],
            "unspsc_renames": unspsc_renames,
            # ES reindex rides the existing */15 queue cron: a synchronous flush
            # here can outlast the HTTP timeout when the queue has a backlog
            # (observed on dev 2026-09-01).
            "run_es": False,
        })
        progress["finalize_done"] = True
        progress["finalize_result"] = {
            k: v for k, v in (result["finalize"] or {}).items()
            if k in ("ok", "deleted", "delete_report", "redirects_created",
                     "redirects_failed", "unspsc_rewritten", "recounted_terms")
        }
        _save_progress(job_id, progress)
    finalize_report = _fence_report(result["finalize"], ("delete_report", "redirects_failed"))
    drifted_deletes = [
        row for row in (result["finalize"] or {}).get("delete_report") or []
        if isinstance(row, dict) and row.get("status") in ("slug_drift", "failed", "has_products")
    ]

    with database.cursor(write=True, actor=actor) as cursor:
        if blog_id == 1 and payload["redirects"]:
            created = {
                (r.get("old_path"), r.get("new_path"))
                for r in (result["finalize"].get("redirects_created") or [])
            }
            for redirect in payload["redirects"]:
                ok = (redirect["old_path"], redirect["new_path"]) in created
                cursor.execute(
                    """
                    UPDATE catmgr.redirect SET status = %s
                     WHERE run_id = %s AND blog_id = %s AND old_path = %s
                    """,
                    ("created" if ok else "failed", run_id, blog_id,
                     redirect["old_path"]),
                )
        if drifted_deletes:
            result["finalize_fence"] = drifted_deletes[:50]
            record_audit(cursor, actor=actor, action="apply_failed",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "blog_id": blog_id,
                                 "request_id": job["request_id"],
                                 "error": "finalize refused deletes",
                                 "delete_report": drifted_deletes[:50]})
        else:
            record_audit(cursor, actor=actor, action="apply_succeeded",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "blog_id": blog_id,
                                 "request_id": job["request_id"],
                                 "result": {k: v for k, v in result.items()
                                            if k != "request_id"}})
    if drifted_deletes:
        raise DraftConflict(
            f"finalize refused {len(drifted_deletes)} delete(s) for blog {blog_id}"
            f" (terms still carry products or moved): {drifted_deletes[:5]}"
        )
    if finalize_report.get("redirects_failed"):
        result["redirect_failures"] = finalize_report["redirects_failed"]
    # The warehouse copy now matches WordPress: refresh the snapshot so a
    # re-preview reports convergence rather than the same plan again.
    if not progress.get("snapshot_refreshed"):
        try:
            result["snapshot"] = _refresh_snapshot(env, blog_id, actor)
            progress["snapshot_refreshed"] = True
            _save_progress(job_id, progress)
        except Exception as exc:  # noqa: BLE001 - the apply itself succeeded
            result["snapshot"] = {"error": str(exc)[:300]}
    _finish_job(job_id, run_id, "done", result)


def _save_progress(job_id: int, progress: Dict[str, Any]) -> None:
    with database.cursor(write=True, actor="worker") as cursor:
        cursor.execute(
            "UPDATE catmgr.run_job SET progress = %s WHERE job_id = %s",
            (Json(progress), job_id),
        )
        # Every cursor advance is a heartbeat: a long membership loop must
        # never look dead to the stale-job reclaim.
        cursor.execute(
            "UPDATE catmgr.run SET worker_heartbeat_at = now()"
            " WHERE run_id = (SELECT run_id FROM catmgr.run_job WHERE job_id = %s)",
            (job_id,),
        )


def _fence_report(outcome: Any, keys) -> Dict[str, Any]:
    """Broker-reported fence violations, compacted for the job result."""
    report: Dict[str, Any] = {}
    if not isinstance(outcome, dict):
        return {"malformed": True}
    if outcome.get("ok") is False:
        report["ok"] = False
    for key in keys:
        rows = outcome.get(key)
        if isinstance(rows, list) and rows:
            report[key] = rows[:50]
            report[f"{key}_count"] = len(rows)
    return report


def _refresh_snapshot(env: str, blog_id: int, actor: str) -> Dict[str, Any]:
    """Re-import one blog after a successful apply/restore so the warehouse
    copy matches WordPress and a re-preview shows convergence, not the same
    diff again."""
    export = categories_service.fetch_export(env, blog_id)
    with database.cursor(write=True, actor=actor) as cursor:
        return categories_service.import_blog_snapshot(
            cursor, env=env, blog_id=blog_id,
            blog_path=str(export.get("blog_path") or ""),
            terms=export.get("terms") or [],
            products=export.get("products") or [],
            actor=actor,
        )


def process_run(run_id: int, *, actor: str = "worker",
                max_jobs: Optional[int] = None) -> Dict[str, Any]:
    """Run pending jobs sequentially. Synchronous; the thread wrapper and
    tests both call this."""

    with database.cursor() as cursor:
        run = get_run(cursor, run_id)
    env = run["env"]
    stop_on_failure = run["stop_on_failure"]
    processed = 0
    while max_jobs is None or processed < max_jobs:
        job = _claim_next_job(run_id)
        if job is None:
            break
        try:
            _execute_job(env, run_id, job, actor)
        except Exception as exc:  # noqa: BLE001 - every failure must land in the job row
            message = str(exc)[:1000]
            with database.cursor(write=True, actor=actor) as cursor:
                record_audit(cursor, actor=actor, action="apply_failed",
                             entity="job", entity_key=str(job["job_id"]),
                             detail={"run_id": run_id,
                                     "blog_id": job["payload"]["blog_id"],
                                     "request_id": job["request_id"],
                                     "error": message})
                cursor.execute(
                    "SELECT progress FROM catmgr.run_job WHERE job_id = %s",
                    (job["job_id"],),
                )
                saved = cursor.fetchone()
            failure = {"error": message, "request_id": job["request_id"]}
            fence = (saved["progress"] or {}).get("membership_fence") if saved else None
            if fence:
                failure["membership_fence"] = fence
            _finish_job(job["job_id"], run_id, "failed", failure)
            if stop_on_failure:
                with database.cursor(write=True, actor=actor) as cursor:
                    _set_run_status(cursor, run_id, "failed", finished=True)
                break
        processed += 1
    with database.cursor() as cursor:
        return get_run(cursor, run_id)


_worker_threads: Dict[int, threading.Thread] = {}


def start_run(run_id: int, *, actor: str) -> None:
    """Kick the worker thread (idempotent while one is alive)."""

    existing = _worker_threads.get(run_id)
    if existing and existing.is_alive():
        return
    for stale_id in [rid for rid, t in _worker_threads.items() if not t.is_alive()]:
        _worker_threads.pop(stale_id, None)
    thread = threading.Thread(
        target=process_run, args=(run_id,), kwargs={"actor": actor},
        name=f"catmgr-run-{run_id}", daemon=True,
    )
    _worker_threads[run_id] = thread
    thread.start()


def recover_runs(*, actor: str = "startup") -> List[int]:
    """At app start, wake every run the previous process left queued or
    running. Stale running jobs are reclaimed by the worker's claim step, so
    a restart mid-job resumes from the job's progress cursor instead of
    wedging the run forever."""

    with database.cursor(write=True, actor=actor) as cursor:
        cursor.execute(
            "SELECT run_id FROM catmgr.run WHERE status IN ('queued', 'running')"
            " ORDER BY run_id",
        )
        run_ids = [row["run_id"] for row in cursor.fetchall()]
        for run_id in run_ids:
            # This process just started: no worker of ours can be alive, so a
            # 'running' job is certainly orphaned - reclaim it now rather than
            # after the heartbeat window.
            cursor.execute(
                "UPDATE catmgr.run_job SET status = 'pending'"
                " WHERE run_id = %s AND status = 'running' RETURNING job_id, blog_id",
                (run_id,),
            )
            for row in cursor.fetchall():
                record_audit(cursor, actor=actor, action="job_reclaimed", entity="job",
                             entity_key=str(row["job_id"]),
                             detail={"run_id": run_id, "blog_id": row["blog_id"],
                                     "reason": "app restart"})
            cursor.execute(
                "UPDATE catmgr.run SET status = 'queued', worker_heartbeat_at = now()"
                " WHERE run_id = %s AND status = 'running'",
                (run_id,),
            )
    for run_id in run_ids:
        start_run(run_id, actor=actor)
    return run_ids


# ---------------------------------------------------------------- restore


def _save_restore_progress(job_id: int, restore: Dict[str, Any]) -> None:
    with database.cursor(write=True, actor="worker") as cursor:
        cursor.execute(
            "UPDATE catmgr.run_job SET progress = progress || %s WHERE job_id = %s",
            (Json({"restore": restore}), job_id),
        )


def _restore_worker(env: str, run_id: int, job_id: int, blog_id: int,
                    snapshot: Dict[str, Any], actor: str) -> None:
    """Paged, resumable restore: terms pass -> membership pages -> finalize.
    Each broker call is bounded so blog 1 never meets the WP time limit."""

    terms = snapshot.get("terms") or []
    products = snapshot.get("products") or []
    blog_path = snapshot.get("blog_path") or ""
    restore = {"status": "running", "phase": "terms", "offset": 0,
               "total": len(products), "terms": len(terms), "error": None}
    _save_restore_progress(job_id, restore)
    try:
        outcome = broker_call(env, "/restore", {
            "blog_id": blog_id, "run_id": run_id, "phase": "terms",
            "snapshot": {"terms": terms, "blog_path": blog_path},
        })
        if not isinstance(outcome, dict) or outcome.get("ok") is False:
            raise DraftConflict(f"restore terms pass refused: {outcome}")
        restore["terms_result"] = {k: outcome.get(k) for k in ("terms", "created", "updated")}
        restore["phase"] = "memberships"
        _save_restore_progress(job_id, restore)
        offset = 0
        restored = 0
        while offset < len(products):
            page = products[offset:offset + RESTORE_PAGE_SIZE]
            outcome = broker_call(env, "/restore", {
                "blog_id": blog_id, "run_id": run_id, "phase": "memberships",
                "snapshot": {"terms": terms, "products": page, "blog_path": blog_path},
                "products_offset": offset,
            })
            if not isinstance(outcome, dict) or outcome.get("ok") is False:
                raise DraftConflict(f"restore membership page refused at {offset}: {outcome}")
            restored += int(outcome.get("products_restored") or 0)
            offset += len(page)
            restore["offset"] = offset
            restore["products_restored"] = restored
            _save_restore_progress(job_id, restore)
        restore["phase"] = "finalize"
        _save_restore_progress(job_id, restore)
        outcome = broker_call(env, "/restore", {
            "blog_id": blog_id, "run_id": run_id, "phase": "finalize",
            "snapshot": {"terms": terms, "blog_path": blog_path},
        })
        if not isinstance(outcome, dict) or outcome.get("ok") is False:
            raise DraftConflict(f"restore finalize refused: {outcome}")
        restore["terms_removed"] = outcome.get("terms_removed")
        restore["status"] = "done"
        restore["phase"] = "done"
        _save_restore_progress(job_id, restore)
        try:
            restore["snapshot"] = _refresh_snapshot(env, blog_id, actor)
        except Exception as exc:  # noqa: BLE001 - restore itself succeeded
            restore["snapshot"] = {"error": str(exc)[:300]}
        _save_restore_progress(job_id, restore)
        with database.cursor(write=True, actor=actor) as cursor:
            record_audit(cursor, actor=actor, action="restore_succeeded",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "blog_id": blog_id,
                                 "result": {k: v for k, v in restore.items() if k != "snapshot"}})
    except Exception as exc:  # noqa: BLE001 - land every failure in the job row
        restore["status"] = "failed"
        restore["error"] = str(exc)[:800]
        _save_restore_progress(job_id, restore)
        with database.cursor(write=True, actor=actor) as cursor:
            record_audit(cursor, actor=actor, action="restore_failed",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "blog_id": blog_id,
                                 "phase": restore.get("phase"),
                                 "offset": restore.get("offset"),
                                 "error": restore["error"]})


_restore_threads: Dict[int, threading.Thread] = {}


def restore_blog(run_id: int, job_id: int, *, actor: str,
                 background: bool = True) -> Dict[str, Any]:
    """Start (or, in tests, run inline) the paged restore of one job's blog
    from its pre-apply snapshot. Progress lives in run_job.progress.restore;
    a restore may be re-requested after a failure and resumes from scratch
    (every pass is convergent)."""

    with database.cursor() as cursor:
        run = get_run(cursor, run_id)
        cursor.execute(
            "SELECT payload FROM catmgr.job_snapshot WHERE job_id = %s",
            (job_id,),
        )
        snapshot = cursor.fetchone()
        job = next((j for j in run["jobs"] if j["job_id"] == job_id), None)
    if job is None:
        raise DraftError(f"job {job_id} is not part of run {run_id}")
    if snapshot is None:
        raise DraftError(f"job {job_id} has no pre-apply snapshot")
    if run["status"] in ("queued", "running") and not _stale(run):
        raise DraftConflict("the run is still active; pause it before restoring a blog")
    current = (job.get("restore") or {})
    existing = _restore_threads.get(job_id)
    if current.get("status") == "running" and existing and existing.is_alive():
        raise DraftConflict("a restore is already running for this blog")
    with database.cursor(write=True, actor=actor) as cursor:
        record_audit(cursor, actor=actor, action="restore_requested",
                     entity="job", entity_key=str(job_id),
                     detail={"run_id": run_id, "blog_id": job["blog_id"],
                             "products": len(snapshot["payload"].get("products") or [])})
    args = (run["env"], run_id, job_id, job["blog_id"], snapshot["payload"], actor)
    if not background:
        _restore_worker(*args)
        with database.cursor() as cursor:
            return {"accepted": True, "restore": _job_restore_state(cursor, job_id)}
    thread = threading.Thread(target=_restore_worker, args=args,
                              name=f"catmgr-restore-{job_id}", daemon=True)
    _restore_threads[job_id] = thread
    thread.start()
    return {"accepted": True, "restore": {"status": "running", "phase": "terms",
                                          "total": len(snapshot["payload"].get("products") or [])}}


def _job_restore_state(cursor, job_id: int) -> Dict[str, Any]:
    cursor.execute("SELECT progress FROM catmgr.run_job WHERE job_id = %s", (job_id,))
    row = cursor.fetchone()
    return ((row["progress"] or {}).get("restore") or {}) if row else {}


# ---------------------------------------------------------------- drift


def drift_audit(env: str, *, actor: str) -> Dict[str, Any]:
    """Re-import every snapshotted blog live, then report what a plan would
    still change. All-zero stats everywhere = converged. Refused while a run
    is active: a re-import bumps snapshot versions and would fail every
    remaining job's staleness fence."""

    with database.cursor() as cursor:
        _assert_env_exclusive(cursor, env, None)
        cursor.execute(
            "SELECT blog_id FROM catmgr.snapshot WHERE env = %s ORDER BY blog_id",
            (env,),
        )
        blog_ids = [row["blog_id"] for row in cursor.fetchall()]
    refreshed = []
    for blog_id in blog_ids:
        export = categories_service.fetch_export(env, blog_id)
        with database.cursor(write=True, actor=actor) as cursor:
            refreshed.append(categories_service.import_blog_snapshot(
                cursor, env=env, blog_id=blog_id,
                blog_path=str(export.get("blog_path") or ""),
                terms=export.get("terms") or [],
                products=export.get("products") or [],
                actor=actor,
            ))
    with database.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '120s'")
        outcome = categories_planner.preview(cursor, env)
    pending = [b for b in outcome["blogs"]
               if any(b["stats"].get(k) for k in
                      ("changed_updates", "creates", "deletes",
                       "membership_changes"))]
    return {
        "env": env,
        "refreshed_blogs": len(refreshed),
        "converged": outcome["ok"] and not pending,
        "blockers": outcome["blockers"],
        "pending": pending,
    }
