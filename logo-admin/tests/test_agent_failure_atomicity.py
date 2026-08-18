"""Failures before human confirmation cannot persist business mutations."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import staging
from db import database
from staging import discard_change_set, get_change_set, new_change_set, stage_write


def _fixture():
    session_id = uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog WHERE suggested=true ORDER BY 1 LIMIT 1"
        )
        store = cursor.fetchone()["fdm4_store"]
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                "admin-one",
                "failure fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        cursor.execute(
            "SELECT to_jsonb(s) AS row FROM logo.store_settings s WHERE fdm4_store=%s",
            (store,),
        )
        row = cursor.fetchone()
        before = dict(row["row"]) if row else None
    return new_change_set(session_id, "admin-one"), store, before


def _settings(store):
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT to_jsonb(s) AS row FROM logo.store_settings s WHERE fdm4_store=%s",
            (store,),
        )
        row = cursor.fetchone()
        return dict(row["row"]) if row else None


def test_preview_failure_leaves_no_item_or_revision(monkeypatch):
    change_set, store, before = _fixture()

    def fail_preview(*_args, **_kwargs):
        raise RuntimeError("preview failed")

    monkeypatch.setattr(staging, "preview_commands", fail_preview)
    with pytest.raises(RuntimeError, match="preview failed"):
        stage_write(
            change_set["id"],
            "update_store_settings",
            {"store": store, "enabled": False, "allows_none": True},
            "failed-preview",
            "admin-one",
            max_items=50,
        )
    persisted = get_change_set(change_set["id"], "admin-one")
    assert persisted["revision"] == 0
    assert persisted["items"] == []
    assert _settings(store) == before


def test_provider_failure_after_staging_leaves_only_pending_metadata():
    change_set, store, before = _fixture()
    stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "provider-failure",
        "admin-one",
        max_items=50,
    )
    # A provider failure has no apply capability; the pending set remains reviewable.
    persisted = get_change_set(change_set["id"], "admin-one")
    assert persisted["status"] == "pending"
    assert persisted["journal"] == []
    assert _settings(store) == before


def test_discard_after_cancel_changes_no_business_state():
    change_set, store, before = _fixture()
    stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "cancelled-turn",
        "admin-one",
        max_items=50,
    )
    discarded = discard_change_set(change_set["id"], "admin-one")
    assert discarded["status"] == "discarded"
    assert _settings(store) == before
