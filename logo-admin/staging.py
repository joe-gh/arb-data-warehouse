"""Owner-scoped cumulative staging, atomic apply, and conflict-safe undo."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from typing import Any, Iterable, Mapping, Optional, Sequence

from psycopg2.extras import Json

from authorization import required_tier
from commands import (
    HARD_DELETE_TOOLS,
    SavePriceRuleCommand,
    SetPriceRuleActiveCommand,
    MutationCommand,
    command_arguments,
    parse_command,
)
from db import database
from domain import (
    Conflict,
    HardDeleteAcknowledgementRequired,
    InvalidCommand,
    NotFound,
    PreviewDrift,
)
from mutations import affected_scopes, dispatch_mutation
from snapshots import (
    MAX_SNAPSHOT_STATE_BYTES,
    VOLATILE_PREVIEW_COLUMNS,
    canonical_json,
    compact_scopes,
    diff_states,
    lock_scopes,
    lock_scope_tables,
    restore_state,
    scope_dict,
    scope_from_dict,
    snapshot_scopes,
    states_equal,
    validate_snapshot_state,
)


MAX_PERSISTED_CHANGE_SET_ITEMS = 500


@dataclass(frozen=True)
class Preview:
    scopes: tuple
    semantic_diff: dict
    results: tuple[dict, ...]


def _json(value: Any) -> Json:
    return Json(
        value,
        dumps=lambda item: json.dumps(
            item,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise NotFound("Agent resource not found") from None


def _commands_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, MutationCommand]]:
    commands = []
    for row in rows:
        name = str(row["tool_name"])
        arguments = dict(row["arguments"])
        reserved_id = arguments.pop("_reserved_rule_id", None)
        required_tier(name)
        command = parse_command(name, arguments)
        if reserved_id is not None:
            if not isinstance(command, SavePriceRuleCommand) or command.rule_id is not None or type(reserved_id) is not int or reserved_id < 1:
                raise InvalidCommand("Stored price-rule reservation is invalid")
            command._reserved_rule_id = reserved_id
        commands.append((name, command))
    return commands


def _all_scopes(commands: Iterable[tuple[str, MutationCommand]]) -> tuple:
    return compact_scopes(
        scope
        for _name, command in commands
        for scope in affected_scopes(command)
    )


def _persisted_scopes(value: Any) -> tuple:
    """Decode only the exact canonical scope list written by preview."""

    if not isinstance(value, list):
        raise InvalidCommand("Stored change-set scope is invalid")
    try:
        decoded = tuple(scope_from_dict(item) for item in value)
    except (TypeError, ValueError, KeyError) as exc:
        raise InvalidCommand("Stored change-set scope is invalid") from exc
    canonical = compact_scopes(decoded)
    if decoded != canonical:
        raise InvalidCommand("Stored change-set scope is not canonical")
    return canonical


def _dispatch_checked(cursor, actor: str, command: MutationCommand):
    """Reject a handler whose reported mutation boundary drifts."""

    expected = compact_scopes(affected_scopes(command))
    if isinstance(command, (SavePriceRuleCommand, SetPriceRuleActiveCommand)):
        result = dispatch_mutation(cursor, actor, command, preview_activation=True)
    else:
        result = dispatch_mutation(cursor, actor, command)
    actual = compact_scopes(result.scopes)
    if actual != expected:
        raise InvalidCommand(
            "Mutation handler scope does not match its command contract"
        )
    return result


def _stored_arguments(command):
    arguments = command_arguments(command)
    if isinstance(command, SavePriceRuleCommand) and command._reserved_rule_id is not None:
        arguments["_reserved_rule_id"] = command._reserved_rule_id
    return arguments


def _reserve_price_rule_id(command):
    if isinstance(command, SavePriceRuleCommand) and command.rule_id is None and command._reserved_rule_id is None:
        # nextval reserves an identity only; preview never leaves a business row.
        with database.cursor(write=True, commit_on_success=False) as cursor:
            cursor.execute("SELECT nextval(pg_get_serial_sequence('woo.price_rule', 'rule_id')) AS rule_id")
            command._reserved_rule_id = int(cursor.fetchone()["rule_id"])


def _preview_diff(before, after, results):
    diff = diff_states(before, after, ignored_columns=VOLATILE_PREVIEW_COLUMNS)
    impacts = [result["price_rule_impact"] for result in results if "price_rule_impact" in result]
    if impacts:
        diff["price_rule_impacts"] = impacts
    return diff


def preview_commands(
    commands: Sequence[tuple[str, MutationCommand]],
    user_login: str,
) -> Preview:
    scopes = _all_scopes(commands)
    results: list[dict] = []
    with database.cursor(
        write=True,
        actor=f"agent-preview:{user_login}"[:100],
        commit_on_success=False,
    ) as cursor:
        lock_scopes(cursor, scopes)
        before = snapshot_scopes(cursor, scopes, for_update=False)
        for name, command in commands:
            required_tier(name)
            result = _dispatch_checked(
                cursor,
                f"agent-preview:{user_login}"[:100],
                command,
            )
            results.append(result.value)
        after = snapshot_scopes(cursor, scopes, for_update=False)
    return Preview(
        scopes=scopes,
        semantic_diff=_preview_diff(before, after, results),
        results=tuple(results),
    )


def _preview_batch_candidates(
    candidates: Sequence[tuple[str, str, MutationCommand]],
    user_login: str,
) -> tuple[Preview, list[tuple[str, str, MutationCommand]], list[dict]]:
    """Preview a batch once, rolling back only invalid domain-level rows."""

    all_commands = [(name, command) for _call_id, name, command in candidates]
    all_scopes = _all_scopes(all_commands)
    accepted: list[tuple[str, str, MutationCommand]] = []
    rejected: list[dict] = []
    results: list[dict] = []
    with database.cursor(
        write=True,
        actor=f"agent-preview:{user_login}"[:100],
        commit_on_success=False,
    ) as cursor:
        lock_scopes(cursor, all_scopes)
        before = snapshot_scopes(cursor, all_scopes, for_update=False)
        for call_id, name, command in candidates:
            cursor.execute("SAVEPOINT agent_batch_candidate")
            try:
                required_tier(name)
                result = _dispatch_checked(
                    cursor,
                    f"agent-preview:{user_login}"[:100],
                    command,
                )
            except (InvalidCommand, NotFound) as exc:
                cursor.execute("ROLLBACK TO SAVEPOINT agent_batch_candidate")
                cursor.execute("RELEASE SAVEPOINT agent_batch_candidate")
                rejected.append({
                    "call_id": call_id,
                    "detail": str(exc)[:500],
                })
            else:
                cursor.execute("RELEASE SAVEPOINT agent_batch_candidate")
                accepted.append((call_id, name, command))
                results.append(result.value)
        after = snapshot_scopes(cursor, all_scopes, for_update=False)
    accepted_scopes = _all_scopes([
        (name, command) for _call_id, name, command in accepted
    ])
    return (
        Preview(
            scopes=accepted_scopes,
            semantic_diff=_preview_diff(before, after, results),
            results=tuple(results),
        ),
        accepted,
        rejected,
    )


def _hash_payload(
    revision: int,
    commands: Sequence[tuple[str, MutationCommand]],
    semantic_diff: Mapping[str, Any],
) -> str:
    payload = {
        "revision": revision,
        "scopes": [scope_dict(scope) for scope in _all_scopes(commands)],
        "items": [
            {"tool_name": name, "arguments": command_arguments(command)}
            for name, command in commands
        ],
        "diff": semantic_diff,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _owned_change_set(cursor, change_set_id, user_login: str, *, lock: bool = False):
    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        f"""
        SELECT * FROM logo.agent_change_set
         WHERE id = %s AND user_login = %s{suffix}
        """,
        (_uuid(change_set_id), user_login),
    )
    row = cursor.fetchone()
    if row is None:
        raise NotFound("Change-set not found")
    return row


def _owned_items(cursor, change_set_id, user_login: str):
    cursor.execute(
        """
        SELECT id, call_id, tool_name, arguments, sort_order, created_at
          FROM logo.agent_change_set_item
         WHERE change_set_id = %s AND user_login = %s
         ORDER BY sort_order, id
         LIMIT %s
        """,
        (
            _uuid(change_set_id),
            user_login,
            MAX_PERSISTED_CHANGE_SET_ITEMS + 1,
        ),
    )
    rows = list(cursor.fetchall())
    if len(rows) > MAX_PERSISTED_CHANGE_SET_ITEMS:
        raise Conflict("Change-set exceeds the supported item limit")
    return rows


def _spreadsheet_build_in_progress(cursor, change_set_id, user_login: str) -> bool:
    cursor.execute(
        """
        SELECT 1
          FROM logo.agent_spreadsheet_job
         WHERE change_set_id = %s AND user_login = %s
           AND status IN ('mapping_processing', 'mapping_confirmed')
         LIMIT 1
        """,
        (_uuid(change_set_id), user_login),
    )
    return cursor.fetchone() is not None


def _assert_review_ready(cursor, change_set_id, user_login: str) -> None:
    if _spreadsheet_build_in_progress(cursor, change_set_id, user_login):
        raise Conflict(
            "Spreadsheet rows are still being staged; review is not ready"
        )


def insert_change_set(
    cursor,
    session_id,
    user_login: str,
    hours: int,
    *,
    origin: str = "chat",
) -> dict:
    cursor.execute(
        """
        INSERT INTO logo.agent_change_set (
            id, session_id, user_login, origin, expires_at
        ) VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            uuid.uuid4(),
            _uuid(session_id),
            user_login,
            origin,
            datetime.now(timezone.utc) + timedelta(hours=hours),
        ),
    )
    return dict(cursor.fetchone())


