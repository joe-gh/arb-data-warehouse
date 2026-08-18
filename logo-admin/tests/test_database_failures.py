"""Injected database failures cannot leave partial stage/apply/undo state."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg2
import pytest

import staging
from db import database
from staging import (
    apply_change_set,
    get_change_set,
    new_change_set,
    stage_write,
    undo_change_set,
)


def _pending():
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
                session_id, "admin-one", "db failure",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    return new_change_set(session_id, "admin-one"), store


def _stage(change_set, store):
    return stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "db-failure",
        "admin-one",
        max_items=50,
    )


def test_stage_database_failure_leaves_no_metadata(monkeypatch):
    change_set, store = _pending()
    monkeypatch.setattr(
        staging,
        "preview_commands",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg2.DatabaseError("preview db")),
    )
    with pytest.raises(psycopg2.DatabaseError):
        _stage(change_set, store)
    persisted = get_change_set(change_set["id"], "admin-one")
    assert persisted["revision"] == 0
    assert persisted["items"] == []


def test_apply_database_failure_rolls_back_business_and_journal(monkeypatch):
    change_set, store = _pending()
    staged = _stage(change_set, store)
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT to_jsonb(s) AS row FROM logo.store_settings s WHERE fdm4_store=%s",
            (store,),
        )
        row = cursor.fetchone()
        before = dict(row["row"]) if row else None
    monkeypatch.setattr(
        staging,
        "dispatch_mutation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg2.DatabaseError("apply db")),
    )
    with pytest.raises(psycopg2.DatabaseError):
        apply_change_set(
            change_set["id"], "admin-one",
            revision=staged["revision"], confirmed_hash=staged["preview_hash"],
            acknowledge_hard_delete=False,
        )
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT to_jsonb(s) AS row FROM logo.store_settings s WHERE fdm4_store=%s",
            (store,),
        )
        row = cursor.fetchone()
        after = dict(row["row"]) if row else None
    assert after == before
    assert get_change_set(change_set["id"], "admin-one")["journal"] == []


def test_undo_database_failure_leaves_applied_state_intact(monkeypatch):
    change_set, store = _pending()
    staged = _stage(change_set, store)
    apply_change_set(
        change_set["id"], "admin-one",
        revision=staged["revision"], confirmed_hash=staged["preview_hash"],
        acknowledge_hard_delete=False,
    )
    monkeypatch.setattr(
        staging,
        "restore_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(psycopg2.DatabaseError("undo db")),
    )
    with pytest.raises(psycopg2.DatabaseError):
        undo_change_set(change_set["id"], "admin-one")
    persisted = get_change_set(change_set["id"], "admin-one")
    assert persisted["status"] == "applied"
    assert [event["event_type"] for event in persisted["journal"]] == ["apply"]
