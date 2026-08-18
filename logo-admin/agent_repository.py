"""Owner-scoped persistence for local agent sessions and replay history."""

from datetime import datetime, timedelta, timezone
import json
import uuid
from typing import Iterable, Optional

from psycopg2.extras import Json


MESSAGE_ROLES = frozenset({"user", "assistant"})
MESSAGE_STATUSES = frozenset({"complete", "failed", "cancelled"})
PUBLIC_MESSAGE_CHAR_LIMIT = 50_000
PUBLIC_HISTORY_BYTE_LIMIT = 1_000_000
SESSION_CHANGE_SET_LIMIT = 20
SESSION_CHANGE_SET_ITEM_LIMIT = 50
SESSION_SPREADSHEET_JOB_LIMIT = 20
REPLAY_TURN_LIMIT = 100
REPLAY_MESSAGE_SCAN_LIMIT = REPLAY_TURN_LIMIT * 4
MAX_PERSISTED_REPLAY_BYTES = 2 * 1024 * 1024


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


def create_session(
    cursor,
    *,
    user_login: str,
    retention_days: int,
    title: str = "",
) -> dict:
    session_id = _new_id()
    cursor.execute(
        """
        INSERT INTO logo.agent_chat_session (
            id, user_login, title, expires_at
        ) VALUES (%s, %s, %s, now() + make_interval(days => %s))
        RETURNING *
        """,
        (session_id, user_login, title[:200], int(retention_days)),
    )
    return dict(cursor.fetchone())


