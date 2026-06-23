#!/usr/bin/env bash
# Reaper + stale-row cleanup for the FDM4 pull. Runs every 10 min via cron.
#
#   1. Kill any pull/load process wedged past the 55-min cap (a hung FDM4 JDBC
#      pull). SIGKILL is required because the process is unresponsive.
#   2. Flip its dangling woo.sync_control row from 'running' -> 'failed'. SIGKILL
#      bypasses run_sync.sh's EXIT trap, so without this step the row would linger
#      as 'running' (tripping sync-status as STUCK) until the daily prune.
#
# Safe: a normal pull finishes in ~7 min, so a 'running' row older than 55 min is
# always a hung/reaped run, never a healthy in-flight one.
set -uo pipefail

# 1) Kill processes past the cap.
ps -eo pid,etimes,args | awk '/[r]un_sync.sh|[p]ull_fdm4|[l]oad_dump/ && $2>3300 {print $1}' | xargs -r kill -9

# 2) Mark any dangling 'running' control row failed (closes the SIGKILL/trap gap).
sudo -u postgres psql -d arb_warehouse -tAc "UPDATE woo.sync_control SET status='failed', finished_at=COALESCE(finished_at,now()), error=COALESCE(NULLIF(error,''),'reaped: exceeded 55m cap') WHERE status='running' AND started_at < now()-interval '55 minutes'" >/dev/null 2>&1

exit 0
