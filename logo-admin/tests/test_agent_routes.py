import asyncio
import json
from threading import Event
import uuid

import pytest

import agent_repository
from authorization import require_agent_access
from db import database
import routes_agent


def test_disabled_agent_is_invisible(client_as):
    client = client_as("admin-one")
    assert client.get("/api/agent/sessions").status_code == 404


def test_non_allowlisted_admin_is_invisible(agent_enabled, client_as):
    client = client_as("admin-two")
    assert client.get("/api/agent/sessions").status_code == 404


def test_unsafe_agent_route_requires_csrf(agent_enabled, client_as):
    client = client_as("admin-one")
    response = client.client.post(
        "/api/agent/sessions",
        json={"title": "Test"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid CSRF token"


def test_owner_can_create_and_read_session(agent_enabled, client_as):
    client = client_as("admin-one")
    created = client.post(
        "/api/agent/sessions",
        json={"title": "My session"},
    )
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]
    assert client.get(f"/api/agent/sessions/{session_id}").status_code == 200


def test_non_owner_gets_404(agent_enabled, client_as):
    owner = client_as("admin-one")
    other = client_as("admin-two")
    session_id = owner.post(
        "/api/agent/sessions",
        json={"title": "Private"},
    ).json()["session"]["id"]
    assert other.get(f"/api/agent/sessions/{session_id}").status_code == 404


def test_chat_stream_persists_local_history(agent_enabled, client_as, monkeypatch):
    async def fake_turn(context, replay, settings, **kwargs):
        del context, settings
        assert kwargs["session_id"] == uuid.UUID(session_id)
        assert replay[-1]["content"][0]["text"] == "hello"
        yield {"type": "token", "text": "Hi"}
        yield {
            "type": "done",
            "replay_items": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi"}],
            }],
            "input_tokens": 4,
            "output_tokens": 1,
            "tool_call_count": 0,
        }

    monkeypatch.setattr(routes_agent, "run_turn", fake_turn)
    client = client_as("admin-one")
    session_id = client.post(
        "/api/agent/sessions",
        json={"title": ""},
    ).json()["session"]["id"]
    response = client.post(
        "/api/agent/chat",
        json={"session_id": session_id, "message": "hello"},
    )
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[0]["type"] == "token"
    assert events[0]["text"] == "Hi"
    assert events[-1]["type"] == "done"
    detail = client.get(f"/api/agent/sessions/{session_id}").json()
    assert [message["role"] for message in detail["messages"]] == [
        "user",
        "assistant",
    ]