def list_sessions(
    cursor,
    *,
    user_login: str,
    limit: int = 50,
    before_updated_at: Optional[datetime] = None,
    before_id: Optional[uuid.UUID] = None,
) -> dict:
    """Return one owner-scoped keyset page of unexpired sessions."""

    safe_limit = max(1, min(100, int(limit)))
    if (before_updated_at is None) != (before_id is None):
        raise ValueError("session cursor is incomplete")
    cursor.execute(
        """
        SELECT id, user_login, title, active_turn_id, turn_lease_expires_at,
               created_at, updated_at, expires_at
          FROM logo.agent_chat_session
         WHERE user_login = %s
           AND expires_at > now()
           AND (
               %s::timestamptz IS NULL
               OR (updated_at, id) < (%s::timestamptz, %s::uuid)
           )
         ORDER BY updated_at DESC, id DESC
         LIMIT %s
        """,
        (
            user_login,
            before_updated_at,
            before_updated_at,
            before_id,
            safe_limit + 1,
        ),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    truncated = len(rows) > safe_limit
    sessions = rows[:safe_limit]
    oldest = sessions[-1] if sessions else None
    return {
        "sessions": sessions,
        "truncated": truncated,
        "oldest_cursor": (
            {"updated_at": oldest["updated_at"], "id": oldest["id"]}
            if oldest is not None and truncated
            else None
        ),
    }


def get_session(cursor, session_id, user_login: str) -> Optional[dict]:
    cursor.execute(
        """
        SELECT *
          FROM logo.agent_chat_session
         WHERE id = %s AND user_login = %s
           AND expires_at > now()
        """,
        (session_id, user_login),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def list_messages(
    cursor,
    session_id,
    user_login: str,
    *,
    limit: int = 200,
    maximum_bytes: int = PUBLIC_HISTORY_BYTE_LIMIT,
    before_created_at: Optional[datetime] = None,
    before_id: Optional[uuid.UUID] = None,
) -> dict:
    safe_limit = max(1, min(500, int(limit)))
    safe_bytes = max(1_024, min(2_000_000, int(maximum_bytes)))
    if (before_created_at is None) != (before_id is None):
        raise ValueError("history cursor is incomplete")
    projection_chars = min(
        PUBLIC_MESSAGE_CHAR_LIMIT,
        max(1, (safe_bytes - 512) // 24),
    )
    cursor.execute(
        """
        WITH recent AS MATERIALIZED (
            SELECT id, turn_id, role, status,
                   left(content, %s) AS content,
                   length(content) > %s AS content_truncated,
                   created_at,
                   (octet_length(left(content, %s)) * 6 + 512)::bigint
                       AS response_bytes
              FROM logo.agent_chat_message
             WHERE session_id = %s AND user_login = %s
               AND (
                   %s::timestamptz IS NULL
                   OR (created_at, id) < (%s::timestamptz, %s::uuid)
               )
             ORDER BY created_at DESC, id DESC
             LIMIT %s
        ), ranked AS (
            SELECT recent.*,
                   count(*) OVER () AS candidate_count,
                   row_number() OVER (
                       ORDER BY created_at DESC, id DESC
                   ) AS recency_rank,
                   sum(response_bytes) OVER (
                       ORDER BY created_at DESC, id DESC
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cumulative_bytes
              FROM recent
        )
        SELECT id, turn_id, role, status, content, content_truncated,
               created_at, candidate_count
          FROM ranked
         WHERE recency_rank <= %s AND cumulative_bytes <= %s
        ORDER BY created_at, id
        """,
        (
            projection_chars,
            projection_chars,
            projection_chars,
            session_id,
            user_login,
            before_created_at,
            before_created_at,
            before_id,
            safe_limit + 1,
            safe_limit,
            safe_bytes,
        ),
    )
    messages = [dict(row) for row in cursor.fetchall()]
    candidate_count = int(messages[0].pop("candidate_count")) if messages else 0
    for message in messages[1:]:
        message.pop("candidate_count", None)
    for message in messages:
        content = str(message.get("content") or "")
        if len(content) > PUBLIC_MESSAGE_CHAR_LIMIT:
            message["content"] = content[:PUBLIC_MESSAGE_CHAR_LIMIT]
            message["content_truncated"] = True
    truncated = candidate_count > len(messages)
    if not messages:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM logo.agent_chat_message
                 WHERE session_id = %s AND user_login = %s
                   AND (
                       %s::timestamptz IS NULL
                       OR (created_at, id) < (%s::timestamptz, %s::uuid)
                   )
                 LIMIT 1
            ) AS has_messages
            """,
            (
                session_id,
                user_login,
                before_created_at,
                before_created_at,
                before_id,
            ),
        )
        truncated = bool(cursor.fetchone()["has_messages"])
    oldest = messages[0] if messages else None
    return {
        "messages": messages,
        "truncated": truncated,
        "limit_bytes": safe_bytes,
        "oldest_cursor": (
            {
                "created_at": oldest["created_at"],
                "id": oldest["id"],
            }
            if oldest is not None and truncated
            else None
        ),
    }


def get_replay_items(
    cursor,
    session_id,
    user_login: str,
    *,
    maximum_bytes: int = 200_000,
) -> list[dict]:
    """Return complete recent turns within a database-enforced byte window.

    The database ranks turn sizes before returning JSON, so a corrupted or
    unexpectedly large history cannot be materialized in the web process.
    A turn is replayable only when it has exactly one complete user message
    and one complete assistant message; abandoned write instructions are
    therefore never replayed on their own.
    """

    safe_bytes = max(1, min(2_000_000, int(maximum_bytes)))
    cursor.execute(
        """
        WITH recent_messages AS MATERIALIZED (
            SELECT id, turn_id, role, status, replay_items, created_at
              FROM logo.agent_chat_message
             WHERE session_id = %s AND user_login = %s
             ORDER BY created_at DESC, id DESC
             LIMIT %s
        ), complete_turns AS (
            SELECT turn_id,
                   min(created_at) AS turn_created,
                   sum(octet_length(replay_items::text))::bigint
                       AS replay_bytes
              FROM recent_messages
             GROUP BY turn_id
            HAVING count(*) = 2
               AND count(*) FILTER (
                       WHERE role = 'user' AND status = 'complete'
                   ) = 1
               AND count(*) FILTER (
                       WHERE role = 'assistant' AND status = 'complete'
                   ) = 1
        ), newest AS (
            SELECT *
              FROM complete_turns
             ORDER BY turn_created DESC, turn_id DESC
             LIMIT %s
        ), ranked AS (
            SELECT newest.*,
                   sum(replay_bytes) OVER (
                       ORDER BY turn_created DESC, turn_id DESC
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS cumulative_bytes
              FROM newest
        ), selected AS (
            SELECT turn_id, turn_created
              FROM ranked
             WHERE cumulative_bytes <= %s
        )
        SELECT message.replay_items
          FROM selected
          JOIN recent_messages AS message
            ON message.turn_id = selected.turn_id
         ORDER BY selected.turn_created,
                  selected.turn_id,
                  CASE message.role WHEN 'user' THEN 0 ELSE 1 END,
                  message.created_at,
                  message.id
        """,
        (
            session_id,
            user_login,
            REPLAY_MESSAGE_SCAN_LIMIT,
            REPLAY_TURN_LIMIT,
            safe_bytes,
        ),
    )
    replay: list[dict] = []
    for row in cursor.fetchall():
        items = row.get("replay_items") or []
        if isinstance(items, list):
            replay.extend(item for item in items if isinstance(item, dict))
    return replay


def list_session_change_sets(
    cursor,
    session_id,
    user_login: str,
    *,
    limit: int = SESSION_CHANGE_SET_LIMIT,
    item_limit: int = SESSION_CHANGE_SET_ITEM_LIMIT,
    before_priority: Optional[int] = None,
    before_updated_at: Optional[datetime] = None,
    before_id: Optional[uuid.UUID] = None,
) -> dict:
    """Return an owner-scoped page with actionable cards ordered first.

    Unexpired pending and applied records remain reachable even when a session
    has more than one page of newer terminal records.  The status priority is
    part of the cursor so page boundaries are stable and non-overlapping.
    """

    safe_limit = max(1, min(SESSION_CHANGE_SET_LIMIT, int(limit)))
    safe_item_limit = max(
        1,
        min(SESSION_CHANGE_SET_ITEM_LIMIT, int(item_limit)),
    )
    cursor_parts = (before_priority, before_updated_at, before_id)
    if any(value is not None for value in cursor_parts) and not all(
        value is not None for value in cursor_parts
    ):
        raise ValueError("change-set cursor is incomplete")
    if before_priority is not None and int(before_priority) not in {0, 1}:
        raise ValueError("change-set cursor priority is invalid")
    cursor.execute(
        """
        WITH owned AS (
            SELECT id, session_id, origin, status, revision, preview_hash,
                   affected_scopes, contains_hard_delete,
                   created_at, updated_at, expires_at, applied_at, undone_at,
                   CASE
                       WHEN status = 'applied'
                         OR (status = 'pending' AND expires_at > now())
                       THEN 0 ELSE 1
                   END AS workflow_priority
              FROM logo.agent_change_set
             WHERE session_id = %s AND user_login = %s
        )
        SELECT id, session_id, origin, status, revision, preview_hash,
               affected_scopes, contains_hard_delete,
               created_at, updated_at, expires_at, applied_at, undone_at,
               workflow_priority
          FROM owned
         WHERE (
               %s::integer IS NULL
               OR workflow_priority > %s::integer
               OR (
                   workflow_priority = %s::integer
                   AND (updated_at, id) < (%s::timestamptz, %s::uuid)
               )
         )
         ORDER BY workflow_priority, updated_at DESC, id DESC
         LIMIT %s
        """,
        (
            session_id,
            user_login,
            before_priority,
            before_priority,
            before_priority,
            before_updated_at,
            before_id,
            safe_limit + 1,
        ),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    truncated = len(rows) > safe_limit
    change_sets = rows[:safe_limit]
    for change_set in change_sets:
        cursor.execute(
            """
            SELECT id, call_id, tool_name, arguments, sort_order, created_at
              FROM logo.agent_change_set_item
             WHERE change_set_id = %s AND user_login = %s
             ORDER BY sort_order, id
             LIMIT %s
            """,
            (change_set["id"], user_login, safe_item_limit + 1),
        )
        items = [dict(row) for row in cursor.fetchall()]
        change_set["items_truncated"] = len(items) > safe_item_limit
        change_set["items"] = items[:safe_item_limit]
        cursor.execute(
            """
            SELECT id, event_type, actor, preview_hash, created_at
              FROM logo.agent_action_journal
             WHERE change_set_id = %s AND user_login = %s
             ORDER BY created_at, id
             LIMIT 3
            """,
            (change_set["id"], user_login),
        )
        change_set["journal"] = [
            dict(row) for row in cursor.fetchall()
        ]
    oldest = change_sets[-1] if change_sets else None
    result = {
        "change_sets": change_sets,
        "truncated": truncated,
        "oldest_cursor": (
            {
                "priority": oldest["workflow_priority"],
                "updated_at": oldest["updated_at"],
                "id": oldest["id"],
            }
            if oldest is not None and truncated
            else None
        ),
    }
    for change_set in change_sets:
        change_set.pop("workflow_priority", None)
    return result


def list_session_spreadsheet_jobs(
    cursor,
    session_id,
    user_login: str,
    *,
    limit: int = SESSION_SPREADSHEET_JOB_LIMIT,
    before_priority: Optional[int] = None,
    before_created_at: Optional[datetime] = None,
    before_id: Optional[uuid.UUID] = None,
) -> dict:
    """Return owner-scoped resumable job metadata without private fields."""

    safe_limit = max(1, min(SESSION_SPREADSHEET_JOB_LIMIT, int(limit)))
    cursor_parts = (before_priority, before_created_at, before_id)
    if any(value is not None for value in cursor_parts) and not all(
        value is not None for value in cursor_parts
    ):
        raise ValueError("spreadsheet cursor is incomplete")
    if before_priority is not None and int(before_priority) not in {0, 1}:
        raise ValueError("spreadsheet cursor priority is invalid")
    cursor.execute(
        """
        WITH owned AS (
            SELECT id, session_id, change_set_id, original_name, media_type,
                   byte_size, format_name, status, mapping_revision,
                   mapping_hash, mapping, rejected_rows, created_at, expires_at,
                   CASE WHEN status IN (
                       'mapping_processing', 'mapping_pending',
                       'mapping_confirmed'
                   ) THEN 0 ELSE 1 END AS workflow_priority
              FROM logo.agent_spreadsheet_job
             WHERE session_id = %s AND user_login = %s
               AND expires_at > now()
        )
        SELECT id, session_id, change_set_id, original_name, media_type,
               byte_size, format_name, status, mapping_revision,
               mapping_hash, mapping, rejected_rows, created_at, expires_at,
               workflow_priority
          FROM owned
         WHERE (
               %s::integer IS NULL
               OR workflow_priority > %s::integer
               OR (
                   workflow_priority = %s::integer
                   AND (created_at, id) < (%s::timestamptz, %s::uuid)
               )
         )
         ORDER BY workflow_priority, created_at DESC, id DESC
         LIMIT %s
        """,
        (
            session_id,
            user_login,
            before_priority,
            before_priority,
            before_priority,
            before_created_at,
            before_id,
            safe_limit + 1,
        ),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    truncated = len(rows) > safe_limit
    jobs = rows[:safe_limit]
    oldest = jobs[-1] if jobs else None
    result = {
        "spreadsheet_jobs": jobs,
        "truncated": truncated,
        "oldest_cursor": (
            {
                "priority": oldest["workflow_priority"],
                "created_at": oldest["created_at"],
                "id": oldest["id"],
            }
            if oldest is not None and truncated
            else None
        ),
    }
    for job in jobs:
        job.pop("workflow_priority", None)
    return result


def append_message(
    cursor,
    *,
    session_id,
    user_login: str,
    turn_id,
    role: str,
    status: str,
    content: str,
    replay_items: Iterable[dict],
) -> dict:
    if role not in MESSAGE_ROLES:
        raise ValueError("invalid message role")
    if status not in MESSAGE_STATUSES:
        raise ValueError("invalid message status")
    replay_list = list(replay_items)
    replay_bytes = sum(
        len(chunk.encode("utf-8"))
        for chunk in json.JSONEncoder(default=str).iterencode(replay_list)
    )
    if replay_bytes > MAX_PERSISTED_REPLAY_BYTES:
        raise ValueError("message replay exceeds the persistence limit")
    message_id = _new_id()
    cursor.execute(
        """
        INSERT INTO logo.agent_chat_message (
            id, session_id, user_login, turn_id, role, status,
            content, replay_items
        )
        SELECT %s, s.id, s.user_login, %s, %s, %s, %s, %s
          FROM logo.agent_chat_session s
         WHERE s.id = %s AND s.user_login = %s
           AND s.expires_at > now()
        RETURNING id, session_id, user_login, turn_id, role, status,
                  content, replay_items, created_at
        """,
        (
            message_id,
            turn_id,
            role,
            status,
            content,
            Json(replay_list),
            session_id,
            user_login,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise LookupError("session not found")
    cursor.execute(
        """
        UPDATE logo.agent_chat_session
           SET updated_at = now(),
               title = CASE
                   WHEN title = '' AND %s = 'user' THEN left(%s, 200)
                   ELSE title
               END
         WHERE id = %s AND user_login = %s
        """,
        (role, " ".join(content.split()), session_id, user_login),
    )
    return dict(row)


def acquire_turn(
    cursor,
    *,
    session_id,
    user_login: str,
    turn_id,
    lease_seconds: int,
) -> Optional[dict]:
    cursor.execute(
        """
        UPDATE logo.agent_chat_session
           SET active_turn_id = %s,
               turn_lease_expires_at = now() + make_interval(secs => %s),
               updated_at = now()
         WHERE id = %s AND user_login = %s
           AND expires_at > now()
           AND (
               active_turn_id IS NULL
               OR turn_lease_expires_at < now()
           )
        RETURNING *
        """,
        (turn_id, int(lease_seconds), session_id, user_login),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def release_turn(
    cursor,
    *,
    session_id,
    user_login: str,
    turn_id,
) -> bool:
    cursor.execute(
        """
        UPDATE logo.agent_chat_session
           SET active_turn_id = NULL,
               turn_lease_expires_at = NULL,
               updated_at = now()
         WHERE id = %s AND user_login = %s
           AND active_turn_id = %s
        """,
        (session_id, user_login, turn_id),
    )
    return cursor.rowcount == 1


def renew_turn(
    cursor,
    *,
    session_id,
    user_login: str,
    turn_id,
    lease_seconds: int,
) -> bool:
    """Extend only the still-owned active turn lease."""

    cursor.execute(
        """
        UPDATE logo.agent_chat_session
           SET turn_lease_expires_at = now() + make_interval(secs => %s),
               updated_at = now()
         WHERE id = %s AND user_login = %s
           AND active_turn_id = %s
           AND expires_at > now()
        """,
        (int(lease_seconds), session_id, user_login, turn_id),
    )
    return cursor.rowcount == 1


def expires_at(retention_days: int) -> datetime:
    """Small pure helper used by tests and future maintenance code."""

    return datetime.now(timezone.utc) + timedelta(days=int(retention_days))
