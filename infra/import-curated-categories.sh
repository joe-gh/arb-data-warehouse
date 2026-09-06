#!/bin/bash
# Repeatable import: curated Woo category tree -> warehouse curated.* tables.
#
#   ./infra/import-curated-categories.sh [blog_id]        (default 1)
#
# Runs from a workstation with SSH access to both boxes. Full-replace per
# blog inside one transaction, so re-running always reflects current curation
# and a failed load leaves the previous snapshot intact.
#
# Every invocation stages through its own mktemp directory on each remote box:
# two imports running side by side (say blog 1 and blog 2) can never read or
# overwrite each other's export. Remote work runs under `set -euo pipefail`
# with a trap that cleans up and keeps the failing status, so `== done` only
# ever prints after an export and a load that both actually worked.
set -euo pipefail

BLOG_ID="${1:-1}"
case "$BLOG_ID" in
  ''|*[!0-9]*) echo "blog id must be a number" >&2; exit 2 ;;
esac
PROD_KEY="$HOME/Projects/arborwear/arborwear_cms_prod.pem"
PROD="ubuntu@3.18.173.225"
WH_KEY="$HOME/.ssh/fdm4-warehouse.pem"
WH="ubuntu@3.20.17.84"
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
REMOTE_EXPORT=""
REMOTE_LOAD=""

# The staging paths come back from the remote boxes and are then spliced into
# remote commands, so accept only what mktemp is supposed to have produced.
require_safe_remote_path() {
  case "$1" in
    /tmp/arb-curated-export.[A-Za-z0-9]*|/tmp/arb-curated-load.[A-Za-z0-9]*) ;;
    *) echo "unexpected staging path from $2: $1" >&2; return 1 ;;
  esac
  case "$1" in
    *[!A-Za-z0-9./_-]*) echo "unsafe staging path from $2: $1" >&2; return 1 ;;
  esac
  return 0
}

cleanup() {
  local rc=$?
  rm -rf "$TMP"
  if [ -n "$REMOTE_EXPORT" ]; then
    ssh -i "$PROD_KEY" "$PROD" "sudo rm -rf '$REMOTE_EXPORT'" >/dev/null 2>&1 || true
  fi
  if [ -n "$REMOTE_LOAD" ]; then
    ssh -i "$WH_KEY" "$WH" "sudo rm -rf '$REMOTE_LOAD'" >/dev/null 2>&1 || true
  fi
  exit $rc
}
trap cleanup EXIT

echo "== exporting blog $BLOG_ID from production"
REMOTE_EXPORT="$(ssh -i "$PROD_KEY" "$PROD" 'mktemp -d /tmp/arb-curated-export.XXXXXXXX')"
require_safe_remote_path "$REMOTE_EXPORT" "$PROD"
scp -q -i "$PROD_KEY" "$HERE/export_curated_categories.php" "$PROD:$REMOTE_EXPORT/ecc.php"
ssh -i "$PROD_KEY" "$PROD" "bash -s -- '$REMOTE_EXPORT' '$BLOG_ID'" <<'REMOTE_EXPORT_SCRIPT'
set -euo pipefail
dir="$1"; blog="$2"
trap 'rc=$?; rm -f "$dir/ecc.php" >/dev/null 2>&1 || true; exit $rc' EXIT
# wp-cli runs as www-data and writes the TSVs into this directory; keep it
# ours so the files can be collected afterwards, and group-writable for it.
sudo install -d -o "$(id -un)" -g www-data -m 770 "$dir"
cd /var/www/arborwear
# grep is only filtering PHP deprecation noise; it must never decide the
# exit status of the export (no matches is not a failure).
sudo -u www-data wp eval-file "$dir/ecc.php" "$blog" "$dir" 2>&1 | { grep -v Deprecated || true; }
REMOTE_EXPORT_SCRIPT
scp -q -i "$PROD_KEY" "$PROD:$REMOTE_EXPORT/curated_categories.tsv" \
  "$PROD:$REMOTE_EXPORT/curated_category_products.tsv" "$TMP/"

# The load replaces this blog's rows outright, so refuse to run it on an empty
# export: a store with no category tree is an export failure, not a curation.
[ -s "$TMP/curated_categories.tsv" ] || {
  echo "export produced no categories for blog $BLOG_ID; not replacing what is there" >&2
  exit 1
}
# Memberships may legitimately be empty (a tree with nothing assigned yet),
# so the file only has to exist.
[ -f "$TMP/curated_category_products.tsv" ] || {
  echo "export produced no membership file for blog $BLOG_ID" >&2
  exit 1
}

echo "== loading into warehouse"
REMOTE_LOAD="$(ssh -i "$WH_KEY" "$WH" 'mktemp -d /tmp/arb-curated-load.XXXXXXXX')"
require_safe_remote_path "$REMOTE_LOAD" "$WH"
scp -q -i "$WH_KEY" "$TMP/curated_categories.tsv" "$TMP/curated_category_products.tsv" "$WH:$REMOTE_LOAD/"
ssh -i "$WH_KEY" "$WH" "bash -s -- '$REMOTE_LOAD' '$BLOG_ID'" <<'REMOTE_LOAD_SCRIPT'
set -euo pipefail
dir="$1"; blog="$2"
trap 'rc=$?; sudo rm -rf "$dir" >/dev/null 2>&1 || true; exit $rc' EXIT
# psql reads the files as postgres; hand it the whole directory rather than
# loose files in a world-writable /tmp.
sudo chown -R postgres "$dir"
sudo -u postgres psql -v ON_ERROR_STOP=1 --single-transaction -d arb_warehouse \
  -c "DELETE FROM curated.category_product WHERE blog_id = $blog" \
  -c "DELETE FROM curated.category WHERE blog_id = $blog" \
  -c "\copy curated.category (blog_id, term_id, slug, name, parent_term_id, depth, path, sort_order, product_count) FROM '$dir/curated_categories.tsv'" \
  -c "\copy curated.category_product (blog_id, term_id, sku, product_id) FROM '$dir/curated_category_products.tsv'"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d arb_warehouse \
  -c "SELECT count(*) AS categories FROM curated.category WHERE blog_id = $blog" \
  -c "SELECT count(*) AS memberships FROM curated.category_product WHERE blog_id = $blog"
REMOTE_LOAD_SCRIPT
REMOTE_LOAD=""
echo "== done"
