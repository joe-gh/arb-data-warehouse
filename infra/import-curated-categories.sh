#!/bin/bash
# Repeatable import: curated Woo category tree -> warehouse curated.* tables.
#
#   ./infra/import-curated-categories.sh [blog_id]        (default 1)
#
# Runs from a workstation with SSH access to both boxes. Full-replace per
# blog inside one transaction, so re-running always reflects current curation
# and a failed load leaves the previous snapshot intact.
set -euo pipefail

BLOG_ID="${1:-1}"
PROD_KEY="$HOME/Projects/arborwear/arborwear_cms_prod.pem"
PROD="ubuntu@3.18.173.225"
WH_KEY="$HOME/.ssh/fdm4-warehouse.pem"
WH="ubuntu@3.20.17.84"
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "== exporting blog $BLOG_ID from production"
scp -q -i "$PROD_KEY" "$HERE/export_curated_categories.php" "$PROD:/tmp/ecc.php"
ssh -i "$PROD_KEY" "$PROD" \
  "cd /var/www/arborwear && sudo -u www-data wp eval-file /tmp/ecc.php $BLOG_ID /tmp 2>&1 | grep -v Deprecated; rm -f /tmp/ecc.php"
scp -q -i "$PROD_KEY" "$PROD:/tmp/curated_categories.tsv" "$PROD:/tmp/curated_category_products.tsv" "$TMP/"
ssh -i "$PROD_KEY" "$PROD" "sudo rm -f /tmp/curated_categories.tsv /tmp/curated_category_products.tsv"

echo "== loading into warehouse"
scp -q -i "$WH_KEY" "$TMP/curated_categories.tsv" "$TMP/curated_category_products.tsv" "$WH:/tmp/"
ssh -i "$WH_KEY" "$WH" "sudo chown postgres /tmp/curated_categories.tsv /tmp/curated_category_products.tsv && sudo -u postgres psql -v ON_ERROR_STOP=1 -d arb_warehouse \
  -c 'BEGIN' \
  -c \"DELETE FROM curated.category_product WHERE blog_id = $BLOG_ID\" \
  -c \"DELETE FROM curated.category WHERE blog_id = $BLOG_ID\" \
  -c \"\\copy curated.category (blog_id, term_id, slug, name, parent_term_id, depth, path, sort_order, product_count) FROM '/tmp/curated_categories.tsv'\" \
  -c \"\\copy curated.category_product (blog_id, term_id, sku, product_id) FROM '/tmp/curated_category_products.tsv'\" \
  -c 'COMMIT' \
  -c \"SELECT count(*) AS categories FROM curated.category WHERE blog_id = $BLOG_ID\" \
  -c \"SELECT count(*) AS memberships FROM curated.category_product WHERE blog_id = $BLOG_ID\"; sudo rm -f /tmp/curated_categories.tsv /tmp/curated_category_products.tsv"
echo "== done"
