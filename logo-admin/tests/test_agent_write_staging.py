"""A model-requested write can only create pending metadata."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import agent
from authorization import AccessContext
from config import get_settings
from db import database
from snapshots import canonical_json
from staging import new_change_set, stage_write
from tests.fakes.openai import FakeOpenAI, completed_text, completed_tool_call
from tool_registry import agent_tool_schemas


def _business_state():
    with database.cursor() as cursor:
        state = {}
        for table in (
            "logo.assignment",
            "logo.store_settings",
            "woo.store_pricing_tier",
        ):
            cursor.execute(
                f"SELECT to_jsonb(t) AS row FROM {table} t ORDER BY to_jsonb(t)::text"
            )
            state[table] = [dict(row["row"]) for row in cursor.fetchall()]
        return state


def _pending_set():
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
                "write staging fixture",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
    return new_change_set(session_id, "admin-one"), store


def test_staged_model_write_changes_only_pending_metadata():
    change_set, store = _pending_set()
    before = _business_state()
    result = stage_write(
        change_set["id"],
        "update_store_settings",
        {"store": store, "enabled": False, "allows_none": True},
        "model-call-1",
        "admin-one",
        max_items=50,
    )
    assert result["status"] == "pending"
    assert result["revision"] == 1
    assert result["items"] == 1
    assert len(result["preview_hash"]) == 64
    assert canonical_json(_business_state()) == canonical_json(before)


def test_write_tool_schemas_never_include_lifecycle_actions():
    names = {item["name"] for item in agent_tool_schemas(writes_enabled=True)}
    assert names.isdisjoint({
        "confirm_change_set",
        "apply_change_set",
        "discard_change_set",
        "undo_change_set",
        "confirm_spreadsheet_mapping",
    })


async def test_fake_model_write_round_stages_only_pending_metadata(monkeypatch):
    change_set, store = _pending_set()
    before = _business_state()
    fake = FakeOpenAI([
        completed_tool_call(
            "update_store_settings",
            "model-write-call",
            json.dumps({
                "store": store,
                "enabled": False,
                "allows_none": True,
            }),
        ),
        completed_text("I staged the settings change for human review."),
    ])
    monkeypatch.setattr(
        agent.quotas,
        "reserve",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        agent.quotas,
        "mark_provider_started",
        lambda reservation: True,
    )
    monkeypatch.setattr(
        agent.quotas,
        "reconcile",
        lambda reservation, **kwargs: True,
    )
    monkeypatch.setattr(
        agent.quotas,
        "retain",
        lambda reservation: True,
    )
    settings = replace(
        get_settings(),
        agent_enabled=True,
        agent_writes_enabled=True,
        openai_api_key="test-key",
        openai_model="test-model",
    )

    events = [event async for event in agent.run_turn(
        AccessContext("admin-one", "Admin One"),
        [],
        settings,
        session_id=change_set["session_id"],
        client_factory=lambda _settings: fake,
    )]

    staged_events = [
        event for event in events
        if event.get("type") == "tool" and event.get("staged") is True
    ]
    assert len(staged_events) == 1
    assert staged_events[0]["change_set_id"] == str(change_set["id"])
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, revision FROM logo.agent_change_set
             WHERE id = %s AND user_login = 'admin-one'
            """,
            (change_set["id"],),
        )
        persisted = cursor.fetchone()
    assert dict(persisted) == {"status": "pending", "revision": 1}
    assert canonical_json(_business_state()) == canonical_json(before)
