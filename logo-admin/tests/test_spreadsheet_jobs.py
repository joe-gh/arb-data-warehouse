"""Schema and ownership guarantees for private spreadsheet jobs."""

from datetime import datetime, timedelta, timezone
import re
from uuid import uuid4

import psycopg2
import pytest

from db import database
from routes_agent import _public_spreadsheet_result


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _constraints(cursor) -> list[tuple[str, str]]:
    cursor.execute(
        """
        SELECT contype, pg_get_constraintdef(oid, true) AS definition
          FROM pg_constraint
         WHERE conrelid = 'logo.agent_spreadsheet_job'::regclass
         ORDER BY contype, conname
        """
    )
    return [
        (row["contype"], _normalized(row["definition"]))
        for row in cursor.fetchall()
    ]


def _insert_session(cursor, session_id, user_login):
    cursor.execute(
        """
        INSERT INTO logo.agent_chat_session (
            id, user_login, title, expires_at
        ) VALUES (%s, %s, %s, %s)
        """,
        (
            session_id,
            user_login,
            "Spreadsheet fixture",
            datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )


def _insert_job(cursor, *, session_id, user_login, status="mapping_pending"):
    cursor.execute(
        """
        INSERT INTO logo.agent_spreadsheet_job (
            id, session_id, user_login, storage_key,
            original_name, media_type, byte_size, sha256,
            format_name, status, mapping_hash, mapping, expires_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s::jsonb, %s
        )
        """,
        (
            uuid4(),
            session_id,
            user_login,
            uuid4(),
            "fixture.csv",
            "text/csv",
            12,
            "a" * 64,
            "csv",
            status,
            "b" * 64,
            "{}",
            datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )


def test_spreadsheet_job_has_owner_fk_and_uuid_uniqueness():
    with database.cursor() as cursor:
        constraints = _constraints(cursor)

    assert any(
        kind == "f"
        and "foreign key (session_id, user_login)" in definition
        and "references agent_chat_session(id, user_login)" in definition
        and "on delete restrict" in definition
        for kind, definition in constraints
    )
    assert any(
        kind == "u" and "unique (id, user_login)" in definition
        for kind, definition in constraints
    )
    assert any(
        kind == "u" and "unique (storage_key)" in definition
        for kind, definition in constraints
    )


def test_spreadsheet_job_checks_allow_only_bounded_workflow_states():
    with database.cursor() as cursor:
        constraints = _constraints(cursor)

    checks = " ".join(
        definition for kind, definition in constraints if kind == "c"
    )
    for value in (
        "csv",
        "xlsx",
        "mapping_processing",
        "mapping_pending",
        "mapping_confirmed",
        "staged",
        "rejected",
        "expired",
    ):
        assert value in checks
    assert "byte_size >= 0" in checks
    assert "mapping_revision >= 1" in checks
    assert checks.count("[0-9a-f]{64}") == 2


def test_spreadsheet_job_owner_must_match_session_owner():
    session_id = uuid4()
    with database.cursor(write=True, actor="test-fixture") as cursor:
        _insert_session(cursor, session_id, "admin-one")

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with database.cursor(write=True, actor="test-fixture") as cursor:
            _insert_job(
                cursor,
                session_id=session_id,
                user_login="admin-two",
            )

    with database.cursor(write=True, actor="test-fixture") as cursor:
        _insert_job(
            cursor,
            session_id=session_id,
            user_login="admin-one",
        )


def test_spreadsheet_status_check_rejects_unknown_state():
    session_id = uuid4()
    with database.cursor(write=True, actor="test-fixture") as cursor:
        _insert_session(cursor, session_id, "admin-one")

    with pytest.raises(psycopg2.errors.CheckViolation):
        with database.cursor(write=True, actor="test-fixture") as cursor:
            _insert_job(
                cursor,
                session_id=session_id,
                user_login="admin-one",
                status="approved",
            )


def test_spreadsheet_job_has_crud_grants_and_app_generated_uuid_id():
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT has_table_privilege(
                       current_user,
                       'logo.agent_spreadsheet_job',
                       'SELECT'
                   ) AS can_select,
                   has_table_privilege(
                       current_user,
                       'logo.agent_spreadsheet_job',
                       'INSERT'
                   ) AS can_insert,
                   has_table_privilege(
                       current_user,
                       'logo.agent_spreadsheet_job',
                       'UPDATE'
                   ) AS can_update,
                   has_table_privilege(
                       current_user,
                       'logo.agent_spreadsheet_job',
                       'DELETE'
                   ) AS can_delete,
                   has_table_privilege(
                       current_user,
                       'logo.agent_spreadsheet_job',
                       'TRUNCATE'
                   ) AS can_truncate,
                   has_table_privilege(
                       current_user,
                       'logo.agent_spreadsheet_job',
                       'REFERENCES'
                   ) AS can_reference,
                   has_table_privilege(
                       current_user,
                       'logo.agent_spreadsheet_job',
                       'TRIGGER'
                   ) AS can_trigger
            """
        )
        privileges = cursor.fetchone()
        cursor.execute(
            """
            SELECT data_type, column_default
              FROM information_schema.columns
             WHERE table_schema = 'logo'
               AND table_name = 'agent_spreadsheet_job'
               AND column_name = 'id'
            """
        )
        id_column = cursor.fetchone()

    assert dict(privileges) == {
        "can_select": True,
        "can_insert": True,
        "can_update": True,
        "can_delete": True,
        "can_truncate": False,
        "can_reference": False,
        "can_trigger": False,
    }
    assert dict(id_column) == {
        "data_type": "uuid",
        "column_default": None,
    }


def test_browser_spreadsheet_projection_omits_private_storage_metadata():
    projected = _public_spreadsheet_result({
        "id": str(uuid4()),
        "storage_key": str(uuid4()),
        "sha256": "a" * 64,
        "user_login": "admin-one",
        "mapping_hash": "b" * 64,
        "change_set": {
            "id": str(uuid4()),
            "user_login": "admin-one",
            "operations": [
                {
                    "storage_key": str(uuid4()),
                    "sha256": "c" * 64,
                    "value": "visible",
                },
            ],
        },
    })
    assert "storage_key" not in projected
    assert "sha256" not in projected
    assert "user_login" not in projected
    assert projected["mapping_hash"] == "b" * 64
    assert "user_login" not in projected["change_set"]
    assert projected["change_set"]["operations"] == [{"value": "visible"}]