def new_change_set(session_id, user_login: str, *, hours: int = 24) -> dict:
    """Create a fresh owned set after locking its live parent session."""

    with database.cursor(write=True, actor=user_login) as cursor:
        cursor.execute(
            """
            SELECT id FROM logo.agent_chat_session
             WHERE id = %s AND user_login = %s AND expires_at > now()
             FOR UPDATE
            """,
            (_uuid(session_id), user_login),
        )
        if cursor.fetchone() is None:
            raise NotFound("Chat session not found")
        return insert_change_set(cursor, session_id, user_login, hours)


def get_or_create_pending_change_set(session_id, user_login: str) -> dict:
    with database.cursor(write=True, actor=user_login) as cursor:
        cursor.execute(
            """
            SELECT id FROM logo.agent_chat_session
             WHERE id = %s AND user_login = %s AND expires_at > now()
             FOR UPDATE
            """,
            (_uuid(session_id), user_login),
        )
        if cursor.fetchone() is None:
            raise NotFound("Chat session not found")
        cursor.execute(
            """
            SELECT * FROM logo.agent_change_set
             WHERE session_id = %s AND user_login = %s
               AND status = 'pending' AND origin = 'chat'
               AND expires_at > now()
               AND NOT EXISTS (
                   SELECT 1 FROM logo.agent_spreadsheet_job AS job
                    WHERE job.change_set_id = logo.agent_change_set.id
                      AND job.user_login = logo.agent_change_set.user_login
               )
             ORDER BY created_at DESC LIMIT 1
            """,
            (_uuid(session_id), user_login),
        )
        row = cursor.fetchone()
        return dict(row) if row else insert_change_set(
            cursor,
            session_id,
            user_login,
            24,
        )


