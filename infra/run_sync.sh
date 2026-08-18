#!/usr/bin/env bash
# Full live FDM4 -> Postgres product sync, run ENTIRELY on the warehouse box.
#   pull (live FDM4 via NAT -> CSV)  ->  load (CSV -> Postgres fdm4.*)  ->  refresh woo.store_product_state
# Run as root (reads the 600 config for the pull; sudo -u postgres for the load):
#   sudo bash /opt/fdm4-extractor/run_sync.sh
#
# Writes its status to woo.sync_control (op='pull', env='global') so WP can gate its
# Woo reconcile on a fresh, SUCCESSFUL pull rather than guessing on a timer.
set -uo pipefail
EXT=/opt/fdm4-extractor
control() { sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d arb_warehouse -tAc "$1"; }

echo "=== FDM4 -> Postgres full product sync (LIVE via NAT) ==="
echo "start: $(date -u +%FT%TZ)"

# Mark a pull running; capture the row id for the final status update.
RUN_ID=$(control "INSERT INTO woo.sync_control (op,env,status,requested_by,started_at) VALUES ('pull','global','running','run_sync.sh',now()) RETURNING id" | grep -oE '^[0-9]+' | head -1)
if [[ ! "$RUN_ID" =~ ^[0-9]+$ ]]; then
  echo "CONTROL FAILED - no sync_control run id; not pulling." >&2
  exit 1
fi
fail() { [ -n "${RUN_ID:-}" ] && control "UPDATE woo.sync_control SET status='failed', finished_at=now(), error='$1' WHERE id=$RUN_ID"; }

# If killed (timeout/SIGTERM) before success/failure is recorded, flip the row off
# 'running' so it never dangles. Idempotent: only touches a row still in 'running'
# (a normal success/failure has already set the final status). SIGKILL can't be
# trapped - the daily prune sweeps any 'running' row older than a few hours.
mark_interrupted() { [ -n "${RUN_ID:-}" ] && control "UPDATE woo.sync_control SET status='failed', finished_at=COALESCE(finished_at,now()), error=COALESCE(error,'interrupted') WHERE id=$RUN_ID AND status='running'"; }
trap mark_interrupted EXIT TERM INT

echo "--- PULL: live FDM4 product tables -> CSV ---"
ARB_DBTEST_ENV="$EXT/fdm4.env" "$EXT/venv/bin/python" "$EXT/pull_fdm4.py" 2>&1 | grep -v "WARNING:"
pull_rc=${PIPESTATUS[0]}
if [ "$pull_rc" != "0" ]; then
  echo "PULL FAILED (rc=$pull_rc) - not loading."
  fail "pull failed rc=$pull_rc"
  echo "=== ABORTED ==="
  exit 1
fi

chmod -R a+rX "$EXT/dump"

echo "--- LOAD: CSV -> Postgres fdm4.* + rebuild woo.store_product_state ---"
load_output=$(sudo -u postgres "$EXT/venv/bin/python" "$EXT/load_dump.py" "$EXT/dump" 2>&1)
load_rc=$?
printf '%s\n' "$load_output"
if [ "$load_rc" != "0" ]; then
  echo "LOAD FAILED (rc=$load_rc)."
  fail "load failed rc=$load_rc"
  echo "=== ABORTED ==="
  exit 1
fi

# Success metadata comes from the same transaction that ran the transform. A
# post-hoc MAX query could accidentally certify the previous projection.
sync_result=$(printf '%s\n' "$load_output" | sed -n 's/^SYNC_RESULT refresh_version=\([0-9][0-9]*\) rows_loaded=\([0-9][0-9]*\)$/\1 \2/p' | tail -1)
read -r VER ROWS <<< "$sync_result"
if [[ ! "${VER:-}" =~ ^[0-9]+$ || ! "${ROWS:-}" =~ ^[0-9]+$ ]]; then
  echo "LOAD FAILED - loader returned no trusted transform result." >&2
  fail "loader returned no trusted transform result"
  exit 1
fi
completed_id=$(control "UPDATE woo.sync_control SET status='success', finished_at=now(), refresh_version=$VER, rows_loaded=$ROWS WHERE id=$RUN_ID AND status='running' RETURNING id" | grep -oE '^[0-9]+' | head -1)
if [[ "$completed_id" != "$RUN_ID" ]]; then
  echo "CONTROL FAILED - success row was not finalized." >&2
  exit 1
fi

# Tell registered feed consumers a new generation is published (Emblem etc.).
# Best-effort and backgrounded: a slow or dead consumer webhook must never
# fail or delay the pipeline. feed-ping.sh itself always exits 0.
if [ -x "$EXT/feed-ping.sh" ]; then
  ( bash "$EXT/feed-ping.sh" "$VER" >/dev/null 2>&1 || true ) &
fi

echo "end: $(date -u +%FT%TZ)"
echo "refresh_version=${VER:-0} active_rows=${ROWS:-0} control_id=${RUN_ID:-?}"
echo "=== DONE ==="
