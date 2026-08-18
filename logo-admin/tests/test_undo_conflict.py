"""Undo refuses to overwrite edits made after the recorded apply."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from commands import UpdateStoreSettingsCommand
from db import database
from domain import Conflict
from mutations import update_store_settings
from staging import apply_change_set, new_change_set, stage_write, undo_change_set


def _applied_settings_change():
    session_id = uuid4()
    with database.cursor(write=True, actor="baseline") as cursor:
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog WHERE suggested=true ORDER BY 1 LIMIT 1"
        )
        store = cursor.fetchone()["fdm4_store"]
        update_store_settings(
            cursor,
            "baseline",
            UpdateStoreSettingsCommand(store=store, enabled=True, allows_none=False),
        )
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                "admin-one",
                "undo conflict",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    change_set = new_change_set(session_id, "admin-one")
    staged = stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "undo-conflict",
        "admin-one",
        max_items=50,
    )
    apply_change_set(
        change_set["id"],
        "admin-one",
        revision=staged["revision"],
        confirmed_hash=staged["preview_hash"],
        acknowledge_hard_delete=False,
    )
    return change_set, store


def test_intervening_human_edit_causes_undo_conflict_and_is_preserved():
    change_set, store = _applied_settings_change()
    with database.cursor(write=True, actor="later-human") as cursor:
        update_store_settings(
            cursor,
            "later-human",
            UpdateStoreSettingsCommand(store=store, enabled=True, allows_none=True),
        )
    with pytest.raises(Conflict, match="changed after apply"):
        undo_change_set(change_set["id"], "admin-one")
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT enabled,allows_none,updated_by FROM logo.store_settings WHERE fdm4_store=%s",
            (store,),
        )
        assert dict(cursor.fetchone()) == {
            "enabled": True,
            "allows_none": True,
            "updated_by": "later-human",
        }
        cursor.execute(
            "SELECT status FROM logo.agent_change_set WHERE id=%s",
            (change_set["id"],),
        )
        assert cursor.fetchone()["status"] == "applied"


def test_second_undo_is_rejected():
    change_set, _store = _applied_settings_change()
    undo_change_set(change_set["id"], "admin-one")
    with pytest.raises(Conflict, match="Only an applied"):
        undo_change_set(change_set["id"], "admin-one")
