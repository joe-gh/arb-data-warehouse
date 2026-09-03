"""Default cost, price-rule toggle/delete and product-mix tools: exact undo on
single-row and whole-store-item scopes, plus the invariants the shared
mix_service enforces for routes, MCP and the assistant alike."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest

from db import database
from domain import InvalidCommand, NotFound
from mutations import MutationScope
from snapshots import snapshot_scopes, states_equal
from staging import apply_change_set, new_change_set, stage_write, undo_change_set
from tests.conftest import TEST_ADMIN_DSN

USER = "admin-one"


def _admin(sql, params=()):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else None


def _session():
    session_id = uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "INSERT INTO logo.agent_chat_session (id,user_login,title,expires_at) VALUES (%s,%s,%s,%s)",
            (session_id, USER, "phase3 fixture", datetime.now(timezone.utc) + timedelta(hours=1)),
        )
    return session_id


def _snapshot(scopes):
    with database.cursor() as cursor:
        return snapshot_scopes(cursor, scopes)


def _round_trip(tool, arguments, scopes, call_id, *, during):
    before = _snapshot(scopes)
    change_set = new_change_set(_session(), USER)
    staged = stage_write(change_set["id"], tool, arguments, call_id, USER, max_items=50)
    assert states_equal(_snapshot(scopes), before), "preview leaked"
    apply_change_set(change_set["id"], USER, revision=staged["revision"],
                     confirmed_hash=staged["preview_hash"], acknowledge_hard_delete=False)
    during()
    assert undo_change_set(change_set["id"], USER)["status"] == "undone"
    assert states_equal(_snapshot(scopes), before)
    return staged


def _reject(tool, arguments, exc):
    change_set = new_change_set(_session(), USER)
    with pytest.raises(exc):
        stage_write(change_set["id"], tool, arguments, "reject-" + tool, USER, max_items=50)


# ---- default cost ---------------------------------------------------------

def test_set_logo_default_cost_creates_and_updates_and_undoes():
    scope = (MutationScope("default_cost_row", {"logo_code": "C1", "color_scheme_id": "SCHEME-1"}),)
    def created():
        assert _admin("SELECT cost, source, locked FROM logo.default_cost WHERE logo_code='C1' AND color_scheme_id='SCHEME-1'") == [(Decimal("7.00"), "manual", True)]
    staged = _round_trip("set_logo_default_cost", {"logo_code": "c1", "color_scheme_id": "scheme-1", "cost": "7", "locked": True}, scope, "dc-new", during=created)
    assert staged["preview_results"][0]["stores_using_default"] == 1
    assert _admin("SELECT count(*) FROM logo.default_cost WHERE logo_code='C1'") == [(0,)]
    _admin("INSERT INTO logo.default_cost (logo_code, color_scheme_id, cost, source, locked, updated_by) VALUES ('C1','SCHEME-1', 3.50, 'vn-reference', false, 'seed')")
    def updated():
        assert _admin("SELECT cost, locked FROM logo.default_cost WHERE logo_code='C1'") == [(Decimal("0.00"), False)]
    staged = _round_trip("set_logo_default_cost", {"logo_code": "C1", "color_scheme_id": "SCHEME-1", "cost": "0", "locked": False}, scope, "dc-upd", during=updated)
    assert staged["preview_diff"]["changes"][0]["before"]["cost"] == 3.5
    assert _admin("SELECT cost, source FROM logo.default_cost WHERE logo_code='C1'") == [(Decimal("3.50"), "vn-reference")]
    _reject("set_logo_default_cost", {"logo_code": "ZZZ", "color_scheme_id": "NOPE", "cost": "1", "locked": True}, NotFound)


# ---- price rules ------------------------------------------------------------

def _fixture_rule_id():
    return _admin("SELECT rule_id FROM woo.price_rule WHERE name='Fixture inactive rule'")[0][0]


def test_price_rule_activation_requires_preview_then_round_trips():
    rule_id = _fixture_rule_id()
    _reject("set_price_rule_active", {"rule_id": rule_id, "active": True}, InvalidCommand)
    _reject("set_price_rule_active", {"rule_id": 999999, "active": False}, NotFound)
    _admin("UPDATE woo.price_rule SET last_previewed_at = now() WHERE rule_id = %s", (rule_id,))
    scope = (MutationScope("price_rule_row", {"rule_id": rule_id}),)
    def on():
        assert _admin("SELECT active FROM woo.price_rule WHERE rule_id=%s", (rule_id,)) == [(True,)]
    staged = _round_trip("set_price_rule_active", {"rule_id": rule_id, "active": True}, scope, "pr-on", during=on)
    assert staged["preview_results"][0]["was_active"] is False
    assert _admin("SELECT active FROM woo.price_rule WHERE rule_id=%s", (rule_id,)) == [(False,)]


def test_delete_price_rule_round_trip_restores_arrays_and_nulls():
    rule_id = _fixture_rule_id()
    _admin("UPDATE woo.price_rule SET stores = ARRAY['S_TEST'], excl_styles = ARRAY['X','Y'], effective_from = DATE '2026-01-01', floor_price = 9.5 WHERE rule_id = %s", (rule_id,))
    scope = (MutationScope("price_rule_row", {"rule_id": rule_id}),)
    def gone():
        assert _admin("SELECT count(*) FROM woo.price_rule WHERE rule_id=%s", (rule_id,)) == [(0,)]
    _round_trip("delete_price_rule", {"rule_id": rule_id}, scope, "pr-del", during=gone)
    assert _admin("SELECT stores, excl_styles, effective_from, floor_price, effect_value FROM woo.price_rule WHERE rule_id=%s", (rule_id,)) == [(["S_TEST"], ["X", "Y"], datetime(2026, 1, 1).date(), Decimal("9.5000"), Decimal("5.0000"))]


# ---- product mix --------------------------------------------------------------

def _mix(store):
    return _admin("SELECT mode, active, note, imported_at IS NOT NULL FROM woo.store_mix_store WHERE fdm4_store=%s", (store,))


def _items(store):
    return _admin("SELECT style_code, colors, size_excludes, source FROM woo.store_mix_item WHERE fdm4_store=%s ORDER BY 1", (store,))


def test_set_product_mix_enrols_list_mode_with_seeded_items_and_undoes():
    scopes = (MutationScope("store_mix_store_row", {"fdm4_store": "S_TEST"}), MutationScope("store_mix_items", {"fdm4_store": "S_TEST"}))
    def check():
        assert _mix("S_TEST") == [("list", True, "curated by agent", True)]
        assert [(r[0], r[1], r[3]) for r in _items("S_TEST")] == [("STYLE-1", ["BLU", "GRN", "RED"], "import"), ("STYLE-2", ["RED"], "import")]
    staged = _round_trip("set_product_mix", {"store": "s_test", "mode": "list", "note": "curated by agent"}, scopes, "mix-list", during=check)
    assert staged["preview_results"][0]["imported"] == 2 and staged["preview_results"][0]["styles_in_list"] == 2
    assert _mix("S_TEST") == [] and _items("S_TEST") == []
    _reject("set_product_mix", {"store": "S_NOPE", "mode": "all", "note": ""}, NotFound)
    _reject("set_product_mix", {"store": "S_EMPTY", "mode": "list", "note": ""}, InvalidCommand)   # nothing to seed


def test_switching_list_store_to_all_and_disabling_round_trip():
    scopes = (MutationScope("store_mix_store_row", {"fdm4_store": "S_MIXED"}), MutationScope("store_mix_items", {"fdm4_store": "S_MIXED"}))
    def check_all():
        assert _mix("S_MIXED")[0][0] == "all"
        assert _items("S_MIXED")[0][0] == "MIX-1"        # the saved list is kept
    staged = _round_trip("set_product_mix", {"store": "S_MIXED", "mode": "all", "note": "follow FDM4"}, scopes, "mix-all", during=check_all)
    assert staged["preview_results"][0]["previous_mode"] == "list"
    def check_off():
        assert _mix("S_MIXED")[0][1] is False
    _round_trip("disable_product_mix", {"store": "S_MIXED"}, scopes[:1], "mix-off", during=check_off)
    assert _mix("S_MIXED")[0][1] is True
    _reject("disable_product_mix", {"store": "S_TEST"}, NotFound)


def test_add_and_remove_mix_styles_round_trip_and_guards():
    scope = (MutationScope("store_mix_items", {"fdm4_store": "S_MIXED"}),)
    def added():
        assert [r[0] for r in _items("S_MIXED")] == ["MIX-1", "MIX-2"]
    staged = _round_trip("add_mix_styles", {"store": "S_MIXED", "styles": ["mix-2", "MIX-2"]}, scope, "mix-add", during=added)
    assert staged["preview_results"][0]["per_style"] == [{"style": "MIX-2", "products": 0}]
    assert _items("S_MIXED") == [("MIX-1", ["RED"], {"RED": ["L"]}, "import")]    # jsonb + array restored exactly
    _reject("remove_mix_styles", {"store": "S_MIXED", "styles": ["MIX-1"]}, InvalidCommand)   # would empty the list
    _reject("add_mix_styles", {"store": "S_ALLMODE", "styles": ["ALL-1"]}, InvalidCommand)   # not list mode
    _reject("remove_mix_styles", {"store": "S_MIXED", "styles": ["NOPE"]}, NotFound)
    _admin("INSERT INTO woo.store_mix_item (fdm4_store, style_code, colors, source, added_by, updated_by) VALUES ('S_MIXED','MIX-9',NULL,'manual','seed','seed')")
    def removed():
        assert [r[0] for r in _items("S_MIXED")] == ["MIX-1"]
    _round_trip("remove_mix_styles", {"store": "S_MIXED", "styles": ["MIX-9"]}, scope, "mix-rm", during=removed)
    assert [r[0] for r in _items("S_MIXED")] == ["MIX-1", "MIX-9"]
