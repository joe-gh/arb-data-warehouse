"""Bulk agent write tools: each stages a rolled-back preview, applies
atomically under the human's confirmation and undoes byte-for-byte."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg2
import pytest

from db import database
from domain import InvalidCommand
from mutations import MutationScope
import queries
from snapshots import snapshot_scopes, states_equal
from staging import apply_change_set, new_change_set, stage_write, undo_change_set
from tests.conftest import TEST_ADMIN_DSN

USER = "admin-one"
SCOPES = tuple(
    MutationScope("assignment_style", {"fdm4_store": "S_TEST", "product_style": style})
    for style in ("STYLE-1", "STYLE-2")
)
ROW = {"option_row": 2, "position": 1, "design_id": "DESIGN-1", "logo_code": "C1",
       "color_scheme_id": "SCHEME-1", "location": "Left Chest", "optional": False,
       "background": "", "cost_override": None, "sort_order": 0, "image_url": "",
       "name_override": "Pasted by agent", "active": True}


def _session():
    session_id = uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "INSERT INTO logo.agent_chat_session (id,user_login,title,expires_at) "
            "VALUES (%s,%s,%s,%s)",
            (session_id, USER, "bulk fixture", datetime.now(timezone.utc) + timedelta(hours=1)),
        )
    return session_id


def _snapshot():
    with database.cursor() as cursor:
        return snapshot_scopes(cursor, SCOPES)


def _rows(style):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT garment_color_code, option_row, position, design_id, logo_code, "
                "color_scheme_id, sort_order, active FROM logo.assignment "
                "WHERE fdm4_store='S_TEST' AND product_style=%s "
                "ORDER BY garment_color_code, option_row, position",
                (style,),
            )
            return cursor.fetchall()


def _stage(tool, arguments, call_id):
    change_set = new_change_set(_session(), USER)
    staged = stage_write(change_set["id"], tool, arguments, call_id, USER, max_items=50)
    return change_set, staged


def _apply(change_set, staged):
    return apply_change_set(
        change_set["id"], USER,
        revision=staged["revision"], confirmed_hash=staged["preview_hash"],
        acknowledge_hard_delete=False,
    )


def _round_trip(tool, arguments, call_id):
    """Stage (preview must not persist), apply, undo; return staged + applied rows."""
    before = _snapshot()
    change_set, staged = _stage(tool, arguments, call_id)
    assert states_equal(_snapshot(), before), "preview leaked into the warehouse"
    _apply(change_set, staged)
    applied = {style: _rows(style) for style in ("STYLE-1", "STYLE-2")}
    undone = undo_change_set(change_set["id"], USER)
    assert undone["status"] == "undone"
    assert states_equal(_snapshot(), before)
    return staged, applied


def test_copy_style_to_many_round_trip():
    staged, applied = _round_trip(
        "copy_style_to_many",
        {"store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2"],
         "color_match": "exact", "mode": "merge"},
        "bulk-copy",
    )
    assert staged["preview_results"][0]["totals"]["created"] == 2
    assert [(r[1], r[2], r[3]) for r in applied["STYLE-2"]] == [(1, 1, "DESIGN-1"), (1, 2, "DESIGN-2")]
    assert len(staged["preview_diff"]["changes"]) == 2
    assert _rows("STYLE-2") == []


def test_copy_style_to_many_rejects_source_among_targets():
    with pytest.raises(InvalidCommand):
        _stage("copy_style_to_many",
               {"store": "S_TEST", "source_style": "STYLE-1", "target_styles": ["STYLE-2", "STYLE-1"],
                "color_match": "exact", "mode": "merge"}, "bulk-copy-bad")


def test_paste_logo_set_on_every_color_of_many_styles_round_trip():
    staged, applied = _round_trip(
        "paste_logo_set",
        {"store": "S_TEST", "styles": ["STYLE-1", "STYLE-2"], "color_scope": "all",
         "match_color": None, "rows": [ROW], "overwrite": False, "as_new_rows": False},
        "bulk-paste",
    )
    assert staged["preview_results"][0]["totals"]["created"] == 4
    assert [(r[0], r[1]) for r in applied["STYLE-1"] if r[1] == 2] == [("BLU", 2), ("GRN", 2), ("RED", 2)]
    assert [(r[0], r[1], r[3]) for r in applied["STYLE-2"]] == [("RED", 2, "DESIGN-1")]


def test_paste_logo_set_match_scope_needs_a_color():
    with pytest.raises(InvalidCommand):
        _stage("paste_logo_set",
               {"store": "S_TEST", "styles": ["STYLE-1"], "color_scope": "match",
                "match_color": None, "rows": [ROW], "overwrite": False, "as_new_rows": False},
               "bulk-paste-bad")


def test_replace_design_round_trip_touches_only_named_styles():
    staged, applied = _round_trip(
        "replace_design",
        {"store": "S_TEST", "from_design_id": "DESIGN-2", "from_color_scheme_id": None,
         "to_design_id": "DESIGN-1", "to_color_scheme_id": "SCHEME-1", "to_logo_code": None,
         "styles": ["STYLE-1"]},
        "bulk-swap",
    )
    result = staged["preview_results"][0]
    assert result["applied"] == 1 and result["target"]["logo_code"] == "C1"
    swapped = [r for r in applied["STYLE-1"] if r[2] == 2][0]
    assert (swapped[3], swapped[4], swapped[5]) == ("DESIGN-1", "C1", "SCHEME-1")
    assert applied["STYLE-2"] == []


def test_reorder_logo_rows_round_trip():
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO logo.assignment (fdm4_store, product_style, garment_color_code, option_row, "
                "position, design_id, logo_code, color_scheme_id, location, optional, background, "
                "cost_override, sort_order, image_url, name_override, active, updated_by) VALUES "
                "('S_TEST','STYLE-1','RED',2,1,'DESIGN-2','C2','SCHEME-2','Left Chest',false,'',NULL,5,'',NULL,true,'seed')"
            )
    staged, applied = _round_trip(
        "reorder_logo_rows",
        {"store": "S_TEST", "style": "STYLE-1", "garment_color_code": "RED",
         "option_rows": [2, 1], "apply_to": "color"},
        "bulk-reorder",
    )
    assert staged["preview_results"][0]["updated"] == 3
    order = {(r[1], r[2]): r[6] for r in applied["STYLE-1"] if r[0] == "RED"}
    assert order == {(2, 1): 10, (1, 1): 20, (1, 2): 20}


def test_set_styles_active_round_trip_reports_styles_without_rows():
    staged, applied = _round_trip(
        "set_styles_active",
        {"store": "S_TEST", "styles": ["STYLE-1", "STYLE-2"], "active": False},
        "bulk-hide",
    )
    result = staged["preview_results"][0]
    assert result["updated"] == 2
    assert result["results"][1]["style"] == "STYLE-2" and "error" in result["results"][1]
    assert all(r[7] is False for r in applied["STYLE-1"])


def test_list_design_usage_returns_style_codes_for_replace_design():
    with database.cursor() as cursor:
        usage = queries.list_design_usage(cursor, store="S_TEST", design_id="DESIGN-2")
        narrowed = queries.list_design_usage(cursor, store="S_TEST", design_id="DESIGN-2", color_scheme_id="nope")
    assert usage["style_codes"] == ["STYLE-1"] and usage["total_rows"] == 1
    assert usage["styles"][0]["name"] == "Style One" and usage["styles"][0]["colors"] == "RED"
    assert narrowed["style_codes"] == [] and narrowed["color_scheme_id"] == "NOPE"
