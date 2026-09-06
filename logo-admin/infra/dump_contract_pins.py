#!/usr/bin/env python3
"""Read-only dump of the catalog facts the write contract pins.

Two jobs:

* ``--json`` (default) prints, for one database, exactly the facts
  ``database_contract.validate_write_database_contract`` compares - columns in
  physical order with their types, nullability and NORMALISED defaults, checks
  with normalised expressions, primary keys, foreign keys, unique indexes and
  triggers. Diff two environments with it before a deploy: a difference here
  is a difference that will refuse a write-enabled start.
* ``--check`` re-renders the generated block of
  ``sql/diagnostics/agent-write-preflight.sql`` from the contract constants and
  exits non-zero when the file does not match, so the operator preflight and
  the startup contract can never drift apart. ``--emit-generated`` prints that
  block instead of comparing it.
* ``--pins`` prints the contract constants as they should read for the given
  database, for a reviewed copy-paste after an intentional schema change.

Nothing here writes to the database or to any file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import database_contract as contract  # noqa: E402  (path set above)

PREFLIGHT_PATH = (
    APP_ROOT.parent / "sql" / "diagnostics" / "agent-write-preflight.sql"
)
BEGIN_MARKER = "-- BEGIN GENERATED (logo-admin/infra/dump_contract_pins.py)"
END_MARKER = "-- END GENERATED"

_PLAIN_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_$]*$")
_SQL_KEYWORDS = frozenset({"position", "user", "order", "check", "table"})


def quote_identifier(value: str) -> str:
    """PostgreSQL format('%I', ...): quote only when it would not round-trip."""

    if _PLAIN_IDENTIFIER.match(value) and value not in _SQL_KEYWORDS:
        return value
    return '"' + value.replace('"', '""') + '"'


def qualified(table: str) -> str:
    schema, _, name = table.partition(".")
    return f"{quote_identifier(schema)}.{quote_identifier(name)}"


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Live catalog facts
# ---------------------------------------------------------------------------

COLUMN_SQL = """
SELECT namespace.nspname || '.' || relation.relname AS table_name,
       attribute.attname AS column_name,
       attribute.attnum::integer AS ordinal_position,
       format_type(attribute.atttypid, attribute.atttypmod) AS formatted_type,
       NOT attribute.attnotnull AS nullable,
       attribute.attgenerated AS generated_kind,
       attribute.attidentity AS identity_kind,
       pg_collation.collname AS collation_name,
       pg_get_expr(default_row.adbin, default_row.adrelid, true)
           AS default_expression
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
   AND attribute.attnum > 0
   AND NOT attribute.attisdropped
  LEFT JOIN pg_collation ON pg_collation.oid = attribute.attcollation
  LEFT JOIN pg_attrdef AS default_row
    ON default_row.adrelid = relation.oid
   AND default_row.adnum = attribute.attnum
 WHERE namespace.nspname || '.' || relation.relname = ANY(%s)
 ORDER BY table_name, attribute.attnum
"""

CONSTRAINT_SQL = """
SELECT source_namespace.nspname || '.' || source.relname AS table_name,
       constraint_row.conname AS constraint_name,
       constraint_row.contype AS constraint_type,
       ARRAY(
           SELECT attribute.attname::text
             FROM unnest(constraint_row.conkey)
                  WITH ORDINALITY AS key_column(attnum, ordinal_position)
             JOIN pg_attribute AS attribute
               ON attribute.attrelid = source.oid
              AND attribute.attnum = key_column.attnum
            ORDER BY key_column.ordinal_position
       ) AS key_columns,
       CASE WHEN target.oid IS NULL THEN NULL
            ELSE target_namespace.nspname || '.' || target.relname END
           AS referenced_table,
       ARRAY(
           SELECT attribute.attname::text
             FROM unnest(constraint_row.confkey)
                  WITH ORDINALITY AS key_column(attnum, ordinal_position)
             JOIN pg_attribute AS attribute
               ON attribute.attrelid = target.oid
              AND attribute.attnum = key_column.attnum
            ORDER BY key_column.ordinal_position
       ) AS referenced_columns,
       constraint_row.confupdtype AS update_action,
       constraint_row.confdeltype AS delete_action,
       constraint_row.confmatchtype AS match_type,
       constraint_row.condeferrable AS deferrable,
       constraint_row.condeferred AS initially_deferred,
       constraint_row.convalidated AS validated,
       constraint_row.connoinherit AS no_inherit,
       pg_get_expr(constraint_row.conbin, constraint_row.conrelid, true)
           AS check_expression
  FROM pg_constraint AS constraint_row
  JOIN pg_class AS source ON source.oid = constraint_row.conrelid
  JOIN pg_namespace AS source_namespace
    ON source_namespace.oid = source.relnamespace
  LEFT JOIN pg_class AS target ON target.oid = constraint_row.confrelid
  LEFT JOIN pg_namespace AS target_namespace
    ON target_namespace.oid = target.relnamespace
 WHERE source_namespace.nspname || '.' || source.relname = ANY(%s)
 ORDER BY table_name, constraint_row.conname