def test_session_detail_reconstructs_review_state_without_private_replay_or_file_keys(
    agent_enabled,
    client_as,
):
    client = client_as("admin-one")
    session_id = uuid.UUID(client.post(
        "/api/agent/sessions",
        json={"title": "Recover me"},
    ).json()["session"]["id"])
    turn_id = uuid.uuid4()
    change_set_id = uuid.uuid4()
    job_id = uuid.uuid4()
    with database.cursor(write=True, actor="admin-one") as cursor:
        agent_repository.append_message(
            cursor,
            session_id=session_id,
            user_login="admin-one",
            turn_id=turn_id,
            role="user",
            status="complete",
            content="stage the sheet",
            replay_items=[{"private": "provider replay must not leave server"}],
        )
        cursor.execute(
            """
            INSERT INTO logo.agent_change_set (
                id, session_id, user_login, origin, status, revision,
                preview_hash, preview_diff, affected_scopes, expires_at
            ) VALUES (
                %s, %s, 'admin-one', 'spreadsheet', 'pending', 1,
                %s, '{}'::jsonb, '[]'::jsonb, now() + interval '1 hour'
            )
            """,
            (change_set_id, session_id, "a" * 64),
        )
        cursor.execute(
            """
            INSERT INTO logo.agent_change_set_item (
                id, change_set_id, user_login, call_id,
                tool_name, arguments, sort_order
            ) VALUES (
                %s, %s, 'admin-one', 'spreadsheet:job:2',
                'set_store_pricing_tier',
                '{"fdm4_store":"S_TEST","tier_name":"MSRP"}'::jsonb,
                0
            )
            """,
            (uuid.uuid4(), change_set_id),
        )
        cursor.execute(
            """
            INSERT INTO logo.agent_spreadsheet_job (
                id, session_id, user_login, storage_key, change_set_id,
                original_name, media_type, byte_size, sha256, format_name,
                status, mapping_revision, mapping_hash, mapping, expires_at
            ) VALUES (
                %s, %s, 'admin-one', %s, %s,
                'pricing.csv', 'text/csv', 10, %s, 'csv',
                'mapping_confirmed', 1, %s,
                '{"command":"set_store_pricing_tier"}'::jsonb,
                now() + interval '1 hour'
            )
            """,
            (
                job_id,
                session_id,
                uuid.uuid4(),
                change_set_id,
                "b" * 64,
                "c" * 64,
            ),
        )

    payload = client.get(f"/api/agent/sessions/{session_id}").json()
    assert payload["messages"][0]["content"] == "stage the sheet"
    assert "replay_items" not in payload["messages"][0]
    assert payload["change_sets"][0]["id"] == str(change_set_id)
    assert payload["change_sets"][0]["origin"] == "spreadsheet"
    assert "preview_diff" not in payload["change_sets"][0]
    assert payload["change_sets"][0]["items"][0]["call_id"] == (
        "spreadsheet:job:2"
    )
    assert payload["spreadsheet_jobs"][0]["id"] == str(job_id)
    assert payload["spreadsheet_jobs"][0]["change_set_id"] == str(
        change_set_id
    )
    assert "storage_key" not in payload["spreadsheet_jobs"][0]
    assert "sha256" not in payload["spreadsheet_jobs"][0]


def test_session_history_cursor_returns_disjoint_older_page(
    agent_enabled,
    client_as,
):
    client = client_as("admin-one")
    session_id = uuid.UUID(client.post(
        "/api/agent/sessions",
        json={"title": "Paged"},
    ).json()["session"]["id"])
    with database.cursor(write=True, actor="admin-one") as cursor:
        for index in range(205):
            agent_repository.append_message(
                cursor,
                session_id=session_id,
                user_login="admin-one",
                turn_id=uuid.uuid4(),
                role="user",
                status="complete",
                content=f"message {index}",
                replay_items=[],
            )
    first = client.get(f"/api/agent/sessions/{session_id}").json()
    assert first["messages_truncated"] is True
    assert len(first["messages"]) == 200
    cursor = first["messages_oldest_cursor"]
    second = client.get(
        f"/api/agent/sessions/{session_id}",
        params={
            "before_created_at": cursor["created_at"],
            "before_id": cursor["id"],
        },
    ).json()
    assert len(second["messages"]) == 5
    assert {
        message["id"] for message in first["messages"]
    }.isdisjoint({message["id"] for message in second["messages"]})


def test_session_list_keyset_reaches_more_than_default_page(
    agent_enabled,
    client_as,
):
    client = client_as("admin-one")
    with database.cursor(write=True, actor="admin-one") as cursor:
        for index in range(55):
            session = agent_repository.create_session(
                cursor,
                user_login="admin-one",
                retention_days=30,
                title=f"session {index}",
            )
            cursor.execute(
                """
                UPDATE logo.agent_chat_session
                   SET updated_at = now() - make_interval(secs => %s)
                 WHERE id = %s AND user_login = 'admin-one'
                """,
                (index, session["id"]),
            )
        agent_repository.create_session(
            cursor,
            user_login="admin-two",
            retention_days=30,
            title="another owner's private session",
        )

    first = client.get("/api/agent/sessions").json()
    assert len(first["sessions"]) == 50
    assert first["sessions_truncated"] is True
    assert first["sessions_oldest_cursor"] is not None
    assert {
        session["user_login"] for session in first["sessions"]
    } == {"admin-one"}

    page_cursor = first["sessions_oldest_cursor"]
    second = client.get(
        "/api/agent/sessions",
        params={
            "before_updated_at": page_cursor["updated_at"],
            "before_id": page_cursor["id"],
        },
    ).json()
    assert len(second["sessions"]) == 5
    assert second["sessions_truncated"] is False
    assert {
        session["id"] for session in first["sessions"]
    }.isdisjoint({session["id"] for session in second["sessions"]})


