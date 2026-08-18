#!/usr/bin/env bash
set -euo pipefail

# Provision one disposable database on an isolated/local PostgreSQL cluster.
# The source DSN is read only and contributes schema (never data). The two DSN
# templates must contain the literal placeholders shown below.
: "${ALLOW_TEST_DB_PROVISION:?set ALLOW_TEST_DB_PROVISION=YES after reviewing the target cluster}"
: "${TEST_SCHEMA_SOURCE_DSN:?read-only DSN whose fdm4/woo/logo schemas will be cloned}"
: "${TEST_DATABASE_ADMIN_DSN_TEMPLATE:?example: postgresql://postgres:pw@127.0.0.1:5432/{database}}"
: "${TEST_DATABASE_APP_DSN_TEMPLATE:?example: postgresql://logo_admin:{password}@127.0.0.1:5432/{database}}"
repull_function_sha256="${AGENT_REPULL_FUNCTION_SHA256:-}"

if [[ -n "$repull_function_sha256" && ! "$repull_function_sha256" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "AGENT_REPULL_FUNCTION_SHA256 must be 64 hexadecimal characters" >&2
  exit 2
fi
repull_function_sha256="${repull_function_sha256,,}"

if [[ "$ALLOW_TEST_DB_PROVISION" != "YES" ]]; then
  echo "Refusing: ALLOW_TEST_DB_PROVISION must equal YES" >&2
  exit 2
fi
if [[ "$TEST_DATABASE_ADMIN_DSN_TEMPLATE" != *'{database}'* ]]; then
  echo "Admin DSN template must contain {database}" >&2
  exit 2
fi
if [[ "$TEST_DATABASE_APP_DSN_TEMPLATE" != *'{database}'* || "$TEST_DATABASE_APP_DSN_TEMPLATE" != *'{password}'* ]]; then
  echo "App DSN template must contain {database} and {password}" >&2
  exit 2
fi
for command_name in psql pg_dump createdb dropdb openssl; do
  command -v "$command_name" >/dev/null || {
    echo "Missing required command: $command_name" >&2
    exit 2
  }
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
nonce="$(openssl rand -hex 16)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
database_name="arb_warehouse_test_agent_${timestamp}_${nonce:0:8}"
logo_admin_password="$(openssl rand -hex 24)"
maintenance_dsn="${TEST_DATABASE_ADMIN_DSN_TEMPLATE//\{database\}/postgres}"
target_admin_dsn="${TEST_DATABASE_ADMIN_DSN_TEMPLATE//\{database\}/$database_name}"
target_app_dsn="${TEST_DATABASE_APP_DSN_TEMPLATE//\{database\}/$database_name}"
target_app_dsn="${target_app_dsn//\{password\}/$logo_admin_password}"

maintenance_database="$(psql "$maintenance_dsn" -X -Atqc 'SELECT current_database()')"
case "$maintenance_database" in
  postgres|template1) ;;
  *)
    echo "Refusing maintenance connection to database: $maintenance_database" >&2
    exit 2
    ;;
esac

created=false
cleanup_failed_provision() {
  if [[ "$created" == true ]]; then
    dropdb --if-exists --force --maintenance-db="$maintenance_dsn" "$database_name" >/dev/null 2>&1 || true
  fi
}
trap cleanup_failed_provision ERR INT TERM

# Roles are cluster-wide, so this script is intentionally restricted to a
# disposable/local cluster. Existing roles are hardened to the same contract.
psql "$maintenance_dsn" -X -v ON_ERROR_STOP=1 \
  -v logo_admin_password="$logo_admin_password" <<'SQL'
