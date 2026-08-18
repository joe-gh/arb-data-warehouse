"""Database invariants for staged mutations and append-only journals."""

import re

from db import database


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _constraints(cursor, table_name: str) -> list[tuple[str, str]]:
    cursor.execute(
        """
        SELECT contype, pg_get_constraintdef(oid, true) AS definition
          FROM pg_constraint
         WHERE conrelid = %s::regclass
         ORDER BY contype, conname
        """,
        (f"logo.{table_name}",),
    )
    return [
        (row["contype"], _normalized(row["definition"]))
        for row in cursor.fetchall()
    ]


def test_change_set_tables_have_owner_composite_foreign_keys():
    with database.cursor() as cursor:
        change_set_constraints = _constraints(cursor, "agent_change_set")
        item_constraints = _constraints(cursor, "agent_change_set_item")
        journal_constraints = _constraints(cursor, "agent_action_journal")

    assert any(
        kind == "f"
        and "foreign key (session_id, user_login)" in definition
        and "references agent_chat_session(id, user_login)" in definition
        and "on delete restrict" in definition
        for kind, definition in change_set_constraints
    )
    assert any(
        kind == "f"
        and "foreign key (change_set_id, user_login)" in definition
        and "references agent_change_set(id, user_login)" in definition
        and "on delete cascade" in definition
        for kind, definition in item_constraints
    )
    assert any(
        kind == "f"
        and "foreign key (change_set_id, user_login)" in definition
        and "references agent_change_set(id, user_login)" in definition
        and "on delete restrict" in definition
        for kind, definition in journal_constraints
    )


def test_change_set_status_hash_and_item_constraints_are_present():
    with database.cursor() as cursor:
        change_set = _constraints(cursor, "agent_change_set")
        items = _constraints(cursor, "agent_change_set_item")
        journal = _constraints(cursor, "agent_action_journal")

    change_checks = " ".join(
        definition for kind, definition in change_set if kind == "c"
    )
    item_checks = " ".join(
        definition for kind, definition in items if kind == "c"
    )
    journal_checks = " ".join(
        definition for kind, definition in journal if kind == "c"
    )

    for status in ("pending", "applied", "discarded", "undone"):
        assert status in change_checks
    assert "revision >= 0" in change_checks
    assert "[0-9a-f]{64}" in change_checks
    assert "jsonb_typeof(arguments)" in item_checks
    assert "'object'" in item_checks
    assert "sort_order >= 0" in item_checks
    assert "apply" in journal_checks
    assert "undo" in journal_checks
    assert "[0-9a-f]{64}" in journal_checks


def test_change_set_and_journal_uniqueness_is_enforced():
    with database.cursor() as cursor:
        change_set = _constraints(cursor, "agent_change_set")
        items = _constraints(cursor, "agent_change_set_item")
        journal = _constraints(cursor, "agent_action_journal")

    assert any(
        kind == "u" and "unique (id, user_login)" in definition
        for kind, definition in change_set
    )
    assert any(
        kind == "u" and "unique (change_set_id, call_id)" in definition
        for kind, definition in items
    )
    assert any(
        kind == "u" and "unique (change_set_id, sort_order)" in definition
        for kind, definition in items
    )
    assert any(
        kind == "u" and "unique (change_set_id, event_type)" in definition
        for kind, definition in journal
    )


def test_action_journal_is_append_only_for_logo_admin():
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT has_table_privilege(
                       current_user,
                       'logo.agent_action_journal',
                       'SELECT'
                   ) AS can_select,
                   has_table_privilege(
                       current_user,
                       'logo.agent_action_journal',
                       'INSERT'
                   ) AS can_insert,
                   has_table_privilege(
                       current_user,
                       'logo.agent_action_journal',
                       'UPDATE'
                   ) AS can_update,
                   has_table_privilege(
                       current_user,
                       'logo.agent_action_journal',
                       'DELETE'
                   ) AS can_delete,
                   has_table_privilege(
                       current_user,
                       'logo.agent_action_journal',
                       'TRUNCATE'
                   ) AS can_truncate
            """
        )
        privileges = cursor.fetchone()

    assert dict(privileges) == {
        "can_select": True,
        "can_insert": True,
        "can_update": False,
        "can_delete": False,
        "can_truncate": False,
    }


def test_agent_tables_use_application_uuid_ids_and_no_sequences():
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_default
              FROM information_schema.columns
             WHERE table_schema = 'logo'
               AND table_name IN (
                   'agent_change_set',
                   'agent_change_set_item',
                   'agent_action_journal'
               )
               AND column_name = 'id'
             ORDER BY table_name
            """
        )
        id_columns = cursor.fetchall()
        cursor.execute(
            """
            SELECT count(*) AS sequence_count
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'logo'
               AND c.relkind = 'S'
               AND c.relname LIKE 'agent_%'
            """
        )
        sequence_count = cursor.fetchone()["sequence_count"]

    assert len(id_columns) == 3
    assert all(row["column_default"] is None for row in id_columns)
    assert sequence_count == 0
