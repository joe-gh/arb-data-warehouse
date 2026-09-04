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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from psycopg2.extras import Json

import categories_planner
import categories_service
from categories_draft import DraftConflict, DraftError, normalize_wp_name
from categories_service import record_audit
from db import database


MEMBERSHIP_PAGE_SIZE = 400
RESTORE_PAGE_SIZE = 400
ACTIVE_STATUSES = ("queued", "running", "paused")
FINISHED_STATUSES = ("completed", "completed_with_skips", "completed_unverified",
                     "failed", "cancelled")
# A job still marked running whose run has not heartbeat for this long has
# lost its worker (app restart, crash): it is reclaimed and its progress
# cursors resume it.
STALE_JOB_MINUTES = 15
RESTORE_STALE_MINUTES = 15
# The WordPress broker protocol this engine speaks (expected_blog_path,
# expected_term_ids, parked_from, keyset export, verified redirects, ...).
BROKER_MIN_VERSION = 2


def broker_call(env: str, path: str, payload: Dict[str, Any]) -> Any:
    """POST one broker call. Module-level so tests can monkeypatch it."""

    return categories_service._broker(env, path, method="POST", payload=payload)


def fetch_wp_status(env: str) -> Dict[str, Any]:
    """GET /status. Module-level so tests can monkeypatch readiness."""

    return categories_service.fetch_wp_status(env)


class WorkerSuperseded(RuntimeError):
    """This worker's claim on the job was taken over (the job was reclaimed
    by a newer worker after a restart / stale heartbeat); it must stop
    writing state."""


# The broker runs every mutating phase as a durable job on the WordPress side:
# it fences, answers 202 with a job key, keeps working with a heartbeat, and
# we poll /job until it lands. The proxy's read timeout therefore never
# decides how far an apply gets (nginx cut a 131-term apply at 300 s on
# 2026-09-03 and PHP died mid-loop, unlogged).
JOB_POLL_SECONDS = 2.0
JOB_MAX_SECONDS = 3600
JOB_STATUS_FAILURES = 5
# After a transport failure on the phase POST the request may still have
# reached WordPress: probe the deterministic key this many times before
# concluding it never started.
JOB_PROBE_ATTEMPTS = 3


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


def job_key(path: str, payload: Dict[str, Any]) -> Optional[str]:
    """The broker's durable-job key for a phase call: request_id:phase[:page].
    Deterministic per (job attempt, phase, page), so a reclaimed or retried
    worker can find the WordPress row of a call it never saw finish."""
    request_id = str(payload.get("request_id") or "")
    if not request_id:
        return None
    key = f"{request_id[:64]}:{path.strip('/')}"
    if payload.get("page") is not None and payload.get("page") != "":
        key += f":{int(payload['page'])}"
    return key


def _probe_job(env: str, key: str) -> Optional[Dict[str, Any]]:
    """The broker's view of a job key, or None when it has no such row (or
    cannot be asked right now)."""
    try:
        status = broker_call(env, "/job", {"key": key})
    except categories_service.BrokerError:
        return None
    job = (status.get("job") if isinstance(status, dict) else None) or {}
    if not job.get("status"):
        return None
    return status


def _poll_job(env: str, key: str, label: str, run_id: Optional[int],
              started: float) -> Any:
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
        outcome = _settled_job(status, label)
        if outcome is not None:
            return outcome
        job = (status.get("job") if isinstance(status, dict) else None) or {}
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


def _settled_job(status: Any, label: str) -> Optional[Any]:
    """The phase body of a finished job, or None while it is still running.
    A job the broker marked failed/refused WITH a stored phase body (a fence
    report: refused rows, drifted deletes, failed redirects) returns that
    body so the caller reads the report; one without a body raises."""
    job = (status.get("job") if isinstance(status, dict) else None) or {}
    state = job.get("status")
    if state == "done":
        result = status.get("result")
        return result if isinstance(result, dict) else {"ok": True}
    if state in ("failed", "refused"):
        result = status.get("result")
        if isinstance(result, dict) and result.get("ok") is False:
            return result
        raise categories_service.BrokerError(
            f"WordPress reported {label} as {state}: {status.get('error') or 'no detail'}",
            500,
        )
    return None


def broker_job(env: str, path: str, payload: Dict[str, Any], *,
               run_id: Optional[int] = None,
               prior_keys: Optional[List[str]] = None) -> Any:
    """POST one phase call; when the broker detaches it as a durable job
    (202 + job key) poll /job until it lands. Returns the phase body the
    synchronous broker used to return, so callers stay unchanged.

    The key is deterministic, so before posting we look for a row WordPress
    may already hold for it (a reclaimed job whose worker is still running,
    or one that finished while we were down) and adopt it instead of
    starting a second attempt against the blog lock. The same probe runs
    when the POST itself fails at the transport layer."""

    key = job_key(path, payload)
    label = f"{path.strip('/')} for blog {payload.get('blog_id')}"
    started = time.monotonic()
    for candidate in [key] + [k for k in (prior_keys or []) if k]:
        if not candidate:
            continue
        probe = _probe_job(env, candidate)
        if probe is None:
            continue
        job = probe.get("job") or {}
        settled = _settled_job(probe, label) if job.get("status") != "running" else None
        if settled is not None:
            return settled
        if job.get("status") == "running" and not job.get("lock_free") and not job.get("stale"):
            # A live WordPress worker still owns this phase: wait for it.
            return _poll_job(env, candidate, label, run_id, started)
    try:
        response = broker_call(env, path, payload)
    except categories_service.BrokerError as exc:
        if key and getattr(exc, "status", 0) in (502, 503, 504):
            for _ in range(JOB_PROBE_ATTEMPTS):
                time.sleep(JOB_POLL_SECONDS)
                probe = _probe_job(env, key)
                if probe is None:
                    continue
                settled = _settled_job(probe, label)
                if settled is not None:
                    return settled
                return _poll_job(env, key, label, run_id, started)
        raise
    job = response.get("job") if isinstance(response, dict) else None
    if not isinstance(job, dict) or job.get("status") != "running" or not job.get("key"):
        return response
    return _poll_job(env, job["key"], label, run_id, started)