SELECT format('CREATE ROLE %I NOLOGIN', role_name)
  FROM (VALUES ('woo_reader'), ('insights_reader'), ('etl_writer')) roles(role_name)
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name)
\gexec
SELECT 'CREATE ROLE logo_admin LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logo_admin')
\gexec
ALTER ROLE logo_admin LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'logo_admin_password';
DO $$
DECLARE inherited_role text;
BEGIN
    FOR inherited_role IN
        SELECT granted.rolname
          FROM pg_auth_members membership
          JOIN pg_roles member ON member.oid = membership.member
          JOIN pg_roles granted ON granted.oid = membership.roleid
         WHERE member.rolname = 'logo_admin'
    LOOP
        EXECUTE format('REVOKE %I FROM logo_admin', inherited_role);
    END LOOP;
END
$$;
SQL

createdb --maintenance-db="$maintenance_dsn" "$database_name"
created=true

# Clone definitions only. Agent objects are supplied exclusively by the local
# immutable migrations below, so their test shape cannot drift with a source.
pg_dump "$TEST_SCHEMA_SOURCE_DSN" \
  --schema-only --no-owner --no-privileges \
  --schema=fdm4 --schema=woo --schema=logo \
  --exclude-table='logo.agent_*' \
  | psql "$target_admin_dsn" -X -v ON_ERROR_STOP=1

# Never self-bless cloned executable code: when the optional legacy function
# is present, its expected definition hash must have been reviewed separately.
repull_present="$(psql "$target_admin_dsn" -X -Atqc \
  "SELECT to_regprocedure('logo.repull_display_name(text,boolean)') IS NOT NULL")"
if [[ "$repull_present" == "t" && -z "$repull_function_sha256" ]]; then
  echo "AGENT_REPULL_FUNCTION_SHA256 is required because the cloned schema contains logo.repull_display_name(text,boolean)" >&2
  exit 2
fi

while IFS= read -r migration; do
  psql "$target_admin_dsn" -X -v ON_ERROR_STOP=1 -f "$migration"
done < <(
  find "$repo_root/sql/migrations" -maxdepth 1 -type f -name '*.sql' -print \
    | LC_ALL=C sort
)
psql "$target_admin_dsn" -X -v ON_ERROR_STOP=1 \
  -v repull_function_sha256="$repull_function_sha256" \
  -f "$repo_root/sql/logo_admin_role.sql"

psql "$target_admin_dsn" -X -v ON_ERROR_STOP=1 \
  -v database_name="$database_name" -v nonce="$nonce" <<'SQL'
CREATE TABLE fdm4.codex_test_harness (
    database_name text NOT NULL,
    nonce text NOT NULL CHECK (nonce ~ '^[0-9a-f]{32}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO fdm4.codex_test_harness (database_name, nonce)
VALUES (:'database_name', :'nonce');
REVOKE ALL ON fdm4.codex_test_harness FROM PUBLIC;
GRANT SELECT ON fdm4.codex_test_harness TO logo_admin;
SQL

# Seed once so the provisioned database is immediately inspectable. The
# autouse pytest fixture repeats reset+seed before every integration test.
psql "$target_admin_dsn" -X -v ON_ERROR_STOP=1 -f "$script_dir/sql/reset.sql"
psql "$target_admin_dsn" -X -v ON_ERROR_STOP=1 -f "$script_dir/sql/seed.sql"

# The app-role preflight is itself forced read-only and contains a terminal
# assertion, so a merely informative inventory can never approve provisioning.
PGOPTIONS='-c default_transaction_read_only=on' \
  psql "$target_app_dsn" -X -v ON_ERROR_STOP=1 \
    -v repull_function_sha256="$repull_function_sha256" \
    -f "$repo_root/sql/diagnostics/agent-write-preflight.sql"

trap - ERR INT TERM
created=false
printf 'export TEST_DATABASE_DSN=%q\n' "$target_app_dsn"
printf 'export TEST_DATABASE_ADMIN_DSN=%q\n' "$target_admin_dsn"
printf 'export TEST_DATABASE_NAME=%q\n' "$database_name"
if [[ -n "$repull_function_sha256" ]]; then
  printf 'export AGENT_REPULL_FUNCTION_SHA256=%q\n' "$repull_function_sha256"
fi
printf 'To remove it later: dropdb --force --maintenance-db=%q %q\n' "$maintenance_dsn" "$database_name"
