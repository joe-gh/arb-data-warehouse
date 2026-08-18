"""Optimistic stage retries prevent duplicate or lost change-set items."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from db import database
from domain import InvalidCommand, NotFound
from staging import get_change_set, new_change_set, stage_write


def _change_set(user="admin-one"):
    session_id = uuid4()
    with database.cursor(write=True, actor=user) as cursor:
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                user,
                "concurrency fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog "
            "WHERE suggested=true ORDER BY fdm4_store LIMIT 1"
        )
        store = cursor.fetchone()["fdm4_store"]
    return new_change_set(session_id, user), store


def _stage(change_set_id, store, call_id, enabled):
    return stage_write(
        change_set_id,
        "update_store_settings",
        {"store": store, "enabled": enabled, "allows_none": not enabled},
        call_id,
        "admin-one",
        max_items=50,
    )


def test_duplicate_call_id_is_idempotent():
    change_set, store = _change_set()
    first = _stage(change_set["id"], store, "same-call", False)
    second = _stage(change_set["id"], store, "same-call", False)
    assert second["revision"] == first["revision"] == 1
    assert second["items"] == 1
    persisted = get_change_set(change_set["id"], "admin-one")
    assert len(persisted["items"]) == 1
    assert persisted["items"][0]["arguments"]["enabled"] is False


def test_duplicate_call_id_with_different_arguments_is_rejected():
    from domain import Conflict

    change_set, store = _change_set()
    _stage(change_set["id"], store, "same-call", False)
    with pytest.raises(Conflict, match="reused with different arguments"):
        _stage(change_set["id"], store, "same-call", True)


def test_change_set_cap_is_enforced_without_extra_item():
    change_set, store = _change_set()
    _stage(change_set["id"], store, "call-1", False)
    with pytest.raises(InvalidCommand, match="item limit"):
        stage_write(
            change_set["id"],
            "update_store_settings",
            {"store": store, "enabled": True, "allows_none": True},
            "call-2",
            "admin-one",
            max_items=1,
        )
    persisted = get_change_set(change_set["id"], "admin-one")
    assert persisted["revision"] == 1
    assert len(persisted["items"]) == 1


def test_concurrent_stage_keeps_both_items_and_advances_revision():
    change_set, store = _change_set()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _stage,
                change_set["id"],
                store,
                f"call-{index}",
                bool(index),
            )
            for index in (1, 2)
        ]
        for future in futures:
            future.result(timeout=15)
    persisted = get_change_set(change_set["id"], "admin-one")
    assert persisted["revision"] == 2
    assert [item["sort_order"] for item in persisted["items"]] == [0, 1]
    assert {item["call_id"] for item in persisted["items"]} == {"call-1", "call-2"}


def test_nonowner_cannot_stage_into_change_set():
    change_set, store = _change_set("admin-one")
    with pytest.raises(NotFound):
        stage_write(
            change_set["id"],
            "update_store_settings",
            {"store": store, "enabled": False, "allows_none": False},
            "call-owner-violation",
            "admin-two",
            max_items=50,
        )
