"""The deployment preflight remains complete and strictly read-only."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import psycopg2
from psycopg2.extensions import parse_dsn
import pytest

from tests.conftest import harness_grants_suspended, repull_function_sha256


PREFLIGHT = (
    Path(__file__).resolve().parents[2]
    / "sql"
    / "diagnostics"
    / "agent-write-preflight.sql"
)
PROVISIONER = Path(__file__).with_name("provision_test_db.sh")
CONFTEST = Path(__file__).with_name("conftest.py")
RUNTIME_CONTRACT = Path(__file__).resolve().parents[1] / "database_contract.py"
DATABASE_POOL = Path(__file__).resolve().parents[1] / "db.py"


def _psql_environment(dsn: str) -> dict[str, str]:
    """Build a psql environment without putting a DSN secret in argv."""

    parsed = parse_dsn(dsn)
    environment = dict(os.environ)
    for inherited in (
        "PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD",
        "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS", "PGSSLMODE",
        "PGSSLROOTCERT",
    ):
        environment.pop(inherited, None)
    for source, target in (
        ("dbname", "PGDATABASE"),
        ("host", "PGHOST"),
        ("port", "PGPORT"),
        ("user", "PGUSER"),
        ("password", "PGPASSWORD"),
        ("sslmode", "PGSSLMODE"),
        ("sslrootcert", "PGSSLROOTCERT"),
    ):
        if parsed.get(source):
            environment[target] = str(parsed[source])
    environment["PGOPTIONS"] = "-c default_transaction_read_only=on"
    return environment


def _psql_binary() -> str:
    found = shutil.which("psql")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/opt/libpq/bin/psql",
        "/usr/local/opt/libpq/bin/psql",
        "/usr/lib/postgresql/16/bin/psql",
    ):
        if Path(candidate).exists():
            return candidate
    pytest.fail("psql is not installed; the preflight tests need libpq's psql")


def _run_sql_preflight() -> subprocess.CompletedProcess[str]:
    expected_hash = (
        os.environ.get("AGENT_REPULL_FUNCTION_SHA256", "").strip()
        or repull_function_sha256()
        or ""
    )
    return subprocess.run(
        [
            _psql_binary(),
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            "repull_function_sha256=" + expected_hash,
            "-f",
            str(PREFLIGHT),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_psql_environment(os.environ["TEST_DATABASE_DSN"]),
    )


@contextmanager
def _committed_schema_drift(apply_sql: str, restore_sql: str):
    """Commit one disposable-target drift and restore it even on assertion."""

    connection = psycopg2.connect(os.environ["TEST_DATABASE_ADMIN_DSN"])
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(apply_sql)
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute(restore_sql)
        finally:
            connection.close()


def _sql_without_comments() -> str:
    return "\n".join(
        line for line in PREFLIGHT.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def test_preflight_enables_fail_fast_noninteractive_psql():
    source = PREFLIGHT.read_text()
    assert "\\pset pager off" in source
    assert "\\set ON_ERROR_STOP on" in source


def test_preflight_terminally_requires_app_role_and_read_only_execution():
    source = PREFLIGHT.read_text()
    assert "execution_contract AS (" in source
    assert "current_user = 'logo_admin'" in source
    assert "session_user = 'logo_admin'" in source
    assert "current_setting('transaction_read_only') = 'on'" in source
    assert "SELECT passes FROM execution_contract" in source


def test_runtime_and_harness_reject_set_role_sessions():
    runtime = RUNTIME_CONTRACT.read_text()
    pool = DATABASE_POOL.read_text()
    harness = CONFTEST.read_text()
    assert "session_user AS session_role_name" in runtime
    assert '"session_role_name",' in runtime
    assert "SELECT current_user, session_user" in pool
    assert "session_user != EXPECTED_DATABASE_ROLE" in pool
    assert "current_database(), current_user, session_user" in harness


def test_preflight_inventories_every_agent_write_business_table():
    source = PREFLIGHT.read_text()
    for qualified_name in (
        "('logo', 'assignment')",
        "('logo', 'store_settings')",
        "('woo', 'store_pricing_tier')",
    ):
        assert qualified_name in source


def test_preflight_covers_triggers_rules_constraints_functions_and_acls():
    source = PREFLIGHT.read_text()
    for required_fragment in (
        "FROM pg_trigger",
        "WHERE NOT trigger.tgisinternal",
        "FROM pg_rules",
        "FROM pg_policy",
        "FROM pg_constraint",
        "pg_get_constraintdef",
        "FROM pg_proc",
        "namespace.nspname <> 'information_schema'",
        "pg_get_functiondef",
        "aclexplode",
        "procedure.proowner",
        "procedure.prosecdef",
        "procedure.proconfig",
        "FROM information_schema.role_table_grants",
        "grantee IN ('logo_admin', 'PUBLIC')",
    ):
        assert required_fragment in source


def test_preflight_checks_effective_role_database_and_schema_authority():
    source = PREFLIGHT.read_text()
    for required_fragment in (
        "role.rolinherit",
        "role.rolsuper",
        "role.rolcreatedb",
        "role.rolcreaterole",
        "role.rolreplication",
        "role.rolbypassrls",
        "role.rolconnlimit = 12",
        "idle_in_transaction_session_timeout=30s",
        "FROM pg_db_role_setting",
        "FROM pg_auth_members",
        "has_database_privilege",
        "'TEMPORARY'",
        "has_schema_privilege",
        "('public', NULL::boolean, false)",
    ):
        assert required_fragment in source


def test_preflight_checks_each_effective_table_and_sequence_privilege():
    source = PREFLIGHT.read_text()
    for privilege in (
        "('SELECT')",
        "('INSERT')",
        "('UPDATE')",
        "('DELETE')",
        "('TRUNCATE')",
        "('REFERENCES')",
        "('TRIGGER')",
    ):
        assert privilege in source
    assert "has_table_privilege(" in source
    assert "has_column_privilege(" in source
    assert "has_sequence_privilege(" in source
    assert source.count("'public', relation.oid, privilege_name") >= 3
    assert "AND NOT public_actual" in source
    assert "'SELECT, INSERT" not in source
    assert "'USAGE, SELECT" not in source


def _normalized_sql(text: str) -> str:
    return " ".join(text.split())


EXTENSION_OWNED_RELATION_EXCLUSION = _normalized_sql("""
    AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
    AND NOT EXISTS (
        SELECT 1
          FROM pg_depend AS dependency
         WHERE dependency.classid = 'pg_class'::regclass
           AND dependency.objid = relation.oid
           AND dependency.refclassid = 'pg_extension'::regclass
           AND dependency.deptype = 'e'
    )
