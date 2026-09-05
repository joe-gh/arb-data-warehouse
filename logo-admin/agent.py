"""Read-only OpenAI Responses loop with local replay and bounded tools."""

import asyncio
from contextlib import AsyncExitStack
import inspect
import json
import logging
import time
from typing import Any, AsyncIterator, Callable, Optional

from fastapi.encoders import jsonable_encoder
from openai import AsyncOpenAI
from pydantic import ValidationError

from authorization import AccessContext, provider_safety_identifier
from agent_logging import log_event
from config import Settings
from domain import DomainError
import quotas
import queries
from tool_registry import (
    ToolRegistryError,
    agent_tool_schemas,
    execute_agent_tool,
)


from agent_prompt import (  # noqa: F401 - re-exported for callers/tests
    READ_ONLY_INSTRUCTIONS,
    WRITE_STAGING_INSTRUCTIONS,
    build_instructions,
)


def _tool_error_detail(exc: BaseException, limit: int = 500) -> str:
    """Compact, model-readable reason a tool call was rejected."""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:8]:
            loc = ".".join(str(x) for x in error.get("loc", ()))
            parts.append(f"{loc or 'input'}: {error.get('msg', 'invalid')}")
        return "; ".join(parts)[:limit]
    return str(exc)[:limit] or type(exc).__name__


logger = logging.getLogger("arb_logo_admin.agent")


class AgentError(RuntimeError):
    pass


class AgentProviderError(AgentError):
    pass


class AgentLimitError(AgentError):
    pass


class AgentToolError(AgentError):
    pass


def _client_factory(settings: Settings) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=30.0,
        max_retries=1,
    )