def get_change_set(change_set_id, user_login: str) -> dict:
    with database.cursor() as cursor:
        row = dict(_owned_change_set(cursor, change_set_id, user_login))
        row["items"] = [dict(item) for item in _owned_items(cursor, change_set_id, user_login)]
        cursor.execute(
            """
            SELECT id, event_type, actor, preview_hash, created_at
              FROM logo.agent_action_journal
             WHERE change_set_id = %s AND user_login = %s
             ORDER BY created_at, id
            """,
            (_uuid(change_set_id), user_login),
        )
        row["journal"] = [dict(item) for item in cursor.fetchall()]
        row["review_blocked"] = _spreadsheet_build_in_progress(
            cursor,
            change_set_id,
            user_login,
        )
        return row


def stage_write(
    change_set_id,
    tool_name: str,
    arguments: Mapping[str, Any],
    call_id: str,
    user_login: str,
    *,
    max_items: int,
) -> dict:
    command = parse_command(tool_name, dict(arguments))
    required_tier(tool_name)
    if not call_id or len(call_id) > 255:
        raise InvalidCommand("tool call ID is invalid")

    for _attempt in range(3):
        with database.cursor() as cursor:
            change_set = dict(_owned_change_set(cursor, change_set_id, user_login))
            if change_set["status"] != "pending":
                raise Conflict("Change-set is no longer pending")
            if change_set["expires_at"] <= datetime.now(timezone.utc):
                raise Conflict("Change-set has expired")
            rows = _owned_items(cursor, change_set_id, user_login)
            existing = next((row for row in rows if row["call_id"] == call_id), None)
            if existing is not None:
                if (
                    str(existing["tool_name"]) != tool_name
                    or {k: v for k, v in dict(existing["arguments"]).items() if k != "_reserved_rule_id"}
                    != command_arguments(command)
                ):
                    raise Conflict(
                        "Tool call ID was reused with different arguments"
                    )
                change_set["items"] = len(rows)
                change_set["preview_results"] = []
                change_set["idempotent"] = True
                return change_set
            if len(rows) >= max_items:
                raise InvalidCommand("Change-set item limit reached")
            base_revision = int(change_set["revision"])
            commands = _commands_from_rows(rows) + [(tool_name, command)]

        _reserve_price_rule_id(command)
        preview = preview_commands(commands, user_login)
        revision = base_revision + 1
        digest = _hash_payload(revision, commands, preview.semantic_diff)
        destructive = any(name in HARD_DELETE_TOOLS for name, _ in commands)
        with database.cursor(write=True, actor=user_login) as cursor:
            locked = dict(_owned_change_set(
                cursor,
                change_set_id,
                user_login,
                lock=True,
            ))
            if locked["status"] != "pending":
                raise Conflict("Change-set is no longer pending")
            if locked["expires_at"] <= datetime.now(timezone.utc):
                raise Conflict("Change-set has expired")
            if int(locked["revision"]) != base_revision:
                continue
            cursor.execute(
                """
                INSERT INTO logo.agent_change_set_item (
                    id, change_set_id, user_login, call_id,
                    tool_name, arguments, sort_order
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid.uuid4(),
                    _uuid(change_set_id),
                    user_login,
                    call_id,
                    tool_name,
                    _json(_stored_arguments(command)),
                    len(rows),
                ),
            )
            cursor.execute(
                """
                UPDATE logo.agent_change_set
                   SET revision = %s,
                       preview_hash = %s,
                       preview_diff = %s,
                       affected_scopes = %s,
                       contains_hard_delete = %s,
                       updated_at = now()
                 WHERE id = %s AND user_login = %s
                RETURNING *
                """,
                (
                    revision,
                    digest,
                    _json(preview.semantic_diff),
                    _json([scope_dict(scope) for scope in preview.scopes]),
                    destructive,
                    _uuid(change_set_id),
                    user_login,
                ),
            )
            result = dict(cursor.fetchone())
            result["items"] = len(commands)
            result["preview_results"] = list(preview.results)
            return result
    raise Conflict("Change-set changed concurrently; retry the request")


def stage_write_batch(
    change_set_id,
    items: Sequence[tuple[str, Mapping[str, Any], str]],
    user_login: str,
    *,
    max_items: int,
) -> dict:
    """Atomically stage an idempotent batch with one cumulative preview."""

    effective_limit = max(1, min(int(max_items), MAX_PERSISTED_CHANGE_SET_ITEMS))
    if len(items) > effective_limit:
        raise InvalidCommand("Change-set item limit reached")
    candidates: list[tuple[str, str, MutationCommand]] = []
    seen_call_ids: set[str] = set()
    for tool_name, arguments, call_id in items:
        if not call_id or len(call_id) > 255 or call_id in seen_call_ids:
            raise InvalidCommand("Tool call ID is invalid or duplicated")
        seen_call_ids.add(call_id)
        required_tier(tool_name)
        candidates.append((
            call_id,
            tool_name,
            parse_command(tool_name, dict(arguments)),
        ))

    for _attempt in range(3):
        with database.cursor() as cursor:
            change_set = dict(_owned_change_set(
                cursor,
                change_set_id,
                user_login,
            ))
            if change_set["status"] != "pending":
                raise Conflict("Change-set is no longer pending")
            if change_set["expires_at"] <= datetime.now(timezone.utc):
                raise Conflict("Change-set has expired")
            existing_rows = _owned_items(cursor, change_set_id, user_login)
            base_revision = int(change_set["revision"])

        stored_commands = {str(row["call_id"]): command for row, (_name, command) in zip(existing_rows, _commands_from_rows(existing_rows))}
        for candidate_id, _name, candidate in candidates:
            if isinstance(candidate, SavePriceRuleCommand) and candidate.rule_id is None:
                stored = stored_commands.get(candidate_id)
                if isinstance(stored, SavePriceRuleCommand):
                    candidate._reserved_rule_id = stored._reserved_rule_id
                _reserve_price_rule_id(candidate)
        preview, accepted, rejected = _preview_batch_candidates(
            candidates,
            user_login,
        )
        accepted_by_call = {
            call_id: (name, command)
            for call_id, name, command in accepted
        }
        for row in existing_rows:
            existing = accepted_by_call.get(str(row["call_id"]))
            if existing is None:
                raise Conflict(
                    "Previously staged spreadsheet row is no longer valid"
                )
            name, command = existing
            if (
                str(row["tool_name"]) != name
                or dict(row["arguments"]) != _stored_arguments(command)
            ):
                raise Conflict(
                    "Tool call ID was reused with different arguments"
                )
        existing_ids = [str(row["call_id"]) for row in existing_rows]
        accepted_ids = [call_id for call_id, _name, _command in accepted]
        if existing_ids != accepted_ids[:len(existing_ids)]:
            raise Conflict("Spreadsheet staging order changed during retry")
        if len(accepted) > effective_limit:
            raise InvalidCommand("Change-set item limit reached")
        if not accepted:
            change_set["items"] = 0
            change_set["preview_results"] = []
            change_set["rejected_items"] = rejected
            change_set["idempotent"] = not existing_rows
            return change_set
        if len(existing_rows) == len(accepted):
            change_set["items"] = len(accepted)
            change_set["preview_results"] = []
            change_set["rejected_items"] = rejected
            change_set["idempotent"] = True
            return change_set

        commands = [
            (name, command) for _call_id, name, command in accepted
        ]
        revision = base_revision + 1
        digest = _hash_payload(revision, commands, preview.semantic_diff)
        destructive = any(name in HARD_DELETE_TOOLS for name, _ in commands)
        with database.cursor(write=True, actor=user_login) as cursor:
            locked = dict(_owned_change_set(
                cursor,
                change_set_id,
                user_login,
                lock=True,
            ))
            if locked["status"] != "pending":
                raise Conflict("Change-set is no longer pending")
            if locked["expires_at"] <= datetime.now(timezone.utc):
                raise Conflict("Change-set has expired")
            if int(locked["revision"]) != base_revision:
                continue
            for sort_order, (call_id, name, command) in enumerate(accepted):
                if sort_order < len(existing_rows):
                    continue
                cursor.execute(
                    """
                    INSERT INTO logo.agent_change_set_item (
                        id, change_set_id, user_login, call_id,
                        tool_name, arguments, sort_order
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid.uuid4(),
                        _uuid(change_set_id),
                        user_login,
                        call_id,
                        name,
                        _json(_stored_arguments(command)),
                        sort_order,
                    ),
                )
            cursor.execute(
                """
                UPDATE logo.agent_change_set
                   SET revision = %s,
                       preview_hash = %s,
                       preview_diff = %s,
                       affected_scopes = %s,
                       contains_hard_delete = %s,
                       updated_at = now()
                 WHERE id = %s AND user_login = %s
                RETURNING *
                """,
                (
                    revision,
                    digest,
                    _json(preview.semantic_diff),
                    _json([scope_dict(scope) for scope in preview.scopes]),
                    destructive,
                    _uuid(change_set_id),
                    user_login,
                ),
            )
            result = dict(cursor.fetchone())
            result["items"] = len(accepted)
            result["preview_results"] = list(preview.results)
            result["rejected_items"] = rejected
            result["idempotent"] = False
            return result
    raise Conflict("Change-set changed concurrently; retry the request")


