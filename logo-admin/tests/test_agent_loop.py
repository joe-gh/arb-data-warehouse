from dataclasses import replace

import pytest

import agent
from authorization import AccessContext
from config import get_settings
import quotas
from tests.fakes.openai import (
    FakeOpenAI,
    completed_text,
    completed_tool_call,
)


class FakeReservation:
    pass


@pytest.fixture
def no_quota(monkeypatch):
    calls = {"reserve": 0, "reconcile": 0}

    def reserve(**kwargs):
        calls["reserve"] += 1
        return FakeReservation()

    def reconcile(reservation, **kwargs):
        del reservation, kwargs
        calls["reconcile"] += 1

    monkeypatch.setattr(agent.quotas, "reserve", reserve)
    monkeypatch.setattr(agent.quotas, "reconcile", reconcile)
    monkeypatch.setattr(agent.quotas, "retain", lambda reservation: None)
    monkeypatch.setattr(
        agent.quotas,
        "mark_provider_started",
        lambda reservation: True,
    )
    return calls


def _settings(**changes):
    return replace(
        get_settings(),
        agent_enabled=True,
        openai_api_key="test-key",
        openai_model="test-model",
        **changes,
    )


async def _collect(iterator):
    return [event async for event in iterator]


async def test_stream_uses_store_false_and_local_replay(no_quota):
    fake = FakeOpenAI([completed_text("hello")])
    prior = [{"role": "user", "content": [{"type": "input_text", "text": "old"}]}]
    settings = _settings()
    events = await _collect(agent.run_turn(
        AccessContext("admin-one", "Admin One"),
        prior,
        settings,
        client_factory=lambda settings: fake,
    ))
    call = fake.responses.calls[0]
    assert call["store"] is False
    assert call["parallel_tool_calls"] is False
    assert call["stream"] is True
    assert "previous_response_id" not in call
    assert call["input"] == prior
    assert call["safety_identifier"] != "admin-one"
    assert len(call["safety_identifier"]) == 64
    assert events[-1]["type"] == "done"
    assert events[-1]["replay_items"][0]["type"] == "message"
    assert no_quota == {"reserve": 1, "reconcile": 1}


async def test_function_result_is_replayed_by_call_id(no_quota):
    fake = FakeOpenAI([
        completed_tool_call("list_stores", "call-1", "{}"),
        completed_text("done"),
    ])

    def dispatch(name, arguments, context, settings):
        del context, settings
        assert name == "list_stores"
        assert arguments == {}
        return {"stores": []}

    events = await _collect(agent.run_turn(
        AccessContext("admin-one", "Admin One"),
        [],
        _settings(),
        dispatch=dispatch,
        client_factory=lambda settings: fake,
    ))
    second_input = fake.responses.calls[1]["input"]
    outputs = [item for item in second_input if item.get("type") == "function_call_output"]
    assert outputs == [{
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"result":{"stores":[]},"truncated":false}',
    }]
    assert [event["type"] for event in events].count("tool") == 1
    assert no_quota == {"reserve": 2, "reconcile": 2}


async def test_quota_failure_happens_before_client_construction(monkeypatch):
    constructed = False

    def fail_reservation(**kwargs):
        del kwargs
        raise quotas.QuotaExceeded("no budget")

    def factory(settings):
        nonlocal constructed
        del settings
        constructed = True
        raise AssertionError("must not construct")

    monkeypatch.setattr(agent.quotas, "reserve", fail_reservation)
    with pytest.raises(quotas.QuotaExceeded):
        await _collect(agent.run_turn(
            AccessContext("admin-one", "Admin One"),
            [],
            _settings(),
            client_factory=factory,
        ))
    assert constructed is False


def test_oversized_tool_output_remains_valid_structured_json():
    bounded = agent.bounded_tool_output({"value": "x" * 10_000}, 100)
    assert bounded["truncated"] is True
    assert bounded["original_bytes"] > bounded["limit_bytes"]
    assert "value" not in bounded


async def test_failed_stream_retains_reservation_when_usage_is_unknown(no_quota):
    fake = FakeOpenAI([[{"type": "response.failed"}]])
    with pytest.raises(agent.AgentProviderError):
        await _collect(agent.run_turn(
            AccessContext("admin-one", "Admin One"),
            [],
            _settings(),
            client_factory=lambda settings: fake,
        ))
    assert no_quota == {"reserve": 1, "reconcile": 0}


async def test_cumulative_turn_replay_cap_fails_before_persistence(no_quota):
    fake = FakeOpenAI([completed_text("x" * 2_000)])
    with pytest.raises(agent.AgentLimitError, match="replay byte limit"):
        await _collect(agent.run_turn(
            AccessContext("admin-one", "Admin One"),
            [],
            _settings(agent_max_turn_replay_bytes=500),
            client_factory=lambda settings: fake,
        ))


def test_replay_size_counter_does_not_materialize_serialized_blob(monkeypatch):
    calls = []
    original_encoder = agent.json.JSONEncoder

    class RecordingEncoder(original_encoder):
        def iterencode(self, value, _one_shot=False):
            calls.append(value)
            return super().iterencode(value, _one_shot=_one_shot)

    monkeypatch.setattr(agent.json, "JSONEncoder", RecordingEncoder)
    bounded = agent.bounded_tool_output({"value": "x" * 10_000}, 100)
    assert bounded["truncated"] is True
    assert calls and isinstance(calls[0], dict)
