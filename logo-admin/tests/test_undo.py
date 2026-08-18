"""Undo restores complete recorded business rows byte-for-byte."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from commands import UpdateStoreSettingsCommand
from db import database
from mutations import MutationScope, update_store_settings
from snapshots import snapshot_scopes, states_equal
from staging import apply_change_set, new_change_set, stage_write, undo_change_set


def _session():
    session_id = uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "INSERT INTO logo.agent_chat_session "
            "(id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (
                session_id,
                "admin-one",
                "undo fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    return session_id


def _apply(change_set_id, staged, *, hard=False):
    return apply_change_set(
        change_set_id,
        "admin-one",
        revision=staged["revision"],
        confirmed_hash=staged["preview_hash"],
        acknowledge_hard_delete=hard,
    )


def test_settings_undo_restores_updated_by_and_updated_at_exactly():
    with database.cursor(write=True, actor="baseline") as cursor:
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog WHERE suggested=true ORDER BY 1 LIMIT 1"
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
    scope = MutationScope("store_settings_row", {"fdm4_store": store})
    with database.cursor() as cursor:
        before = snapshot_scopes(cursor, (scope,))
    change_set = new_change_set(_session(), "admin-one")
    staged = stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "undo-settings",
        "admin-one",
        max_items=50,
    )
    _apply(change_set["id"], staged)
    undone = undo_change_set(change_set["id"], "admin-one")
    assert undone["status"] == "undone"
    with database.cursor() as cursor:
        after = snapshot_scopes(cursor, (scope,))
        cursor.execute(
            "SELECT event_type FROM logo.agent_action_journal "
            "WHERE change_set_id=%s ORDER BY created_at,id",
            (change_set["id"],),
        )
        events = [row["event_type"] for row in cursor.fetchall()]
    assert states_equal(after, before)
    assert events == ["apply", "undo"]


def test_hard_delete_undo_restores_complete_option_row():
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM logo.assignment WHERE position=1 "
            "ORDER BY fdm4_store,product_style,garment_color_code,option_row LIMIT 1"
        )
        row = dict(cursor.fetchone())
    scope = MutationScope(
        "assignment_option_row",
        {
            "fdm4_store": row["fdm4_store"],
            "product_style": row["product_style"],
            "garment_color_code": row["garment_color_code"],
            "option_row": row["option_row"],
        },
    )
    with database.cursor() as cursor:
        before = snapshot_scopes(cursor, (scope,))
    change_set = new_change_set(_session(), "admin-one")
    staged = stage_write(
        change_set["id"],
        "hard_delete_assignment",
        {
            **scope.key,
            "position": 1,
        },
        "undo-delete",
        "admin-one",
        max_items=50,
    )
    _apply(change_set["id"], staged, hard=True)
    undo_change_set(change_set["id"], "admin-one")
    with database.cursor() as cursor:
        after = snapshot_scopes(cursor, (scope,))
    assert states_equal(after, before)


def test_pricing_insert_undo_restores_absence_or_original_row():
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog WHERE suggested=true ORDER BY 1 LIMIT 1"
        )
        store = cursor.fetchone()["fdm4_store"]
        cursor.execute("SELECT tier_name FROM woo.pricing_tier ORDER BY sort_order LIMIT 1")
        tier = cursor.fetchone()["tier_name"]
    scope = MutationScope("store_pricing_tier_row", {"fdm4_store": store})
    with database.cursor() as cursor:
        before = snapshot_scopes(cursor, (scope,))
    change_set = new_change_set(_session(), "admin-one")
    staged = stage_write(
        change_set["id"],
        "set_store_pricing_tier",
        {"fdm4_store": store, "tier_name": tier, "note": "undo pricing"},
        "undo-pricing",
        "admin-one",
        max_items=50,
    )
    _apply(change_set["id"], staged)
    undo_change_set(change_set["id"], "admin-one")
    with database.cursor() as cursor:
        after = snapshot_scopes(cursor, (scope,))
    assert states_equal(after, before)
