"""Write-enabled startup rejects excessive or incomplete DB authority."""

import hashlib
import os

import pytest

from database_contract import (
    AGENT_CHECKS,
    AGENT_COLUMN_CONTRACTS,
    AGENT_FOREIGN_KEYS,
    AGENT_PRIMARY_KEYS,
    AGENT_UNIQUE_CONSTRAINTS,
    EXPECTED_AUDIT_SOURCE_SHA256,
    EXPECTED_CHECKS,
    EXPECTED_FOREIGN_KEYS,
    EXPECTED_PRIMARY_KEYS,
    EXPECTED_PRUNE_SOURCE_SHA256,
    RESTORE_COLUMN_CONTRACTS,
    TABLE_POLICIES,
    _assert_audit_function_contract,
    _assert_callable_inventory,
    _assert_agent_column_signatures,
    _assert_agent_constraint_signatures,
    _assert_agent_index_signatures,
    _assert_agent_relation_signatures,
    _assert_column_privileges,
    _assert_prune_contract,
    _assert_repull_contract,
    _assert_restore_column_contract,
    _assert_restore_constraint_contract,
    _assert_security_definer_inventory,
    _assert_table_privileges,
    _assert_trigger_inventory,
    _assert_write_relation_shapes,
    _expected_table_privileges,
    _expected_agent_indexes,
    validate_write_database_contract,
)
from db import database


def _table_rows():
    return [
        {
            "table_name": table_name,
            "privilege_name": privilege,
            "allowed": allowed,
        }
        for (table_name, privilege), allowed
        in _expected_table_privileges().items()
    ]


def _safe_prune_row():
    return {
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "procedure_kind": "f",
        "argument_count": 0,
        "language_name": "plpgsql",
        "security_definer": True,
        "fixed_settings": ["search_path=pg_catalog, logo"],
        "function_result": (
            "TABLE(journals_deleted bigint, change_sets_deleted bigint)"
        ),
        "source_sha256": EXPECTED_PRUNE_SOURCE_SHA256,
        "app_execute": True,
        "public_execute": False,
        "app_execute_grantable": False,
    }


def test_provisioned_application_role_satisfies_write_contract():
    with database.cursor() as cursor:
        validate_write_database_contract(
            cursor,
            expected_repull_sha256=(
                os.environ.get("AGENT_REPULL_FUNCTION_SHA256", "").strip()
                or None
            ),
        )


@pytest.mark.parametrize(
    ("table_name", "privilege", "unsafe_value"),
    (
        ("logo.assignment", "UPDATE", False),
        ("logo.agent_chat_session", "TRUNCATE", True),
        ("logo.audit_log", "DELETE", True),
        ("logo.agent_action_journal", "UPDATE", True),
    ),
)
def test_table_contract_rejects_missing_or_excessive_effective_privilege(
    table_name,
    privilege,
    unsafe_value,
):
    rows = _table_rows()
    for row in rows:
        if (
            row["table_name"] == table_name
            and row["privilege_name"] == privilege
        ):
            row["allowed"] = unsafe_value
            break
    with pytest.raises(RuntimeError, match="effective table privilege mismatch"):
        _assert_table_privileges(rows)


def test_table_contract_rejects_incomplete_inventory():
    with pytest.raises(RuntimeError, match="incomplete table privilege inventory"):
        _assert_table_privileges(_table_rows()[:-1])


def test_table_contract_rejects_write_on_unallowlisted_schema_table():
    rows = _table_rows()
    rows.extend({
        "table_name": "woo.unreviewed_table",
        "privilege_name": privilege,
        "allowed": privilege == "INSERT",
    } for privilege in (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    ))
    with pytest.raises(RuntimeError, match="unexpected_privileges"):
        _assert_table_privileges(rows)


def test_table_contract_requires_select_on_unlisted_warehouse_relation():
    rows = _table_rows()
    rows.extend({
        "table_name": "fdm4.new_fact",
        "privilege_name": privilege,
        "allowed": False,
    } for privilege in (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    ))
    with pytest.raises(RuntimeError, match="unexpected_privileges"):
        _assert_table_privileges(rows)