# ---------------------------------------------------------------- readiness


def readiness(env: str, blog_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Everything an apply relies on, checked BEFORE a run exists: broker
    protocol version, durable jobs, the edit freeze (prod), the derived-data
    services (Elasticsearch queue, UNSPSC) and, when blog 1 is in scope, a
    working Redirection group. Failures are exact and operator-facing."""

    try:
        status = fetch_wp_status(env)
    except categories_service.BrokerError as exc:
        return {"ok": False, "failures": [f"WordPress cannot be reached: {exc}"],
                "warnings": [], "status": None}
    failures: List[str] = []
    warnings: List[str] = []
    version = int(status.get("broker_version") or 0)
    if version < BROKER_MIN_VERSION:
        failures.append(
            f"the WordPress side of the category editor is out of date (version"
            f" {version}, needs {BROKER_MIN_VERSION} or newer) - ask a developer"
            " to deploy the arb-admin plugin update"
        )
    if not status.get("durable_jobs"):
        failures.append("WordPress cannot run category jobs in the background"
                        " (PHP-FPM fastcgi_finish_request is missing) - ask a"
                        " developer; applying would time out")
    if not status.get("job_table"):
        failures.append("WordPress could not create its category job table"
                        " - ask a developer")
    if status.get("freeze") is not True:
        (failures if env == "prod" else warnings).append(
            "category editing in WordPress is not locked - lock it on the Runs"
            " tab so nobody changes categories while the run is in progress"
        )
    if not status.get("avne_available"):
        (failures if env == "prod" else warnings).append(
            "the site search index cannot be updated right now (the"
            " Elasticsearch queue is unavailable) - search results would be"
            " out of date after the run"
        )
    if not status.get("unspsc_available"):
        (failures if env == "prod" else warnings).append(
            "the UNSPSC product-code service is unavailable - product codes"
            " would not be recalculated after the run"
        )
    if blog_ids is None or 1 in blog_ids:
        if not status.get("redirection_active"):
            failures.append("the Redirection plugin is not active on the public"
                            " store - old category links would break without it")
        elif not int(status.get("redirect_group_id") or 0):
            failures.append("the Redirection plugin has no enabled group to"
                            " hold the category redirects - ask a developer")
    return {"ok": not failures, "failures": failures, "warnings": warnings,
            "status": status}


def _require_ready(env: str, blog_ids: Optional[List[int]]) -> Dict[str, Any]:
    ready = readiness(env, blog_ids)
    if not ready["ok"]:
        raise DraftConflict(
            "The websites are not ready for an apply: " + "; ".join(ready["failures"])
        )
    return ready


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
            f"another run (#{other['run_id']}) is {other['status']} on {env}"
            " - finish, pause or cancel it first"
        )
    restoring = _running_restores(cursor, env)
    if restoring:
        first = restoring[0]
        raise DraftConflict(
            f"store {first['blog_id']} is still being restored (run"
            f" #{first['run_id']}) - wait for the restore to finish"
        )


def _running_restores(cursor, env: str,
                      exclude_job_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Restores in progress for an environment whose worker is still alive
    (fresh heartbeat). A restore whose heartbeat went stale is orphaned and
    no longer blocks anything."""
    cursor.execute(
        """
        SELECT j.job_id, j.run_id, j.blog_id, j.progress -> 'restore' AS restore
          FROM catmgr.run_job j JOIN catmgr.run r ON r.run_id = j.run_id
         WHERE r.env = %s AND j.progress -> 'restore' ->> 'status' = 'running'
           AND j.job_id IS DISTINCT FROM %s
         ORDER BY j.job_id
        """,
        (env, exclude_job_id),
    )
    alive = []
    for row in cursor.fetchall():
        restore = row["restore"] or {}
        if not _restore_stale(restore):
            alive.append({"job_id": row["job_id"], "run_id": row["run_id"],
                          "blog_id": row["blog_id"]})
    return alive


def _restore_stale(restore: Dict[str, Any]) -> bool:
    beat = restore.get("heartbeat_at")
    if not beat:
        return True
    try:
        seen = datetime.fromisoformat(str(beat))
    except ValueError:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen < datetime.now(timezone.utc) - timedelta(minutes=RESTORE_STALE_MINUTES)


def blogs_locked_by_runs(cursor, env: str) -> Dict[int, int]:
    """blog_id -> run_id for every blog a queued/running/paused run still has
    to converge (a snapshot re-import would bump the version its plan was
    built on and stop the job at the staleness fence)."""
    cursor.execute(
        """
        SELECT j.blog_id, j.run_id FROM catmgr.run_job j
          JOIN catmgr.run r ON r.run_id = j.run_id
         WHERE r.env = %s AND r.status IN %s AND j.status IN ('pending', 'running')
         ORDER BY j.run_id, j.blog_id
        """,
        (env, ACTIVE_STATUSES),
    )
    locked: Dict[int, int] = {}
    for row in cursor.fetchall():
        locked.setdefault(int(row["blog_id"]), int(row["run_id"]))
    return locked


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
        raise DraftConflict(
            "the plan check still reports problems to clear first: "
            + str(kinds).replace("_", " ")
        )
    if not preview["blogs"]:
        raise DraftError("no blogs to apply")
    ready = _require_ready(env, [b["blog_id"] for b in preview["blogs"]])

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
                         "totals": preview["totals"],
                         "readiness_warnings": ready["warnings"]})
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
        run["heartbeat_age"] = _heartbeat_age(run)
    return runs