"""

INDEX_SQL = """
SELECT namespace.nspname || '.' || relation.relname AS table_name,
       index_class.relname AS index_name,
       index_row.indisunique AS is_unique,
       index_row.indisprimary AS is_primary,
       (SELECT 1 FROM pg_constraint AS c
         WHERE c.conindid = index_row.indexrelid LIMIT 1) IS NOT NULL
           AS constraint_backed,
       pg_get_indexdef(index_row.indexrelid) AS definition
  FROM pg_index AS index_row
  JOIN pg_class AS relation ON relation.oid = index_row.indrelid
  JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
 WHERE namespace.nspname || '.' || relation.relname = ANY(%s)
 ORDER BY table_name, index_class.relname
"""

TRIGGER_SQL = """
SELECT namespace.nspname || '.' || relation.relname AS table_name,
       trigger_row.tgname AS trigger_name,
       trigger_row.tgtype::integer AS trigger_type,
       trigger_row.tgenabled AS enabled,
       function_namespace.nspname || '.' || function_row.proname
           AS function_name,
       trigger_row.tgnargs AS argument_count,
       trigger_row.tgqual IS NULL AS no_when_clause,
       trigger_row.tgconstraint = 0 AS not_constraint_trigger
  FROM pg_trigger AS trigger_row
  JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN pg_proc AS function_row ON function_row.oid = trigger_row.tgfoid
  JOIN pg_namespace AS function_namespace
    ON function_namespace.oid = function_row.pronamespace
 WHERE namespace.nspname || '.' || relation.relname = ANY(%s)
   AND NOT trigger_row.tgisinternal
 ORDER BY table_name, trigger_row.tgname
