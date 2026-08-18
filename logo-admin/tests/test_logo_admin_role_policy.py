"""Canonical role SQL remains rerunnable and least-privileged."""

import hashlib
from pathlib import Path
import re

from database_contract import (
    EXPECTED_AUDIT_SOURCE_SHA256,
    EXPECTED_PRUNE_SOURCE_SHA256,
)


ROLE_SQL = (
    Path(__file__).resolve().parents[2]
    / "sql"
    / "logo_admin_role.sql"
)
PRUNE_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "sql"
    / "migrations"
    / "2026-07-17-agent-change-sets.sql"
)
AUDIT_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "sql"
    / "migrations"
    / "2026-07-15-logo-audit-log.sql"
)


def test_role_policy_removes_public_database_and_function_authority():
    source = ROLE_SQL.read_text()
    assert "current_database()" in source
    assert (
        "REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC, logo_admin"
        in source
    )
    assert (
        "REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA logo FROM PUBLIC"
        in source
    )
    assert (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA logo, woo, fdm4"
        in source
    )
    assert "ALTER ROLE logo_admin IN DATABASE %I RESET ALL" in source


def test_each_additive_agent_table_is_guarded_independently():
    source = ROLE_SQL.read_text()
    for table_name in (
        "agent_chat_session",
        "agent_chat_message",
        "agent_change_set",
        "agent_change_set_item",
        "agent_spreadsheet_job",
        "agent_usage_daily",
        "agent_usage_monthly",
        "agent_rate_window",
        "agent_quota_reservation",
        "agent_action_journal",
    ):
        assert f"'logo.{table_name}'" in source


def test_role_reset_removes_unknown_schema_and_column_authority():
    source = ROLE_SQL.read_text()
    assert "namespace.nspname <> 'information_schema'" in source
    assert "namespace.nspname !~ '^pg_'" in source
    assert "aclexplode(attribute.attacl)" in source
    assert "REVOKE %s (%s) ON TABLE %I.%I FROM logo_admin" in source
    assert "relation.relkind IN ('r', 'p', 'v', 'f', 'm')" in source


def test_only_reviewed_legacy_repull_signature_receives_execute():
    source = ROLE_SQL.read_text()
    assert "to_regprocedure('logo.repull_display_name(text,boolean)')" in source
    assert "logo.repull_display_name(text, boolean) TO logo_admin" in source
    assert "procedure.proname = 'repull_display_name'" not in source
    for required_fragment in (
        "repull_function_sha256",
        "expected_repull_function_sha256",
        "AND NOT procedure.prosecdef",
        "language.lanname = 'plpgsql'",
        "function_result = 'integer'",
        "encode(sha256(convert_to(",
        "logo.display_name",
        "fdm4.design_pool",
    ):
        assert required_fragment in source


def test_retention_grant_is_bound_to_canonical_definer_body():
    source = ROLE_SQL.read_text()
    assert "procedure.proconfig = ARRAY[" in source
    assert "pg_get_function_result(procedure.oid)" in source
    assert "encode(sha256(convert_to(" in source
    assert (
        "378f41091ba89926fda1364b2c99bd2901b8e01ddde9c8fa52f97b3f3f8c2269"
        in source
    )


def test_role_policy_refuses_writable_semantic_escape_hatches():
    source = ROLE_SQL.read_text()
    for required_fragment in (
        "relation.relkind = 'r'",
        "relation.relpersistence = 'p'",
        "relation.relispartition",
        "database.datdba",
        "FROM pg_trigger AS trigger",
        "trigger.tgtype::integer",
        "FROM pg_rewrite AS rewrite",
        "FROM pg_policy AS policy",
        "relation.relrowsecurity",
        "relation.relforcerowsecurity",
        "logo.audit_row()",
        EXPECTED_AUDIT_SOURCE_SHA256,
    ):
        assert required_fragment in source


def test_role_policy_validates_current_catalog_before_revoking_authority():
    source = ROLE_SQL.read_text()
    validation = source.index("DO $validate_live_contract$")
    first_revoke = source.index("REVOKE CREATE, TEMPORARY ON DATABASE")
    assert validation < first_revoke
    for required_fragment in (
        "required Warehouse Operations relations are absent",
        "attribute.attnum::integer AS ordinal_position",
        "logo.assignment must match the live 18-column",
        "required audit-trigger inventory has drifted",
        "writable trigger/rule/RLS semantics have drifted",
        "relation.relowner = database.datdba",
    ):
        assert required_fragment in source


def test_role_policy_pins_exact_undo_catalog_contract():
    source = ROLE_SQL.read_text()
    for required_fragment in (
        "(17, 'option_row', 'integer', false)",
        "(18, 'name_override', 'text', true)",
        "logo.bulk_batch_row",
        "logo.agent_action_journal",
    ):
        assert required_fragment in source


def test_runtime_retention_hash_matches_migration_body():
    match = re.search(
        r"CREATE OR REPLACE FUNCTION logo\.prune_agent_history\(\).*?"
        r"AS \$\$(.*?)\$\$;",
        PRUNE_MIGRATION.read_text(),
        re.DOTALL,
    )
    assert match is not None
    actual = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
    assert actual == EXPECTED_PRUNE_SOURCE_SHA256


def test_runtime_audit_hash_matches_migration_body():
    match = re.search(
        r"CREATE OR REPLACE FUNCTION logo\.audit_row\(\) RETURNS trigger AS "
        r"\$\$(.*?)\$\$ LANGUAGE plpgsql;",
        AUDIT_MIGRATION.read_text(),
        re.DOTALL,
    )
    assert match is not None
    actual = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
    assert actual == EXPECTED_AUDIT_SOURCE_SHA256