def refresh_change_set(change_set_id, user_login: str) -> dict:
    for _attempt in range(3):
        with database.cursor() as cursor:
            row = dict(_owned_change_set(cursor, change_set_id, user_login))
            if row["status"] != "pending":
                raise Conflict("Change-set is no longer pending")
            if row["expires_at"] <= datetime.now(timezone.utc):
                raise Conflict("Change-set has expired")
            item_rows = _owned_items(cursor, change_set_id, user_login)
            commands = _commands_from_rows(item_rows)
            base_revision = int(row["revision"])
        preview = preview_commands(commands, user_login)
        revision = base_revision + 1
        digest = _hash_payload(revision, commands, preview.semantic_diff)
        with database.cursor(write=True, actor=user_login) as cursor:
            locked = dict(_owned_change_set(cursor, change_set_id, user_login, lock=True))
            if locked["status"] != "pending":
                raise Conflict("Change-set is no longer pending")
            if locked["expires_at"] <= datetime.now(timezone.utc):
                raise Conflict("Change-set has expired")
            if int(locked["revision"]) != base_revision:
                continue
            cursor.execute(
                """
                UPDATE logo.agent_change_set
                   SET revision = %s, preview_hash = %s,
                       preview_diff = %s, affected_scopes = %s,
                       updated_at = now()
                 WHERE id = %s AND user_login = %s
                RETURNING *
                """,
                (
                    revision,
                    digest,
                    _json(preview.semantic_diff),
                    _json([scope_dict(scope) for scope in preview.scopes]),
                    _uuid(change_set_id),
                    user_login,
                ),
            )
            return dict(cursor.fetchone())
    raise Conflict("Change-set changed concurrently; retry the request")


