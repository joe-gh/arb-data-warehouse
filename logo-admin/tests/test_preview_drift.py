"""Apply rejects stale confirmations and changed business state."""

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from commands import UpdateStoreSettingsCommand
from db import database
from domain import Conflict, NotFound, PreviewDrift
from mutations import update_store_settings
from staging import (
    apply_change_set,
    get_change_set,
    new_change_set,
    refresh_change_set,
    stage_write,
)


def _staged_settings():
    session_id = uuid4()
    with database.cursor(write=True, actor="baseline") as cursor:
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog "
            "WHERE suggested=true ORDER BY fdm4_store LIMIT 1"
        )
        store = cursor.fetchone()["fdm4_store"]
        update_store_settings(
            cursor,
            "baseline",
            UpdateStoreSettingsCommand(
                store=store,
                enabled=True,
                allows_none=False,
            ),
        )
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                "admin-one",
                "drift fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    change_set = new_change_set(session_id, "admin-one")
    staged = stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": False},
        "drift-call",
        "admin-one",
        max_items=50,
    )
    return change_set, staged, store


def test_apply_detects_business_state_drift_and_rolls_back():
    change_set, staged, store = _staged_settings()
    with database.cursor(write=True, actor="human-editor") as cursor:
        update_store_settings(
            cursor,
            "human-editor",
            UpdateStoreSettingsCommand(
                store=store,
                enabled=True,
                allows_none=True,
            ),
        )
    with pytest.raises(PreviewDrift) as raised:
        apply_change_set(
            change_set["id"],
            "admin-one",
            revision=staged["revision"],
            confirmed_hash=staged["preview_hash"],
            acknowledge_hard_delete=False,
        )
    assert len(raised.value.preview["preview_hash"]) == 64
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT enabled,allows_none,updated_by FROM logo.store_settings WHERE fdm4_store=%s",
            (store,),
        )
        row = cursor.fetchone()
    assert dict(row) == {
        "enabled": True,
        "allows_none": True,
        "updated_by": "human-editor",
    }
    assert get_change_set(change_set["id"], "admin-one")["status"] == "pending"


@pytest.mark.parametrize(
    ("revision_delta", "hash_value"),
    [(1, None), (0, "0" * 64)],
)
def test_apply_rejects_stale_revision_or_hash(revision_delta, hash_value):
    change_set, staged, _store = _staged_settings()
    with pytest.raises(Conflict, match="Confirmation"):
        apply_change_set(
            change_set["id"],
            "admin-one",
            revision=staged["revision"] + revision_delta,
            confirmed_hash=hash_value or staged["preview_hash"],
            acknowledge_hard_delete=False,
        )


def test_nonowner_gets_not_found_not_authorization_detail():
    change_set, staged, _store = _staged_settings()
    with pytest.raises(NotFound):
        apply_change_set(
            change_set["id"],
            "admin-two",
            revision=staged["revision"],
            confirmed_hash=staged["preview_hash"],
            acknowledge_hard_delete=False,
        )


def test_apply_rejects_scope_contract_drift_before_business_dml():
    change_set, staged, store = _staged_settings()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            """
            UPDATE logo.agent_change_set
               SET affected_scopes = %s::jsonb
             WHERE id = %s AND user_login = 'admin-one'
            """,
            (
                json.dumps([{
                    "kind": "store_pricing_tier_row",
                    "key": {"fdm4_store": store},
                }]),
                change_set["id"],
            ),
        )

    with pytest.raises(PreviewDrift) as raised:
        apply_change_set(
            change_set["id"],
            "admin-one",
            revision=staged["revision"],
            confirmed_hash=staged["preview_hash"],
            acknowledge_hard_delete=False,
        )
    assert raised.value.preview["scope_contract_changed"] is True
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT enabled FROM logo.store_settings WHERE fdm4_store = %s",
            (store,),
        )
        assert cursor.fetchone()["enabled"] is True

    refreshed = refresh_change_set(change_set["id"], "admin-one")
    assert refreshed["revision"] == staged["revision"] + 1
    assert refreshed["preview_hash"] != staged["preview_hash"]
    assert refreshed["affected_scopes"] == [{
        "kind": "store_settings_row",
        "key": {"fdm4_store": store},
    }]
