#!/usr/bin/env bash
# Full dev blog-1 (Public Web) reconcile. Order: reparent stranded -> full-reconcile -> purge orphans.
set -euo pipefail

step="initialization"
on_exit() {
    rc=$?
    trap - EXIT
    if (( rc != 0 )); then
        echo "FAILED step=${step} rc=${rc} $(date -u +%FT%TZ)" >&2
    fi
    exit "$rc"
}
trap on_exit EXIT

cd /var/www/arborwear
echo "START $(date -u +%FT%TZ)"
step="reparent"
echo "===== STEP 1: reparent stranded variations (fix #7) ====="
/usr/bin/wp arb_product_sync reparent --blog_id=1 --execute 2>&1
step="full-reconcile"
echo "===== STEP 2: full-reconcile sync (republish desired + retire non-desired + create missing) ====="
/usr/bin/wp arb_product_sync sync --blog_id=1 --full-reconcile --execute 2>&1
step="purge-orphans"
echo "===== STEP 3: purge orphaned variations ====="
/usr/bin/wp arb_product_sync purge-orphans --blog_id=1 --execute 2>&1
step="complete"
echo "DONE $(date -u +%FT%TZ)"