def test_table_contract_rejects_select_on_unlisted_logo_relation():
    rows = _table_rows()
    rows.extend({
        "table_name": "logo.unreviewed_private_state",
        "privilege_name": privilege,
        "allowed": privilege == "SELECT",
    } for privilege in (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    ))
    with pytest.raises(RuntimeError, match="unexpected_privileges"):
        _assert_table_privileges(rows)


@pytest.mark.parametrize(
    ("table_name", "column_name", "privilege"),
    (
        ("custom.updatable_view", "payload", "UPDATE"),
        ("custom.foreign_orders", "payload", "INSERT"),
        ("logo.audit_log", "detail", "UPDATE"),
        ("logo.assignment", "fdm4_store", "REFERENCES"),
    ),
)
def test_column_contract_rejects_unreviewed_or_excessive_column_grant(
    table_name,
    column_name,
    privilege,
):
    with pytest.raises(RuntimeError, match="effective column privilege mismatch"):
        _assert_column_privileges([{
            "table_name": table_name,
            "column_name": column_name,
            "privilege_name": privilege,
            "allowed": True,
        }])


def test_column_contract_requires_warehouse_read_and_denies_private_read():
    with pytest.raises(RuntimeError, match="effective column privilege mismatch"):
        _assert_column_privileges([{
            "table_name": "fdm4.new_fact",
            "column_name": "payload",
            "privilege_name": "SELECT",
            "allowed": False,
        }])
    with pytest.raises(RuntimeError, match="effective column privilege mismatch"):
        _assert_column_privileges([{
            "table_name": "public.private_fact",
            "column_name": "payload",
            "privilege_name": "SELECT",
            "allowed": True,
        }])


def _safe_relation_rows():
    return [
        {
            "table_name": table_name,
            "relation_kind": "r",
            "persistence": "p",
            "is_partition": False,
            "owned_by_database_owner": True,
        }
        for table_name in TABLE_POLICIES
    ]


@pytest.mark.parametrize("unsafe_kind", ("v", "f", "p"))
def test_writable_relation_contract_rejects_view_foreign_or_partitioned_table(
    unsafe_kind,
):
    rows = _safe_relation_rows()
    rows[0]["relation_kind"] = unsafe_kind
    with pytest.raises(RuntimeError, match="writable relation shape drift"):
        _assert_write_relation_shapes(rows)


def test_writable_relation_contract_rejects_non_database_owner():
    rows = _safe_relation_rows()
    rows[0]["owned_by_database_owner"] = False
    with pytest.raises(RuntimeError, match="writable relation shape drift"):
        _assert_write_relation_shapes(rows)


def _safe_restore_column_rows():
    return [
        {
            "table_name": table_name,
            "column_name": column_name,
            "ordinal_position": ordinal_position,
            "formatted_type": formatted_type,
            "nullable": nullable,
            "generated_kind": "",
            "identity_kind": "",
            "collation_name": "default" if formatted_type == "text" else None,
            "default_expression": default,
        }
        for table_name, columns in RESTORE_COLUMN_CONTRACTS.items()
        for ordinal_position, (
            column_name,
            (formatted_type, nullable, default),
        ) in enumerate(columns.items(), start=1)
    ]


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("ordinal_position", 99),
        ("formatted_type", "varchar(255)"),
        ("nullable", True),
        ("default_expression", "gen_random_uuid()"),
        ("generated_kind", "s"),
        ("identity_kind", "d"),
        ("collation_name", "C"),
    ),
)
def test_restore_column_contract_rejects_metadata_drift(field, unsafe_value):
    rows = _safe_restore_column_rows()
    rows[0][field] = unsafe_value
    with pytest.raises(RuntimeError, match="exact-undo column metadata drift"):
        _assert_restore_column_contract(rows)


def _constraint_row(**overrides):
    row = {
        "table_name": "",
        "constraint_name": "",
        "constraint_type": "",
        "key_columns": [],
        "referenced_table": None,
        "referenced_columns": [],
        "update_action": " ",
        "delete_action": " ",
        "match_type": " ",
        "deferrable": False,
        "initially_deferred": False,
        "validated": True,
        "no_inherit": False,
        "check_expression": None,
    }
    row.update(overrides)
    return row


