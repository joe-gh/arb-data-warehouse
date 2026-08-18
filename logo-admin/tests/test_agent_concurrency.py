import asyncio
import uuid

import agent_repository
from db import database
from mutations import MutationScope
import routes_agent
from snapshots import lock_scopes, lock_scope_tables


class _RecordingCursor:
    def __init__(self):
        self.tokens = []

    def execute(self, query, params=None):
        if params:
            self.tokens.append(params[0])
        else:
            self.tokens.append(query)


def test_overlapping_assignment_scopes_share_style_advisory_lock():
    option_cursor = _RecordingCursor()
    style_cursor = _RecordingCursor()
    lock_scopes(option_cursor, [MutationScope(
        "assignment_option_row",
        {
            "fdm4_store": "S_TEST",
            "product_style": "STYLE-1",
            "garment_color_code": "RED",
            "option_row": 1,
        },
    )])
    lock_scopes(style_cursor, [MutationScope(
        "assignment_style",
        {"fdm4_store": "S_TEST", "product_style": "STYLE-1"},
    )])
    assert set(option_cursor.tokens) & set(style_cursor.tokens)


def test_exact_lifecycle_locks_business_tables_against_external_writers():
    cursor = _RecordingCursor()
    tables = lock_scope_tables(cursor, [
        MutationScope(
            "assignment_option_row",
            {
                "fdm4_store": "S_TEST",
                "product_style": "STYLE-1",
                "garment_color_code": "RED",
                "option_row": 1,
            },
        ),
        MutationScope("store_settings_row", {"fdm4_store": "S_TEST"}),
    ])
    assert tables == ("logo.assignment", "logo.store_settings")
    assert all("SHARE ROW EXCLUSIVE" in query for query in cursor.tokens)


def test_same_session_rejects_a_second_turn(agent_enabled, client_as):
    client = client_as("admin-one")
    session_id = client.post(
        "/api/agent/sessions",
        json={"title": "Busy"},
    ).json()["session"]["id"]
    with database.cursor(write=True, actor="admin-one") as cursor:
        leased = agent_repository.acquire_turn(
            cursor,
            session_id=session_id,
            user_login="admin-one",
            turn_id=uuid.uuid4(),
            lease_seconds=120,
        )
    assert leased is not None
    response = client.post(
        "/api/agent/chat",
        json={"session_id": session_id, "message": "hello"},
    )
    assert response.status_code == 409


def test_expired_lease_can_be_reacquired():
    with database.cursor(write=True, actor="admin-one") as cursor:
        session = agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=30,
        )
        cursor.execute(
            """
            UPDATE logo.agent_chat_session
               SET active_turn_id = %s,
                   turn_lease_expires_at = now() - interval '1 second'
             WHERE id = %s AND user_login = 'admin-one'
            """,
            (uuid.uuid4(), session["id"]),
        )
        acquired = agent_repository.acquire_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=uuid.uuid4(),
            lease_seconds=120,
        )
    assert acquired is not None


def test_saturated_global_capacity_rejects_before_session_lease(
    agent_enabled,
    client_as,
    monkeypatch,
):
    class SaturatedSemaphore:
        async def acquire(self):
            raise TimeoutError()

        def release(self):
            raise AssertionError("unacquired capacity must not be released")

    monkeypatch.setattr(routes_agent, "_turn_semaphore", SaturatedSemaphore())
    client = client_as("admin-one")
    response = client.post(
        "/api/agent/chat",
        json={"message": "hello"},
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    with database.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM logo.agent_chat_session")
        assert cursor.fetchone()["count"] == 0
