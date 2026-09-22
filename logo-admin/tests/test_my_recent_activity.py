"""my_recent_activity: the person's own recent operations, grouped by kind,
each with the route to undo it. Scoped server-side to the current login
unless an actor is named for a hand-off; change-set cards stay owner-only."""

from psycopg2.extras import Json
import pytest
from pydantic import ValidationError

import staging
from read_commands import MyRecentActivityCommand
from tests.test_rule_agent_tools import _admin, _session, USER
from tests.test_warehouse_ops_tools import _read


def _seed():
    _admin(
        "INSERT INTO logo.audit_log(actor,action,fdm4_store,product_style,garment_color_code,option_row,position,detail) "
        "VALUES (%s,'assignment_updated','S_TEST','STYLE-1','0001',1,1,%s)",
        (USER, Json({"changes": {"cost_override": {"from": None, "to": "0"}}})),
    )
    _admin("INSERT INTO logo.audit_log(actor,action,fdm4_store,product_style) VALUES (%s,'assignment_updated','S_TEST','STYLE-2')", (USER,))
    _admin("INSERT INTO logo.audit_log(actor,action,fdm4_store,product_style) VALUES (%s,'assignment_created','S_OTHER','STYLE-9')", (USER,))
    _admin("INSERT INTO logo.audit_log(actor,action,fdm4_store,detail) VALUES (%s,'sync_failed','S_TEST',%s)", (USER, Json({"error": "Design conflict in S_TEST: logo A8Y"})))
    _admin("INSERT INTO logo.audit_log(actor,action,fdm4_store,product_style) VALUES ('someone-else','assignment_updated','S_TEST','STYLE-3')")
    _admin("INSERT INTO logo.audit_log(at,actor,action,fdm4_store,product_style) VALUES (now()-interval '5 days',%s,'assignment_deleted','S_TEST','STYLE-WEEK')", (USER,))
    _admin("INSERT INTO logo.audit_log(at,actor,action,fdm4_store,product_style) VALUES (now()-interval '40 days',%s,'assignment_updated','S_TEST','STYLE-OLD')", (USER,))
    _admin(
        "INSERT INTO logo.bulk_batch(fdm4_store,logo_code,created_by,applied,target) VALUES ('S_TEST','C1',%s,12,%s)",
        ("agent:" + USER, Json({"kind": "paste", "style": "STYLE-1"})),
    )
    _admin("INSERT INTO logo.bulk_batch(fdm4_store,logo_code,created_by,undone_at) VALUES ('S_TEST','C2','someone-else',now())")
    mine = staging.new_change_set(_session(), USER)
    staging.stage_write(mine["id"], "set_stock_override", {"style_code": "STYLE-1", "mode": "fake"}, "mine", USER, max_items=50)
    return mine


def test_groups_the_persons_own_work_with_undo_routes():
    mine = _seed()
    result = _read("my_recent_activity", {})

    assert result["actor"] == USER and result["is_you"] is True
    assert result["since_days"] == 1

    edits = {(g["store"], g["action"]): g for g in result["logo_edits"]}
    assert edits[("S_TEST", "assignment_updated")]["styles"] == 2
    assert set(edits[("S_TEST", "assignment_updated")]["sample_styles"]) == {"STYLE-1", "STYLE-2"}
    assert edits[("S_OTHER", "assignment_created")]["styles"] == 1
    assert ("S_TEST", "assignment_deleted") not in edits, "five-day-old work is outside the default day"
    assert "STYLE-3" not in str(result["logo_edits"]), "another person's work is not mine"

    assert [s["action"] for s in result["syncs"]] == ["sync_failed"]
    assert "Design conflict" in result["syncs"][0]["error"]

    assert len(result["bulk_batches"]) == 1
    batch = result["bulk_batches"][0]
    assert batch["kind"] == "paste" and batch["rows_applied"] == 12 and batch["undone_at"] is None
    assert batch["created_by"] == "agent:" + USER

    cards = {c["change_set_id"]: c for c in result["change_sets"]}
    assert cards[str(mine["id"])]["status"] == "pending"
    assert cards[str(mine["id"])]["items"] == 1
    assert cards[str(mine["id"])]["tools"] == ["set_stock_override"]

    entries = result["recent_entries"]
    assert entries and entries[0]["at"] >= entries[-1]["at"]
    cell = next(e for e in entries if e["style"] == "STYLE-1" and e["action"] == "assignment_updated")
    assert (cell["color"], cell["option_row"], cell["position"]) == ("0001", 1, 1)
    assert "cost_override" in cell["change"]

    assert set(result["undo_routes"]) == {"logo_edits", "bulk_batches", "change_sets", "syncs"}
    assert result["truncated"] is False


def test_store_filter_days_and_raw_entry_limit():
    _seed()
    scoped = _read("my_recent_activity", {"store": "s_other"})
    assert {g["store"] for g in scoped["logo_edits"]} == {"S_OTHER"}
    assert scoped["syncs"] == [] and scoped["bulk_batches"] == []
    assert scoped["store"] == "S_OTHER"

    week = _read("my_recent_activity", {"since_days": 7})
    assert ("S_TEST", "assignment_deleted") in {(g["store"], g["action"]) for g in week["logo_edits"]}
    assert "STYLE-OLD" not in str(week)

    one = _read("my_recent_activity", {"limit": 1})
    assert len(one["recent_entries"]) == 1 and one["truncated"] is True


def test_hand_off_actor_sees_their_work_but_never_their_cards():
    _seed()
    result = _read("my_recent_activity", {"actor": "someone-else"})
    assert result["actor"] == "someone-else" and result["is_you"] is False
    assert {g["store"] for g in result["logo_edits"]} == {"S_TEST"}
    assert "STYLE-3" in str(result["logo_edits"])
    assert result["change_sets"] == []
    assert "own" in result["change_sets_note"]
    assert len(result["bulk_batches"]) == 1 and result["bulk_batches"][0]["undone_at"] is not None


def test_command_rejects_unknown_fields_and_bad_ranges():
    with pytest.raises(ValidationError):
        MyRecentActivityCommand(user_login="x")
    with pytest.raises(ValidationError):
        MyRecentActivityCommand(since_days=31)
    assert MyRecentActivityCommand().since_days == 1