def _safe_constraint_rows():
    rows = [
        _constraint_row(
            table_name=table_name,
            constraint_name=constraint_name,
            constraint_type="p",
            key_columns=list(key_columns),
        )
        for table_name, (constraint_name, key_columns)
        in EXPECTED_PRIMARY_KEYS.items()
    ]
    rows.extend(
        _constraint_row(
            table_name=table_name,
            constraint_name=constraint_name,
            constraint_type="c",
            key_columns=list(key_columns),
            check_expression=expression,
        )
        for (
            table_name,
            constraint_name,
            key_columns,
        ), expression in EXPECTED_CHECKS.items()
    )
    rows.extend(
        _constraint_row(
            table_name=table_name,
            constraint_name=constraint_name,
            constraint_type="f",
            key_columns=list(key_columns),
            referenced_table=referenced_table,
            referenced_columns=list(referenced_columns),
            update_action=update_action,
            delete_action=delete_action,
            match_type=match_type,
        )
        for (
            table_name,
            constraint_name,
            key_columns,
            referenced_table,
            referenced_columns,
            update_action,
            delete_action,
            match_type,
        ) in EXPECTED_FOREIGN_KEYS
    )
    return rows


def test_restore_constraint_contract_rejects_incoming_cascade():
    rows = _safe_constraint_rows()
    rows.append(_constraint_row(
        table_name="custom.dependent",
        constraint_name="dependent_assignment_fkey",
        constraint_type="f",
        key_columns=["assignment_id"],
        referenced_table="logo.assignment",
        referenced_columns=["fdm4_store"],
        delete_action="c",
        update_action="a",
        match_type="s",
    ))
    with pytest.raises(RuntimeError, match="exact-undo constraint drift"):
        _assert_restore_constraint_contract(rows)


def _safe_trigger_rows():
    return [{
        "table_name": table_name,
        "trigger_name": trigger_name,
        "trigger_type": trigger_type,
        "enabled": enabled,
        "function_schema": function_schema,
        "function_name": function_name,
        "argument_types": "",
        "argument_count": 0,
        "no_when_clause": True,
        "not_constraint_trigger": True,
    } for (
        table_name, trigger_name, trigger_type, enabled,
        function_schema, function_name,
    ) in (
        (
            "logo.assignment", "logo_assignment_audit", 29, "O",
            "logo", "audit_row",
        ),
        (
            "logo.store_settings", "logo_store_settings_audit", 29, "O",
            "logo", "audit_row",
        ),
        (
            "logo.color_class", "logo_color_class_audit", 29, "O",
            "logo", "audit_row",
        ),
        (
            "logo.display_name", "logo_display_name_audit", 29, "O",
            "logo", "audit_display_name_row",
        ),
        (
            "woo.price_rule", "price_rule_audit", 29, "O",
            "woo", "audit_price_rule_row",
        ),
    )]


def test_trigger_contract_rejects_unexpected_mutating_trigger():
    rows = _safe_trigger_rows()
    rows.append({
        **rows[0],
        "trigger_name": "unreviewed_side_effect",
        "function_schema": "custom",
        "function_name": "mutate_other_table",
    })
    with pytest.raises(RuntimeError, match="trigger inventory drift"):
        _assert_trigger_inventory(rows)


def _safe_audit_function_row():
    return {
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "procedure_kind": "f",
        "argument_count": 0,
        "language_name": "plpgsql",
        "security_definer": False,
        "fixed_settings": None,
        "function_result": "trigger",
        "source_sha256": EXPECTED_AUDIT_SOURCE_SHA256,
        "app_execute": False,
        "public_execute": False,
    }