def _heartbeat_age(run: Dict[str, Any]) -> Optional[int]:
    beat = run.get("worker_heartbeat_at")
    if beat is None:
        return None
    return int((datetime.now(timezone.utc) - beat).total_seconds())


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
        restore = (job.get("progress") or {}).get("restore") or None
        if restore and restore.get("status") == "running":
            restore = {**restore, "stale": _restore_stale(restore)}
        job["restore"] = restore
    run["worker_stale"] = run["status"] == "running" and _stale(run)
    run["heartbeat_age"] = _heartbeat_age(run)
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
        raise DraftConflict(f"the run is already {run['status']} and cannot be cancelled")
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
            raise DraftConflict("the run is still working - pause it first, or wait"
                                " for the current store to finish")
        _reclaim_stale_jobs(cursor, run_id, actor=actor)
    elif run["status"] not in ("paused", "failed", "queued"):
        raise DraftConflict(f"the run is {run['status']} and cannot be resumed")
    _assert_env_exclusive(cursor, run["env"], run_id)
    _require_ready(run["env"], list(run["target_blogs"] or []))
    cursor.execute(
        "UPDATE catmgr.run SET cancel_requested = false, status = 'queued',"
        " finished_at = NULL WHERE run_id = %s",
        (run_id,),
    )
    record_audit(cursor, actor=actor, action="run_resumed", entity="run",
                 entity_key=str(run_id), detail={})
    return get_run(cursor, run_id)


def start(cursor, run_id: int, *, actor: str) -> Dict[str, Any]:
    """The only way a created-but-unstarted run begins: an atomic queued ->
    queued(started) transition under the environment lock, re-checking
    exclusivity and readiness. Any other status is refused (resume / retry
    are the paths for paused and failed runs)."""
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtext('catmgr_run_' || %s))",
        ((get_run(cursor, run_id))["env"],),
    )
    run = get_run(cursor, run_id)
    if run["status"] != "queued":
        raise DraftConflict(f"the run is {run['status']} - only a queued run can be started")
    _assert_env_exclusive(cursor, run["env"], run_id)
    _require_ready(run["env"], list(run["target_blogs"] or []))
    record_audit(cursor, actor=actor, action="run_started", entity="run",
                 entity_key=str(run_id), detail={})
    return run


def cancel(cursor, run_id: int, *, actor: str) -> Dict[str, Any]:
    run = get_run(cursor, run_id)
    if run["status"] in ("completed", "cancelled"):
        raise DraftConflict(f"the run is already {run['status']}")
    if run["status"] == "running":
        raise DraftConflict("pause the run first - the store it is working on has to finish")
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
        raise DraftConflict("the run is still working - wait for the current store to finish")
    _assert_env_exclusive(cursor, run["env"], run_id)
    _require_ready(run["env"], list(run["target_blogs"] or []))
    # A retry is a NEW attempt: its phase keys differ from the failed
    # attempt's, so a finalize the broker refused (drifted deletes, failed
    # redirects) actually runs again instead of replaying its stored report.
    cursor.execute(
        "UPDATE catmgr.run_job SET status = 'pending', attempt = attempt + 1"
        " WHERE run_id = %s AND job_id = %s AND status IN ('failed', 'skipped')"
        " RETURNING blog_id",
        (run_id, job_id),
    )
    if cursor.fetchone() is None:
        raise DraftConflict("only a store that failed or was skipped can be retried")
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
        raise DraftConflict("only a store that failed can be skipped")
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
        _finish_run(cursor, run_id)
    return get_run(cursor, run_id)


def _finish_run(cursor, run_id: int) -> str:
    """The run's terminal status from its jobs: failed beats everything; a
    skipped blog makes the migration PARTIAL; a job whose post-apply
    verification could not run makes it UNVERIFIED. Plain completed means
    every blog was applied and verified against a fresh export."""
    cursor.execute(
        "SELECT status, result FROM catmgr.run_job WHERE run_id = %s", (run_id,),
    )
    jobs = [dict(r) for r in cursor.fetchall()]
    if any(j["status"] == "failed" for j in jobs):
        status = "failed"
    elif any(j["status"] == "skipped" for j in jobs):
        status = "completed_with_skips"
    elif any(j["status"] == "done" and (j["result"] or {}).get("verified") is False
             for j in jobs):
        status = "completed_unverified"
    else:
        status = "completed"
    cursor.execute(
        "UPDATE catmgr.run SET status = %s, finished_at = now() WHERE run_id = %s",
        (status, run_id),
    )
    return status


# ---------------------------------------------------------------- engine


def _claim_next_job(run_id: int) -> Optional[Dict[str, Any]]:
    with database.cursor(write=True, actor="worker") as cursor:
        cursor.execute(
            "SELECT cancel_requested, status, worker_heartbeat_at FROM catmgr.run"
            " WHERE run_id = %s FOR UPDATE",
            (run_id,),
        )
        run = cursor.fetchone()
        if run is None:
            return None
        # Only a queued or running run may hand out work: a failed, paused,
        # cancelled or finished run never mutates a store again until an
        # operator transitions it explicitly (resume / retry).
        if run["status"] not in ("queued", "running"):
            return None
        # A job left 'running' by a dead worker (app restart mid-job) blocks
        # the run forever unless reclaimed; the heartbeat says whether the
        # worker is really gone.
        if _stale({"worker_heartbeat_at": run["worker_heartbeat_at"]}):
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
            _finish_run(cursor, run_id)
            return None
        # The attempt number is part of every broker job key. It only moves
        # on an explicit retry; a reclaim after a crash keeps it, so the
        # resumed worker finds (and adopts) the WordPress rows of the attempt
        # it is continuing instead of colliding with them.
        attempt = int(job["attempt"] or 0) or 1
        request_id = f"j{job['job_id']}a{attempt}"
        worker_token = secrets.token_hex(8)
        cursor.execute(
            """
            UPDATE catmgr.run_job
               SET status = 'running', attempt = %s,
                   started_at = COALESCE(started_at, now()),
                   request_id = %s, worker_token = %s
             WHERE job_id = %s
            """,
            (attempt, request_id, worker_token, job["job_id"]),
        )
        cursor.execute(
            "UPDATE catmgr.run SET status = 'running',"
            " started_at = COALESCE(started_at, now()),"
            " worker_heartbeat_at = now() WHERE run_id = %s",
            (run_id,),
        )
        return {**dict(job), "attempt": attempt, "request_id": request_id,
                "worker_token": worker_token}


