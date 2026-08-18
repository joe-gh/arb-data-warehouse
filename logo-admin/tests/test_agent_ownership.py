import agent_repository
from db import database
import pytest


def test_session_repository_is_owner_scoped():
    with database.cursor(write=True, actor="admin-one") as cursor:
        session = agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=30,
        )
    with database.cursor() as cursor:
        assert agent_repository.get_session(
            cursor, session["id"], "admin-one"
        ) is not None
        assert agent_repository.get_session(
            cursor, session["id"], "admin-two"
        ) is None


def test_messages_are_owner_scoped():
    with database.cursor(write=True, actor="admin-one") as cursor:
        session = agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=30,
        )
        agent_repository.append_message(
            cursor,
            session_id=session["id"],
            user_login="admin-one",
            turn_id=__import__("uuid").uuid4(),
            role="user",
            status="complete",
            content="hello",
            replay_items=[{"role": "user", "content": []}],
        )
    with database.cursor() as cursor:
        owned = agent_repository.list_messages(cursor, session["id"], "admin-one")
        other = agent_repository.list_messages(cursor, session["id"], "admin-two")
        assert len(owned["messages"]) == 1
        assert owned["truncated"] is False
        assert other["messages"] == []


def test_persistence_rejects_replay_over_absolute_byte_cap():
    class CursorMustNotRun:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("oversized replay reached SQL")

    with pytest.raises(ValueError, match="persistence limit"):
        agent_repository.append_message(
            CursorMustNotRun(),
            session_id=__import__("uuid").uuid4(),
            user_login="admin-one",
            turn_id=__import__("uuid").uuid4(),
            role="assistant",
            status="complete",
            content="",
            replay_items=[{
                "type": "message",
                "content": "x" * (
                    agent_repository.MAX_PERSISTED_REPLAY_BYTES + 1
                ),
            }],
        )


def test_public_history_has_aggregate_byte_window_for_multibyte_text():
    with database.cursor(write=True, actor="admin-one") as cursor:
        session = agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=30,
        )
        for index in range(3):
            agent_repository.append_message(
                cursor,
                session_id=session["id"],
                user_login="admin-one",
                turn_id=__import__("uuid").uuid4(),
                role="user",
                status="complete",
                content="🙂" * 300,
                replay_items=[],
            )
    with database.cursor() as cursor:
        history = agent_repository.list_messages(
            cursor,
            session["id"],
            "admin-one",
            maximum_bytes=4_000,
        )
    assert history["truncated"] is True
    assert history["limit_bytes"] == 4_000
    assert history["oldest_cursor"] is not None
    assert sum(
        len(message["content"].encode("utf-8"))
        for message in history["messages"]
    ) <= history["limit_bytes"]

    with database.cursor() as cursor:
        older = agent_repository.list_messages(
            cursor,
            session["id"],
            "admin-one",
            maximum_bytes=4_000,
            before_created_at=history["oldest_cursor"]["created_at"],
            before_id=history["oldest_cursor"]["id"],
        )
    assert {
        message["id"] for message in older["messages"]
    }.isdisjoint({message["id"] for message in history["messages"]})
