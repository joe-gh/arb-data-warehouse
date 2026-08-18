"""Turn leases survive process loss only until their database expiry."""

from uuid import uuid4

import agent_repository
from db import database


def _session():
    with database.cursor(write=True, actor="fixture") as cursor:
        return agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=30,
            title="process recovery",
        )


def test_unexpired_lease_blocks_second_turn_but_expired_lease_is_recoverable():
    session = _session()
    first_turn = uuid4()
    second_turn = uuid4()
    with database.cursor(write=True, actor="admin-one") as cursor:
        first = agent_repository.acquire_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=first_turn,
            lease_seconds=120,
        )
    assert first is not None
    with database.cursor(write=True, actor="admin-one") as cursor:
        blocked = agent_repository.acquire_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=second_turn,
            lease_seconds=120,
        )
    assert blocked is None
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "UPDATE logo.agent_chat_session "
            "SET turn_lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (session["id"],),
        )
    database.close()  # Simulate losing the process-local pool.
    with database.cursor(write=True, actor="admin-one") as cursor:
        recovered = agent_repository.acquire_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=second_turn,
            lease_seconds=120,
        )
    assert recovered["active_turn_id"] == second_turn


def test_release_is_bound_to_owner_and_turn_id():
    session = _session()
    turn = uuid4()
    with database.cursor(write=True, actor="admin-one") as cursor:
        agent_repository.acquire_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=turn,
            lease_seconds=120,
        )
        wrong = agent_repository.release_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=uuid4(),
        )
    assert wrong is False
    with database.cursor(write=True, actor="admin-one") as cursor:
        released = agent_repository.release_turn(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=turn,
        )
    assert released is True


def test_session_remains_owner_scoped_after_pool_reopen():
    session = _session()
    database.close()
    with database.cursor() as cursor:
        assert agent_repository.get_session(cursor, session["id"], "admin-one") is not None
        assert agent_repository.get_session(cursor, session["id"], "admin-two") is None


def test_failed_or_cancelled_turn_instructions_are_not_replayed():
    session = _session()
    failed_turn = uuid4()
    with database.cursor(write=True, actor="admin-one") as cursor:
        agent_repository.append_message(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=failed_turn,
            role="user",
            status="complete",
            content="delete it",
            replay_items=[{"role": "user", "content": "delete it"}],
        )
        agent_repository.append_message(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=failed_turn,
            role="assistant",
            status="failed",
            content="",
            replay_items=[],
        )
    with database.cursor() as cursor:
        replay = agent_repository.get_replay_items(
            cursor,
            session["id"],
            "admin-one",
        )
    assert replay == []


def test_replay_window_keeps_newest_complete_turn_and_provider_pairs_together():
    session = _session()
    old_turn = uuid4()
    new_turn = uuid4()
    with database.cursor(write=True, actor="admin-one") as cursor:
        agent_repository.append_message(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=old_turn,
            role="user",
            status="complete",
            content="old",
            replay_items=[{"role": "user", "content": "old-" + "x" * 2_000}],
        )
        agent_repository.append_message(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=old_turn,
            role="assistant",
            status="complete",
            content="old answer",
            replay_items=[{"type": "message", "content": "old answer"}],
        )
        agent_repository.append_message(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=new_turn,
            role="user",
            status="complete",
            content="new",
            replay_items=[{"role": "user", "content": "new"}],
        )
        agent_repository.append_message(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=new_turn,
            role="assistant",
            status="complete",
            content="new answer",
            replay_items=[
                {"type": "function_call", "call_id": "call-new"},
                {
                    "type": "function_call_output",
                    "call_id": "call-new",
                    "output": "{}",
                },
            ],
        )
    with database.cursor() as cursor:
        replay = agent_repository.get_replay_items(
            cursor,
            session["id"],
            "admin-one",
            maximum_bytes=500,
        )
    assert replay == [
        {"role": "user", "content": "new"},
        {"type": "function_call", "call_id": "call-new"},
        {
            "type": "function_call_output",
            "call_id": "call-new",
            "output": "{}",
        },
    ]


def test_replay_query_bounds_messages_before_grouping():
    class RecordingCursor:
        def __init__(self):
            self.query = ""
            self.params = ()

        def execute(self, query, params):
            self.query = query
            self.params = params

        def fetchall(self):
            return []

    cursor = RecordingCursor()
    agent_repository.get_replay_items(
        cursor,
        __import__("uuid").uuid4(),
        "admin-one",
        maximum_bytes=1000,
    )
    assert "recent_messages AS MATERIALIZED" in cursor.query
    assert cursor.query.index("LIMIT %s") < cursor.query.index("GROUP BY turn_id")
    assert agent_repository.REPLAY_MESSAGE_SCAN_LIMIT in cursor.params