def _item_dict(item: Any) -> dict:
    if isinstance(item, dict):
        return jsonable_encoder(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    raise AgentProviderError("OpenAI returned an invalid output item")


def _item_value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _event_value(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def bounded_tool_output(result: Any, maximum_bytes: int) -> dict:
    encoded = jsonable_encoder(result)
    encoded_bytes = sum(
        len(chunk.encode("utf-8"))
        for chunk in json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).iterencode(encoded)
    )
    if encoded_bytes <= maximum_bytes:
        return {"truncated": False, "result": encoded}
    return {
        "truncated": True,
        "original_bytes": encoded_bytes,
        "limit_bytes": int(maximum_bytes),
        "message": "Tool result exceeded the agent result limit; narrow the query.",
    }


def _json_item_bytes(item: Any) -> int:
    encoded = jsonable_encoder(item)
    return sum(
        len(chunk.encode("utf-8"))
        for chunk in json.JSONEncoder(default=str).iterencode(encoded)
    )


def _extend_emitted_replay(
    emitted: list[dict],
    items: list[dict],
    current_bytes: int,
    maximum_bytes: int,
) -> int:
    next_bytes = current_bytes
    for item in items:
        if emitted or next_bytes > 2:
            next_bytes += 2  # Default JSON list separator is `, `.
        next_bytes += _json_item_bytes(item)
        if next_bytes > maximum_bytes:
            raise AgentLimitError("Agent turn replay byte limit reached")
        emitted.append(item)
    return next_bytes


def _usage(response: Any) -> tuple[bool, int, int]:
    usage = _item_value(response, "usage")
    if usage is None:
        return False, 0, 0
    return (
        True,
        int(_item_value(usage, "input_tokens", 0) or 0),
        int(_item_value(usage, "output_tokens", 0) or 0),
    )


def _estimate_tokens(
    replay: list[dict],
    maximum_output: int,
    *,
    instructions: str,
    tools: list[dict],
) -> int:
    raw = json.dumps(
        {
            "instructions": instructions,
            "input": jsonable_encoder(replay),
            "tools": tools,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # One token cannot encode less than one UTF-8 byte. Reserving one token per
    # serialized byte is deliberately conservative and keeps the hard local
    # cap ahead of provider accounting for multilingual/code-heavy content.
    estimated_input = max(1, len(raw))
    return estimated_input + int(maximum_output)


async def _reconcile(
    reservation: quotas.Reservation,
    input_tokens: int,
    output_tokens: int,
) -> None:
    await _joinable_to_thread(
        quotas.reconcile,
        reservation,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def _retain(reservation: quotas.Reservation) -> None:
    await _joinable_to_thread(quotas.retain, reservation)


async def _joinable_to_thread(function: Callable, /, *args, **kwargs):
    """Do not abandon a mutating worker when the async caller is cancelled."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # asyncio cancellation cannot stop a running worker thread. Keep the
        # route's lease/capacity until the bounded DB operation has actually
        # finished, then propagate cancellation.
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


async def _close_async_resource(resource: Any) -> None:
    """Close a provider stream/client and join cleanup across cancellation."""

    if resource is None:
        return
    close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if close is None:
        return
    result = close()
    if not inspect.isawaitable(result):
        return
    task = asyncio.create_task(result)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


async def run_turn(
    context: AccessContext,
    replay_items: list[dict],
    settings: Settings,
    *,
    session_id=None,
    dispatch: Optional[Callable] = None,
    client_factory: Callable[[Settings], Any] = _client_factory,
    screen: Optional[dict] = None,
) -> AsyncIterator[dict]:
    """Stream one turn and return replay material only in the terminal event."""

    replay = list(replay_items)
    emitted_replay: list[dict] = []
    tool_call_count = 0
    total_input_tokens = 0
    total_output_tokens = 0
    emitted_replay_bytes = 2  # JSON array brackets.
    maximum_replay_bytes = int(getattr(
        settings,
        "agent_max_turn_replay_bytes",
        1_000_000,
    ))
    client = None
    writes_enabled = bool(getattr(settings, "agent_writes_enabled", False))
    # Static knowledge + mode rules, plus one trusted line naming the store the
    # operator has selected in the UI (validated; never free text).
    instructions = build_instructions(
        writes_enabled=writes_enabled,
        screen=screen,
    )
    tools = agent_tool_schemas(
        writes_enabled=writes_enabled,
        write_tools=getattr(settings, "agent_write_tools", None),
        context=context,
        settings=settings,
    )

    async with (
        AsyncExitStack() as provider_resources,
        asyncio.timeout(settings.agent_turn_timeout_seconds),
    ):
        while True:
            reserved_tokens = _estimate_tokens(
                replay,
                settings.agent_max_output_tokens,
                instructions=instructions,
                tools=tools,
            )
            try:
                reservation = await quotas.reserve_async(
                    user_login=context.user_login,
                    reserved_tokens=reserved_tokens,
                    settings=settings,
                )
            except quotas.QuotaExceeded:
                log_event(
                    "quota_reject",
                    user_login=context.user_login,
                    status="rejected",
                )
                raise
            response_input = 0
            response_output = 0
            usage_known = False
            stream = None
            completed_response = None
            request_started = False
            provider_started = time.monotonic()
            try:
                if client is None:
                    # The first quota reservation always happens before this.
                    client = client_factory(settings)
                    provider_resources.push_async_callback(
                        _close_async_resource,
                        client,
                    )
                marker_written = await _joinable_to_thread(
                    quotas.mark_provider_started,
                    reservation,
                )
                if not marker_written:
                    raise AgentProviderError(
                        "Quota reservation could not be marked provider-started"
                    )
                request_started = True
                stream = await client.responses.create(
                    model=settings.openai_model,
                    instructions=instructions,
                    input=replay,
                    tools=tools,
                    parallel_tool_calls=False,
                    max_output_tokens=settings.agent_max_output_tokens,
                    store=False,
                    stream=True,
                    safety_identifier=provider_safety_identifier(
                        context.user_login,
                        settings,
                    ),
                )
                async for event in stream:
                    event_type = str(_event_value(event, "type", ""))
                    if event_type == "response.output_text.delta":
                        yield {
                            "type": "token",
                            "text": str(_event_value(event, "delta", "")),
                        }
                    elif event_type == "response.output_item.done":
                        item = _event_value(event, "item")
                        yield {
                            "type": "output_item",
                            "item_type": str(_item_value(item, "type", "unknown")),
                        }
                    elif event_type == "response.completed":
                        completed_response = _event_value(event, "response")
                    elif event_type in {"response.failed", "error"}:
                        raise AgentProviderError("OpenAI response failed")

                if completed_response is None:
                    raise AgentProviderError(
                        "stream ended without response.completed"
                    )
                usage_known, response_input, response_output = _usage(
                    completed_response
                )
                log_event(
                    "provider_complete",
                    user_login=context.user_login,
                    status="complete",
                    duration_ms=round(
                        (time.monotonic() - provider_started) * 1000,
                    ),
                    input_tokens=response_input,
                    output_tokens=response_output,
                )
            except Exception:
                log_event(
                    "provider_failure",
                    user_login=context.user_login,
                    status="failed",
                    duration_ms=round(
                        (time.monotonic() - provider_started) * 1000,
                    ),
                )
                raise
            finally:
                try:
                    await _close_async_resource(stream)
                finally:
                    if usage_known:
                        await _reconcile(
                            reservation,
                            response_input,
                            response_output,
                        )
                    elif request_started:
                        # Unknown/partial provider failures may still be billable.
                        # Retaining the conservative reservation keeps the local
                        # daily/monthly ceilings fail-closed.
                        log_event(
                            "quota_reservation_retained",
                            user_login=context.user_login,
                            status="usage_unknown",
                        )
                        await _retain(reservation)
                    else:
                        await _reconcile(reservation, 0, 0)

            total_input_tokens += response_input
            total_output_tokens += response_output
            output = _item_value(completed_response, "output", []) or []
            output_items = [_item_dict(item) for item in output]
            emitted_replay_bytes = _extend_emitted_replay(
                emitted_replay,
                output_items,
                emitted_replay_bytes,
                maximum_replay_bytes,
            )
            replay.extend(output_items)

            calls = [
                item
                for item in output
                if _item_value(item, "type") == "function_call"
            ]
            if not calls:
                break
            tool_call_count += len(calls)
            if tool_call_count > settings.agent_max_tool_calls:
                raise AgentLimitError("Agent tool-call limit reached")

            function_outputs: list[dict] = []
            for call in calls:
                name = str(_item_value(call, "name", ""))
                call_id = str(_item_value(call, "call_id", ""))
                if not call_id:
                    raise AgentProviderError(
                        "OpenAI returned a function call without a call_id"
                    )
                raw_arguments = _item_value(call, "arguments", "{}")
                tool_started = time.monotonic()
                try:
                    arguments = json.loads(str(raw_arguments))
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                except ValueError as exc:
                    # Not JSON at all: the model malfunctioned rather than
                    # chose bad values, so the turn stops here.
                    log_event(
                        "tool_failure",
                        user_login=context.user_login,
                        tool_name=name,
                        status="rejected",
                        kind="MalformedArguments",
                    )
                    raise AgentToolError(
                        f"Tool {name or 'unknown'} rejected its arguments"
                    ) from exc
                try:
                    if dispatch is None:
                        result = await _joinable_to_thread(
                            execute_agent_tool,
                            name,
                            arguments,
                            context,
                            settings,
                            session_id=session_id,
                            call_id=call_id,
                        )
                    else:
                        # Test/custom dispatchers retain the original bounded
                        # four-argument contract.
                        result = await _joinable_to_thread(
                            dispatch,
                            name,
                            arguments,
                            context,
                            settings,
                        )
                except (
                    ValidationError,
                    ToolRegistryError,
                    queries.QueryServiceError,
                    DomainError,
                    ValueError,
                ) as exc:
                    # A rejected call is information for the model, not a
                    # reason to abandon the turn: hand the (bounded) reason
                    # back as the tool output so it can correct the call or
                    # explain to the person what is missing. Logged so the
                    # cause is visible server-side.
                    detail = _tool_error_detail(exc)
                    logger.warning(
                        "agent tool %s rejected: %s: %s", name or "unknown", type(exc).__name__, detail,
                    )
                    log_event(
                        "tool_failure",
                        user_login=context.user_login,
                        tool_name=name,
                        status="rejected",
                        kind=type(exc).__name__,
                        duration_ms=round(
                            (time.monotonic() - tool_started) * 1000,
                        ),
                    )
                    result = {
                        "ok": False,
                        "error": type(exc).__name__,
                        "detail": detail,
                        "hint": (
                            "The call was not executed. Fix the arguments and call the "
                            "tool again, or tell the person what is missing."
                        ),
                    }
                bounded = bounded_tool_output(
                    result,
                    settings.agent_max_tool_result_bytes,
                )
                log_event(
                    "tool_complete",
                    user_login=context.user_login,
                    tool_name=name,
                    change_set_id=(
                        result.get("change_set_id")
                        if isinstance(result, dict)
                        else None
                    ),
                    revision=(
                        result.get("revision")
                        if isinstance(result, dict)
                        else None
                    ),
                    status=(
                        "staged"
                        if isinstance(result, dict) and result.get("staged")
                        else "complete"
                    ),
                    duration_ms=round(
                        (time.monotonic() - tool_started) * 1000,
                    ),
                    result_bytes=len(json.dumps(
                        bounded,
                        ensure_ascii=False,
                        default=str,
                    ).encode("utf-8")),
                )
                function_outputs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(
                        bounded,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                })
                public_tool_event = {"type": "tool", "name": name}
                if isinstance(result, dict) and result.get("staged") is True:
                    public_tool_event.update({
                        "staged": True,
                        "change_set_id": result.get("change_set_id"),
                        "revision": result.get("revision"),
                        "preview_hash": result.get("preview_hash"),
                        "contains_hard_delete": result.get(
                            "contains_hard_delete",
                            False,
                        ),
                    })
                yield public_tool_event
            emitted_replay_bytes = _extend_emitted_replay(
                emitted_replay,
                function_outputs,
                emitted_replay_bytes,
                maximum_replay_bytes,
            )
            replay.extend(function_outputs)

    yield {
        "type": "done",
        "replay_items": emitted_replay,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "tool_call_count": tool_call_count,
    }