def test_audit_trigger_function_contract_rejects_body_drift():
    row = _safe_audit_function_row()
    row["source_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="audit trigger function body drift"):
        _assert_audit_function_contract(row)


def _safe_agent_relation_rows():
    return [{
        "table_name": table_name,
        "relation_kind": "r",
        "persistence": "p",
        "is_partition": False,
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "access_method": "heap",
        "relation_options": None,
        "replica_identity": "d",
        "tablespace_oid": 0,
    } for table_name in AGENT_COLUMN_CONTRACTS]


def test_agent_relation_signature_rejects_non_owner_or_custom_storage():
    rows = _safe_agent_relation_rows()
    rows[0]["owner_name"] = "logo_admin"
    with pytest.raises(RuntimeError, match="agent relation metadata drift"):
        _assert_agent_relation_signatures(rows)


def _safe_agent_column_rows():
    rows = []
    for table_name, columns in AGENT_COLUMN_CONTRACTS.items():
        for ordinal_position, (
            column_name,
            (formatted_type, nullable, default),
        ) in enumerate(columns.items(), start=1):
            if isinstance(default, frozenset):
                default = sorted(default)[0]
            rows.append({
                "table_name": table_name,
                "column_name": column_name,
                "ordinal_position": ordinal_position,
                "formatted_type": formatted_type,
                "nullable": nullable,
                "generated_kind": "",
                "identity_kind": "",
                "collation_name": (
                    "default" if formatted_type == "text" else None
                ),
                "default_expression": default,
            })
    return rows


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("ordinal_position", 99),
        ("nullable", True),
        ("formatted_type", "text"),
        ("default_expression", "gen_random_uuid()"),
        ("generated_kind", "s"),
        ("collation_name", "C"),
    ),
)
def test_agent_column_signature_rejects_schema_drift(field, unsafe_value):
    rows = _safe_agent_column_rows()
    rows[0][field] = unsafe_value
    with pytest.raises(RuntimeError, match="agent column signature drift"):
        _assert_agent_column_signatures(rows)


def _safe_agent_constraint_rows():
    rows = [
        _constraint_row(
            table_name=table_name,
            constraint_name=constraint_name,
            constraint_type="p",
            key_columns=list(key_columns),
        )
        for table_name, (constraint_name, key_columns)
        in AGENT_PRIMARY_KEYS.items()
    ]
    rows.extend(
        _constraint_row(
            table_name=table_name,
            constraint_name=constraint_name,
            constraint_type="u",
            key_columns=list(key_columns),
        )
        for table_name, constraint_name, key_columns
        in AGENT_UNIQUE_CONSTRAINTS
    )
    rows.extend(
        _constraint_row(
            table_name=table_name,
            constraint_name=constraint_name,
            constraint_type="c",
            key_columns=list(key_columns),
            check_expression=expression,
        )
        for (
            table_name,
            constraint_name,
            key_columns,
        ), expression in AGENT_CHECKS.items()
    )
    rows.extend(
        _constraint_row(
            table_name=table_name,
            constraint_name=constraint_name,
            constraint_type="f",
            key_columns=list(key_columns),
            referenced_table=referenced_table,
            referenced_columns=list(referenced_columns),
            update_action=update_action,
            delete_action=delete_action,
            match_type=match_type,
        )
        for (
            table_name,
            constraint_name,
            key_columns,
            referenced_table,
            referenced_columns,
            update_action,
            delete_action,
            match_type,
        ) in AGENT_FOREIGN_KEYS
    )
    return rows


def test_agent_constraint_signature_rejects_owner_scope_fk_drift():
    rows = _safe_agent_constraint_rows()
    for row in rows:
        if row["constraint_type"] == "f":
            row["delete_action"] = "c" if row["delete_action"] != "c" else "r"
            break
    with pytest.raises(RuntimeError, match="agent constraint signature drift"):
        _assert_agent_constraint_signatures(rows)


def _safe_agent_index_rows():
    return [{
        "table_name": table_name,
        "index_name": index_name,
        "is_unique": signature[0],
        "is_primary": signature[1],
        "key_columns": list(signature[2]),
        "key_options": list(signature[3]),
        "predicate": signature[4],
        "access_method": "btree",
        "is_valid": True,
        "is_ready": True,
        "is_live": True,
        "is_clustered": False,
        "is_replica_identity": False,
        "nulls_not_distinct": False,
        "has_expressions": False,
        "key_attribute_count": len(signature[2]),
        "attribute_count": len(signature[2]),
        "tablespace_oid": 0,
    } for (table_name, index_name), signature in _expected_agent_indexes().items()]


def test_agent_index_signature_rejects_unreviewed_unique_index():
    rows = _safe_agent_index_rows()
    rows.append({
        **rows[0],
        "index_name": "unreviewed_unique",
        "is_primary": False,
    })
    with pytest.raises(RuntimeError, match="agent index signature drift"):
        _assert_agent_index_signatures(rows)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("owner_name", "logo_admin"),
        ("security_definer", False),
        ("fixed_settings", None),
        ("app_execute", False),
        ("public_execute", True),
        ("app_execute_grantable", True),
        ("source_sha256", "0" * 64),
    ),
)
def test_retention_contract_rejects_owner_acl_or_definer_drift(
    field,
    unsafe_value,
):
    row = _safe_prune_row()
    row[field] = unsafe_value
    with pytest.raises(RuntimeError, match="unsafe write-enabled database contract"):
        _assert_prune_contract(row)


