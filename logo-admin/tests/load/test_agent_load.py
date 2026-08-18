"""Bounded local load checks for previews, result size, and concurrency."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time
import tracemalloc
import uuid

from fastapi import HTTPException
from psycopg2.extras import Json
import pytest

from agent import bounded_tool_output
import agent_repository
from commands import UpdateStoreSettingsCommand
from db import database
import routes_agent
from staging import preview_commands


def _store():
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT fdm4_store FROM woo.store_catalog WHERE suggested=true ORDER BY 1 LIMIT 1"
        )
        return cursor.fetchone()["fdm4_store"]


def _preview(index, store):
    return preview_commands(
        [(
            "update_store_settings",
            UpdateStoreSettingsCommand(
                store=store,
                enabled=bool(index % 2),
                allows_none=bool((index + 1) % 2),
            ),
        )],
        f"admin-{index}",
    )


def test_four_concurrent_previews_fit_the_eight_connection_pool():
    store = _store()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_preview, index, store) for index in range(4)]
        previews = [future.result(timeout=15) for future in futures]
    assert len(previews) == 4
    assert all(preview.semantic_diff["count"] <= 1 for preview in previews)


def test_fifty_item_cumulative_preview_collapses_to_net_effect():
    store = _store()
    commands = [
        (
            "update_store_settings",
            UpdateStoreSettingsCommand(
                store=store,
                enabled=bool(index % 2),
                allows_none=bool(index % 3),
            ),
        )
        for index in range(50)
    ]
    preview = preview_commands(commands, "load-admin")
    assert len(preview.results) == 50
    assert preview.semantic_diff["count"] <= 1


def test_oversized_tool_result_becomes_valid_bounded_object():
    output = bounded_tool_output({"rows": ["x" * 1000]}, 100)
    assert output == {
        "truncated": True,
        "original_bytes": output["original_bytes"],
        "limit_bytes": 100,
        "message": "Tool result exceeded the agent result limit; narrow the query.",
    }
    assert output["original_bytes"] > output["limit_bytes"]


class _ConnectedRequest:
    async def is_disconnected(self):
        return False


async def _consume_chat(message: str) -> bytes:
    response = await routes_agent.chat_route(
        routes_agent.ChatRequest(message=message),
        _ConnectedRequest(),
        user={
            "user_login": "admin-one",
            "display_name": "Admin One",
            "csrf": "test",
        },
    )
    chunks = [chunk async for chunk in response.body_iterator]
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_four_real_route_streams_share_pool_and_fifth_is_bounded_busy(
    monkeypatch,
):
    """Exercise route leases/DB work with four live streams on one event loop."""

    all_started = asyncio.Event()
    release_provider = asyncio.Event()
    started = 0
    started_lock = asyncio.Lock()

    async def blocked_turn(context, replay, settings, *, session_id=None):
        nonlocal started
        del context, replay, settings, session_id
        async with started_lock:
            started += 1
            if started == 4:
                all_started.set()
        yield {"type": "token", "text": "ready"}
        await release_provider.wait()
        yield {
            "type": "done",
            "replay_items": [],
            "input_tokens": 1,
            "output_tokens": 1,
            "tool_call_count": 0,
        }

    monkeypatch.setattr(routes_agent, "run_turn", blocked_turn)
    monkeypatch.setattr(routes_agent, "_turn_semaphore", asyncio.Semaphore(4))
    streams = [
        asyncio.create_task(_consume_chat(f"concurrent stream {index}"))
        for index in range(4)
    ]
    await asyncio.wait_for(all_started.wait(), timeout=5)
    with pytest.raises(HTTPException) as busy:
        await routes_agent.chat_route(
            routes_agent.ChatRequest(message="fifth stream"),
            _ConnectedRequest(),
            user={
                "user_login": "admin-one",
                "display_name": "Admin One",
                "csrf": "test",
            },
        )
    assert busy.value.status_code == 503
    assert busy.value.headers == {"Retry-After": "2"}
    release_provider.set()
    payloads = await asyncio.wait_for(
        asyncio.gather(*streams),
        timeout=10,
    )
    assert all(b'"type":"token"' in payload for payload in payloads)
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS count
              FROM logo.agent_chat_session
             WHERE active_turn_id IS NOT NULL
            """
        )
        assert cursor.fetchone()["count"] == 0


@pytest.mark.asyncio
async def test_route_emits_heartbeat_before_a_slow_provider_and_nginx_is_unbuffered(
    monkeypatch,
):
    provider_release = asyncio.Event()

    async def slow_turn(context, replay, settings, *, session_id=None):
        del context, replay, settings, session_id
        await provider_release.wait()
        yield {
            "type": "done",
            "replay_items": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_call_count": 0,
        }

    real_wait = asyncio.wait
    wait_calls = 0

    async def immediate_first_wait(tasks, *, timeout=None):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            assert timeout == 15.0
            return set(), set(tasks)
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(routes_agent, "run_turn", slow_turn)
    monkeypatch.setattr(routes_agent, "_turn_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(routes_agent.asyncio, "wait", immediate_first_wait)
    response = await routes_agent.chat_route(
        routes_agent.ChatRequest(message="slow stream"),
        _ConnectedRequest(),
        user={
            "user_login": "admin-one",
            "display_name": "Admin One",
            "csrf": "test",
        },
    )
    iterator = response.body_iterator.__aiter__()
    assert await iterator.__anext__() == b": heartbeat\n\n"
    assert response.headers["x-accel-buffering"] == "no"
    provider_release.set()
    remaining = [chunk async for chunk in iterator]
    assert any(b'"type":"done"' in chunk for chunk in remaining)
    artifact = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "deploy"
        / "nginx"
        / "agent-chat.conf"
    ).read_text()
    assert "proxy_buffering off;" in artifact
    assert "proxy_cache off;" in artifact
    assert "gzip off;" in artifact


def test_maximum_public_history_has_bounded_time_and_python_memory():
    session_id = uuid.uuid4()
    rows = []
    now = datetime.now(timezone.utc)
    for index in range(150):
        turn_id = uuid.uuid4()
        for role in ("user", "assistant"):
            rows.append((
                uuid.uuid4(),
                session_id,
                "admin-one",
                turn_id,
                role,
                "complete",
                f"{role}-{index}-" + ("x" * 4_000),
                Json([]),
                now + timedelta(microseconds=len(rows)),
            ))
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            """
            INSERT INTO logo.agent_chat_session (
                id, user_login, title, expires_at
            ) VALUES (%s, 'admin-one', 'maximum history', now() + interval '1 hour')
            """,
            (session_id,),
        )
        cursor.executemany(
            """
            INSERT INTO logo.agent_chat_message (
                id, session_id, user_login, turn_id, role, status,
                content, replay_items, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )

    tracemalloc.start()
    started_at = time.monotonic()
    try:
        with database.cursor() as cursor:
            page = agent_repository.list_messages(
                cursor,
                session_id,
                "admin-one",
                limit=200,
                maximum_bytes=1_000_000,
            )
        elapsed = time.monotonic() - started_at
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert elapsed < 5
    assert peak < 32 * 1024 * 1024
    assert len(page["messages"]) <= 200
    assert page["truncated"] is True
    assert page["oldest_cursor"] is not None
