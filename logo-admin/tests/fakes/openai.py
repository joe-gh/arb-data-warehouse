"""Deterministic async Responses API fake with complete call recording."""

from collections import deque
from copy import deepcopy


class FakeStream:
    def __init__(self, events):
        self._events = iter(deepcopy(events))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(event, BaseException):
            raise event
        return event


class FakeResponses:
    def __init__(self, streams):
        self.streams = deque(deepcopy(streams))
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if not self.streams:
            raise AssertionError("No fake response stream remains")
        return FakeStream(self.streams.popleft())


class FakeOpenAI:
    def __init__(self, streams):
        self.responses = FakeResponses(streams)


def completed_text(text: str, *, input_tokens: int = 10, output_tokens: int = 5):
    item = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return [
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "output": [item],
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        },
    ]


def completed_tool_call(name: str, call_id: str, arguments: str):
    item = {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": arguments,
    }
    return [
        {
            "type": "response.function_call_arguments.done",
            "name": name,
            "call_id": call_id,
            "arguments": arguments,
        },
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        },
    ]