def test_workflow_keysets_prioritize_and_reach_resumable_records(
    agent_enabled,
    client_as,
):
    client = client_as("admin-one")
    session_id = uuid.UUID(client.post(
        "/api/agent/sessions",
        json={"title": "Many workflows"},
    ).json()["session"]["id"])
    actionable_change_sets = []
    resumable_jobs = []
    with database.cursor(write=True, actor="admin-one") as cursor:
        for index in range(25):
            cursor.execute(
                """
                INSERT INTO logo.agent_change_set (
                    id, session_id, user_login, origin, status, revision,
                    preview_hash, preview_diff, affected_scopes,
                    created_at, updated_at, expires_at
                ) VALUES (
                    %s, %s, 'admin-one', 'chat', 'discarded', 1,
                    %s, '{}'::jsonb, '[]'::jsonb,
                    now(), now() - make_interval(secs => %s),
                    now() + interval '1 day'
                )
                """,
                (uuid.uuid4(), session_id, f"{index:064x}", index),
            )
        cursor.execute(
            """
            INSERT INTO logo.agent_change_set (
                id, session_id, user_login, origin, status, revision,
                preview_hash, preview_diff, affected_scopes,
                created_at, updated_at, expires_at
            ) VALUES (
                %s, %s, 'admin-one', 'chat', 'pending', 1,
                %s, '{}'::jsonb, '[]'::jsonb,
                now(), now(), now() - interval '1 minute'
            )
            """,
            (uuid.uuid4(), session_id, "f" * 64),
        )
        for index, status in enumerate(("pending", "applied")):
            change_set_id = uuid.uuid4()
            actionable_change_sets.append(str(change_set_id))
            cursor.execute(
                """
                INSERT INTO logo.agent_change_set (
                    id, session_id, user_login, origin, status, revision,
                    preview_hash, preview_diff, affected_scopes,
                    created_at, updated_at, expires_at
                ) VALUES (
                    %s, %s, 'admin-one', 'chat', %s, 1,
                    %s, '{}'::jsonb, '[]'::jsonb,
                    now() - interval '2 days',
                    now() - make_interval(secs => %s),
                    now() + interval '1 day'
                )
                """,
                (
                    change_set_id,
                    session_id,
                    status,
                    f"{index + 100:064x}",
                    index + 10_000,
                ),
            )

        for index in range(25):
            cursor.execute(
                """
                INSERT INTO logo.agent_spreadsheet_job (
                    id, session_id, user_login, storage_key,
                    original_name, media_type, byte_size, sha256,
                    format_name, status, mapping_revision, mapping_hash,
                    mapping, created_at, expires_at
                ) VALUES (
                    %s, %s, 'admin-one', %s,
                    'done.csv', 'text/csv', 1, %s,
                    'csv', 'staged', 1, %s, '{}'::jsonb,
                    now() - make_interval(secs => %s),
                    now() + interval '1 day'
                )
                """,
                (
                    uuid.uuid4(),
                    session_id,
                    uuid.uuid4(),
                    f"{index + 200:064x}",
                    f"{index + 300:064x}",
                    index,
                ),
            )
        for index, status in enumerate((
            "mapping_processing",
            "mapping_pending",
            "mapping_confirmed",
        )):
            job_id = uuid.uuid4()
            resumable_jobs.append(str(job_id))
            cursor.execute(
                """
                INSERT INTO logo.agent_spreadsheet_job (
                    id, session_id, user_login, storage_key,
                    original_name, media_type, byte_size, sha256,
                    format_name, status, mapping_revision, mapping_hash,
                    mapping, created_at, expires_at
                ) VALUES (
                    %s, %s, 'admin-one', %s,
                    'resume.csv', 'text/csv', 1, %s,
                    'csv', %s, 1, %s, '{}'::jsonb,
                    now() - make_interval(secs => %s),
                    now() + interval '1 day'
                )
                """,
                (
                    job_id,
                    session_id,
                    uuid.uuid4(),
                    f"{index + 400:064x}",
                    status,
                    f"{index + 500:064x}",
                    index + 10_000,
                ),
            )

    first = client.get(f"/api/agent/sessions/{session_id}").json()
    first_change_ids = {item["id"] for item in first["change_sets"]}
    assert set(actionable_change_sets) <= first_change_ids
    assert {
        item["id"] for item in first["change_sets"][:2]
    } == set(actionable_change_sets)
    assert first["change_sets_truncated"] is True
    assert first["change_sets_oldest_cursor"] is not None
    assert all(
        "workflow_priority" not in item for item in first["change_sets"]
    )
    first_job_ids = {item["id"] for item in first["spreadsheet_jobs"]}
    assert set(resumable_jobs) <= first_job_ids
    assert first["spreadsheet_jobs_truncated"] is True
    assert first["spreadsheet_jobs_oldest_cursor"] is not None
    assert all("storage_key" not in job for job in first["spreadsheet_jobs"])
    assert all("sha256" not in job for job in first["spreadsheet_jobs"])
    assert all(
        "workflow_priority" not in job for job in first["spreadsheet_jobs"]
    )

    change_cursor = first["change_sets_oldest_cursor"]
    older_changes = client.get(
        f"/api/agent/sessions/{session_id}",
        params={
            "change_set_before_priority": change_cursor["priority"],
            "change_set_before_updated_at": change_cursor["updated_at"],
            "change_set_before_id": change_cursor["id"],
        },
    ).json()
    assert len(older_changes["change_sets"]) == 8
    assert first_change_ids.isdisjoint({
        item["id"] for item in older_changes["change_sets"]
    })

    job_cursor = first["spreadsheet_jobs_oldest_cursor"]
    older_jobs = client.get(
        f"/api/agent/sessions/{session_id}",
        params={
            "spreadsheet_before_priority": job_cursor["priority"],
            "spreadsheet_before_created_at": job_cursor["created_at"],
            "spreadsheet_before_id": job_cursor["id"],
        },
    ).json()
    assert len(older_jobs["spreadsheet_jobs"]) == 8
    assert first_job_ids.isdisjoint({
        item["id"] for item in older_jobs["spreadsheet_jobs"]
    })


