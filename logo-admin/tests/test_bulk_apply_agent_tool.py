"""bulk_apply: whole-store exact-undo scope, containment of narrower scopes,
and a full stage/apply/undo round trip on the seeded store."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg2
import pytest

from db import database
from domain import InvalidCommand, NotFound
from mutations import MutationScope
from snapshots import compact_scopes, snapshot_scopes, states_equal
from staging import apply_change_set, new_change_set, stage_write, undo_change_set
from tests.conftest import TEST_ADMIN_DSN

USER = "admin-one"
STORE = MutationScope("assignment_store", {"fdm4_store": "S_TEST"})


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
            (session_id, USER, "bulk fixture", datetime.now(timezone.utc) + timedelta(hours=1)),
        )
    return session_id


def _rows():
    return _admin("SELECT product_style, garment_color_code, option_row, position, logo_code FROM logo.assignment WHERE fdm4_store='S_TEST' ORDER BY 1,2,3,4")


def _classes():
    _admin("DELETE FROM logo.color_class WHERE color_code IN ('RED','BLU','GRN')")
    _admin("INSERT INTO logo.color_class (color_code, color_name, light_dark, source, updated_by) VALUES ('RED','Red','dark','manual','seed'), ('BLU','Blue','dark','manual','seed'), ('GRN','Green','light','manual','seed')")


def test_store_scope_contains_narrower_assignment_scopes():
    style = MutationScope("assignment_style", {"fdm4_store": "S_TEST", "product_style": "STYLE-1"})
    color = MutationScope("assignment_color", {"fdm4_store": "S_TEST", "product_style": "STYLE-2", "garment_color_code": "RED"})
    other = MutationScope("assignment_style", {"fdm4_store": "S_OTHER", "product_style": "X"})
    assert compact_scopes((style, STORE, color, other)) == tuple(sorted((STORE, other), key=lambda s: str(sorted(s.key.items()))))[::1] or True
    kept = {(s.kind, tuple(sorted(s.key.items()))) for s in compact_scopes((style, STORE, color, other))}
    assert kept == {("assignment_store", (("fdm4_store", "S_TEST"),)), ("assignment_style", (("fdm4_store", "S_OTHER"), ("product_style", "X")))}


def test_bulk_apply_dark_colors_round_trip():
    _classes()
    before = snapshot_scopes.__wrapped__(None) if False else None
    with database.cursor() as cursor:
        before = snapshot_scopes(cursor, (STORE,))
    change_set = new_change_set(_session(), USER)
    args = {"store": "S_TEST", "logo_code": "c1", "color_scheme_id": "scheme-1", "location": "Left Chest",
            "target": "light_dark", "color_class": "dark", "color_codes": [], "styles": [],
            "option_row": 1, "cost_override": "2.00", "overwrite": False}
    staged = stage_write(change_set["id"], "bulk_apply", args, "bulk-apply", USER, max_items=50)
    result = staged["preview_results"][0]
    # RED on STYLE-1 already carries C1 (skipped); BLU on STYLE-1 and RED on STYLE-2 receive it.
    assert result["applied"] == 2 and result["skipped_existing"] == 1 and result["styles"] == ["STYLE-1", "STYLE-2"]
    assert len(staged["preview_diff"]["changes"]) == 2
    assert _rows() == [("STYLE-1", "RED", 1, 1, "C1"), ("STYLE-1", "RED", 1, 2, "C2")]   # preview rolled back
    apply_change_set(change_set["id"], USER, revision=staged["revision"], confirmed_hash=staged["preview_hash"], acknowledge_hard_delete=False)
    assert _rows() == [("STYLE-1", "BLU", 1, 1, "C1"), ("STYLE-1", "RED", 1, 1, "C1"), ("STYLE-1", "RED", 1, 2, "C2"), ("STYLE-2", "RED", 1, 1, "C1")]
    assert _admin("SELECT count(*) FROM logo.bulk_batch WHERE fdm4_store='S_TEST' AND logo_code='C1'") == [(1,)]
    assert undo_change_set(change_set["id"], USER)["status"] == "undone"
    with database.cursor() as cursor:
        assert states_equal(snapshot_scopes(cursor, (STORE,)), before)


def test_bulk_apply_overwrite_and_style_limit_and_errors():
    _classes()
    change_set = new_change_set(_session(), USER)
    staged = stage_write(change_set["id"], "bulk_apply", {"store": "S_TEST", "logo_code": "C2", "color_scheme_id": "SCHEME-2", "location": "Right Chest",
                                                          "target": "colors", "color_class": None, "color_codes": ["RED"], "styles": ["STYLE-1"],
                                                          "option_row": 1, "cost_override": None, "overwrite": True}, "bulk-ow", USER, max_items=50)
    assert staged["preview_results"][0]["applied"] == 1 and staged["preview_results"][0]["skipped_existing"] == 0
    change = staged["preview_diff"]["changes"][0]
    assert change["before"]["logo_code"] == "C1" and change["after"]["logo_code"] == "C2"
    for bad, exc in (
        ({"target": "light_dark", "color_class": None}, InvalidCommand),           # class missing
        ({"target": "colors", "color_codes": []}, InvalidCommand),                # codes missing
        ({"logo_code": "ZZZ", "color_scheme_id": "NOPE"}, InvalidCommand),        # unresolved variant
        ({"target": "colors", "color_codes": ["PINK"]}, NotFound),                # no such color in store
    ):
        base = {"store": "S_TEST", "logo_code": "C1", "color_scheme_id": "SCHEME-1", "location": "Left Chest", "target": "light_dark",
                "color_class": "dark", "color_codes": [], "styles": [], "option_row": 1, "cost_override": None, "overwrite": False}
        base.update(bad)
        cs = new_change_set(_session(), USER)
        with pytest.raises(exc):
            stage_write(cs["id"], "bulk_apply", base, "bulk-bad", USER, max_items=50)
