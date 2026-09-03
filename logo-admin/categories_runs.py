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
import time
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


# The broker runs every mutating phase as a durable job on the WordPress side:
# it fences, answers 202 with a job key, keeps working with a heartbeat, and
# we poll /job until it lands. The proxy's read timeout therefore never
# decides how far an apply gets (nginx cut a 131-term apply at 300 s on
# 2026-09-03 and PHP died mid-loop, unlogged).
JOB_POLL_SECONDS = 2.0
JOB_MAX_SECONDS = 3600
JOB_STATUS_FAILURES = 5


class BrokerJobLost(categories_service.BrokerError):
    """The broker accepted a job but stopped reporting on it. WordPress may
    still be working (or its worker died); the message tells the operator
    to retry, which converges from whatever landed."""


def _touch_run_heartbeat(run_id: int) -> None:
    with database.cursor(write=True, actor="worker") as cursor:
        cursor.execute(
            "UPDATE catmgr.run SET worker_heartbeat_at = now() WHERE run_id = %s",
            (run_id,),
        )


def broker_job(env: str, path: str, payload: Dict[str, Any], *,
               run_id: Optional[int] = None) -> Any:
    """POST one phase call; when the broker detaches it as a durable job
    (202 + job key) poll /job until it lands. Returns the phase body the
    synchronous broker used to return, so callers stay unchanged."""

    response = broker_call(env, path, payload)
    job = response.get("job") if isinstance(response, dict) else None
    if not isinstance(job, dict) or job.get("status") != "running" or not job.get("key"):
        return response
    key = job["key"]
    label = f"{path.strip('/')} for blog {payload.get('blog_id')}"
    started = time.monotonic()
    status_failures = 0
    while True:
        time.sleep(JOB_POLL_SECONDS)
        if run_id is not None:
            _touch_run_heartbeat(run_id)
        try:
            status = broker_call(env, "/job", {"key": key})
        except categories_service.BrokerError as exc:
            status_failures += 1
            if status_failures >= JOB_STATUS_FAILURES:
                raise BrokerJobLost(
                    f"could not read the status of {label} ({exc}). WordPress may"
                    " still be working on it: wait a minute, then Retry - the retry"
                    " resumes from whatever landed.", 504,
                ) from exc
            continue
        status_failures = 0
        job = (status.get("job") if isinstance(status, dict) else None) or {}
        state = job.get("status")
        if state == "done":
            result = status.get("result")
            return result if isinstance(result, dict) else {"ok": True}
        if state in ("failed", "refused"):
            # A definite outcome from the site (its own error text), not a
            # transport failure: status 500 keeps it out of wp_side_unknown.
            raise categories_service.BrokerError(
                f"WordPress reported {label} as {state}: {status.get('error') or 'no detail'}",
                500,
            )
        elapsed = time.monotonic() - started
        if job.get("lock_free"):
            raise BrokerJobLost(
                f"the WordPress worker running {label} died mid-phase"
                f" (progress: {job.get('progress')}). Retry resumes from whatever"
                " landed; the fences make that safe.", 504,
            )
        if job.get("stale") or elapsed > JOB_MAX_SECONDS:
            reason = (f"no heartbeat for {job.get('heartbeat_age')}s" if job.get("stale")
                      else f"still running after {int(elapsed)}s")
            raise BrokerJobLost(
                f"WordPress stopped reporting progress on {label} ({reason}). The"
                " site may still be finishing it: wait a minute, then Retry - the"
                " retry resumes from whatever landed.", 504,
            )


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

    if progress.get("snapshot_refreshed"):
        # Every phase landed and the post-apply refresh ran; only the status
        # flip was lost (worker died between the refresh and _finish_job).
        # Nothing is left to do - and re-running would trip the version fence
        # on the job's own refresh.
        result["finalize"] = progress.get("finalize_result") or {"ok": True}
        result["replayed"] = True
        with database.cursor(write=True, actor=actor) as cursor:
            record_audit(cursor, actor=actor, action="apply_succeeded",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "blog_id": blog_id,
                                 "request_id": job["request_id"],
                                 "replayed": True})
        _finish_job(job_id, run_id, "done", result)
        return

    with database.cursor(write=True, actor=actor) as cursor:
        # Staleness fence: the plan must match the CURRENT snapshot version -
        # until the first mutation. Once apply-terms has landed the plan is in
        # flight: a resumed job continues (the broker's expected-slug and SKU
        # fences protect every remaining phase) rather than stranding a
        # half-applied blog behind a "re-plan required".
        cursor.execute(
            "SELECT version FROM catmgr.snapshot WHERE env = %s AND blog_id = %s",
            (env, blog_id),
        )
        row = cursor.fetchone()
        current_version = row["version"] if row else None
        if current_version != payload["snapshot_version"]:
            if not progress.get("terms_done"):
                raise DraftConflict(
                    f"snapshot for blog {blog_id} is v{current_version}, plan was"
                    f" built on v{payload['snapshot_version']}; re-plan required"
                )
            result["resumed_past_version_fence"] = {
                "plan": payload["snapshot_version"], "snapshot": current_version,
            }
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
        result["terms"] = broker_job(env, "/apply-terms", {
            "blog_id": blog_id,
            "run_id": run_id,
            "request_id": job["request_id"],
            "updates": payload["terms"]["update"],
            "creates": payload["terms"]["create"],
            # Terms finalize will delete: the broker parks them on temp slugs
            # in the same pass so a merge target can take their slug.
            "doomed": [{"term_id": d["term_id"], "expected_slug": d["expected_slug"]}
                       for d in payload["terms"]["delete"]],
        }, run_id=run_id)
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
        outcome = broker_job(env, "/apply-memberships", {
            "blog_id": blog_id,
            "run_id": run_id,
            "request_id": job["request_id"],
            "page": offset,
            "rows": page,
        }, run_id=run_id)
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
        result["finalize"] = broker_job(env, "/finalize", {
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
        }, run_id=run_id)
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
            if isinstance(exc, categories_service.BrokerError) \
                    and getattr(exc, "status", 0) in (502, 503, 504):
                # The call died between us and WordPress: the site may have
                # applied the step anyway. A retry converges either way.
                failure["wp_side_unknown"] = True
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


