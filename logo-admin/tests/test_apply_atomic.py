"""Confirmed batches apply in one transaction or not at all."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import staging
from db import database
from staging import apply_change_set, get_change_set, new_change_set, stage_write


def _fixture():
    session_id = uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                "admin-one",
                "apply fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog "
            "WHERE suggested=true ORDER BY fdm4_store LIMIT 1"
        )
        store = cursor.fetchone()["fdm4_store"]
    return new_change_set(session_id, "admin-one"), store


def _stage_two(change_set_id, store):
    stage_write(
        change_set_id,
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": False},
        "apply-1",
        "admin-one",
        max_items=50,
    )
    return stage_write(
        change_set_id,
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "apply-2",
        "admin-one",
        max_items=50,
    )


def _settings(store):
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT to_jsonb(s) AS row FROM logo.store_settings s WHERE fdm4_store=%s",
            (store,),
        )
        row = cursor.fetchone()
        return dict(row["row"]) if row else None


def test_two_items_apply_atomically_with_one_apply_journal():
    change_set, store = _fixture()
    staged = _stage_two(change_set["id"], store)
    result = apply_change_set(
        change_set["id"],
        "admin-one",
        revision=staged["revision"],
        confirmed_hash=staged["preview_hash"],
        acknowledge_hard_delete=False,
    )
    assert result["status"] == "applied"
    assert _settings(store)["allows_none"] is True
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT event_type, before_state, after_state "
            "FROM logo.agent_action_journal WHERE change_set_id=%s",
            (change_set["id"],),
        )
        journals = cursor.fetchall()
    assert len(journals) == 1
    assert journals[0]["event_type"] == "apply"
    assert journals[0]["before_state"] != journals[0]["after_state"]


def test_second_item_failure_rolls_back_first_and_journal(monkeypatch):
    change_set, store = _fixture()
    staged = _stage_two(change_set["id"], store)
    before = _settings(store)
    original = staging.dispatch_mutation
    calls = 0

    def fail_second(cursor, actor, command):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second-item failure")
        return original(cursor, actor, command)

    monkeypatch.setattr(staging, "dispatch_mutation", fail_second)
    with pytest.raises(RuntimeError, match="second-item"):
        apply_change_set(
            change_set["id"],
            "admin-one",
            revision=staged["revision"],
            confirmed_hash=staged["preview_hash"],
            acknowledge_hard_delete=False,
        )
    assert _settings(store) == before
    persisted = get_change_set(change_set["id"], "admin-one")
    assert persisted["status"] == "pending"
    assert persisted["journal"] == []
