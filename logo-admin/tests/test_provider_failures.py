"""Provider failures retain unknown usage reservations and expose safe errors."""

import asyncio
from types import SimpleNamespace

import pytest

import agent
from agent import AgentProviderError, AgentToolError, run_turn
from authorization import AccessContext
import quotas


class Stream:
    def __init__(self, events):
        self.events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)


class Responses:
    def __init__(self, *, events=None, error=None):
        self.events = events or []
        self.error = error

    async def create(self, **_kwargs):
        if self.error:
            raise self.error
        return Stream(self.events)


class ClosableStream(Stream):
    def __init__(self, events, *, block=False):
        super().__init__(events)
        self.block = block
        self.closed = False

    async def __anext__(self):
        if self.block:
            await asyncio.Event().wait()
        return await super().__anext__()

    async def close(self):
        self.closed = True


class ClosableClient:
    def __init__(self, stream):
        self.stream = stream
        self.create_calls = 0
        self.closed = False
        self.responses = self

    async def create(self, **_kwargs):
        self.create_calls += 1
        return self.stream

    async def close(self):
        self.closed = True


def _settings():
    return SimpleNamespace(
        agent_turn_timeout_seconds=10,
        agent_max_output_tokens=128,
        agent_max_tool_calls=3,
        agent_max_tool_result_bytes=1024,
        openai_model="test-model",
        session_secret="provider-failure-test-secret-0123456789",
    )


def _quota_fakes(monkeypatch):
    activity = {"reconciled": [], "retained": []}
    reservation = SimpleNamespace(token="reservation")
    monkeypatch.setattr(agent.quotas, "reserve", lambda **_kwargs: reservation)
    monkeypatch.setattr(
        agent.quotas,
        "reconcile",
        lambda value, **usage: activity["reconciled"].append((value, usage)),
    )
    monkeypatch.setattr(
        agent.quotas,
        "retain",
        lambda value: activity["retained"].append(value),
    )
    monkeypatch.setattr(
        agent.quotas,
        "mark_provider_started",
        lambda value: True,
    )
    return activity


async def _collect(iterator):
    return [event async for event in iterator]


@pytest.mark.asyncio
async def test_response_failed_event_retains_reservation_then_raises(monkeypatch):
    activity = _quota_fakes(monkeypatch)
    client = SimpleNamespace(responses=Responses(events=[{"type": "response.failed"}]))
    with pytest.raises(AgentProviderError):
        await _collect(run_turn(
            AccessContext("admin-one", "Admin"),
            [{"role": "user", "content": "hello"}],
            _settings(),
            client_factory=lambda _settings: client,
        ))
    assert activity["reconciled"] == []
    assert len(activity["retained"]) == 1


@pytest.mark.asyncio
async def test_truncated_stream_without_completed_event_is_rejected(monkeypatch):
    activity = _quota_fakes(monkeypatch)
    client = SimpleNamespace(responses=Responses(events=[{
        "type": "response.output_text.delta",
        "delta": "partial",
    }]))
    with pytest.raises(AgentProviderError, match="without response.completed"):
        await _collect(run_turn(
            AccessContext("admin-one", "Admin"), [], _settings(),
            client_factory=lambda _settings: client,
        ))
    assert activity["reconciled"] == []
    assert len(activity["retained"]) == 1


@pytest.mark.asyncio
async def test_provider_exception_retains_unknown_usage_reservation(monkeypatch):
    activity = _quota_fakes(monkeypatch)
    client = SimpleNamespace(responses=Responses(error=RuntimeError("provider 500")))
    with pytest.raises(RuntimeError, match="provider 500"):
        await _collect(run_turn(
            AccessContext("admin-one", "Admin"), [], _settings(),
            client_factory=lambda _settings: client,
        ))
    assert activity["reconciled"] == []
    assert len(activity["retained"]) == 1


@pytest.mark.asyncio
async def test_malformed_function_arguments_are_rejected(monkeypatch):
    _quota_fakes(monkeypatch)
    response = {
        "output": [{
            "type": "function_call",
            "name": "list_stores",
            "call_id": "call-1",
            "arguments": "not-json",
        }],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    client = SimpleNamespace(responses=Responses(events=[{
        "type": "response.completed",
        "response": response,
    }]))
    with pytest.raises(AgentToolError):
        await _collect(run_turn(
            AccessContext("admin-one", "Admin"), [], _settings(),
            client_factory=lambda _settings: client,
        ))


@pytest.mark.asyncio
async def test_quota_rejection_happens_before_client_factory(monkeypatch):
    called = False

    def reject(**_kwargs):
        raise quotas.QuotaExceeded("cap")

    def client_factory(_settings):
        nonlocal called
        called = True

    monkeypatch.setattr(agent.quotas, "reserve", reject)
    with pytest.raises(quotas.QuotaExceeded):
        await _collect(run_turn(
            AccessContext("admin-one", "Admin"), [], _settings(),
            client_factory=client_factory,
        ))
    assert called is False


@pytest.mark.asyncio
async def test_provider_stream_and_owned_client_close_after_success(monkeypatch):
    _quota_fakes(monkeypatch)
    response = {
        "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    stream = ClosableStream([{
        "type": "response.completed",
        "response": response,
    }])
    client = ClosableClient(stream)
    events = await _collect(run_turn(
        AccessContext("admin-one", "Admin"),
        [],
        _settings(),
        client_factory=lambda _settings: client,
    ))
    assert events[-1]["type"] == "done"
    assert stream.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_provider_stream_and_owned_client_close_on_cancellation(monkeypatch):
    activity = _quota_fakes(monkeypatch)
    stream = ClosableStream([], block=True)
    client = ClosableClient(stream)
    task = asyncio.create_task(_collect(run_turn(
        AccessContext("admin-one", "Admin"),
        [],
        _settings(),
        client_factory=lambda _settings: client,
    )))
    while client.create_calls == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True
    assert client.closed is True
    assert len(activity["retained"]) == 1


@pytest.mark.asyncio
async def test_failed_provider_started_marker_aborts_before_provider_io(monkeypatch):
    activity = _quota_fakes(monkeypatch)
    monkeypatch.setattr(
        agent.quotas,
        "mark_provider_started",
        lambda _reservation: False,
    )
    stream = ClosableStream([])
    client = ClosableClient(stream)
    with pytest.raises(AgentProviderError, match="provider-started"):
        await _collect(run_turn(
            AccessContext("admin-one", "Admin"),
            [],
            _settings(),
            client_factory=lambda _settings: client,
        ))
    assert client.create_calls == 0
    assert client.closed is True
    assert len(activity["reconciled"]) == 1
    assert activity["retained"] == []
