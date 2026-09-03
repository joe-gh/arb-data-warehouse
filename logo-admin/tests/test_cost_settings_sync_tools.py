"""Phase-1 tools: logo cost across styles, extra customers, sync status and
product link (the WordPress calls are stubbed; the DB side is real)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest

from db import database
from domain import NotFound
from mutations import MutationScope
import queries
from snapshots import snapshot_scopes, states_equal
from staging import apply_change_set, new_change_set, stage_write, undo_change_set
from tests.conftest import TEST_ADMIN_DSN
import tool_registry
import wp_bridge

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
            (session_id, USER, "phase1 fixture", datetime.now(timezone.utc) + timedelta(hours=1)),
        )
    return session_id


def _snapshot(scopes):
    with database.cursor() as cursor:
        return snapshot_scopes(cursor, scopes)


def _round_trip(tool, arguments, scopes, call_id, *, during):
    before = _snapshot(scopes)
    change_set = new_change_set(_session(), USER)
    staged = stage_write(change_set["id"], tool, arguments, call_id, USER, max_items=50)
    assert states_equal(_snapshot(scopes), before)
    apply_change_set(change_set["id"], USER, revision=staged["revision"],
                     confirmed_hash=staged["preview_hash"], acknowledge_hard_delete=False)
    during()
    assert undo_change_set(change_set["id"], USER)["status"] == "undone"
    assert states_equal(_snapshot(scopes), before)
    return staged


def test_set_logo_cost_updates_only_matching_rows_and_undoes():
    scopes = (MutationScope("assignment_style", {"fdm4_store": "S_TEST", "product_style": "STYLE-1"}),)
    def check():
        rows = _admin("SELECT position, design_id, cost_override FROM logo.assignment WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' AND garment_color_code='RED' ORDER BY position")
        assert rows == [(1, "DESIGN-1", Decimal("5.50")), (2, "DESIGN-2", None)]
    staged = _round_trip("set_logo_cost", {"store": "S_TEST", "design_id": "DESIGN-1", "color_scheme_id": None,
                                           "cost_override": "5.50", "styles": ["STYLE-1"]}, scopes, "cost-1", during=check)
    result = staged["preview_results"][0]
    assert result["matching_rows"] == 1 and result["updated"] == 1
    assert len(staged["preview_diff"]["changes"]) == 1
    change_set = new_change_set(_session(), USER)
    with pytest.raises(NotFound):
        stage_write(change_set["id"], "set_logo_cost", {"store": "S_TEST", "design_id": "NOPE", "color_scheme_id": None,
                                                        "cost_override": "1", "styles": ["STYLE-1"]}, "cost-bad", USER, max_items=50)


def test_set_logo_cost_null_clears_override():
    _admin("UPDATE logo.assignment SET cost_override = 3 WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' AND position = 1")
    scopes = (MutationScope("assignment_style", {"fdm4_store": "S_TEST", "product_style": "STYLE-1"}),)
    def check():
        assert _admin("SELECT cost_override FROM logo.assignment WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' AND position=1") == [(None,)]
    _round_trip("set_logo_cost", {"store": "S_TEST", "design_id": "DESIGN-1", "color_scheme_id": "scheme-1",
                                  "cost_override": None, "styles": ["STYLE-1", "STYLE-2"]}, scopes + (MutationScope("assignment_style", {"fdm4_store": "S_TEST", "product_style": "STYLE-2"}),), "cost-null", during=check)


def test_set_store_extra_customers_round_trip_and_validation():
    scope = (MutationScope("store_settings_row", {"fdm4_store": "S_TEST"}),)
    def check():
        assert _admin("SELECT extra_customers, enabled FROM logo.store_settings WHERE fdm4_store='S_TEST'") == [(["OTHER"], True)]
    _round_trip("set_store_extra_customers", {"store": "S_TEST", "customers": ["OTHER", "OTHER"]}, scope, "extra-1", during=check)
    assert _admin("SELECT extra_customers FROM logo.store_settings WHERE fdm4_store='S_TEST'") == [([],)]
    change_set = new_change_set(_session(), USER)
    with pytest.raises(NotFound):
        stage_write(change_set["id"], "set_store_extra_customers", {"store": "S_TEST", "customers": ["NOBODY"]}, "extra-bad", USER, max_items=50)


def test_get_sync_status_reads_pipeline_and_store(monkeypatch):
    _admin("INSERT INTO woo.sync_control (op, env, status, requested_by, started_at, finished_at, rows_loaded) VALUES ('pull','global','success','test', now() - interval '20 minutes', now() - interval '5 minutes', 10)")
    _admin("INSERT INTO woo.sync_exclusion (fdm4_store, style_code, note, active, updated_by) VALUES ('S_TEST','', 'hold', true, 'seed')")
    monkeypatch.setattr(wp_bridge, "logo_ownership", lambda: {"stores": [{"fdm4_store": "S_TEST", "blog_id": 7, "owned": True}], "owned_blogs": [7]})
    spec = next(s for s in tool_registry.TOOL_SPECS if s.name == "get_sync_status")
    with database.cursor() as cursor:
        plain = spec.handler(cursor, spec.command_model(store=None), None)
        detailed = spec.handler(cursor, spec.command_model(store="s_test"), None)
    assert plain["pipeline"]["latest"][0]["op"] == "pull" and plain["pipeline"]["latest"][0]["status"] == "success"
    assert plain["pipeline"]["last_24h"][0]["ok_24h"] == 1
    assert detailed["store"] == "S_TEST"
    assert detailed["store_status"]["whole_store_frozen"] is True
    assert detailed["store_status"]["active_logo_rows"] == 2
    assert detailed["logo_sync_ownership"] == {"available": True, "owned": True, "blog_id": 7, "store": "S_TEST"}
    _admin("DELETE FROM woo.sync_exclusion WHERE fdm4_store='S_TEST'")


def test_store_ownership_soft_fails_when_wordpress_is_down(monkeypatch):
    from auth import WordPressRequestError
    def boom():
        raise WordPressRequestError("WordPress is currently unreachable", 502)
    monkeypatch.setattr(wp_bridge, "logo_ownership", boom)
    result = wp_bridge.store_ownership("S_TEST")
    assert result["available"] is False and result["owned"] is None


def test_get_product_link_tool_uses_the_shared_bridge(monkeypatch):
    calls = []
    monkeypatch.setattr(wp_bridge, "product_link", lambda store, style: calls.append((store, style)) or {"ok": True, "view_url": "https://x/p", "edit_url": "https://x/e"})
    spec = next(s for s in tool_registry.TOOL_SPECS if s.name == "get_product_link")
    result = spec.handler(None, spec.command_model(store=" S_TEST ", style="STYLE-1"), None)
    assert calls == [("S_TEST", "STYLE-1")] and result["view_url"] == "https://x/p"