""")
EXTENSION_OWNED_ROUTINE_EXCLUSION = _normalized_sql("""
    AND has_function_privilege(
        'logo_admin', procedure.oid, 'EXECUTE'
    )
    AND NOT EXISTS (
        SELECT 1
          FROM pg_depend AS dependency
         WHERE dependency.classid = 'pg_proc'::regclass
           AND dependency.objid = procedure.oid
           AND dependency.refclassid = 'pg_extension'::regclass
           AND dependency.deptype = 'e'
    )
""")


def _enclosing_privilege_enumeration(source: str, position: int) -> str:
    """Return the SELECT that owns an exclusion found at ``position``."""

    start = max(
        source.rfind("has_table_privilege(", 0, position),
        source.rfind("has_column_privilege(", 0, position),
    )
    assert start >= 0
    enumeration = source[start:position]
    assert ";" not in enumeration
    assert enumeration.count("FROM pg_class AS relation") == 1
    return enumeration


def test_preflight_skips_only_extension_owned_objects_in_privilege_inventories():
    """Production carries pg_stat_statements, whose views grant SELECT and
    whose functions grant EXECUTE to PUBLIC. Those objects belong to the
    extension (pg_depend deptype 'e'), so every effective-privilege
    enumeration skips them and nothing else."""

    source = _normalized_sql(_sql_without_comments())
    # Report-mode table and column matrices plus the terminal table_inventory
    # and column_inventory: each is an effective relation-privilege
    # enumeration, and each carries the exclusion inside its own SELECT.
    assert source.count(EXTENSION_OWNED_RELATION_EXCLUSION) == 4
    position = source.find(EXTENSION_OWNED_RELATION_EXCLUSION)
    while position >= 0:
        _enclosing_privilege_enumeration(source, position)
        position = source.find(EXTENSION_OWNED_RELATION_EXCLUSION, position + 1)
    # The callable inventory (every routine logo_admin can EXECUTE).
    assert source.count(EXTENSION_OWNED_ROUTINE_EXCLUSION) == 1
    routine_position = source.find(EXTENSION_OWNED_ROUTINE_EXCLUSION)
    assert "callable_contract AS (" in source[
        routine_position:routine_position + 600
    ]
    # Extension membership is the only pg_depend predicate, always deptype
    # 'e', and the app-schema policies are untouched.
    assert source.count("FROM pg_depend AS dependency") == 5
    assert set(re.findall(r"dependency\.deptype = '(\w)'", source)) == {"e"}
    assert source.count("dependency.refclassid = 'pg_extension'::regclass") == 5
    assert "namespace.nspname IN ('woo', 'fdm4')" in source
    assert "schema_name IN ('woo', 'fdm4')" in source
    assert "AND NOT public_actual" in source


def test_preflight_pins_retention_definer_contract():
    source = PREFLIGHT.read_text()
    for required_fragment in (
        "logo.prune_agent_history()",
        "procedure.proowner = database.datdba",
        "procedure.prokind = 'f'",
        "procedure.pronargs = 0",
        "search_path=pg_catalog, logo",
        "'public', procedure.oid, 'EXECUTE'",
        "execute_not_grantable",
        "378f41091ba89926fda1364b2c99bd2901b8e01ddde9c8fa52f97b3f3f8c2269",
    ):
        assert required_fragment in source


def test_preflight_exhaustively_rejects_unallowlisted_authority():
    source = PREFLIGHT.read_text()
    for required_fragment in (
        "relation.relkind IN ('r', 'p', 'v', 'f', 'm')",
        "namespace.nspname <> 'information_schema'",
        "namespace.nspname !~ '^pg_'",
        "namespace.nspname IN ('woo', 'fdm4')",
        "schema_name IN ('woo', 'fdm4')",
        "oidvectortypes(procedure.proargtypes)",
        "repull_display_name",
        "callable_inventory AS (",
        "callable_contract AS (",
        "definer_inventory AS (",
    ):
        assert required_fragment in source
    assert "agent_quota_reservation" in source
    assert "SELECT passes FROM callable_contract" in source


def test_preflight_terminally_asserts_writable_semantics_and_exact_undo():
    source = PREFLIGHT.read_text()
    for required_fragment in (
        "writable_relation_contract AS (",
        "relation.relpersistence <> 'p'",
        "relation.relowner IS DISTINCT FROM (",
        "restore_column_contract AS (",
        "attribute.attgenerated",
        "attribute.attidentity",
        "collation.collname AS collation_name",
        "restore_constraint_contract AS (",
        "constraint_row.confdeltype",
        "restore_unique_index_contract AS (",
        "trigger_contract AS (",
        "trigger.tgtype::integer",
        "rule_contract AS (",
        "rls_contract AS (",
        "audit_function_contract AS (",
        "logo.audit_row()",
        "0ffa5f09bd205a694dfe288347074a85195458d1eb3ae74577d4723343d7e58b",
    ):
        assert required_fragment in source
    for contract_name in (
        "writable_relation_contract",
        "restore_column_contract",
        "restore_constraint_contract",
        "restore_unique_index_contract",
        "trigger_contract",
        "rule_contract",
        "rls_contract",
        "audit_function_contract",
    ):
        assert f"SELECT passes FROM {contract_name}" in source


def test_preflight_ownership_gates_accept_superuser_owned_objects():
    """Owner passes when it is the database owner OR a superuser (production:
    etl_writer owns arb_warehouse while postgres owns every object)."""

    source = PREFLIGHT.read_text()
    gates = [
        match.start()
        for match in re.finditer(
            r"\.(?:relowner|proowner) = database\.datdba", source
        )
    ]
    # writable inventory, prune diagnostic, audit_function_contract,
    # agent_relation_contract, definer_inventory, prune_contract,
    # repull_contract
    assert len(gates) == 7
    for position in gates:
        assert "rolsuper" in source[position:position + 160]
    distinct_gate = source.index("relation.relowner IS DISTINCT FROM (")
    assert "relation_owner.rolsuper" in source[distinct_gate:distinct_gate + 800]
    assert "owner.rolsuper AS owner_is_superuser" in source
    assert "function_owner.rolsuper AS owner_is_superuser" in source
    assert "OR owner.rolsuper" in source
    assert "OR function_owner.rolsuper" in source
    assert "OR relation_owner.rolsuper" in source
    assert source.count(") AS owner_passes") == 3


def test_preflight_pins_full_agent_table_catalog_signatures():
    source = PREFLIGHT.read_text()
    for required_fragment in (
        "agent_relation_contract AS (",
        "agent_column_policy(",
        "agent_column_order_policy(",
        "agent_column_contract AS (",
        "attribute.attnum::integer AS ordinal_position",
        "array_position(",
        "default_signature",
        "agent_constraint_inventory AS (",
        "agent_constraint_contract AS (",
        "agent_index_inventory AS (",
        "agent_index_contract AS (",
        "agent_chat_message_session_id_user_login_fkey",
        "agent_change_set_item_change_set_id_call_id_key",
        "agent_spreadsheet_job_status_check",
        "agent_quota_reservation_stale_idx",
    ):
        assert required_fragment in source
    for contract_name in (
        "agent_relation_contract",
        "agent_column_contract",
        "agent_constraint_contract",
        "agent_index_contract",
    ):
        assert f"SELECT passes FROM {contract_name}" in source


def test_preflight_pins_legacy_repull_as_non_elevated_function():
    source = PREFLIGHT.read_text()
    for required_fragment in (
        "oidvectortypes(procedure.proargtypes) = 'text, boolean'",
        "AND NOT procedure.prosecdef",
        "pg_get_function_result(procedure.oid) = 'integer'",
        "logo.display_name",
        "fdm4.design_pool",
        "!~ '\\mexecute\\M'",
        "repull_function_sha256",
        "encode(sha256(convert_to(",
    ):
        assert required_fragment in source


def test_preflight_has_a_terminal_nonzero_assertion():
    source = PREFLIGHT.read_text()
    assert "ON_ERROR_STOP" in source
    assert "AS preflight_assertion" in source
    assert "SELECT 1 / CASE" in source
    assert "THEN 1 ELSE 0" in source


def test_preflight_contains_no_mutating_sql():
    source = _sql_without_comments()
    mutating_statement = re.compile(
        r"(?im)^\s*(insert|update|delete|merge|alter|drop|create|truncate|"
        r"grant|revoke|call|do)\b"
    )
    assert mutating_statement.search(source) is None


def test_provisioner_marks_fdm4_and_runs_app_preflight_read_only():
    provisioner = PROVISIONER.read_text()
    conftest = CONFTEST.read_text()
    for required_fragment in (
        "fdm4.codex_test_harness",
        "PGOPTIONS='-c default_transaction_read_only=on'",
        'psql "$target_app_dsn"',
        "agent-write-preflight.sql",
        'repull_function_sha256="${AGENT_REPULL_FUNCTION_SHA256:-}"',
    ):
        assert required_fragment in provisioner
    assert "fdm4.codex_test_harness" in conftest
    assert "public.codex_test_harness" not in provisioner
    assert "public.codex_test_harness" not in conftest


def test_sql_preflight_accepts_the_clean_disposable_target():
    with harness_grants_suspended():
        result = _run_sql_preflight()
    assert result.returncode == 0, result.stderr[-2_000:]


@pytest.mark.parametrize(
    ("drift_name", "apply_sql", "restore_sql"),
    (
        (
            "business-column",
            "ALTER TABLE logo.store_settings "
            "ALTER COLUMN updated_by DROP DEFAULT",
            "ALTER TABLE logo.store_settings "
            "ALTER COLUMN updated_by SET DEFAULT ''::text",
        ),
        (
            "foreign-key",
            "ALTER TABLE logo.agent_chat_message DROP CONSTRAINT "
            "agent_chat_message_session_id_user_login_fkey",
            "ALTER TABLE logo.agent_chat_message ADD CONSTRAINT "
            "agent_chat_message_session_id_user_login_fkey "
            "FOREIGN KEY (session_id, user_login) REFERENCES "
            "logo.agent_chat_session(id, user_login) ON DELETE CASCADE",
        ),
        (
            "check",
            "ALTER TABLE logo.agent_chat_message DROP CONSTRAINT "
            "agent_chat_message_role_check",
            "ALTER TABLE logo.agent_chat_message ADD CONSTRAINT "
            "agent_chat_message_role_check "
            "CHECK (role IN ('user', 'assistant'))",
        ),
        (
            "index",
            "DROP INDEX logo.agent_chat_message_owner_session_idx",
            "CREATE INDEX agent_chat_message_owner_session_idx ON "
            "logo.agent_chat_message "
            "(user_login, session_id, created_at DESC, id DESC)",
        ),
        (
            "trigger",
            "CREATE TRIGGER codex_agent_drift_trigger BEFORE INSERT ON "
            "logo.agent_chat_session FOR EACH ROW EXECUTE FUNCTION "
            "logo.audit_row()",
            "DROP TRIGGER codex_agent_drift_trigger ON "
            "logo.agent_chat_session",
        ),
        (
            "row-level-security",
            "ALTER TABLE logo.agent_chat_session ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE logo.agent_chat_session DISABLE ROW LEVEL SECURITY",
        ),
        (
            "grant",
            "GRANT TRUNCATE ON logo.agent_chat_session TO logo_admin",
            "REVOKE TRUNCATE ON logo.agent_chat_session FROM logo_admin",
        ),
        (
            "role-setting",
            "ALTER ROLE logo_admin SET work_mem = '64MB'",
            "ALTER ROLE logo_admin RESET work_mem",
        ),
        (
            "agent-table-column",
            "ALTER TABLE logo.agent_chat_session "
            "ALTER COLUMN title DROP DEFAULT",
            "ALTER TABLE logo.agent_chat_session "
            "ALTER COLUMN title SET DEFAULT ''::text",
        ),
    ),
)
def test_sql_preflight_rejects_each_committed_contract_drift(
    drift_name,
    apply_sql,
    restore_sql,
):
    del drift_name
    with _committed_schema_drift(apply_sql, restore_sql):
        assert _run_sql_preflight().returncode != 0


def test_sql_preflight_rejects_a_function_body_change():
    admin_dsn = os.environ["TEST_DATABASE_ADMIN_DSN"]
    with psycopg2.connect(admin_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_functiondef('logo.audit_row()'::regprocedure)"
            )
            original_definition = str(cursor.fetchone()[0])
    changed_definition = """
        CREATE OR REPLACE FUNCTION logo.audit_row()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RETURN NEW;
        END
        $function$
    """
    with _committed_schema_drift(changed_definition, original_definition):
        assert _run_sql_preflight().returncode != 0


def test_application_role_writable_cte_is_blocked_by_read_only_transaction():
    connection = psycopg2.connect(os.environ["TEST_DATABASE_DSN"])
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
                cursor.execute(
                    """
                    WITH attempted AS (
                        INSERT INTO logo.agent_chat_session (
                            id, user_login, expires_at
                        ) VALUES (%s, 'admin-one', now() + interval '1 hour')
                        RETURNING id
                    )
                    SELECT id FROM attempted
                    """,
                    (uuid.uuid4(),),
                )
        connection.rollback()
    finally:
        connection.close()
