"""Undo restores complete recorded business rows byte-for-byte."""

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from commands import UpdateStoreSettingsCommand
from db import database
from mutations import MutationScope, update_store_settings
from snapshots import dumps_exact, restore_state, snapshot_scopes, states_equal
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


def test_states_equal_ignores_trigger_managed_row_version_only():
    left = [{
        "scope": {"kind": "assignment_option_row", "key": {"fdm4_store": "S_TEST"}},
        "rows": [{"ordinal": 1, "row": {"design_id": "DESIGN-1", "row_version": 5}}],
    }]
    right = copy.deepcopy(left)
    right[0]["rows"][0]["row"]["row_version"] = 9
    assert states_equal(left, right)
    right[0]["rows"][0]["row"]["design_id"] = "DESIGN-2"
    assert not states_equal(left, right)


def test_dumps_exact_writes_a_decimal_as_a_bare_number():
    assert dumps_exact(Decimal("1.50")) == "1.50"
    assert dumps_exact({"n": Decimal("0.1234567890123456789")}) == (
        '{"n":0.1234567890123456789}'
    )
    # A non-finite numeric has no JSON number form; it stays a string, as
    # before, rather than producing invalid JSON.
    assert dumps_exact(Decimal("NaN")) == '"NaN"'


def _color_class_confidence(codes):
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT color_code, confidence::text AS confidence "
            "FROM logo.color_class WHERE color_code = ANY(%s) ORDER BY color_code",
            (list(codes),),
        )
        return {r["color_code"]: r["confidence"] for r in cursor.fetchall()}


def test_undo_restores_unrestricted_numerics_digit_for_digit():
    """logo.color_class.confidence is an unrestricted numeric.

    The journal stores rows as jsonb, so a float round trip would quietly
    round 0.90 to 0.9 and truncate a 29-digit value; undo has to put back the
    digits PostgreSQL gave out.
    """

    codes = ("ZZUNDO90", "ZZUNDO29")
    exact = {
        "ZZUNDO90": "0.90",
        "ZZUNDO29": "0.12345678901234567890123456789",
    }
    with database.cursor(write=True, actor="fixture") as cursor:
        for code, confidence in exact.items():
            cursor.execute(
                "INSERT INTO logo.color_class "
                "(color_code, color_name, light_dark, source, confidence, updated_by) "
                "VALUES (%s, %s, 'light', 'ai', %s, 'fixture') "
                "ON CONFLICT (color_code) DO UPDATE SET confidence = EXCLUDED.confidence, "
                "light_dark = 'light', source = 'ai'",
                (code, f"Undo {code}", Decimal(confidence)),
            )
    try:
        before = _color_class_confidence(codes)
        assert before == exact
        change_set = new_change_set(_session(), "admin-one")
        staged = None
        for code in codes:
            staged = stage_write(
                change_set["id"],
                "set_color_class",
                {"color_code": code, "light_dark": "dark"},
                f"undo-numeric-{code}",
                "admin-one",
                max_items=50,
            )
        _apply(change_set["id"], staged)
        # set_color_class clears confidence, so undo has to restore it.
        assert set(_color_class_confidence(codes).values()) == {None}
        undo_change_set(change_set["id"], "admin-one")
        assert _color_class_confidence(codes) == exact
    finally:
        with database.cursor(write=True, actor="fixture") as cursor:
            cursor.execute(
                "DELETE FROM logo.color_class WHERE color_code = ANY(%s)",
                (list(codes),),
            )


def test_restore_puts_back_a_jsonb_number_unchanged():
    """catmgr.assignment_rule.spec carries numbers inside a jsonb document."""

    spec = '{"n": 0.1234567890123456789}'
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "INSERT INTO catmgr.node (name, slug, updated_by) "
            "VALUES ('Undo Numeric', 'undo-numeric', 'fixture') RETURNING node_id"
        )
        node_id = int(cursor.fetchone()["node_id"])
        cursor.execute(
            "INSERT INTO catmgr.assignment_rule (node_id, spec, updated_by) "
            "VALUES (%s, %s::jsonb, 'fixture') RETURNING rule_id",
            (node_id, spec),
        )
        rule_id = int(cursor.fetchone()["rule_id"])
    scope = MutationScope("catmgr_rule_row", {"rule_id": rule_id})
    try:
        with database.cursor() as cursor:
            before = snapshot_scopes(cursor, (scope,))
            cursor.execute(
                "SELECT spec::text AS spec FROM catmgr.assignment_rule WHERE rule_id=%s",
                (rule_id,),
            )
            original = cursor.fetchone()["spec"]
        with database.cursor(write=True, actor="fixture") as cursor:
            cursor.execute(
                "UPDATE catmgr.assignment_rule SET spec = '{\"n\": 1}'::jsonb "
                "WHERE rule_id=%s",
                (rule_id,),
            )
        with database.cursor(write=True, actor="fixture") as cursor:
            restore_state(cursor, before, expected_scopes=(scope,))
        with database.cursor() as cursor:
            after = snapshot_scopes(cursor, (scope,))
            cursor.execute(
                "SELECT spec::text AS spec FROM catmgr.assignment_rule WHERE rule_id=%s",
                (rule_id,),
            )
            restored = cursor.fetchone()["spec"]
        assert restored == original
        assert states_equal(after, before)
    finally:
        with database.cursor(write=True, actor="fixture") as cursor:
            cursor.execute(
                "DELETE FROM catmgr.assignment_rule WHERE rule_id=%s", (rule_id,)
            )
            cursor.execute("DELETE FROM catmgr.node WHERE node_id=%s", (node_id,))
