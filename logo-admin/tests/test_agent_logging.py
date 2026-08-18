"""Agent logs contain bounded metadata and never sensitive content."""

import json
import logging

import agent_logging


def _payload(caplog):
    record = next(
        record for record in reversed(caplog.records)
        if record.name == "arb_logo_admin.agent"
    )
    return json.loads(record.getMessage())


def test_logging_keeps_only_allowlisted_metadata(caplog):
    caplog.set_level(logging.INFO, logger="arb_logo_admin.agent")
    agent_logging.log_event(
        "tool_complete",
        user_login="admin-one",
        tool_name="list_stores",
        duration_ms=12,
        prompt="SECRET-PROMPT",
        arguments={"password": "SECRET-PASSWORD"},
        result="SECRET-RESULT",
        authorization="Bearer SECRET-TOKEN",
    )
    payload = _payload(caplog)
    assert payload == {
        "duration_ms": 12,
        "event": "tool_complete",
        "tool_name": "list_stores",
        "user_login": "admin-one",
    }
    rendered = json.dumps(payload)
    assert "SECRET" not in rendered


def test_allowed_string_fields_are_length_bounded(caplog):
    caplog.set_level(logging.INFO, logger="arb_logo_admin.agent")
    agent_logging.log_event("x" * 200, provider_status="y" * 1000)
    payload = _payload(caplog)
    assert len(payload["event"]) == 100
    assert len(payload["provider_status"]) == 255


def test_diff_cells_and_provider_objects_are_dropped(caplog):
    caplog.set_level(logging.INFO, logger="arb_logo_admin.agent")
    agent_logging.log_event(
        "apply_conflict",
        change_set_id="00000000-0000-0000-0000-000000000001",
        status="conflict",
        diff={"SECRET-CELL": "SECRET-VALUE"},
        provider_response=object(),
    )
    payload = _payload(caplog)
    assert set(payload) == {"event", "change_set_id", "status"}