def test_pagination_cursors_must_be_complete(agent_enabled, client_as):
    client = client_as("admin-one")
    session_id = client.post(
        "/api/agent/sessions",
        json={"title": "Cursor validation"},
    ).json()["session"]["id"]
    assert client.get(
        "/api/agent/sessions",
        params={"before_id": uuid.uuid4()},
    ).status_code == 422
    assert client.get(
        f"/api/agent/sessions/{session_id}",
        params={"change_set_before_priority": 0},
    ).status_code == 422
    assert client.get(
        f"/api/agent/sessions/{session_id}",
        params={"spreadsheet_before_priority": 0},
    ).status_code == 422


def test_every_agent_route_uses_single_access_dependency():
    for route in routes_agent.router.routes:
        calls = {
            dependency.call
            for dependency in route.dependant.dependencies
        }
        assert require_agent_access in calls, route.path


def test_prepare_cancellation_compensates_committed_lease(monkeypatch):
    started = Event()
    release = Event()
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    finished = []

    def fake_prepare(**kwargs):
        assert kwargs["turn_id"] == turn_id
        started.set()
        release.wait(timeout=2)
        return ({"id": session_id}, [{"role": "user"}])

    def fake_finish(**kwargs):
        finished.append(kwargs)

    monkeypatch.setattr(routes_agent, "_prepare_turn", fake_prepare)
    monkeypatch.setattr(routes_agent, "_finish_turn", fake_finish)

    async def exercise():
        task = asyncio.create_task(
            routes_agent._prepare_turn_cancellation_safe(
                session_id=session_id,
                message="hello",
                user_login="admin-one",
                turn_id=turn_id,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert len(finished) == 1
    assert finished[0] == {
        "session_id": session_id,
        "user_login": "admin-one",
        "turn_id": turn_id,
        "status": "cancelled",
        "content": "",
        "replay_items": [],
    }


def test_terminal_finish_reports_cancellation_after_commit_without_duplicate(
    monkeypatch,
):
    committed = Event()
    release_worker = Event()
    turn_id = uuid.uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        session = agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=1,
        )
        session_id = session["id"]
        assert agent_repository.acquire_turn(
            cursor,
            session_id=session_id,
            user_login="admin-one",
            turn_id=turn_id,
            lease_seconds=60,
        )

    original_finish = routes_agent._finish_turn

    def finish_then_pause(**kwargs):
        result = original_finish(**kwargs)
        committed.set()
        release_worker.wait(timeout=2)
        return result

    monkeypatch.setattr(routes_agent, "_finish_turn", finish_then_pause)

    async def exercise():
        task = asyncio.create_task(routes_agent._finish_turn_cancellation_safe(
            session_id=session_id,
            user_login="admin-one",
            turn_id=turn_id,
            status="complete",
            content="done",
            replay_items=[],
        ))
        await asyncio.wait_for(asyncio.to_thread(committed.wait), timeout=1)
        task.cancel()
        release_worker.set()
        message, cancelled = await task
        assert message["status"] == "complete"
        assert cancelled is True

    asyncio.run(exercise())

    # The cancellation cleanup may safely repeat with a different requested
    # status; the first committed terminal row remains canonical.
    repeated = original_finish(
        session_id=session_id,
        user_login="admin-one",
        turn_id=turn_id,
        status="cancelled",
        content="",
        replay_items=[],
    )
    assert repeated["status"] == "complete"
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) AS count
              FROM logo.agent_chat_message
             WHERE session_id = %s AND user_login = 'admin-one'
               AND turn_id = %s AND role = 'assistant'
            """,
            (session_id, turn_id),
        )
        assert cursor.fetchone()["count"] == 1
        cursor.execute(
            """
            SELECT active_turn_id FROM logo.agent_chat_session
             WHERE id = %s AND user_login = 'admin-one'
            """,
            (session_id,),
        )
        assert cursor.fetchone()["active_turn_id"] is None


def test_terminal_persistence_is_fenced_after_lease_is_stolen():
    old_turn_id = uuid.uuid4()
    new_turn_id = uuid.uuid4()
    with database.cursor(write=True, actor="fixture") as cursor:
        session = agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=1,
        )
        assert agent_repository.acquire_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=old_turn_id,
            lease_seconds=60,
        )
        cursor.execute(
            """
            UPDATE logo.agent_chat_session
               SET active_turn_id = %s,
                   turn_lease_expires_at = now() + interval '1 minute'
             WHERE id = %s AND user_login = 'admin-one'
            """,
            (new_turn_id, session["id"]),
        )

    with pytest.raises(routes_agent.SessionBusy, match="no longer owned"):
        routes_agent._finish_turn(
            session_id=session["id"],
            user_login="admin-one",
            turn_id=old_turn_id,
            status="complete",
            content="stale response",
            replay_items=[],
        )

    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT active_turn_id FROM logo.agent_chat_session
             WHERE id = %s AND user_login = 'admin-one'
            """,
            (session["id"],),
        )
        assert cursor.fetchone()["active_turn_id"] == new_turn_id
        cursor.execute(
            """
            SELECT count(*) AS count FROM logo.agent_chat_message
             WHERE session_id = %s AND user_login = 'admin-one'
               AND turn_id = %s AND role = 'assistant'
            """,
            (session["id"], old_turn_id),
        )
        assert cursor.fetchone()["count"] == 0
