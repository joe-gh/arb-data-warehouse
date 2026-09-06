#!/usr/bin/env bash
# Build the warehouse schema from nothing, in dependency order.
#
#   bash sql/bootstrap.sh "<psql DSN of an EMPTY database>"
#
# Order matters and is not lexical: 2026-07-31-price-rule-hardening.sql ALTERs
# a table 2026-07-31-price-rules.sql creates, and logo.assignment ends up with
# its columns in the order database_contract.py pins only when
# 2026-08-03-logo-feed-versioning.sql runs before
# 2026-08-03-assignment-catalog-scope.sql. sql/migrations/APPLY_ORDER is the
# one list; this script refuses to run when it and the directory disagree, so
# a new migration cannot be forgotten or applied in an accidental position.
#
# Prerequisites this script does NOT create, on purpose:
#   * the roles woo_reader, insights_reader, etl_writer and logo_admin, which
#     are cluster-wide and carry passwords this repo must not hold;
#   * schema fdm4, which is not DDL in this repo at all - infra/load_dump.py
#     creates its all-TEXT tables from the pull's CSV headers. Load one pull
#     before bootstrapping: four migrations read fdm4.mill / fdm4.item /
#     fdm4.dec_design and fail without them.
# AGENT_REPULL_FUNCTION_SHA256 (64 hex chars) is required when the legacy
# logo.repull_display_name(text,boolean) exists: sql/logo_admin_role.sql only
# grants EXECUTE on a body whose hash was reviewed.
set -euo pipefail

dsn="${1:-}"
if [[ -z "$dsn" ]]; then
  echo "usage: bash sql/bootstrap.sh '<psql DSN>'" >&2
  exit 2
fi
command -v psql >/dev/null || { echo "psql is not on PATH" >&2; exit 2; }

sql_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
manifest="$sql_dir/migrations/APPLY_ORDER"
[[ -f "$manifest" ]] || { echo "missing manifest: $manifest" >&2; exit 2; }

repull_function_sha256="${AGENT_REPULL_FUNCTION_SHA256:-}"
if [[ -n "$repull_function_sha256" && ! "$repull_function_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "AGENT_REPULL_FUNCTION_SHA256 must be 64 hexadecimal characters" >&2
  exit 2
fi
repull_function_sha256="${repull_function_sha256,,}"

run() {
  echo "--- $1"
  psql "$dsn" -X -v ON_ERROR_STOP=1 -q \
    -v repull_function_sha256="$repull_function_sha256" -f "$1"
}

# The manifest is the authority; a file present in one and not the other is a
# packaging error, never something to guess around.
listed="$(grep -v '^[[:space:]]*$' "$manifest" | LC_ALL=C sort)"
present="$(cd "$sql_dir/migrations" && ls -1 *.sql 2>/dev/null | LC_ALL=C sort || true)"
if [[ "$listed" != "$present" ]]; then
  echo "APPLY_ORDER does not match sql/migrations/:" >&2
  comm -3 <(printf '%s\n' "$listed") <(printf '%s\n' "$present") \
    | sed 's/^\t/  only on disk: /; s/^\([^ ]\)/  only in APPLY_ORDER: \1/' >&2
  exit 2
fi
duplicates="$(printf '%s\n' "$listed" | uniq -d)"
if [[ -n "$duplicates" ]]; then
  echo "APPLY_ORDER lists a file twice: $duplicates" >&2
  exit 2
fi

# sql/pim_writer_role.sql names its database literally (that is the point: it
# can only ever configure the production warehouse), so refuse early rather
# than after sixty migrations.
target_database="$(psql "$dsn" -X -Atqc 'SELECT current_database()')"
pim_database="$(sed -n 's/.*ON DATABASE \([a-z0-9_]*\) .*/\1/p' \
  "$sql_dir/pim_writer_role.sql" | head -1)"
if [[ -z "$pim_database" ]]; then
  echo "cannot read the database name from sql/pim_writer_role.sql" >&2
  exit 2
fi
if [[ "$target_database" != "$pim_database" ]]; then
  echo "target database is '$target_database' but sql/pim_writer_role.sql" \
       "configures '$pim_database'; bootstrap builds that database only" >&2
  exit 2
fi

for role in woo_reader insights_reader etl_writer logo_admin; do
  has_role="$(psql "$dsn" -X -Atqc \
    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role')")"
  if [[ "$has_role" != "t" ]]; then
    echo "required role is absent: $role (provision roles before bootstrapping)" >&2
    exit 2
  fi
done

run "$sql_dir/logo_schema.sql"
run "$sql_dir/sync_control.sql"
run "$sql_dir/pim_schema.sql"
run "$sql_dir/woo_transform.sql"

while IFS= read -r migration; do
  [[ -n "$migration" ]] || continue
  run "$sql_dir/migrations/$migration"
done < "$manifest"

# Role policies last: both validate the live catalog before revoking anything,
# so they only pass once every migration above has landed.
run "$sql_dir/logo_admin_role.sql"
run "$sql_dir/pim_writer_role.sql"

echo "=== bootstrap complete"