def apply_change_set(
    change_set_id,
    user_login: str,
    *,
    revision: int,
    confirmed_hash: str,
    acknowledge_hard_delete: bool,
) -> dict:
    with database.cursor(
        write=True,
        actor=f"agent:{user_login}"[:100],
    ) as cursor:
        change_set = dict(_owned_change_set(cursor, change_set_id, user_login, lock=True))
        _assert_review_ready(cursor, change_set_id, user_login)
        if change_set["status"] != "pending":
            raise Conflict("Change-set is no longer pending")
        if change_set["expires_at"] <= datetime.now(timezone.utc):
            raise Conflict("Change-set has expired")
        if (
            int(change_set["revision"]) != revision
            or str(change_set.get("preview_hash") or "") != confirmed_hash
        ):
            raise Conflict("Confirmation does not match the current preview")
        if change_set["contains_hard_delete"] and not acknowledge_hard_delete:
            raise HardDeleteAcknowledgementRequired(
                "Explicit hard-delete acknowledgement is required"
            )
        item_rows = _owned_items(cursor, change_set_id, user_login)
        commands = _commands_from_rows(item_rows)
        if not commands:
            raise InvalidCommand("Cannot apply an empty change-set")
        scopes = _all_scopes(commands)
        if _persisted_scopes(change_set.get("affected_scopes")) != scopes:
            # A deployment changed the command/scope contract after preview.
            # Abort before business locks or DML; the HTTP adapter refreshes
            # the set to a new revision/hash that requires fresh confirmation.
            raise PreviewDrift({
                "revision": revision,
                "preview_hash": "",
                "preview_diff": {},
                "scope_contract_changed": True,
            })
        lock_scopes(cursor, scopes)
        lock_scope_tables(cursor, scopes)
        before_exact = snapshot_scopes(cursor, scopes, for_update=True)
        results = []
        for name, command in commands:
            required_tier(name)
            results.append(_dispatch_checked(
                cursor,
                f"agent:{user_login}"[:100],
                command,
            ).value)
        after_exact = snapshot_scopes(cursor, scopes, for_update=False)
        validate_snapshot_state(before_exact, expected_scopes=scopes)
        validate_snapshot_state(after_exact, expected_scopes=scopes)
        semantic_diff = _preview_diff(before_exact, after_exact, results)
        actual_hash = _hash_payload(revision, commands, semantic_diff)
        if actual_hash != confirmed_hash:
            raise PreviewDrift({
                "revision": revision,
                "preview_hash": actual_hash,
                "preview_diff": semantic_diff,
            })
        cursor.execute(
            """
            INSERT INTO logo.agent_action_journal (
                id, change_set_id, user_login, event_type, actor,
                preview_hash, before_state, after_state
            ) VALUES (%s, %s, %s, 'apply', %s, %s, %s, %s)
            """,
            (
                uuid.uuid4(),
                _uuid(change_set_id),
                user_login,
                f"agent:{user_login}"[:100],
                actual_hash,
                _json(before_exact),
                _json(after_exact),
            ),
        )
        cursor.execute(
            """
            UPDATE logo.agent_change_set
               SET status = 'applied', applied_at = now(), updated_at = now()
             WHERE id = %s AND user_login = %s
            RETURNING *
            """,
            (_uuid(change_set_id), user_login),
        )
        return dict(cursor.fetchone())


