#!/usr/bin/env bash
# Verify an already-restored, isolated warehouse database + logo-media tree and
# emit reviewable recovery evidence. This script never creates or attaches AWS
# resources; operations must provision the isolated restore target first.
set -euo pipefail

: "${RESTORE_DATABASE_DSN:?set RESTORE_DATABASE_DSN for the isolated restore}"
: "${RESTORE_EXPECTED_DATABASE:?set the unique isolated restore database name}"
: "${RESTORE_UPLOAD_DIR:?set RESTORE_UPLOAD_DIR for the restored media tree}"
: "${RESTORE_EVIDENCE_DIR:?set RESTORE_EVIDENCE_DIR for the evidence artifact}"
: "${RESTORE_DRILL_CONFIRM:?set RESTORE_DRILL_CONFIRM=isolated-restore}"

if [[ "$RESTORE_DRILL_CONFIRM" != "isolated-restore" ]]; then
    echo "Refusing to run without RESTORE_DRILL_CONFIRM=isolated-restore" >&2
    exit 2
fi
if [[ "$RESTORE_EXPECTED_DATABASE" == "arb_warehouse" ]]; then
    echo "Refusing the production database name; use a uniquely named restore" >&2
    exit 2
fi

upload_dir=$(realpath "$RESTORE_UPLOAD_DIR")
evidence_dir=$(realpath -m "$RESTORE_EVIDENCE_DIR")
if [[ "$upload_dir" == "/" || "$upload_dir" == "/var/lib/arb-logo-admin/uploads" ]]; then
    echo "Refusing to inspect a production or broad upload path" >&2
    exit 2
fi
if [[ "$evidence_dir" == "/" || "$evidence_dir" == "$upload_dir" ]]; then
    echo "Evidence directory must be separate from the restored media tree" >&2
    exit 2
fi

mkdir -p "$evidence_dir"
started_at=$(date -u +%FT%TZ)
export PGOPTIONS="${PGOPTIONS:-} -c default_transaction_read_only=on"

actual_database=$(psql "$RESTORE_DATABASE_DSN" -X -v ON_ERROR_STOP=1 -Atqc \
    "SELECT current_database()")
if [[ "$actual_database" != "$RESTORE_EXPECTED_DATABASE" ]]; then
    echo "Refusing unexpected database: $actual_database" >&2
    exit 2
fi

psql "$RESTORE_DATABASE_DSN" -X -v ON_ERROR_STOP=1 -At >"$evidence_dir/database.tsv" <<'SQL'
SELECT 'database', current_database();
SELECT 'server_time', now();
SELECT 'assignment_rows', count(*) FROM logo.assignment;
SELECT 'store_settings_rows', count(*) FROM logo.store_settings;
SELECT 'display_name_rows', count(*) FROM logo.display_name;
SELECT 'active_product_rows', count(*) FROM woo.store_product_state WHERE is_active;
SELECT 'latest_product_version', COALESCE(max(row_version), 0) FROM woo.store_product_state;
SELECT 'invalid_assignment_positions', count(*)
  FROM logo.assignment
 WHERE position NOT BETWEEN 1 AND 3 OR option_row < 1;
SELECT 'orphan_companions', count(*)
  FROM logo.assignment child
 WHERE child.position > 1
   AND NOT EXISTS (
       SELECT 1 FROM logo.assignment parent
        WHERE parent.fdm4_store = child.fdm4_store
          AND parent.product_style = child.product_style
          AND parent.garment_color_code = child.garment_color_code
          AND parent.option_row = child.option_row
          AND parent.position = 1
          AND parent.active
   );
SQL

psql "$RESTORE_DATABASE_DSN" -X -v ON_ERROR_STOP=1 -AtF $'\t' \
    -c "SELECT source_url, filename, bytes FROM logo.image_import ORDER BY source_url" \
    >"$evidence_dir/image-import.tsv"

missing=0
size_mismatch=0
while IFS=$'\t' read -r _source filename expected_size; do
    [[ -n "$filename" ]] || continue
    if [[ "$filename" == */* || ! -f "$upload_dir/$filename" ]]; then
        ((missing += 1))
        continue
    fi
    actual_size=$(stat -c %s "$upload_dir/$filename")
    if [[ "$actual_size" != "$expected_size" ]]; then
        ((size_mismatch += 1))
    fi
done <"$evidence_dir/image-import.tsv"

find "$upload_dir" -maxdepth 1 -type f -printf '%f\n' \
    | LC_ALL=C sort | sed -n '1,100p' \
    | while IFS= read -r filename; do
        sha256sum "$upload_dir/$filename"
      done >"$evidence_dir/media-sample.sha256"

finished_at=$(date -u +%FT%TZ)
{
    printf 'started_at\t%s\n' "$started_at"
    printf 'finished_at\t%s\n' "$finished_at"
    printf 'upload_dir\t%s\n' "$upload_dir"
    printf 'missing_mapped_files\t%s\n' "$missing"
    printf 'size_mismatches\t%s\n' "$size_mismatch"
    printf 'sampled_media_files\t%s\n' "$(wc -l <"$evidence_dir/media-sample.sha256")"
} >"$evidence_dir/summary.tsv"

if (( missing != 0 || size_mismatch != 0 )); then
    echo "Restore drill FAILED; evidence: $evidence_dir" >&2
    exit 1
fi

echo "Restore drill PASSED; evidence: $evidence_dir"