def test_security_definer_inventory_rejects_unreviewed_executable_function():
    rows = [{
        "schema_name": "logo",
        "function_name": "prune_agent_history",
        "argument_types": "",
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "public_execute": False,
        "app_execute_grantable": False,
    }, {
        "schema_name": "woo",
        "function_name": "unreviewed_mutation",
        "argument_types": "text",
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "public_execute": False,
        "app_execute_grantable": False,
    }]
    with pytest.raises(RuntimeError, match="unexpected=.*unreviewed_mutation"):
        _assert_security_definer_inventory(rows)


def test_callable_inventory_rejects_unreviewed_invoker_function():
    rows = [{
        "schema_name": "logo",
        "routine_name": "prune_agent_history",
        "argument_types": "",
        "routine_kind": "f",
    }, {
        "schema_name": "public",
        "routine_name": "unreviewed_invoker",
        "argument_types": "text",
        "routine_kind": "f",
    }]
    with pytest.raises(RuntimeError, match="unreviewed_invoker"):
        _assert_callable_inventory(rows)


def test_security_definer_inventory_rejects_legacy_repull_elevation():
    rows = [{
        "schema_name": "logo",
        "function_name": "prune_agent_history",
        "argument_types": "",
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "public_execute": False,
        "app_execute_grantable": False,
    }, {
        "schema_name": "logo",
        "function_name": "repull_display_name",
        "argument_types": "text, boolean",
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "public_execute": False,
        "app_execute_grantable": False,
    }]
    with pytest.raises(RuntimeError, match="repull_display_name"):
        _assert_security_definer_inventory(rows)


def _safe_repull_row():
    return {
        "argument_types": "text, boolean",
        "owner_name": "test_database_owner",
        "database_owner": "test_database_owner",
        "procedure_kind": "f",
        "language_name": "plpgsql",
        "security_definer": False,
        "fixed_settings": None,
        "function_result": "integer",
        "app_execute": True,
        "public_execute": False,
        "app_execute_grantable": False,
        "definition": """
            CREATE FUNCTION logo.repull_display_name(text, boolean)
            RETURNS integer LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO logo.display_name
                SELECT * FROM fdm4.design_pool;
                RETURN 1;
            END $$;
        """,
    }


def _repull_hash(row):
    return hashlib.sha256(row["definition"].encode("utf-8")).hexdigest()


def test_repull_contract_is_narrow_and_security_invoker():
    row = _safe_repull_row()
    _assert_repull_contract(
        [row],
        expected_definition_sha256=_repull_hash(row),
    )


def test_repull_contract_requires_reviewed_definition_hash():
    with pytest.raises(RuntimeError, match="AGENT_REPULL_FUNCTION_SHA256"):
        _assert_repull_contract([_safe_repull_row()])


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    (
        ("argument_types", "text, text"),
        ("owner_name", "logo_admin"),
        ("language_name", "c"),
        ("security_definer", True),
        ("function_result", "void"),
        ("public_execute", True),
    ),
)
def test_repull_contract_rejects_metadata_drift(field, unsafe_value):
    row = _safe_repull_row()
    row[field] = unsafe_value
    with pytest.raises(RuntimeError, match="unsafe write-enabled database contract"):
        _assert_repull_contract(
            [row],
            expected_definition_sha256=_repull_hash(row),
        )


def test_repull_contract_rejects_dynamic_sql():
    row = _safe_repull_row()
    row["definition"] += "\nEXECUTE 'DELETE FROM logo.assignment';"
    with pytest.raises(RuntimeError, match="dynamic SQL"):
        _assert_repull_contract(
            [row],
            expected_definition_sha256=_repull_hash(row),
        )


def test_repull_contract_rejects_static_dml_definition_drift():
    row = _safe_repull_row()
    reviewed_hash = _repull_hash(row)
    row["definition"] += "\nDELETE FROM logo.assignment;"
    with pytest.raises(RuntimeError, match="SHA-256 drift"):
        _assert_repull_contract(
            [row],
            expected_definition_sha256=reviewed_hash,
        )
