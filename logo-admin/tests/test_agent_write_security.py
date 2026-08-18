"""Security invariants for owner-scoped staged writes."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from db import database
from domain import InvalidCommand, NotFound
from staging import get_change_set, new_change_set, stage_write
from tool_registry import EXCLUDED_AGENT_TOOLS, agent_tool_schemas


def _set():
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
                session_id,
                "admin-one",
                "security fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    return new_change_set(session_id, "admin-one"), store


def test_change_set_item_and_journal_are_invisible_to_nonowner():
    change_set, store = _set()
    stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "security-call",
        "admin-one",
        max_items=50,
    )
    with pytest.raises(NotFound):
        get_change_set(change_set["id"], "admin-two")


@pytest.mark.parametrize("invalid_id", ["", "not-a-uuid", "../../etc/passwd"])
def test_public_change_set_identifier_is_strict_uuid(invalid_id):
    with pytest.raises(NotFound):
        get_change_set(invalid_id, "admin-one")


def test_call_id_and_change_set_caps_fail_closed():
    change_set, store = _set()
    with pytest.raises(InvalidCommand, match="call ID"):
        stage_write(
            change_set["id"],
            "update_store_settings",
            {"store": store, "enabled": False, "allows_none": True},
            "x" * 256,
            "admin-one",
            max_items=50,
        )
    stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "within-cap",
        "admin-one",
        max_items=1,
    )
    with pytest.raises(InvalidCommand, match="item limit"):
        stage_write(
            change_set["id"],
            "update_store_settings",
            {"store": store, "enabled": True, "allows_none": False},
            "over-cap",
            "admin-one",
            max_items=1,
        )


def test_nontransactional_mcp_tools_are_absent_from_model_surface():
    names = {schema["name"] for schema in agent_tool_schemas(writes_enabled=True)}
    assert names.isdisjoint(EXCLUDED_AGENT_TOOLS)
