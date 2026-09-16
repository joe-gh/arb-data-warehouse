#!/usr/bin/env python3
"""On-demand PIM push worker.

The Warehouse Ops app queues requests as append-only rows in logo.audit_log
(action pim_push_requested, detail {request_id, mode, styles}). This worker,
run every minute from root's crontab as postgres with PIM_API_KEY in the
environment, takes the oldest queued request and:

  preview  runs `push_pim.py --diff` (a dry diff: writes a change set,
           sends nothing) so the app can show what the next push would send
  push     runs `pull_pim.py` (refresh the PIM mirror) then
           `push_pim.py --auto` (diff + send under the hourly job's caps)

Either mode accepts an optional style list; a scoped run creates, publishes
and fills only those styles and never removes (see push_pim.run_diff).

It appends pim_push_started when it picks a request up and
pim_push_finished / pim_push_failed with the outcome: the change set it
produced (counts by action/status come from the app's own query), the run's
last lines of output and the duration. The cron line wraps it in the same
flock the hourly push uses, so the two never overlap; a request found while
the hourly run holds the lock simply waits for the next minute.
"""
import datetime
import json
import os
import subprocess
import sys
import time

import psycopg2
import psycopg2.extras

DB_NAME = "arb_warehouse"
DB_SOCKET = "/var/run/postgresql"
HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(HERE, "venv", "bin", "python")
PUSH = os.path.join(HERE, "push_pim.py")
PULL = os.path.join(HERE, "pull_pim.py")
REQUEST_ACTION = "pim_push_requested"
STARTED_ACTION = "pim_push_started"
FINISHED_ACTION = "pim_push_finished"
FAILED_ACTION = "pim_push_failed"
STEP_TIMEOUT = int(os.environ.get("PIM_WORKER_STEP_TIMEOUT", "3000"))


def connect():
    connection = psycopg2.connect(dbname=DB_NAME, host=DB_SOCKET)
    connection.autocommit = True
    return connection


def next_request(cursor):
    cursor.execute(
        """
        SELECT r.id, r.actor, r.detail
          FROM logo.audit_log r
         WHERE r.action = %(req)s
           AND NOT EXISTS (SELECT 1 FROM logo.audit_log a
                            WHERE a.action IN (%(started)s, %(finished)s, %(failed)s)
                              AND a.detail ->> 'request_id' = r.detail ->> 'request_id')
         ORDER BY r.id
         LIMIT 1
        """,
        {"req": REQUEST_ACTION, "started": STARTED_ACTION, "finished": FINISHED_ACTION, "failed": FAILED_ACTION})
    return cursor.fetchone()


def log_row(cursor, action, actor, detail):
    cursor.execute(
        "INSERT INTO logo.audit_log (actor, action, fdm4_store, detail) VALUES (%s, %s, '', %s)",
        (actor, action, json.dumps(detail)))


def run_step(args, env):
    started = time.monotonic()
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=STEP_TIMEOUT)
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out, round(time.monotonic() - started, 1)


def main():
    if not os.environ.get("PIM_API_KEY"):
        print("pim_push_worker: PIM_API_KEY is not set", file=sys.stderr)
        return 2
    connection = connect()
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    request = next_request(cursor)
    if not request:
        return 0
    detail = request["detail"] or {}
    request_id = str(detail.get("request_id") or "")
    mode = str(detail.get("mode") or "preview")
    styles = [str(s).strip().upper() for s in (detail.get("styles") or []) if str(s).strip()]
    actor = request["actor"] or "worker"
    if not request_id:
        log_row(cursor, FAILED_ACTION, actor, {"request_id": "", "error": "request without id", "audit_id": request["id"]})
        return 0
    log_row(cursor, STARTED_ACTION, actor, {"request_id": request_id, "mode": mode, "styles": styles})
    print(f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ} request {request_id}: {mode} by {actor}"
          + (f" styles={','.join(styles)}" if styles else ""))

    env = dict(os.environ)
    env.setdefault("PIM_PUSH_MIN_INTERVAL", "0.3")
    env["PYTHONUNBUFFERED"] = "1"
    cursor.execute("SELECT coalesce(max(set_id), 0) AS n FROM pim.push_change_set")
    before = int(cursor.fetchone()["n"])
    steps = []
    error = None
    try:
        if mode == "push":
            env["PIM_PUSH_ENABLED"] = "1"
            rc, out, secs = run_step([PYTHON, PULL], env)
            steps.append({"step": "pull_pim", "rc": rc, "seconds": secs, "tail": out.strip().splitlines()[-8:]})
            if rc != 0:
                error = f"pull_pim failed (rc={rc})"
            else:
                args = [PYTHON, PUSH, "--auto"] + (["--styles", ",".join(styles)] if styles else [])
                rc, out, secs = run_step(args, env)
                steps.append({"step": "push_auto", "rc": rc, "seconds": secs, "tail": out.strip().splitlines()[-30:]})
                if rc != 0:
                    error = f"push_pim --auto failed (rc={rc})"
        else:
            env.pop("PIM_PUSH_ENABLED", None)
            args = [PYTHON, PUSH, "--diff", "--note", f"preview requested by {actor}"] + (["--styles", ",".join(styles)] if styles else [])
            rc, out, secs = run_step(args, env)
            steps.append({"step": "diff", "rc": rc, "seconds": secs, "tail": out.strip().splitlines()[-30:]})
            if rc != 0:
                error = f"push_pim --diff failed (rc={rc})"
    except subprocess.TimeoutExpired as exc:
        error = f"step timed out after {STEP_TIMEOUT}s: {exc.cmd[-1] if exc.cmd else ''}"
    except Exception as exc:  # noqa: BLE001 - always record an outcome
        error = f"{type(exc).__name__}: {exc}"

    cursor.execute("SELECT coalesce(max(set_id), 0) AS n FROM pim.push_change_set")
    after = int(cursor.fetchone()["n"])
    set_id = after if after > before else None
    outcome = {"request_id": request_id, "mode": mode, "styles": styles, "set_id": set_id, "steps": steps}
    if error:
        outcome["error"] = error
        log_row(cursor, FAILED_ACTION, actor, outcome)
        print(f"  FAILED: {error}")
        return 1
    log_row(cursor, FINISHED_ACTION, actor, outcome)
    print(f"  finished: set {set_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
