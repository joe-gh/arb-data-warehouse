"""Older turns are replayed as the assistant's visible text only.

A tool-using turn can weigh far more than the whole history budget. Instead of
dropping every turn once the newest one overflows, the window keeps the newest
turn verbatim (reasoning, calls and results intact) and reduces older turns to
the person's message plus the assistant's own reply with a note of the tool
calls it made. The assistant therefore keeps the lists and decisions it already
gave, and re-runs a lookup only when it needs the data again.
"""

from uuid import uuid4

import agent_repository
from db import database


def _session():
    with database.cursor(write=True, actor="fixture") as cursor:
        return agent_repository.create_session(
            cursor,
            user_login="admin-one",
            retention_days=30,
            title="compaction",
        )


def _user(text):
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _message(text, ident="msg_1"):
    return {
        "id": ident,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _turn(session, minutes_ago, user_text, assistant_items, assistant_text="reply"):
    turn = uuid4()
    with database.cursor(write=True, actor="admin-one") as cursor:
        for role, content, items in (
            ("user", user_text, [_user(user_text)]),
            ("assistant", assistant_text, assistant_items),
        ):
            agent_repository.append_message(
                cursor,
                session_id=session["id"],
                user_login="admin-one",
                turn_id=turn,
                role=role,
                status="complete",
                content=content,
                replay_items=items,
            )
        cursor.execute(
            "UPDATE logo.agent_chat_message SET created_at=now()-%s*interval '1 minute' "
            "WHERE session_id=%s AND turn_id=%s",
            (minutes_ago, session["id"], turn),
        )
    return turn


def _replay(session, maximum_bytes):
    with database.cursor() as cursor:
        return agent_repository.get_replay_items(
            cursor,
            session["id"],
            "admin-one",
            maximum_bytes=maximum_bytes,
        )


LOOKUP_ARGS = '{"store":"S_TEST","method":"emb"}'
TOOL_TURN = [
    {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "e" * 400},
    {"id": "fc_1", "type": "function_call", "status": "completed", "call_id": "c1",
     "name": "list_styles", "arguments": LOOKUP_ARGS},
    {"type": "function_call_output", "call_id": "c1",
     "output": '{"result":{"styles":[' + ",".join('{"product_style":"A%d"}' % i for i in range(300)) + "]}}"},
    {"id": "rs_2", "type": "reasoning", "summary": [], "encrypted_content": "f" * 400},
    _message("I found 2 styles: A1, B2. Proceed?"),
]


def test_older_tool_turn_is_compacted_and_newest_turn_stays_verbatim():
    session = _session()
    _turn(session, 5, "remove the scr logos", TOOL_TURN)
    newest = [_message("Staged the hide for A1 and B2.", "msg_2")]
    _turn(session, 1, "yes please", newest)

    replay = _replay(session, 300_000)

    assert replay == [
        _user("remove the scr logos"),
        {
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": "I found 2 styles: A1, B2. Proceed?\n\n"
                        + agent_repository.COMPACT_NOTE_PREFIX
                        + "list_styles " + LOOKUP_ARGS
                        + agent_repository.COMPACT_NOTE_SUFFIX,
            }],
        },
        _user("yes please"),
        *newest,
    ]


def test_newest_turn_over_budget_is_compacted_rather_than_dropping_history():
    session = _session()
    _turn(session, 1, "what is on A1", [
        {"id": "fc_9", "type": "function_call", "status": "completed", "call_id": "c9",
         "name": "get_style", "arguments": '{"store":"S_TEST","style":"A1"}'},
        {"type": "function_call_output", "call_id": "c9", "output": "x" * 6_000},
        _message("Done: A1 carries one logo."),
    ])

    replay = _replay(session, 2_000)

    assert replay == [
        _user("what is on A1"),
        {
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": "Done: A1 carries one logo.\n\n"
                        + agent_repository.COMPACT_NOTE_PREFIX
                        + 'get_style {"store":"S_TEST","style":"A1"}'
                        + agent_repository.COMPACT_NOTE_SUFFIX,
            }],
        },
    ]


def test_budget_drops_the_oldest_compacted_turns_first():
    session = _session()
    _turn(session, 30, "first", [_message("o" * 3_000, "msg_a")])
    _turn(session, 20, "second", [_message("m" * 300, "msg_b")])
    newest = [_message("n" * 20, "msg_c")]
    _turn(session, 10, "third", newest)

    replay = _replay(session, 1_200)

    assert replay == [
        _user("second"),
        {"role": "assistant", "content": [{"type": "output_text", "text": "m" * 300}]},
        _user("third"),
        *newest,
    ]


def test_compacted_history_carries_no_provider_ids_reasoning_or_tool_results():
    session = _session()
    _turn(session, 5, "look it up", TOOL_TURN)
    _turn(session, 1, "thanks", [_message("You're welcome.", "msg_2")])

    replay = _replay(session, 300_000)
    older_assistant = replay[1]

    assert "id" not in older_assistant
    assert {item.get("type") for item in replay[:2] if isinstance(item, dict)} <= {None}
    assert not any(item.get("type") in {"reasoning", "function_call", "function_call_output"} for item in replay[:2])
    assert "e" * 400 not in str(older_assistant)
    assert "A299" not in str(older_assistant)


def test_compact_assistant_items_shapes():
    compact = agent_repository.compact_assistant_items
    assert compact([]) is None
    assert compact([{"type": "reasoning", "id": "rs"}]) is None
    assert compact([{"type": "message", "content": "plain string"}]) == {
        "role": "assistant", "content": [{"type": "output_text", "text": "plain string"}],
    }
    calls_only = compact([
        {"type": "function_call", "name": "list_stores", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "x", "output": "{}"},
    ])
    assert calls_only["content"][0]["text"] == (
        agent_repository.COMPACT_NOTE_PREFIX + "list_stores {}" + agent_repository.COMPACT_NOTE_SUFFIX
    )
    long_arguments = compact([
        {"type": "function_call", "name": "n" * 200, "arguments": "a" * 1_000},
    ])["content"][0]["text"]
    assert len(long_arguments) < 400


def test_prepare_turn_uses_the_configured_history_budget(monkeypatch):
    from types import SimpleNamespace

    import routes_agent

    seen = {}

    def fake_replay(cursor, session_id, user_login, *, maximum_bytes):
        seen["maximum_bytes"] = maximum_bytes
        return []

    monkeypatch.setattr(routes_agent.agent_repository, "get_replay_items", fake_replay)
    monkeypatch.setattr(routes_agent, "get_settings", lambda: SimpleNamespace(
        agent_max_history_bytes=123_456,
        agent_chat_retention_days=30,
        agent_turn_timeout_seconds=90,
    ))
    session, replay = routes_agent._prepare_turn(
        session_id=None, message="hello", user_login="admin-one", turn_id=uuid4(),
    )
    assert seen["maximum_bytes"] == 123_456
    assert replay == [_user("hello")]
    assert session["id"]
