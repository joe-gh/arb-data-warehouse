"""Logo-name, colour-class, stock-rule and sync-block agent tools: each stages
a rolled-back preview on its single-row scope, applies, and undoes exactly."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg2
from pydantic import ValidationError
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
            (session_id, USER, "rule fixture", datetime.now(timezone.utc) + timedelta(hours=1)),
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


def test_set_logo_name_store_and_shared_round_trips():
    scope = MutationScope("display_name_row", {"design_id": "DESIGN-1", "color_scheme_id": "SCHEME-1", "fdm4_store": "S_TEST"})
    def check():
        rows = _admin("SELECT name, source, locked FROM logo.display_name WHERE design_id='DESIGN-1' AND color_scheme_id='SCHEME-1' AND fdm4_store='S_TEST'")
        assert rows == [("Renamed by agent", "manual", True)]
    staged = _round_trip("set_logo_name", {"design_id": "DESIGN-1", "color_scheme_id": "scheme-1", "name": "Renamed by agent", "store": "S_TEST"}, (scope,), "name-store", during=check)
    assert staged["preview_diff"]["changes"][0]["before"]["name"] == "Store test logo"
    shared = MutationScope("display_name_row", {"design_id": "DESIGN-1", "color_scheme_id": "SCHEME-1", "fdm4_store": ""})
    def check_shared():
        assert _admin("SELECT name FROM logo.display_name WHERE design_id='DESIGN-1' AND color_scheme_id='SCHEME-1' AND fdm4_store=''") == [("Shared name",)]
    _round_trip("set_logo_name", {"design_id": "DESIGN-1", "color_scheme_id": "SCHEME-1", "name": "Shared name", "store": None}, (shared,), "name-shared", during=check_shared)


def test_set_logo_name_rejects_unknown_logo():
    change_set = new_change_set(_session(), USER)
    with pytest.raises(NotFound):
        stage_write(change_set["id"], "set_logo_name", {"design_id": "NOPE", "color_scheme_id": "X", "name": "n", "store": None}, "name-bad", USER, max_items=50)


def test_clear_logo_name_removes_only_the_store_row_and_restores_it():
    scope = MutationScope("display_name_row", {"design_id": "DESIGN-1", "color_scheme_id": "SCHEME-1", "fdm4_store": "S_TEST"})
    def check():
        assert _admin("SELECT count(*) FROM logo.display_name WHERE design_id='DESIGN-1' AND fdm4_store='S_TEST'") == [(0,)]
        assert _admin("SELECT count(*) FROM logo.display_name WHERE design_id='DESIGN-1'") == [(2,)]
    _round_trip("clear_logo_name", {"design_id": "DESIGN-1", "color_scheme_id": "SCHEME-1", "store": "S_TEST"}, (scope,), "name-clear", during=check)
    change_set = new_change_set(_session(), USER)
    with pytest.raises((InvalidCommand, ValidationError)):   # blank store: the shared default is never removable
        stage_write(change_set["id"], "clear_logo_name", {"design_id": "DESIGN-1", "color_scheme_id": "SCHEME-1", "store": " "}, "name-clear-bad", USER, max_items=50)


def test_set_color_class_round_trip():
    _admin("DELETE FROM logo.color_class WHERE color_code='RED'")
    _admin("INSERT INTO logo.color_class (color_code, color_name, light_dark, source, confidence, updated_by) VALUES ('RED','Red','dark','ai',0.71,'seed')")
    scope = MutationScope("color_class_row", {"color_code": "RED"})
    def check():
        assert _admin("SELECT light_dark, source, confidence FROM logo.color_class WHERE color_code='RED'") == [("light", "manual", None)]
    staged = _round_trip("set_color_class", {"color_code": "RED", "light_dark": "light"}, (scope,), "class", during=check)
    assert staged["preview_diff"]["changes"][0]["after"]["light_dark"] == "light"
    change_set = new_change_set(_session(), USER)
    with pytest.raises(NotFound):
        stage_write(change_set["id"], "set_color_class", {"color_code": "NOPE", "light_dark": "dark"}, "class-bad", USER, max_items=50)


def test_stock_override_set_and_remove_round_trips():
    _admin("DELETE FROM woo.stock_override WHERE style_code='STYLE-1'")
    scope = MutationScope("stock_override_row", {"style_code": "STYLE-1"})
    def check_set():
        assert _admin("SELECT mode, note, active FROM woo.stock_override WHERE style_code='STYLE-1'") == [("fake", "demo", True)]
    staged = _round_trip("set_stock_override", {"style_code": "style-1", "mode": "fake", "note": "demo", "active": True}, (scope,), "stock-set", during=check_set)
    assert staged["preview_results"][0]["variants"] == 3
    _admin("INSERT INTO woo.stock_override (style_code, mode, note, active, updated_by) VALUES ('STYLE-1','real','keep',true,'seed')")
    def check_removed():
        assert _admin("SELECT count(*) FROM woo.stock_override WHERE style_code='STYLE-1'") == [(0,)]
    _round_trip("remove_stock_override", {"style_code": "STYLE-1"}, (scope,), "stock-remove", during=check_removed)
    assert _admin("SELECT mode FROM woo.stock_override WHERE style_code='STYLE-1'") == [("real",)]
    change_set = new_change_set(_session(), USER)
    with pytest.raises(NotFound):
        stage_write(change_set["id"], "set_stock_override", {"style_code": "NO-SUCH", "mode": "fake", "note": "", "active": True}, "stock-bad", USER, max_items=50)


def test_brand_stock_rule_set_and_remove_round_trips():
    # The harness database carries warehouse rows outside the seed; use a mill
    # of our own and start from a known-absent rule.
    _admin("DELETE FROM woo.brand_stock_rule WHERE mill_code='T-MILL'")
    _admin('DELETE FROM fdm4.mill WHERE "mill-code" = %s', ("T-MILL",))
    _admin('INSERT INTO fdm4.mill ("mill-code", description) VALUES (%s, %s)', ("T-MILL", "Test Mill"))
    scope = MutationScope("brand_stock_rule_row", {"mill_code": "T-MILL"})
    def check_set():
        assert _admin("SELECT brand_name, mode, active FROM woo.brand_stock_rule WHERE mill_code='T-MILL'") == [("Test Mill", "real", True)]
    _round_trip("set_brand_stock_rule", {"mill_code": "T-MILL", "mode": "real", "active": True}, (scope,), "brand-set", during=check_set)
    assert _admin("SELECT count(*) FROM woo.brand_stock_rule WHERE mill_code='T-MILL'") == [(0,)]
    _admin("INSERT INTO woo.brand_stock_rule (mill_code, brand_name, mode, active, updated_by) VALUES ('T-MILL','Test Mill','fake',true,'seed')")
    def check_removed():
        assert _admin("SELECT count(*) FROM woo.brand_stock_rule WHERE mill_code='T-MILL'") == [(0,)]
    _round_trip("remove_brand_stock_rule", {"mill_code": "T-MILL"}, (scope,), "brand-remove", during=check_removed)
    assert _admin("SELECT mode FROM woo.brand_stock_rule WHERE mill_code='T-MILL'") == [("fake",)]
    _admin("DELETE FROM woo.brand_stock_rule WHERE mill_code='T-MILL'")
    change_set = new_change_set(_session(), USER)
    with pytest.raises(NotFound):
        stage_write(change_set["id"], "set_brand_stock_rule", {"mill_code": "999", "mode": "fake", "active": True}, "brand-bad", USER, max_items=50)


def test_sync_block_whole_store_and_styles_round_trips():
    _admin("DELETE FROM woo.sync_exclusion WHERE fdm4_store='S_TEST'")
    whole = (MutationScope("sync_exclusion_row", {"fdm4_store": "S_TEST", "style_code": ""}),)
    def check_whole():
        assert _admin("SELECT scope, note, active FROM woo.sync_exclusion WHERE fdm4_store='S_TEST' AND style_code=''") == [("pricing", "hold prices", True)]
    _round_trip("set_sync_block", {"store": "s_test", "styles": [], "scope": "pricing", "note": "hold prices", "active": True}, whole, "block-store", during=check_whole)
    styles = tuple(MutationScope("sync_exclusion_row", {"fdm4_store": "S_TEST", "style_code": code}) for code in ("STYLE-1", "STYLE-2"))
    def check_styles():
        assert _admin("SELECT style_code, scope FROM woo.sync_exclusion WHERE fdm4_store='S_TEST' ORDER BY 1") == [("STYLE-1", "full"), ("STYLE-2", "full")]
    staged = _round_trip("set_sync_block", {"store": "S_TEST", "styles": ["style-1", "STYLE-2", "style-1"], "scope": "pricing", "note": "", "active": True}, styles, "block-styles", during=check_styles)
    assert staged["preview_results"][0]["per_style"] == [{"style": "STYLE-1", "products": 3}, {"style": "STYLE-2", "products": 1}]
    _admin("INSERT INTO woo.sync_exclusion (fdm4_store, style_code, note, active, scope, updated_by) VALUES ('S_TEST','STYLE-1','x',true,'full','seed')")
    def check_removed():
        assert _admin("SELECT count(*) FROM woo.sync_exclusion WHERE fdm4_store='S_TEST'") == [(0,)]
    _round_trip("remove_sync_block", {"store": "S_TEST", "styles": ["STYLE-1"]}, styles[:1], "block-remove", during=check_removed)
    assert _admin("SELECT note FROM woo.sync_exclusion WHERE fdm4_store='S_TEST' AND style_code='STYLE-1'") == [("x",)]
    change_set = new_change_set(_session(), USER)
    with pytest.raises(NotFound):
        stage_write(change_set["id"], "set_sync_block", {"store": "S_NOPE", "styles": [], "scope": "full", "note": "", "active": True}, "block-bad", USER, max_items=50)
