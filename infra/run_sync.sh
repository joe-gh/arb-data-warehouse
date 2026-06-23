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
control() { sudo -u postgres psql -d arb_warehouse -tAc "$1" 2>/dev/null; }

echo "=== FDM4 -> Postgres full product sync (LIVE via NAT) ==="
echo "start: $(date -u +%FT%TZ)"

# Mark a pull running; capture the row id for the final status update.
RUN_ID=$(control "INSERT INTO woo.sync_control (op,env,status,requested_by,started_at) VALUES ('pull','global','running','run_sync.sh',now()) RETURNING id" | grep -oE '^[0-9]+' | head -1)
fail() { [ -n "${RUN_ID:-}" ] && control "UPDATE woo.sync_control SET status='failed', finished_at=now(), error='$1' WHERE id=$RUN_ID"; }

# If killed (timeout/SIGTERM) before success/failure is recorded, flip the row off
# 'running' so it never dangles. Idempotent: only touches a row still in 'running'
# (a normal success/failure has already set the final status). SIGKILL can't be
# trapped — the daily prune sweeps any 'running' row older than a few hours.
mark_interrupted() { [ -n "${RUN_ID:-}" ] && control "UPDATE woo.sync_control SET status='failed', finished_at=COALESCE(finished_at,now()), error=COALESCE(error,'interrupted') WHERE id=$RUN_ID AND status='running'"; }
trap mark_interrupted EXIT TERM INT

echo "--- PULL: live FDM4 product tables -> CSV ---"
ARB_DBTEST_ENV="$EXT/fdm4.env" "$EXT/venv/bin/python" "$EXT/pull_fdm4.py" 2>&1 | grep -v "WARNING:"
pull_rc=${PIPESTATUS[0]}
if [ "$pull_rc" != "0" ]; then
  echo "PULL FAILED (rc=$pull_rc) — not loading."
  fail "pull failed rc=$pull_rc"
  echo "=== ABORTED ==="
  exit 1
fi

chmod -R a+rX "$EXT/dump"

echo "--- LOAD: CSV -> Postgres fdm4.* + rebuild woo.store_product_state ---"
sudo -u postgres "$EXT/venv/bin/python" "$EXT/load_dump.py" "$EXT/dump"
load_rc=$?
if [ "$load_rc" != "0" ]; then
  echo "LOAD FAILED (rc=$load_rc)."
  fail "load failed rc=$load_rc"
  echo "=== ABORTED ==="
  exit 1
fi

# Success: stamp the new warehouse version (max row_version) + active row count, so the
# WP gate can tell whether anything actually changed since its last reconcile.
VER=$(control "SELECT COALESCE(MAX(row_version),0) FROM woo.store_product_state" | tr -d '[:space:]')
ROWS=$(control "SELECT count(*) FROM woo.store_product_state WHERE is_active" | tr -d '[:space:]')
[ -n "${RUN_ID:-}" ] && control "UPDATE woo.sync_control SET status='success', finished_at=now(), refresh_version=${VER:-0}, rows_loaded=${ROWS:-0} WHERE id=$RUN_ID"

echo "end: $(date -u +%FT%TZ)"
echo "refresh_version=${VER:-0} active_rows=${ROWS:-0} control_id=${RUN_ID:-?}"
echo "=== DONE ==="
