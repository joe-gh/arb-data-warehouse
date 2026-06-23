#!/usr/bin/env bash
# Full dev blog-1 (Public Web) reconcile. Order: reparent stranded -> full-reconcile -> purge orphans.
cd /var/www/arborwear
echo "START $(date -u +%FT%TZ)"
echo "===== STEP 1: reparent stranded variations (fix #7) ====="
/usr/bin/wp arb_product_sync reparent --blog_id=1 --execute 2>&1
echo "===== STEP 2: full-reconcile sync (republish desired + retire non-desired + create missing) ====="
/usr/bin/wp arb_product_sync sync --blog_id=1 --full-reconcile --execute 2>&1
echo "===== STEP 3: purge orphaned variations ====="
/usr/bin/wp arb_product_sync purge-orphans --blog_id=1 --execute 2>&1
echo "DONE $(date -u +%FT%TZ)"