def _guarded_update(cursor, job_id: int, worker_token: str, sql: str, params) -> None:
    """Run a job-row update only while this worker still holds the claim."""
    cursor.execute(sql, params)
    if cursor.rowcount == 0:
        raise WorkerSuperseded(
            f"job {job_id} was reclaimed by another worker; this worker stops"
        )


def _finish_job(job_id: int, run_id: int, status: str,
                result: Dict[str, Any], worker_token: str = "") -> None:
    with database.cursor(write=True, actor="worker") as cursor:
        if worker_token:
            _guarded_update(
                cursor, job_id, worker_token,
                "UPDATE catmgr.run_job SET status = %s, result = %s,"
                " finished_at = now() WHERE job_id = %s AND worker_token = %s",
                (status, Json(result), job_id, worker_token),
            )
        else:
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


def _live_diff(env: str, blog_id: int, live: Dict[str, Any]) -> Dict[str, Any]:
    """What changed between the warehouse snapshot and the live export
    (counts + samples), for the operator-facing fence report."""
    clean_terms, clean_products, clean_uncategorized = categories_service.normalize_export(
        live.get("terms") or [], live.get("products") or [], live.get("uncategorized") or [],
    )
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT term_id, slug, name, parent_term_id, sort_order FROM catmgr.wp_term"
            " WHERE env = %s AND blog_id = %s", (env, blog_id),
        )
        snap_terms = {r["term_id"]: dict(r) for r in cursor.fetchall()}
        cursor.execute(
            "SELECT term_id, product_id, sku FROM catmgr.wp_term_product"
            " WHERE env = %s AND blog_id = %s", (env, blog_id),
        )
        snap_members = {(r["term_id"], r["product_id"], r["sku"]) for r in cursor.fetchall()}
        cursor.execute(
            "SELECT product_id, sku FROM catmgr.wp_uncategorized_product"
            " WHERE env = %s AND blog_id = %s", (env, blog_id),
        )
        snap_uncat = {(r["product_id"], r["sku"]) for r in cursor.fetchall()}
    live_terms = {t["term_id"]: t for t in clean_terms}
    changed_terms = []
    for term_id in sorted(set(snap_terms) | set(live_terms)):
        before, after = snap_terms.get(term_id), live_terms.get(term_id)
        if before is None or after is None:
            changed_terms.append({"term_id": term_id, "live": after["slug"] if after else None,
                                  "snapshot": before["slug"] if before else None})
        elif (before["slug"], before["name"], before["parent_term_id"], before["sort_order"]) != (
                after["slug"], after["name"], after["parent"], after["sort_order"]):
            changed_terms.append({"term_id": term_id, "live": after["slug"], "snapshot": before["slug"]})
    live_members = set(clean_products)
    added = sorted(live_members - snap_members)
    removed = sorted(snap_members - live_members)
    live_uncat = set(clean_uncategorized)
    return {
        "terms_changed": len(changed_terms), "terms_sample": changed_terms[:10],
        "memberships_added": len(added), "memberships_removed": len(removed),
        "memberships_sample": [{"term_id": t, "product_id": p, "sku": s, "change": "added"} for t, p, s in added[:10]]
                              + [{"term_id": t, "product_id": p, "sku": s, "change": "removed"} for t, p, s in removed[:10]],
        "uncategorized_changed": len(live_uncat ^ snap_uncat),
    }


