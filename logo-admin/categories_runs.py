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
ACTIVE_STATUSES = ("queued", "running", "paused")


def broker_call(env: str, path: str, payload: Dict[str, Any]) -> Any:
    """POST one broker call. Module-level so tests can monkeypatch it."""

    return categories_service._broker(env, path, method="POST", payload=payload)


# ---------------------------------------------------------------- gating


def apply_allowed(user_login: str) -> bool:
    from config import get_settings

    allowed = get_settings().catmgr_apply_users
    return bool(allowed) and user_login.strip().lower() in allowed


# ---------------------------------------------------------------- create


def create_run(cursor, *, env: str, blog_ids: Optional[List[int]],
               stop_on_failure: bool = True, actor: str) -> Dict[str, Any]:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtext('catmgr_run_' || %s))", (env,)
    )
    cursor.execute(
        "SELECT run_id, status FROM catmgr.run"
        " WHERE env = %s AND status IN %s",
        (env, ACTIVE_STATUSES),
    )
    active = cursor.fetchone()
    if active:
        raise DraftConflict(
            f"run {active['run_id']} is {active['status']} for {env};"
            " finish or cancel it first"
        )

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
    if env:
        cursor.execute(
            "SELECT * FROM catmgr.run WHERE env = %s ORDER BY run_id DESC LIMIT 50",
            (env,),
        )
    else:
        cursor.execute("SELECT * FROM catmgr.run ORDER BY run_id DESC LIMIT 50")
    return [dict(row) for row in cursor.fetchall()]


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


def resume(cursor, run_id: int, *, actor: str) -> Dict[str, Any]:
    run = get_run(cursor, run_id)
    if run["status"] not in ("paused", "failed", "queued"):
        raise DraftConflict(f"run is {run['status']}")
    cursor.execute(
        "UPDATE catmgr.run SET cancel_requested = false, status = 'queued'"
        " WHERE run_id = %s",
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
            "updates": payload["terms"]["update"],
            "creates": payload["terms"]["create"],
        })
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
            "rows": page,
        })
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
    result["finalize"] = broker_call(env, "/finalize", {
        "blog_id": blog_id,
        "run_id": run_id,
        "deletes": payload["terms"]["delete"],
        "redirects": payload["redirects"],
        "unspsc_renames": unspsc_renames,
        # ES reindex rides the existing */15 queue cron: a synchronous flush
        # here can outlast the HTTP timeout when the queue has a backlog
        # (observed on dev 2026-09-01).
        "run_es": False,
    })

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
        record_audit(cursor, actor=actor, action="apply_succeeded",
                     entity="job", entity_key=str(job_id),
                     detail={"run_id": run_id, "blog_id": blog_id,
                             "request_id": job["request_id"],
                             "result": {k: v for k, v in result.items()
                                        if k != "request_id"}})
    _finish_job(job_id, run_id, "done", result)


def _save_progress(job_id: int, progress: Dict[str, Any]) -> None:
    with database.cursor(write=True, actor="worker") as cursor:
        cursor.execute(
            "UPDATE catmgr.run_job SET progress = %s WHERE job_id = %s",
            (Json(progress), job_id),
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
            _finish_job(job["job_id"], run_id, "failed", {"error": message})
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
    thread = threading.Thread(
        target=process_run, args=(run_id,), kwargs={"actor": actor},
        name=f"catmgr-run-{run_id}", daemon=True,
    )
    _worker_threads[run_id] = thread
    thread.start()


# ---------------------------------------------------------------- restore


def restore_blog(run_id: int, job_id: int, *, actor: str) -> Dict[str, Any]:
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
    with database.cursor(write=True, actor=actor) as cursor:
        record_audit(cursor, actor=actor, action="restore_requested",
                     entity="job", entity_key=str(job_id),
                     detail={"run_id": run_id, "blog_id": job["blog_id"]})
    try:
        result = broker_call(run["env"], "/restore", {
            "blog_id": job["blog_id"],
            "snapshot": snapshot["payload"],
        })
    except Exception as exc:
        with database.cursor(write=True, actor=actor) as cursor:
            record_audit(cursor, actor=actor, action="restore_failed",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "error": str(exc)[:500]})
        raise
    with database.cursor(write=True, actor=actor) as cursor:
        record_audit(cursor, actor=actor, action="restore_succeeded",
                     entity="job", entity_key=str(job_id),
                     detail={"run_id": run_id, "result": result})
    return result


# ---------------------------------------------------------------- drift


def drift_audit(env: str, *, actor: str) -> Dict[str, Any]:
    """Re-import every snapshotted blog live, then report what a plan would
    still change. All-zero stats everywhere = converged."""

    with database.cursor() as cursor:
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