def discard_change_set(change_set_id, user_login: str) -> dict:
    with database.cursor(write=True, actor=user_login) as cursor:
        row = dict(_owned_change_set(cursor, change_set_id, user_login, lock=True))
        _assert_review_ready(cursor, change_set_id, user_login)
        if row["status"] != "pending":
            raise Conflict("Only a pending change-set can be discarded")
        cursor.execute(
            """
            UPDATE logo.agent_change_set
               SET status = 'discarded', updated_at = now()
             WHERE id = %s AND user_login = %s
            RETURNING *
            """,
            (_uuid(change_set_id), user_login),
        )
        return dict(cursor.fetchone())


def _validated_undo_payload(
    cursor,
    change_set_id,
    user_login: str,
    *,
    lock: bool,
) -> tuple[dict, dict, tuple]:
    """Read a bounded journal and validate every stored restore value."""

    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        f"""
        SELECT id, status,
               CASE
                   WHEN octet_length(affected_scopes::text) <= %s
                   THEN affected_scopes
                   ELSE NULL
               END AS affected_scopes,
               octet_length(affected_scopes::text) > %s
                   AS affected_scopes_oversized
          FROM logo.agent_change_set
         WHERE id = %s AND user_login = %s{suffix}
        """,
        (
            MAX_SNAPSHOT_STATE_BYTES,
            MAX_SNAPSHOT_STATE_BYTES,
            _uuid(change_set_id),
            user_login,
        ),
    )
    change_set = cursor.fetchone()
    if change_set is None:
        raise NotFound("Change-set not found")
    change_set = dict(change_set)
    if change_set["status"] != "applied":
        raise Conflict("Only an applied change-set can be undone")
    if change_set["affected_scopes_oversized"]:
        raise InvalidCommand("Stored undo scope exceeds its safety limit")
    try:
        scopes = tuple(
            scope_from_dict(value)
            for value in (change_set["affected_scopes"] or [])
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise InvalidCommand("Stored undo scope is invalid") from exc

    cursor.execute(
        """
        SELECT id, preview_hash,
               CASE WHEN octet_length(before_state::text) <= %s
                    THEN before_state ELSE NULL END AS before_state,
               CASE WHEN octet_length(after_state::text) <= %s
                    THEN after_state ELSE NULL END AS after_state,
               octet_length(before_state::text) > %s AS before_oversized,
               octet_length(after_state::text) > %s AS after_oversized
          FROM logo.agent_action_journal
         WHERE change_set_id = %s AND user_login = %s
           AND event_type = 'apply'
        """,
        (
            MAX_SNAPSHOT_STATE_BYTES,
            MAX_SNAPSHOT_STATE_BYTES,
            MAX_SNAPSHOT_STATE_BYTES,
            MAX_SNAPSHOT_STATE_BYTES,
            _uuid(change_set_id),
            user_login,
        ),
    )
    journal = cursor.fetchone()
    if journal is None:
        raise Conflict("Apply journal is missing")
    journal = dict(journal)
    if journal["before_oversized"] or journal["after_oversized"]:
        raise InvalidCommand("Stored undo state exceeds its safety limit")
    validate_snapshot_state(journal["before_state"], expected_scopes=scopes)
    validate_snapshot_state(journal["after_state"], expected_scopes=scopes)
    return change_set, journal, scopes


def undo_change_set(change_set_id, user_login: str) -> dict:
    # Validate corrupted/tampered journal data before acquiring any metadata or
    # business-row lock. The locked transaction repeats the same bounded read
    # and full validation to close the time-of-check race.
    with database.cursor() as validation_cursor:
        _validated_undo_payload(
            validation_cursor,
            change_set_id,
            user_login,
            lock=False,
        )

    with database.cursor(
        write=True,
        actor=f"agent-undo:{user_login}"[:100],
    ) as cursor:
        change_set, journal, scopes = _validated_undo_payload(
            cursor,
            change_set_id,
            user_login,
            lock=True,
        )
        lock_scopes(cursor, scopes)
        lock_scope_tables(cursor, scopes)
        current = snapshot_scopes(cursor, scopes, for_update=True)
        if not states_equal(current, journal["after_state"]):
            raise Conflict(
                "Affected rows changed after apply; undo was not performed"
            )
        restore_state(
            cursor,
            journal["before_state"],
            expected_scopes=scopes,
        )
        restored = snapshot_scopes(cursor, scopes, for_update=False)
        if not states_equal(restored, journal["before_state"]):
            raise Conflict("Exact restoration check failed")
        actor = f"agent-undo:{user_login}"[:100]
        cursor.execute(
            """
            INSERT INTO logo.agent_action_journal (
                id, change_set_id, user_login, event_type, actor,
                preview_hash, before_state, after_state
            ) VALUES (%s, %s, %s, 'undo', %s, %s, %s, %s)
            """,
            (
                uuid.uuid4(),
                _uuid(change_set_id),
                user_login,
                actor,
                journal["preview_hash"],
                _json(journal["after_state"]),
                _json(journal["before_state"]),
            ),
        )
        cursor.execute(
            """
            UPDATE logo.agent_change_set
               SET status = 'undone', undone_at = now(), updated_at = now()
             WHERE id = %s AND user_login = %s
            RETURNING *
            """,
            (_uuid(change_set_id), user_login),
        )
        return dict(cursor.fetchone())