def _verify_plan_applied(payload: Dict[str, Any], live: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compare a fresh export with the plan's desired end state. Empty = the
    blog converged exactly; otherwise a bounded list of mismatches."""
    clean_terms, clean_products, _ = categories_service.normalize_export(
        live.get("terms") or [], live.get("products") or [], live.get("uncategorized") or [],
    )
    by_id = {t["term_id"]: t for t in clean_terms}
    by_slug = {t["slug"]: t for t in clean_terms}
    problems: List[Dict[str, Any]] = []

    def parent_slug(term: Dict[str, Any]) -> str:
        parent = by_id.get(int(term.get("parent") or 0))
        return parent["slug"] if parent else ""

    def check_term(term: Optional[Dict[str, Any]], wanted: Dict[str, Any], what: str) -> None:
        if term is None:
            problems.append({"kind": what, "slug": wanted.get("slug"), "problem": "missing"})
            return
        if term["slug"] != wanted["slug"]:
            problems.append({"kind": what, "term_id": term["term_id"], "problem": "slug",
                             "live": term["slug"], "wanted": wanted["slug"]})
        if normalize_wp_name(term["name"]) != normalize_wp_name(wanted["name"]):
            problems.append({"kind": what, "term_id": term["term_id"], "problem": "name",
                             "live": term["name"], "wanted": wanted["name"]})
        if parent_slug(term) != (wanted.get("parent_slug") or ""):
            problems.append({"kind": what, "term_id": term["term_id"], "problem": "parent",
                             "live": parent_slug(term), "wanted": wanted.get("parent_slug") or ""})
        if int(term.get("sort_order") or 0) != int(wanted.get("sort_order") or 0):
            problems.append({"kind": what, "term_id": term["term_id"], "problem": "sort_order",
                             "live": term.get("sort_order"), "wanted": wanted.get("sort_order")})
        if term.get("parked_from"):
            problems.append({"kind": what, "term_id": term["term_id"], "problem": "still parked"})

    for update in payload["terms"]["update"]:
        check_term(by_id.get(int(update["term_id"])), update["set"], "update")
    for create in payload["terms"]["create"]:
        check_term(by_slug.get(create["slug"]), create, "create")
    for delete in payload["terms"]["delete"]:
        if int(delete["term_id"]) in by_id:
            problems.append({"kind": "delete", "term_id": delete["term_id"],
                             "problem": "still present", "live": by_id[int(delete["term_id"])]["slug"]})
    product_slugs: Dict[int, Set[str]] = {}
    for term_id, product_id, _sku in clean_products:
        term = by_id.get(term_id)
        if term:
            product_slugs.setdefault(product_id, set()).add(term["slug"])
    for row in payload["memberships"]:
        live_set = product_slugs.get(int(row["product_id"]), set())
        if live_set != set(row["final_slugs"]):
            problems.append({"kind": "membership", "product_id": row["product_id"],
                             "live": sorted(live_set), "wanted": row["final_slugs"]})
    return problems


def _execute_job(env: str, run_id: int, job: Dict[str, Any], actor: str) -> None:
    job_id = job["job_id"]
    payload = job["payload"]
    blog_id = payload["blog_id"]
    progress = dict(job["progress"] or {})
    token = job["worker_token"]
    result: Dict[str, Any] = {"request_id": job["request_id"], "attempt": job["attempt"]}
    unspsc = payload.get("unspsc") or {
        "renames": ({u["expected_slug"]: u["set"]["slug"] for u in payload["terms"]["update"]
                     if u.get("changed", {}).get("slug")} if blog_id == 1 else {}),
        "merges": {},
    }
    common = {"blog_id": blog_id, "run_id": run_id, "request_id": job["request_id"],
              "expected_blog_path": payload.get("blog_path") or ""}
    prior_keys = lambda phase, page=None: [  # noqa: E731 - tiny local helper
        f"j{job_id}a{n}:{phase}" + (f":{page}" if page is not None else "")
        for n in range(int(job["attempt"]) - 1, 0, -1)
    ][:2]

    if progress.get("snapshot_refreshed"):
        # Every phase landed and the post-apply refresh ran; only the status
        # flip was lost (worker died between the refresh and _finish_job).
        # Nothing is left to do - and re-running would trip the version fence
        # on the job's own refresh.
        result["finalize"] = progress.get("finalize_result") or {"ok": True}
        result["verified"] = progress.get("verified", True)
        result["replayed"] = True
        with database.cursor(write=True, actor=actor) as cursor:
            record_audit(cursor, actor=actor, action="apply_succeeded",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "blog_id": blog_id,
                                 "request_id": job["request_id"],
                                 "replayed": True})
        _finish_job(job_id, run_id, "done", result, token)
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
                    f"the copy of store {blog_id} was refreshed (copy #{current_version})"
                    f" after this plan was built (copy #{payload['snapshot_version']})"
                    " - re-plan required: press Check the plan again"
                )
            result["resumed_past_version_fence"] = {
                "plan": payload["snapshot_version"], "snapshot": current_version,
            }
        record_audit(cursor, actor=actor, action="apply_requested",
                     entity="job", entity_key=str(job_id),
                     detail={"run_id": run_id, "blog_id": blog_id,
                             "request_id": job["request_id"],
                             "attempt": job["attempt"],
                             "stats": payload["stats"]})

    # Pre-apply live capture: the emergency-restore source AND the live-state
    # fence. The export must still hash to the fingerprint the plan was built
    # on; anything WordPress changed since the import (a product gained a
    # category, a term appeared) refuses the apply before the first mutation
    # instead of being overwritten by whole-set membership writes.
    if not progress.get("terms_done"):
        live = categories_service.fetch_export(env, blog_id)
        fingerprint = categories_service.export_fingerprint(
            live.get("terms") or [], live.get("products") or [], live.get("uncategorized") or [],
        )
        expected = payload.get("snapshot_fingerprint") or ""
        if not expected:
            result["fingerprint_unchecked"] = True   # snapshot predates fingerprints
        elif fingerprint != expected:
            diff = _live_diff(env, blog_id, live)
            result["live_drift"] = diff
            _save_progress(job_id, {**progress, "live_drift": diff}, token)
            with database.cursor(write=True, actor=actor) as cursor:
                record_audit(cursor, actor=actor, action="apply_failed",
                             entity="job", entity_key=str(job_id),
                             detail={"run_id": run_id, "blog_id": blog_id,
                                     "request_id": job["request_id"],
                                     "error": "live state drifted from the snapshot",
                                     "diff": diff})
            raise DraftConflict(
                f"store {blog_id} changed since snapshot v{payload['snapshot_version']}"
                f" was imported ({diff['terms_changed']} term(s), {diff['memberships_added']}"
                f" membership(s) added, {diff['memberships_removed']} removed):"
                " re-import the snapshot and re-plan"
            )
        if not progress.get("snapshot_taken"):
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
                                   "uncategorized": live.get("uncategorized") or [],
                                   "site_options": live.get("site_options") or {},
                                   "blog_path": live.get("blog_path") or "",
                                   "fingerprint": fingerprint})),
                )
            progress["snapshot_taken"] = True
            _save_progress(job_id, progress, token)

    if not progress.get("terms_done"):
        result["terms"] = broker_job(env, "/apply-terms", {
            **common,
            "updates": payload["terms"]["update"],
            "creates": payload["terms"]["create"],
            # Terms finalize will delete: the broker parks them on temp slugs
            # in the same pass so a merge target can take their slug.
            "doomed": [{"term_id": d["term_id"], "expected_slug": d["expected_slug"]}
                       for d in payload["terms"]["delete"]],
            # Blog 1 re-keys the UNSPSC mapping HERE, before any product is
            # re-derived against a slug that no longer exists.
            "unspsc_renames": unspsc.get("renames") or {},
            "unspsc_merges": unspsc.get("merges") or {},
        }, run_id=run_id, prior_keys=prior_keys("apply-terms"))
        if not isinstance(result["terms"], dict) or result["terms"].get("ok") is False:
            raise DraftConflict(
                f"WordPress refused the category changes for store {blog_id}: {result['terms']}"
            )
        progress["terms_done"] = True
        _save_progress(job_id, progress, token)

    memberships = payload["memberships"]
    offset = int(progress.get("membership_offset") or 0)
    applied = int(progress.get("membership_applied") or 0)
    while offset < len(memberships):
        page = memberships[offset:offset + MEMBERSHIP_PAGE_SIZE]
        outcome = broker_job(env, "/apply-memberships", {
            **common,
            "page": offset,
            "rows": page,
        }, run_id=run_id, prior_keys=prior_keys("apply-memberships", offset))
        fence = _fence_report(outcome, ("skipped", "missing_slugs"))
        if fence:
            # Rows the broker refused (product gone, SKU changed, categories
            # changed since the snapshot, target slug missing) mean WordPress
            # drifted: stop here with the report - the cursor keeps the page
            # so a re-plan + retry can resume - rather than finishing a job
            # WordPress did not apply.
            result["memberships"] = {"applied": applied, "total": len(memberships),
                                     "offset": offset, "fence": fence}
            _save_progress(job_id, {**progress, "membership_fence": fence}, token)
            raise DraftConflict(
                f"WordPress refused some product moves for store {blog_id}: {fence}"
            )
        applied += int(outcome.get("applied") or 0)
        offset += len(page)
        progress["membership_offset"] = offset
        progress["membership_applied"] = applied
        _save_progress(job_id, progress, token)
    result["memberships"] = {"applied": applied, "total": len(memberships)}

    if progress.get("finalize_done"):
        # A retry after a CLEAN finalize must not repeat it (Redirection rows
        # would duplicate); the recorded outcome stands. A refused finalize
        # never sets finalize_done, so it runs again under the new attempt.
        result["finalize"] = progress.get("finalize_result") or {"ok": True, "replayed": True}
    else:
        result["finalize"] = broker_job(env, "/finalize", {
            **common,
            "deletes": payload["terms"]["delete"],
            "redirects": payload["redirects"],
            "unspsc_renames": unspsc.get("renames") or {},
            "unspsc_merges": unspsc.get("merges") or {},
            # ES reindex rides the existing */15 queue cron: a synchronous flush
            # here can outlast the HTTP timeout when the queue has a backlog
            # (observed on dev 2026-09-01).
            "run_es": False,
        }, run_id=run_id, prior_keys=prior_keys("finalize"))
    finalize_body = result["finalize"] if isinstance(result["finalize"], dict) else {}
    finalize_report = _fence_report(finalize_body, ("delete_report", "redirects_failed"))
    drifted_deletes = [
        row for row in finalize_body.get("delete_report") or []
        if isinstance(row, dict) and row.get("status") in ("slug_drift", "failed", "has_products")
    ]
    failed_redirects = [r for r in finalize_body.get("redirects_failed") or [] if isinstance(r, dict)]

    with database.cursor(write=True, actor=actor) as cursor:
        if blog_id == 1 and payload["redirects"]:
            created = {
                r.get("old_path"): r.get("new_path")
                for r in (finalize_body.get("redirects_created") or [])
                if isinstance(r, dict)
            }
            failed_detail = {r.get("old_path"): str(r.get("reason") or "") for r in failed_redirects}
            for redirect in payload["redirects"]:
                ok = created.get(redirect["old_path"]) == redirect["new_path"]
                cursor.execute(
                    """
                    UPDATE catmgr.redirect SET status = %s, detail = %s
                     WHERE run_id = %s AND blog_id = %s AND old_path = %s
                    """,
                    ("created" if ok else "failed",
                     "" if ok else failed_detail.get(redirect["old_path"], "not created")[:500],
                     run_id, blog_id, redirect["old_path"]),
                )
        if drifted_deletes or failed_redirects:
            result["finalize_fence"] = {"deletes": drifted_deletes[:50],
                                        "redirects": failed_redirects[:50]}
            record_audit(cursor, actor=actor, action="apply_failed",
                         entity="job", entity_key=str(job_id),
                         detail={"run_id": run_id, "blog_id": blog_id,
                                 "request_id": job["request_id"],
                                 "error": "finalize refused deletes or redirects",
                                 "delete_report": drifted_deletes[:50],
                                 "redirects_failed": failed_redirects[:50]})
    if drifted_deletes or failed_redirects:
        # NOT finalize_done: the next attempt runs finalize again (deletes and
        # redirects are idempotent) instead of replaying this report forever.
        _save_progress(job_id, {**progress, "finalize_attempt": {
            "attempt": job["attempt"], "deletes": drifted_deletes[:20],
            "redirects": failed_redirects[:20]}}, token)
        parts = []
        if drifted_deletes:
            parts.append(f"{len(drifted_deletes)} delete(s) (terms still carry products or moved): {drifted_deletes[:3]}")
        if failed_redirects:
            parts.append(f"{len(failed_redirects)} redirect(s) not created/verified: {failed_redirects[:3]}")
        raise DraftConflict(f"WordPress refused the final step for store {blog_id}: " + "; ".join(parts))
    if not progress.get("finalize_done"):
        progress["finalize_done"] = True
        progress["finalize_result"] = {
            k: v for k, v in finalize_body.items()
            if k in ("ok", "deleted", "delete_report", "redirects_created",
                     "redirects_failed", "unspsc_rewritten", "recounted_terms")
        }
        _save_progress(job_id, progress, token)
    del finalize_report

    # Post-apply verification: a fresh export must match the plan's desired
    # end state exactly. A mismatch fails the job (WordPress did not
    # converge; the retry re-runs the fenced phases). An export that cannot
    # be fetched leaves the job done-but-UNVERIFIED, which the run status
    # shows.
    if not progress.get("snapshot_refreshed"):
        try:
            live_after = categories_service.fetch_export(env, blog_id)
        except Exception as exc:  # noqa: BLE001 - the apply itself succeeded
            result["verified"] = False
            result["verification_error"] = str(exc)[:300]
            result["snapshot"] = {"error": str(exc)[:300]}
            progress["verified"] = False
            _save_progress(job_id, progress, token)
        else:
            problems = _verify_plan_applied(payload, live_after)
            if problems:
                result["verified"] = False
                result["verification"] = problems[:50]
                with database.cursor(write=True, actor=actor) as cursor:
                    record_audit(cursor, actor=actor, action="apply_failed",
                                 entity="job", entity_key=str(job_id),
                                 detail={"run_id": run_id, "blog_id": blog_id,
                                         "request_id": job["request_id"],
                                         "error": "post-apply verification failed",
                                         "problems": problems[:50]})
                _save_progress(job_id, {**progress, "verification": problems[:50]}, token)
                raise DraftConflict(
                    f"store {blog_id} did not converge: after the apply,"
                    f" {len(problems)} thing(s) still differ between the plan and"
                    f" the website, e.g. {problems[:3]}"
                )
            result["verified"] = True
            try:
                with database.cursor(write=True, actor=actor) as cursor:
                    result["snapshot"] = categories_service.import_export(
                        cursor, env=env, blog_id=blog_id, export=live_after, actor=actor,
                    )
                progress["snapshot_refreshed"] = True
                progress["verified"] = True
                _save_progress(job_id, progress, token)
            except Exception as exc:  # noqa: BLE001 - verified; only the refresh failed
                result["snapshot"] = {"error": str(exc)[:300]}
    with database.cursor(write=True, actor=actor) as cursor:
        record_audit(cursor, actor=actor, action="apply_succeeded",
                     entity="job", entity_key=str(job_id),
                     detail={"run_id": run_id, "blog_id": blog_id,
                             "request_id": job["request_id"],
                             "verified": result.get("verified"),
                             "result": {k: v for k, v in result.items()
                                        if k not in ("request_id", "terms")}})
    _finish_job(job_id, run_id, "done", result, token)


def _save_progress(job_id: int, progress: Dict[str, Any],
                   worker_token: str = "") -> None:
    with database.cursor(write=True, actor="worker") as cursor:
        if worker_token:
            _guarded_update(
                cursor, job_id, worker_token,
                "UPDATE catmgr.run_job SET progress = %s WHERE job_id = %s AND worker_token = %s",
                (Json(progress), job_id, worker_token),
            )
        else:
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
        return categories_service.import_export(
            cursor, env=env, blog_id=blog_id, export=export, actor=actor,
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
        except WorkerSuperseded:
            # A newer worker owns the job now; it will finish it.
            break
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
            saved_progress = (saved["progress"] or {}) if saved else {}
            for key in ("membership_fence", "verification", "finalize_attempt", "live_drift"):
                if saved_progress.get(key):
                    failure[key] = saved_progress[key]
            try:
                _finish_job(job["job_id"], run_id, "failed", failure, job["worker_token"])
            except WorkerSuperseded:
                break
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
        # Restore workers were threads of the dead process: every restore
        # still marked running is orphaned. Mark it so instead of leaving it
        # "running" forever with its retry hidden; every restore pass is
        # convergent, so the operator simply requests it again.
        cursor.execute(
            "SELECT job_id, run_id, blog_id, progress FROM catmgr.run_job"
            " WHERE progress -> 'restore' ->> 'status' = 'running'",
        )
        for row in cursor.fetchall():
            restore = dict((row["progress"] or {}).get("restore") or {})
            restore["status"] = "failed"
            restore["error"] = ("the app restarted while this restore was running;"
                                " request the restore again (every pass converges)")
            restore["orphaned"] = True
            cursor.execute(
                "UPDATE catmgr.run_job SET progress = progress || %s WHERE job_id = %s",
                (Json({"restore": restore}), row["job_id"]),
            )
            record_audit(cursor, actor=actor, action="restore_failed", entity="job",
                         entity_key=str(row["job_id"]),
                         detail={"run_id": row["run_id"], "blog_id": row["blog_id"],
                                 "error": restore["error"], "orphaned": True})
    for run_id in run_ids:
        start_run(run_id, actor=actor)
    return run_ids


# ---------------------------------------------------------------- restore


def _save_restore_progress(job_id: int, restore: Dict[str, Any]) -> None:
    restore["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
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


def _verify_restored(snapshot: Dict[str, Any], live: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The restored blog must match the pre-apply snapshot: same terms (by
    slug: name, parent slug, sort order, description), same memberships per
    product (by slug), same uncategorized products. Term ids may differ for
    terms the run deleted (they recreate), so identity is by slug."""
    snap_terms, snap_products, snap_uncat = categories_service.normalize_export(
        snapshot.get("terms") or [], snapshot.get("products") or [], snapshot.get("uncategorized") or [],
    )
    live_terms, live_products, live_uncat = categories_service.normalize_export(
        live.get("terms") or [], live.get("products") or [], live.get("uncategorized") or [],
    )

    def shape(terms):
        by_id = {t["term_id"]: t for t in terms}
        out = {}
        for t in terms:
            parent = by_id.get(int(t.get("parent") or 0))
            out[t["slug"]] = (normalize_wp_name(t["name"]), parent["slug"] if parent else "",
                              int(t.get("sort_order") or 0), t.get("description") or "")
        return by_id, out

    snap_by_id, snap_shape = shape(snap_terms)
    live_by_id, live_shape = shape(live_terms)
    problems: List[Dict[str, Any]] = []
    for slug in sorted(set(snap_shape) | set(live_shape)):
        if slug not in live_shape:
            problems.append({"kind": "term", "slug": slug, "problem": "missing after restore"})
        elif slug not in snap_shape:
            if slug != "uncategorized":
                problems.append({"kind": "term", "slug": slug, "problem": "extra term left behind"})
        elif snap_shape[slug] != live_shape[slug]:
            problems.append({"kind": "term", "slug": slug, "problem": "attributes differ",
                             "snapshot": snap_shape[slug], "live": live_shape[slug]})

    def memberships(products, by_id):
        out: Dict[int, Set[str]] = {}
        for term_id, product_id, _ in products:
            term = by_id.get(term_id)
            if term:
                out.setdefault(product_id, set()).add(term["slug"])
        return out

    snap_m = memberships(snap_products, snap_by_id)
    live_m = memberships(live_products, live_by_id)
    for product_id in sorted(set(snap_m) | set(live_m)):
        if snap_m.get(product_id, set()) != live_m.get(product_id, set()):
            problems.append({"kind": "membership", "product_id": product_id,
                             "snapshot": sorted(snap_m.get(product_id, set())),
                             "live": sorted(live_m.get(product_id, set()))})
    if {p for p, _ in snap_uncat} != {p for p, _ in live_uncat}:
        problems.append({"kind": "uncategorized",
                         "snapshot": len(snap_uncat), "live": len(live_uncat)})
    return problems


def _restore_worker(env: str, run_id: int, job_id: int, blog_id: int,
                    snapshot: Dict[str, Any], actor: str) -> None:
    """Paged, resumable restore: terms pass -> membership pages -> finalize
    -> verification against the snapshot. Each broker call is bounded so blog
    1 never meets the WP time limit, every broker result is checked for
    completeness, and the restore is only 'done' when a fresh export matches
    the snapshot."""

    terms = snapshot.get("terms") or []
    products = snapshot.get("products") or []
    blog_path = snapshot.get("blog_path") or ""
    site_options = snapshot.get("site_options") or {}
    restore = {"status": "running", "phase": "terms", "offset": 0,
               "total": len(products), "terms": len(terms), "error": None}
    # One request id per restore attempt: each broker phase/page is keyed on
    # it, so a replayed page returns its stored result instead of re-running.
    request_id = f"restore-{job_id}-{secrets.token_hex(6)}"
    restore["request_id"] = request_id
    _save_restore_progress(job_id, restore)
    common = {"blog_id": blog_id, "run_id": run_id, "request_id": request_id,
              "expected_blog_path": blog_path}

    def refused(outcome: Any, what: str) -> None:
        if not isinstance(outcome, dict):
            raise DraftConflict(f"the restore step '{what}' returned no result: {outcome!r}")
        failures = outcome.get("failures") or []
        if outcome.get("ok") is False or failures:
            raise DraftConflict(
                f"the restore step '{what}' reported {len(failures) or 'a'} problem(s): "
                f"{failures[:5] if failures else outcome}"
            )

    try:
        outcome = broker_job(env, "/restore", {
            **common, "phase": "terms",
            "snapshot": {"terms": terms, "blog_path": blog_path,
                         "site_options": site_options},
        }, run_id=run_id)
        refused(outcome, "terms pass")
        if int(outcome.get("terms") or 0) != len(terms):
            raise DraftConflict(
                f"the restore put back {outcome.get('terms')} of {len(terms)} categories"
            )
        restore["terms_result"] = {k: outcome.get(k) for k in ("terms", "created", "updated")}
        restore["phase"] = "memberships"
        _save_restore_progress(job_id, restore)
        offset = 0
        restored = 0
        for page in _restore_pages(products):
            outcome = broker_job(env, "/restore", {
                **common, "phase": "memberships", "page": offset,
                "snapshot": {"terms": terms, "products": page, "blog_path": blog_path},
                "products_offset": offset,
            }, run_id=run_id)
            refused(outcome, f"membership page {offset}")
            expected_products = len({int(r.get("product_id") or 0) for r in page})
            if int(outcome.get("products_restored") or 0) != expected_products:
                raise DraftConflict(
                    f"the restore put back {outcome.get('products_restored')} of"
                    f" {expected_products} products (page {offset})"
                )
            restored += int(outcome.get("products_restored") or 0)
            offset += len(page)
            restore["offset"] = offset
            restore["products_restored"] = restored
            _save_restore_progress(job_id, restore)
        restore["phase"] = "finalize"
        _save_restore_progress(job_id, restore)
        outcome = broker_job(env, "/restore", {
            **common, "phase": "finalize",
            "snapshot": {"terms": terms, "blog_path": blog_path},
        }, run_id=run_id)
        refused(outcome, "finalize")
        restore["terms_removed"] = outcome.get("terms_removed")
        restore["phase"] = "verify"
        _save_restore_progress(job_id, restore)
        live = categories_service.fetch_export(env, blog_id)
        problems = _verify_restored(snapshot, live)
        if problems:
            restore["verification"] = problems[:50]
            raise DraftConflict(
                f"the restore did not converge: {len(problems)} difference(s) from"
                f" the saved copy remain, e.g. {problems[:3]}"
            )
        restore["verified"] = True
        restore["status"] = "done"
        restore["phase"] = "done"
        _save_restore_progress(job_id, restore)
        try:
            with database.cursor(write=True, actor=actor) as cursor:
                restore["snapshot"] = categories_service.import_export(
                    cursor, env=env, blog_id=blog_id, export=live, actor=actor,
                )
        except Exception as exc:  # noqa: BLE001 - restore itself succeeded and verified
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
    (every pass is convergent).

    Exclusive per environment: refused while ANY run of the environment is
    active (a newer run's plan would be invalidated) and while another
    restore is still running there (its WordPress phases would interleave
    with this one's)."""

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
        cursor.execute(
            "SELECT run_id, status, worker_heartbeat_at FROM catmgr.run"
            " WHERE env = %s AND status IN ('queued', 'running') ORDER BY run_id",
            (run["env"],),
        )
        for other in cursor.fetchall():
            # A queued run has no heartbeat yet but is active; a running run
            # is active unless its worker is dead (stale heartbeat).
            active = other["status"] == "queued" or not _stale(dict(other))
            if active:
                raise DraftConflict(
                    f"run #{other['run_id']} is {other['status']} on {run['env']}"
                    " - pause or finish it before restoring a store"
                )
        running = _running_restores(cursor, run["env"], exclude_job_id=job_id)
        if running:
            raise DraftConflict(
                f"store {running[0]['blog_id']} (run #{running[0]['run_id']}) is still"
                " being restored - one restore at a time"
            )
    current = (job.get("restore") or {})
    existing = _restore_threads.get(job_id)
    if current.get("status") == "running" and existing and existing.is_alive():
        raise DraftConflict("a restore is already running for this store")
    if current.get("status") == "running" and not _restore_stale(current):
        raise DraftConflict("a restore is already running for this store (it reported progress moments ago)")
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
            refreshed.append(categories_service.import_export(
                cursor, env=env, blog_id=blog_id, export=export, actor=actor,
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