"""


def contract_tables() -> list[str]:
    return sorted(
        set(contract.RESTORE_COLUMN_CONTRACTS) | set(contract.AGENT_COLUMN_CONTRACTS)
    )


def collect_facts(dsn: str) -> dict:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    tables = contract_tables()
    connection = psycopg2.connect(dsn)
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(COLUMN_SQL, (tables,))
            columns = [dict(row) for row in cursor.fetchall()]
            cursor.execute(CONSTRAINT_SQL, (tables,))
            constraints = [dict(row) for row in cursor.fetchall()]
            cursor.execute(INDEX_SQL, (tables,))
            indexes = [dict(row) for row in cursor.fetchall()]
            cursor.execute(TRIGGER_SQL, (tables,))
            triggers = [dict(row) for row in cursor.fetchall()]
        connection.rollback()
    finally:
        connection.close()

    facts: dict[str, Any] = {"tables": {}, "checks": [], "primary_keys": [],
                             "unique_constraints": [], "foreign_keys": [],
                             "unique_indexes": [], "triggers": []}
    for row in columns:
        facts["tables"].setdefault(row["table_name"], []).append({
            "column": row["column_name"],
            "ordinal": int(row["ordinal_position"]),
            "type": row["formatted_type"],
            "nullable": bool(row["nullable"]),
            "generated": row["generated_kind"],
            "identity": row["identity_kind"],
            "collation": row["collation_name"],
            "default": (
                None if row["default_expression"] is None
                else contract._normalized_sql_expression(row["default_expression"])
            ),
        })
    for row in constraints:
        key = [row["table_name"], row["constraint_name"],
               list(row["key_columns"] or ())]
        kind = row["constraint_type"]
        if kind == "p":
            facts["primary_keys"].append(key)
        elif kind == "u":
            facts["unique_constraints"].append(key)
        elif kind == "c":
            facts["checks"].append(key + [
                contract._normalized_sql_expression(row["check_expression"])
            ])
        elif kind == "f":
            facts["foreign_keys"].append(key + [
                row["referenced_table"],
                list(row["referenced_columns"] or ()),
                row["update_action"], row["delete_action"], row["match_type"],
            ])
        facts.setdefault("constraint_flags", []).append([
            row["table_name"], row["constraint_name"], kind,
            bool(row["deferrable"]), bool(row["initially_deferred"]),
            bool(row["validated"]), bool(row["no_inherit"]),
        ])
    for row in indexes:
        if row["is_unique"]:
            facts["unique_indexes"].append([
                row["table_name"], row["index_name"],
                bool(row["is_primary"]), bool(row["constraint_backed"]),
                row["definition"],
            ])
    for row in triggers:
        facts["triggers"].append([
            row["table_name"], row["trigger_name"], int(row["trigger_type"]),
            row["enabled"], row["function_name"], int(row["argument_count"]),
            bool(row["no_when_clause"]), bool(row["not_constraint_trigger"]),
        ])
    return facts


# ---------------------------------------------------------------------------
# The generated half of the operator preflight
# ---------------------------------------------------------------------------

def _default_signature(value: Any) -> str:
    """The preflight stores alternatives as a '|'-separated string.

    The pins hold defaults as written; the preflight compares NORMALISED
    signatures, so normalise here with the same function the startup contract
    uses (database_contract._normalized_sql_expression).
    """

    if value is None:
        return "NULL"
    if isinstance(value, frozenset):
        joined = "|".join(
            sorted(contract._normalized_sql_expression(item) for item in value)
        )
        return sql_literal(joined)
    return sql_literal(contract._normalized_sql_expression(value))


def _constraint_signature(
    table: str,
    name: str,
    kind: str,
    key_columns: Iterable[str],
    *,
    referenced_table: str = "",
    referenced_columns: Iterable[str] = (),
    update_action: str = "",
    delete_action: str = "",
    match_type: str = "",
    check_expression: str = "",
) -> str:
    return "|".join([
        qualified(table),
        name,
        kind,
        ",".join(key_columns),
        qualified(referenced_table) if referenced_table else "",
        ",".join(referenced_columns),
        update_action,
        delete_action,
        match_type,
        check_expression,
    ])


def expected_restore_constraint_signatures() -> list[str]:
    signatures = []
    for table, (name, columns) in contract.EXPECTED_PRIMARY_KEYS.items():
        signatures.append(_constraint_signature(table, name, "p", columns))
    for table, name, columns in contract.EXPECTED_RESTORE_UNIQUE_KEYS:
        signatures.append(_constraint_signature(table, name, "u", columns))
    for (table, name, columns), expression in contract.EXPECTED_CHECKS.items():
        signatures.append(_constraint_signature(
            table, name, "c", columns, check_expression=expression,
        ))
    for (
        table, name, columns, target, target_columns,
        update_action, delete_action, match_type,
    ) in contract.EXPECTED_FOREIGN_KEYS:
        signatures.append(_constraint_signature(
            table, name, "f", columns,
            referenced_table=target,
            referenced_columns=target_columns,
            update_action=update_action,
            delete_action=delete_action,
            match_type=match_type,
        ))
    return sorted(signatures)


def render_generated_block() -> str:
    lines = [BEGIN_MARKER]
    lines.append(
        "-- Every kernel table the exact-undo restore path writes, and the"
    )
    lines.append(
        "-- column, order and constraint policy for each, taken from"
    )
    lines.append(
        "-- logo-admin/database_contract.py. Regenerate with:"
    )
    lines.append(
        "--   python logo-admin/infra/dump_contract_pins.py --emit-generated"
    )
    lines.append("restore_table_policy(schema_name, table_name) AS (")
    lines.append("    VALUES")
    rows = []
    for table in sorted(contract.RESTORE_COLUMN_CONTRACTS):
        schema, _, name = table.partition(".")
        rows.append(f"        ({sql_literal(schema)}, {sql_literal(name)})")
    lines.append(",\n".join(rows))
    lines.append("), restore_column_policy(")
    lines.append(
        "    schema_name, table_name, column_name, formatted_type, nullable,"
    )
    lines.append("    identity_kind, default_signature")
    lines.append(") AS (")
    lines.append("    VALUES")
    rows = []
    for table in sorted(contract.RESTORE_COLUMN_CONTRACTS):
        schema, _, name = table.partition(".")
        for column, (formatted_type, nullable, default) in (
            contract.RESTORE_COLUMN_CONTRACTS[table].items()
        ):
            identity = contract.RESTORE_IDENTITY_COLUMNS.get((table, column), "")
            rows.append(
                f"        ({sql_literal(schema)}, {sql_literal(name)},"
                f" {sql_literal(column)}, {sql_literal(formatted_type)},"
                f" {'true' if nullable else 'false'},"
                f" {sql_literal(identity)},"
                f" {_default_signature(default)})"
            )
    lines.append(",\n".join(rows))
    lines.append(
        "), restore_column_order_policy(schema_name, table_name, column_names)"
        " AS ("
    )
    lines.append("    VALUES")
    rows = []
    for table in sorted(contract.RESTORE_COLUMN_CONTRACTS):
        schema, _, name = table.partition(".")
        columns = ", ".join(
            sql_literal(column)
            for column in contract.RESTORE_COLUMN_CONTRACTS[table]
        )
        rows.append(
            f"        ({sql_literal(schema)}, {sql_literal(name)},"
            f" ARRAY[{columns}]::text[])"
        )
    lines.append(",\n".join(rows))
    lines.append("), restore_constraint_policy(signature) AS (")
    lines.append("    VALUES")
    rows = [
        f"        ({sql_literal(signature)})"
        for signature in expected_restore_constraint_signatures()
    ]
    lines.append(",\n".join(rows))
    lines.append(
        "), restore_unique_index_policy(table_name, index_name, definition)"
        " AS ("
    )
    lines.append("    VALUES")
    rows = [
        f"        ({sql_literal(table)}, {sql_literal(index_name)},"
        f"\n         {sql_literal(definition)})"
        for (table, index_name), definition in sorted(
            contract.EXPECTED_RESTORE_UNIQUE_INDEXES.items()
        )
    ]
    lines.append(",\n".join(rows))
    lines.append("),")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def current_generated_block(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER, start + 1)
    if start < 0 or end < 0:
        return None
    return text[start:end + len(END_MARKER)] + "\n"


# ---------------------------------------------------------------------------
# Pin regeneration (reviewed copy-paste)
# ---------------------------------------------------------------------------

def render_pins(facts: Mapping[str, Any]) -> str:
    out = ["# Regenerated pins. Review every line before pasting.", ""]
    out.append("RESTORE_COLUMN_CONTRACTS defaults / AGENT_COLUMN_CONTRACTS defaults:")
    for table in sorted(facts["tables"]):
        out.append(f"    {table!r}: {{")
        for column in facts["tables"][table]:
            out.append(
                f"        {column['column']!r}: ("
                f"{column['type']!r}, {column['nullable']!r}, "
                f"{column['default']!r}),"
            )
        out.append("    },")
    out.append("")
    out.append("EXPECTED_CHECKS = {")
    for table, name, columns, expression in sorted(facts["checks"]):
        out.append(
            f"    ({table!r}, {name!r}, {tuple(columns)!r}): {expression!r},"
        )
    out.append("}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_DSN", ""),
        help="libpq connection string (default: $DATABASE_DSN)",
    )
    parser.add_argument(
        "--preflight",
        default=str(PREFLIGHT_PATH),
        help="path to agent-write-preflight.sql",
    )
    parser.add_argument("--json", action="store_true", help="print catalog facts (default)")
    parser.add_argument("--pins", action="store_true", help="print pins for review")
    parser.add_argument(
        "--emit-generated",
        action="store_true",
        help="print the preflight's generated block",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the preflight's generated block is stale",
    )
    args = parser.parse_args(argv)

    if args.emit_generated:
        sys.stdout.write(render_generated_block())
        return 0

    if args.check:
        path = Path(args.preflight)
        current = current_generated_block(path)
        expected = render_generated_block()
        if current is None:
            sys.stderr.write(
                f"{path}: no generated block; expected markers "
                f"{BEGIN_MARKER!r} / {END_MARKER!r}\n"
            )
            return 1
        if current != expected:
            sys.stderr.write(
                f"{path}: generated block is stale; regenerate with "
                "--emit-generated\n"
            )
            return 1
        sys.stdout.write("preflight generated block matches the contract\n")
        return 0

    if not args.dsn:
        sys.stderr.write("a DSN is required (--dsn or $DATABASE_DSN)\n")
        return 2
    facts = collect_facts(args.dsn)
    if args.pins:
        sys.stdout.write(render_pins(facts))
        return 0
    json.dump(facts, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
