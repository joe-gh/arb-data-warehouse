"""Hard deletes are distinct tools with an additional human acknowledgement."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from db import database
from domain import HardDeleteAcknowledgementRequired
from staging import apply_change_set, new_change_set, stage_write


def _hard_delete_fixture():
    session_id = uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            """
            SELECT * FROM logo.assignment
             WHERE position=1
             ORDER BY fdm4_store,product_style,garment_color_code,option_row
             LIMIT 1
            """
        )
        assignment = dict(cursor.fetchone())
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                "admin-one",
                "hard delete fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    change_set = new_change_set(session_id, "admin-one")
    arguments = {
        key: assignment[key]
        for key in (
            "fdm4_store", "product_style", "garment_color_code",
            "position", "option_row",
        )
    }
    staged = stage_write(
        change_set["id"],
        "hard_delete_assignment",
        arguments,
        "hard-delete-call",
        "admin-one",
        max_items=50,
    )
    return change_set, staged, arguments


def _remaining(arguments):
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS count FROM logo.assignment
             WHERE fdm4_store=%(fdm4_store)s
               AND product_style=%(product_style)s
               AND garment_color_code=%(garment_color_code)s
               AND option_row=%(option_row)s
            """,
            arguments,
        )
        return cursor.fetchone()["count"]


def test_hard_delete_without_acknowledgement_changes_nothing():
    change_set, staged, arguments = _hard_delete_fixture()
    before = _remaining(arguments)
    assert staged["contains_hard_delete"] is True
    with pytest.raises(HardDeleteAcknowledgementRequired):
        apply_change_set(
            change_set["id"],
            "admin-one",
            revision=staged["revision"],
            confirmed_hash=staged["preview_hash"],
            acknowledge_hard_delete=False,
        )
    assert _remaining(arguments) == before


def test_hard_delete_with_acknowledgement_applies():
    change_set, staged, arguments = _hard_delete_fixture()
    result = apply_change_set(
        change_set["id"],
        "admin-one",
        revision=staged["revision"],
        confirmed_hash=staged["preview_hash"],
        acknowledge_hard_delete=True,
    )
    assert result["status"] == "applied"
    assert _remaining(arguments) == 0