def _restore_pages(products: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Membership rows grouped into pages that never split one product.

    The broker replaces a product's whole category set per page
    (wp_set_object_terms, append=false), and the export orders rows by TERM,
    so a naive slice would hand the same product to several pages and each
    later page would wipe what the earlier one set (blog 9 lost 259
    memberships to this on 2026-09-03). Rows are sorted by product first and a
    page only closes at a product boundary."""
    rows = sorted(products, key=lambda r: (int(r.get("product_id") or 0), int(r.get("term_id") or 0)))
    pages: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for row in rows:
        if (current and len(current) >= RESTORE_PAGE_SIZE
                and row.get("product_id") != current[-1].get("product_id")):
            pages.append(current)
            current = []
        current.append(row)
    if current:
        pages.append(current)
    return pages


def _restore_worker(env: str, run_id: int, job_id: int, blog_id: int,
                    snapshot: Dict[str, Any], actor: str) -> None:
    """Paged, resumable restore: terms pass -> membership pages -> finalize.
    Each broker call is bounded so blog 1 never meets the WP time limit."""

    terms = snapshot.get("terms") or []
    products = snapshot.get("products") or []
    blog_path = snapshot.get("blog_path") or ""
    restore = {"status": "running", "phase": "terms", "offset": 0,
               "total": len(products), "terms": len(terms), "error": None}
    # One request id per restore attempt: each broker phase/page is keyed on
    # it, so a replayed page returns its stored result instead of re-running.
    request_id = f"restore-{job_id}-{secrets.token_hex(6)}"
    restore["request_id"] = request_id
    _save_restore_progress(job_id, restore)
    try:
        outcome = broker_job(env, "/restore", {
            "blog_id": blog_id, "run_id": run_id, "phase": "terms",
            "request_id": request_id,
            "snapshot": {"terms": terms, "blog_path": blog_path},
        }, run_id=run_id)
        if not isinstance(outcome, dict) or outcome.get("ok") is False:
            raise DraftConflict(f"restore terms pass refused: {outcome}")
        restore["terms_result"] = {k: outcome.get(k) for k in ("terms", "created", "updated")}
        restore["phase"] = "memberships"
        _save_restore_progress(job_id, restore)
        offset = 0
        restored = 0
        for page in _restore_pages(products):
            outcome = broker_job(env, "/restore", {
                "blog_id": blog_id, "run_id": run_id, "phase": "memberships",
                "request_id": request_id, "page": offset,
                "snapshot": {"terms": terms, "products": page, "blog_path": blog_path},
                "products_offset": offset,
            }, run_id=run_id)
            if not isinstance(outcome, dict) or outcome.get("ok") is False:
                raise DraftConflict(f"restore membership page refused at {offset}: {outcome}")
            restored += int(outcome.get("products_restored") or 0)
            offset += len(page)
            restore["offset"] = offset
            restore["products_restored"] = restored
            _save_restore_progress(job_id, restore)
        restore["phase"] = "finalize"
        _save_restore_progress(job_id, restore)
        outcome = broker_job(env, "/restore", {
            "blog_id": blog_id, "run_id": run_id, "phase": "finalize",
            "request_id": request_id,
            "snapshot": {"terms": terms, "blog_path": blog_path},
        }, run_id=run_id)
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


def drift_audit(env: str, *, actor: str,
                blog_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Re-import every snapshotted blog live (or just `blog_ids` for a phased
    rollout), then report what a plan would still change. All-zero stats
    everywhere = converged. Refused while a run is active: a re-import bumps
    snapshot versions and would fail every remaining job's staleness fence.
    Recorded in the audit log like every other apply-tier action."""

    with database.cursor() as cursor:
        _assert_env_exclusive(cursor, env, None)
        cursor.execute(
            "SELECT blog_id FROM catmgr.snapshot WHERE env = %s ORDER BY blog_id",
            (env,),
        )
        known = [row["blog_id"] for row in cursor.fetchall()]
    scope: Optional[List[int]] = None
    if blog_ids:
        scope = sorted({int(b) for b in blog_ids})
        unknown = [b for b in scope if b not in known]
        if unknown:
            raise DraftError(f"no snapshot for {env} blog(s) {unknown}")
    targets = scope if scope is not None else known
    refreshed = []
    for blog_id in targets:
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
        outcome = categories_planner.preview(cursor, env, scope)
    pending = [b for b in outcome["blogs"]
               if any(b["stats"].get(k) for k in
                      ("changed_updates", "creates", "deletes",
                       "membership_changes"))]
    result = {
        "env": env,
        "scope": scope,
        "refreshed_blogs": len(refreshed),
        "converged": outcome["ok"] and not pending,
        "blockers": outcome["blockers"],
        "pending": pending,
    }
    with database.cursor(write=True, actor=actor) as cursor:
        record_audit(cursor, actor=actor, action="drift_audit", entity="env",
                     entity_key=env,
                     detail={"scope": scope, "refreshed": len(refreshed),
                             "converged": result["converged"],
                             "pending_blogs": [b["blog_id"] for b in pending][:200],
                             "blockers": [b["kind"] for b in outcome["blockers"]]})
    return result
