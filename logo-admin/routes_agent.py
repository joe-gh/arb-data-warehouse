"""Allowlisted, owner-scoped read-only agent HTTP and SSE routes."""

import asyncio
import json
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent import AgentError, run_turn
from agent_logging import log_event
import agent_repository
from authorization import AccessContext, require_agent_access
from config import get_settings
from db import database
import re
import logging
import queries

logger = logging.getLogger("arb_logo_admin.agent")
from domain import (
    Conflict,
    DomainError,
    HardDeleteAcknowledgementRequired,
    InvalidCommand,
    NotFound,
    PreviewDrift,
)
from quotas import QuotaExceeded
import spreadsheet
import staging


router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    include_in_schema=False,
)

_turn_semaphore = asyncio.Semaphore(
    get_settings().agent_max_concurrent_turns
)
CAPACITY_WAIT_SECONDS = 1.0


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="", max_length=200)


_UI_STORE_CODE = re.compile(r"^S_[A-Za-z0-9_]{1,30}$")
_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_VIEW = re.compile(r"^[a-z]{1,24}$")
_DIALOG = re.compile(r"^[a-z\-]{1,32}$")


def _clean_code(value: Any, pattern: re.Pattern = _CODE) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if pattern.match(value) else None


class ScreenContext(BaseModel):
    """What the operator has on screen. Identifiers only; each field is
    sanitized to None rather than rejecting the turn, so a stale client can
    never break chat. Names are resolved server-side."""

    model_config = ConfigDict(extra="ignore")
    view: Optional[str] = None
    store: Optional[str] = None
    style: Optional[str] = None
    color: Optional[str] = None
    option_row: Optional[int] = None
    position: Optional[int] = None
    dialog: Optional[str] = None
    batch_styles: Optional[list[str]] = None

    @field_validator("view", mode="before")
    @classmethod
    def _view(cls, value):
        return _clean_code(value, _VIEW)

    @field_validator("store", mode="before")
    @classmethod
    def _store(cls, value):
        return _clean_code(value, _UI_STORE_CODE)

    @field_validator("style", "color", mode="before")
    @classmethod
    def _code(cls, value):
        return _clean_code(value)

    @field_validator("dialog", mode="before")
    @classmethod
    def _dialog(cls, value):
        return _clean_code(value, _DIALOG)

    @field_validator("option_row", mode="before")
    @classmethod
    def _row(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if 1 <= value <= 999 else None

    @field_validator("position", mode="before")
    @classmethod
    def _position(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if 1 <= value <= 3 else None

    @field_validator("batch_styles", mode="before")
    @classmethod
    def _batch(cls, value):
        if not isinstance(value, list):
            return None
        codes = [c for c in (_clean_code(v) for v in value[:50]) if c]
        return codes or None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # strict=False on this field only: a session_id arrives as a JSON string,
    # which strict mode would reject for a uuid.UUID field (422). Allow the
    # string to coerce to UUID while keeping strict validation for message.
    session_id: Optional[uuid.UUID] = Field(default=None, strict=False)
    message: str = Field(min_length=1)
    # The store selected in the app header (S_xxxx). Optional context only;
    # validated against a strict pattern and resolved to a display name
    # server-side, never echoed as free text into the prompt.
    store: Optional[str] = Field(default=None, max_length=40)
    # The current screen (view, selections, open dialog). Sanitized field by
    # field; see ScreenContext.
    context: Optional[ScreenContext] = None


class ApplyChangeSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1)
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledge_hard_delete: bool = False


class ConfirmSpreadsheetMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mapping_revision: int = Field(ge=1)
    mapping_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SessionBusy(RuntimeError):
    pass


async def _joinable_to_thread(function, /, *args, **kwargs):
    """Wait for started DB work before propagating request cancellation."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


async def _close_agent_iterator(next_event, iterator) -> None:
    """Drain provider/tool cleanup before releasing turn ownership."""

    if next_event is not None:
        if not next_event.done():
            next_event.cancel()
        try:
            await asyncio.shield(next_event)
        except (asyncio.CancelledError, StopAsyncIteration, Exception):
            pass
    if iterator is not None:
        try:
            await iterator.aclose()
        except (asyncio.CancelledError, RuntimeError):
            pass


def _require_agent_writes() -> None:
    if not get_settings().agent_writes_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def _raise_domain(exc: DomainError) -> None:
    if isinstance(exc, NotFound):
        raise HTTPException(status_code=404, detail="Not found") from None
    if isinstance(exc, HardDeleteAcknowledgementRequired):
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if isinstance(exc, InvalidCommand):
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if isinstance(exc, Conflict):
        raise HTTPException(status_code=409, detail=str(exc)) from None
    raise HTTPException(status_code=400, detail=str(exc)) from None


def sse(event: dict) -> bytes:
    return (
        "data: "
        + json.dumps(
            jsonable_encoder(event),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n"
    ).encode("utf-8")


def _create_session(user_login: str, title: str = "") -> dict:
    settings = get_settings()
    with database.cursor(write=True, actor=user_login) as cursor:
        return agent_repository.create_session(
            cursor,
            user_login=user_login,
            retention_days=settings.agent_chat_retention_days,
            title=" ".join(title.split()),
        )


def _list_sessions(
    user_login: str,
    limit: int,
    before_updated_at: Optional[datetime] = None,
    before_id: Optional[uuid.UUID] = None,
) -> dict:
    with database.cursor() as cursor:
        return agent_repository.list_sessions(
            cursor,
            user_login=user_login,
            limit=limit,
            before_updated_at=before_updated_at,
            before_id=before_id,
        )


def _session_detail(
    session_id,
    user_login: str,
    before_created_at: Optional[datetime] = None,
    before_id: Optional[uuid.UUID] = None,
    change_set_before_priority: Optional[int] = None,
    change_set_before_updated_at: Optional[datetime] = None,
    change_set_before_id: Optional[uuid.UUID] = None,
    spreadsheet_before_priority: Optional[int] = None,
    spreadsheet_before_created_at: Optional[datetime] = None,
    spreadsheet_before_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    with database.cursor() as cursor:
        session = agent_repository.get_session(
            cursor,
            session_id,
            user_login,
        )
        if session is None:
            return None
        history = agent_repository.list_messages(
            cursor,
            session_id,
            user_login,
            before_created_at=before_created_at,
            before_id=before_id,
        )
        change_set_page = agent_repository.list_session_change_sets(
            cursor,
            session_id,
            user_login,
            before_priority=change_set_before_priority,
            before_updated_at=change_set_before_updated_at,
            before_id=change_set_before_id,
        )
        spreadsheet_page = agent_repository.list_session_spreadsheet_jobs(
            cursor,
            session_id,
            user_login,
            before_priority=spreadsheet_before_priority,
            before_created_at=spreadsheet_before_created_at,
            before_id=spreadsheet_before_id,
        )
        return {
            "session": session,
            "messages": history["messages"],
            "messages_truncated": history["truncated"],
            "messages_limit_bytes": history["limit_bytes"],
            "messages_oldest_cursor": history["oldest_cursor"],
            "change_sets": change_set_page["change_sets"],
            "change_sets_truncated": change_set_page["truncated"],
            "change_sets_oldest_cursor": change_set_page["oldest_cursor"],
            "spreadsheet_jobs": spreadsheet_page["spreadsheet_jobs"],
            "spreadsheet_jobs_truncated": spreadsheet_page["truncated"],
            "spreadsheet_jobs_oldest_cursor": spreadsheet_page["oldest_cursor"],
        }


_PRIVATE_SPREADSHEET_RESPONSE_FIELDS = frozenset({
    "storage_key",
    "sha256",
    "user_login",
})


def _public_spreadsheet_value(value):
    """Recursively strip server-only metadata from browser responses."""

    if isinstance(value, dict):
        return {
            key: _public_spreadsheet_value(item)
            for key, item in value.items()
            if key not in _PRIVATE_SPREADSHEET_RESPONSE_FIELDS
        }
    if isinstance(value, list):
        return [_public_spreadsheet_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_public_spreadsheet_value(item) for item in value)
    return value


def _public_spreadsheet_result(result: dict) -> dict:
    """Strip private storage metadata from every browser response."""

    return _public_spreadsheet_value(result)


def _prepare_turn(
    *,
    session_id,
    message: str,
    user_login: str,
    turn_id,
) -> tuple[dict, list[dict]]:
    settings = get_settings()
    with database.cursor(write=True, actor=user_login) as cursor:
        session = None
        if session_id is None:
            session = agent_repository.create_session(
                cursor,
                user_login=user_login,
                retention_days=settings.agent_chat_retention_days,
            )
            session_id = session["id"]
        else:
            session = agent_repository.get_session(
                cursor,
                session_id,
                user_login,
            )
        if session is None:
            raise LookupError("session not found")
        replay = agent_repository.get_replay_items(
            cursor,
            session_id,
            user_login,
            maximum_bytes=min(
                500_000,
                max(20_000, settings.agent_max_input_chars * 8),
            ),
        )
        leased = agent_repository.acquire_turn(
            cursor,
            session_id=session_id,
            user_login=user_login,
            turn_id=turn_id,
            lease_seconds=settings.agent_turn_timeout_seconds + 30,
        )
        if leased is None:
            raise SessionBusy("session already has an active turn")
        user_item = {
            "role": "user",
            "content": [{"type": "input_text", "text": message}],
        }
        agent_repository.append_message(
            cursor,
            session_id=session_id,
            user_login=user_login,
            turn_id=turn_id,
            role="user",
            status="complete",
            content=message,
            replay_items=[user_item],
        )
        replay.append(user_item)
        leased["id"] = session_id
        return leased, replay


def _finish_turn(
    *,
    session_id,
    user_login: str,
    turn_id,
    status: str,
    content: str,
    replay_items: list[dict],
) -> dict:
    with database.cursor(write=True, actor=user_login) as cursor:
        cursor.execute(
            """
            SELECT (
                       active_turn_id = %s
                       AND turn_lease_expires_at > now()
                   ) AS turn_owned
              FROM logo.agent_chat_session
             WHERE id = %s AND user_login = %s
             FOR UPDATE
            """,
            (turn_id, session_id, user_login),
        )
        session = cursor.fetchone()
        if session is None:
            raise LookupError("session not found")
        cursor.execute(
            """
            SELECT id, session_id, user_login, turn_id, role, status,
                   content, replay_items, created_at
              FROM logo.agent_chat_message
             WHERE session_id = %s AND user_login = %s
               AND turn_id = %s AND role = 'assistant'
             FOR UPDATE
            """,
            (session_id, user_login, turn_id),
        )
        existing = cursor.fetchone()
        if not bool(session["turn_owned"]) and existing is None:
            raise SessionBusy("active turn lease is no longer owned")
        message = (
            dict(existing)
            if existing is not None
            else agent_repository.append_message(
                cursor,
                session_id=session_id,
                user_login=user_login,
                turn_id=turn_id,
                role="assistant",
                status=status,
                content=content,
                replay_items=replay_items,
            )
        )
        if bool(session["turn_owned"]):
            if not agent_repository.release_turn(
                cursor,
                session_id=session_id,
                user_login=user_login,
                turn_id=turn_id,
            ):
                raise SessionBusy(
                    "active turn lease was lost during persistence"
                )
        return message


async def _finish_turn_cancellation_safe(**kwargs) -> tuple[dict, bool]:
    """Join a committing terminal write and report deferred cancellation."""

    task = asyncio.create_task(asyncio.to_thread(_finish_turn, **kwargs))
    try:
        return await asyncio.shield(task), False
    except asyncio.CancelledError:
        # The caller needs the committed marker before propagating cancellation;
        # otherwise its cancellation handler can attempt a second terminal row.
        return await asyncio.shield(task), True


async def _prepare_turn_cancellation_safe(
    *,
    session_id,
    message: str,
    user_login: str,
    turn_id,
) -> tuple[dict, list[dict]]:
    """Compensate if request cancellation races a committed turn lease."""

    prepare_task = asyncio.create_task(asyncio.to_thread(
        _prepare_turn,
        session_id=session_id,
        message=message,
        user_login=user_login,
        turn_id=turn_id,
    ))
    try:
        return await asyncio.shield(prepare_task)
    except asyncio.CancelledError:
        try:
            prepared_session, _ = await asyncio.shield(prepare_task)
        except Exception:
            pass
        else:
            finish_task = asyncio.create_task(asyncio.to_thread(
                _finish_turn,
                session_id=prepared_session["id"],
                user_login=user_login,
                turn_id=turn_id,
                status="cancelled",
                content="",
                replay_items=[],
            ))
            try:
                await asyncio.shield(finish_task)
            except asyncio.CancelledError:
                await asyncio.shield(finish_task)
            except SessionBusy:
                pass
        raise


def _release_turn(session_id, user_login: str, turn_id) -> None:
    with database.cursor(write=True, actor=user_login) as cursor:
        agent_repository.release_turn(
            cursor,
            session_id=session_id,
            user_login=user_login,
            turn_id=turn_id,
        )


def _renew_turn(
    session_id,
    user_login: str,
    turn_id,
    lease_seconds: int,
) -> bool:
    with database.cursor(write=True, actor=user_login) as cursor:
        return agent_repository.renew_turn(
            cursor,
            session_id=session_id,
            user_login=user_login,
            turn_id=turn_id,
            lease_seconds=lease_seconds,
        )


class _TurnCleanup:
    """Idempotently release both persistent and process-local capacity."""

    def __init__(self, session_id, user_login: str, turn_id) -> None:
        self.session_id = session_id
        self.user_login = user_login
        self.turn_id = turn_id
        self._lock = asyncio.Lock()
        self._released = False

    async def __call__(self) -> None:
        async with self._lock:
            if self._released:
                return
            self._released = True
            try:
                await asyncio.to_thread(
                    _release_turn,
                    self.session_id,
                    self.user_login,
                    self.turn_id,
                )
            finally:
                _turn_semaphore.release()


class _CleanupStreamingResponse(StreamingResponse):
    """Run turn cleanup even when ASGI send fails before body iteration."""

    def __init__(self, *args, cleanup: _TurnCleanup, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._turn_cleanup = cleanup

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(self._turn_cleanup())


async def _lease_heartbeat(
    *,
    session_id,
    user_login: str,
    turn_id,
    lease_seconds: int,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    interval = max(2.0, min(10.0, lease_seconds / 3))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            renewed = await asyncio.to_thread(
                _renew_turn,
                session_id,
                user_login,
                turn_id,
                lease_seconds,
            )
        except Exception:
            renewed = False
        if not renewed:
            lost.set()
            return


@router.post("/sessions")
async def create_session_route(
    body: CreateSessionRequest,
    user: dict[str, str] = Depends(require_agent_access),
):
    context = AccessContext.from_session(user)
    session = await _joinable_to_thread(
        _create_session,
        context.user_login,
        body.title,
    )
    return {"session": session}


@router.get("/sessions")
async def list_sessions_route(
    limit: int = Query(50, ge=1, le=100),
    before_updated_at: Optional[datetime] = Query(None),
    before_id: Optional[uuid.UUID] = Query(None),
    user: dict[str, str] = Depends(require_agent_access),
):
    if (before_updated_at is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="Incomplete session cursor")
    context = AccessContext.from_session(user)
    page = await asyncio.to_thread(
        _list_sessions,
        context.user_login,
        limit,
        before_updated_at,
        before_id,
    )
    return {
        "sessions": page["sessions"],
        "sessions_truncated": page["truncated"],
        "sessions_oldest_cursor": page["oldest_cursor"],
    }


@router.get("/sessions/{session_id}")
async def session_detail_route(
    session_id: uuid.UUID,
    before_created_at: Optional[datetime] = Query(None),
    before_id: Optional[uuid.UUID] = Query(None),
    change_set_before_priority: Optional[int] = Query(None, ge=0, le=1),
    change_set_before_updated_at: Optional[datetime] = Query(None),
    change_set_before_id: Optional[uuid.UUID] = Query(None),
    spreadsheet_before_priority: Optional[int] = Query(None, ge=0, le=1),
    spreadsheet_before_created_at: Optional[datetime] = Query(None),
    spreadsheet_before_id: Optional[uuid.UUID] = Query(None),
    user: dict[str, str] = Depends(require_agent_access),
):
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="Incomplete history cursor")
    change_set_cursor = (
        change_set_before_priority,
        change_set_before_updated_at,
        change_set_before_id,
    )
    if any(value is not None for value in change_set_cursor) and not all(
        value is not None for value in change_set_cursor
    ):
        raise HTTPException(status_code=422, detail="Incomplete change-set cursor")
    spreadsheet_cursor = (
        spreadsheet_before_priority,
        spreadsheet_before_created_at,
        spreadsheet_before_id,
    )
    if any(value is not None for value in spreadsheet_cursor) and not all(
        value is not None for value in spreadsheet_cursor
    ):
        raise HTTPException(status_code=422, detail="Incomplete spreadsheet cursor")
    context = AccessContext.from_session(user)
    detail = await asyncio.to_thread(
        _session_detail,
        session_id,
        context.user_login,
        before_created_at,
        before_id,
        change_set_before_priority,
        change_set_before_updated_at,
        change_set_before_id,
        spreadsheet_before_priority,
        spreadsheet_before_created_at,
        spreadsheet_before_id,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Not found")
    return detail


@router.get("/change-sets/{change_set_id}")
async def change_set_detail_route(
    change_set_id: str,
    user: dict[str, str] = Depends(require_agent_access),
):
    context = AccessContext.from_session(user)
    try:
        result = await asyncio.to_thread(
            staging.get_change_set,
            change_set_id,
            context.user_login,
        )
    except DomainError as exc:
        _raise_domain(exc)
    return jsonable_encoder(result)


@router.post("/change-sets/{change_set_id}/apply")
async def apply_change_set_route(
    change_set_id: str,
    body: ApplyChangeSetRequest,
    user: dict[str, str] = Depends(require_agent_access),
):
    _require_agent_writes()
    context = AccessContext.from_session(user)
    started = time.monotonic()
    try:
        result = await _joinable_to_thread(
            staging.apply_change_set,
            change_set_id,
            context.user_login,
            revision=body.revision,
            confirmed_hash=body.preview_hash,
            acknowledge_hard_delete=body.acknowledge_hard_delete,
        )
    except PreviewDrift as exc:
        log_event(
            "apply_drift",
            change_set_id=change_set_id,
            user_login=context.user_login,
            status="conflict",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        try:
            refreshed = await _joinable_to_thread(
                staging.refresh_change_set,
                change_set_id,
                context.user_login,
            )
        except DomainError:
            refreshed = exc.preview
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "change_set": jsonable_encoder(refreshed),
            },
        ) from None
    except DomainError as exc:
        log_event(
            "apply_failure",
            change_set_id=change_set_id,
            user_login=context.user_login,
            status="conflict" if isinstance(exc, Conflict) else "rejected",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        _raise_domain(exc)
    log_event(
        "apply_complete",
        change_set_id=change_set_id,
        revision=body.revision,
        user_login=context.user_login,
        status="applied",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return jsonable_encoder(result)


@router.post("/change-sets/{change_set_id}/discard")
async def discard_change_set_route(
    change_set_id: str,
    user: dict[str, str] = Depends(require_agent_access),
):
    _require_agent_writes()
    context = AccessContext.from_session(user)
    started = time.monotonic()
    try:
        result = await _joinable_to_thread(
            staging.discard_change_set,
            change_set_id,
            context.user_login,
        )
    except DomainError as exc:
        _raise_domain(exc)
    log_event(
        "discard_complete",
        change_set_id=change_set_id,
        user_login=context.user_login,
        status="discarded",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return jsonable_encoder(result)


@router.post("/change-sets/{change_set_id}/undo")
async def undo_change_set_route(
    change_set_id: str,
    user: dict[str, str] = Depends(require_agent_access),
):
    _require_agent_writes()
    context = AccessContext.from_session(user)
    started = time.monotonic()
    try:
        result = await _joinable_to_thread(
            staging.undo_change_set,
            change_set_id,
            context.user_login,
        )
    except DomainError as exc:
        log_event(
            "undo_failure",
            change_set_id=change_set_id,
            user_login=context.user_login,
            status="conflict" if isinstance(exc, Conflict) else "rejected",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        _raise_domain(exc)
    log_event(
        "undo_complete",
        change_set_id=change_set_id,
        user_login=context.user_login,
        status="undone",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return jsonable_encoder(result)


@router.post("/spreadsheets")
async def upload_spreadsheet_route(
    session_id: str = Form(..., min_length=1, max_length=100),
    instruction: str = Form(default="", max_length=4_000),
    file: UploadFile = File(...),
    user: dict[str, str] = Depends(require_agent_access),
):
    _require_agent_writes()
    settings = get_settings()
    context = AccessContext.from_session(user)
    started = time.monotonic()
    original_name = file.filename or ""
    media_type = file.content_type or "application/octet-stream"
    try:
        await asyncio.wait_for(
            _turn_semaphore.acquire(),
            timeout=CAPACITY_WAIT_SECONDS,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="The assistant is busy; retry shortly",
            headers={"Retry-After": "2"},
        ) from None
    try:
        try:
            try:
                data = await file.read(
                    settings.agent_max_spreadsheet_bytes + 1
                )
            finally:
                await file.close()
            if len(data) > settings.agent_max_spreadsheet_bytes:
                raise HTTPException(
                    status_code=413,
                    detail="Spreadsheet is too large",
                )
            result = await spreadsheet.create_spreadsheet_job(
                session_id,
                context.user_login,
                data,
                original_name,
                media_type,
                instruction,
                settings,
                mapping_semaphore=None,
            )
        finally:
            _turn_semaphore.release()
    except QuotaExceeded as exc:
        log_event(
            "spreadsheet_quota_reject",
            user_login=context.user_login,
            status="rejected",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except spreadsheet.MappingCapacityExceeded as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "2"},
        ) from None
    except DomainError as exc:
        _raise_domain(exc)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="The spreadsheet mapping could not be validated",
        ) from None
    log_event(
        "spreadsheet_mapping_ready",
        session_id=session_id,
        user_login=context.user_login,
        status="mapping_pending",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return jsonable_encoder(_public_spreadsheet_result(result))


@router.get("/spreadsheets/{job_id}")
async def spreadsheet_status_route(
    job_id: str,
    user: dict[str, str] = Depends(require_agent_access),
):
    context = AccessContext.from_session(user)
    try:
        result = await asyncio.to_thread(
            spreadsheet.get_spreadsheet_job,
            job_id,
            context.user_login,
        )
    except DomainError as exc:
        _raise_domain(exc)
    return jsonable_encoder(_public_spreadsheet_result(result))


@router.post("/spreadsheets/{job_id}/confirm-mapping")
async def confirm_spreadsheet_mapping_route(
    job_id: str,
    body: ConfirmSpreadsheetMappingRequest,
    user: dict[str, str] = Depends(require_agent_access),
):
    _require_agent_writes()
    settings = get_settings()
    context = AccessContext.from_session(user)
    started = time.monotonic()
    try:
        await asyncio.wait_for(
            _turn_semaphore.acquire(),
            timeout=CAPACITY_WAIT_SECONDS,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="The assistant is busy; retry shortly",
            headers={"Retry-After": "2"},
        ) from None
    try:
        try:
            result = await _joinable_to_thread(
                spreadsheet.confirm_spreadsheet_mapping,
                job_id,
                context.user_login,
                body.mapping_revision,
                body.mapping_hash,
                settings.agent_max_spreadsheet_rows,
                settings,
            )
        finally:
            _turn_semaphore.release()
    except DomainError as exc:
        _raise_domain(exc)
    change_set = result.get("change_set") if isinstance(result, dict) else None
    log_event(
        "spreadsheet_stage_complete",
        session_id=result.get("session_id") if isinstance(result, dict) else None,
        user_login=context.user_login,
        change_set_id=(
            change_set.get("id") if isinstance(change_set, dict) else None
        ),
        status=result.get("status") if isinstance(result, dict) else "complete",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return jsonable_encoder(_public_spreadsheet_result(result))


def _resolve_screen(body: "ChatRequest") -> Optional[dict]:
    """Merge the legacy store field with the screen context and resolve names.
    Returns None when nothing usable was sent."""
    context = body.context or ScreenContext()
    store = context.store or _clean_code(body.store, _UI_STORE_CODE)
    screen: dict = {
        "view": context.view,
        "store": store,
        "style": context.style,
        "color": context.color,
        "option_row": context.option_row,
        "position": context.position,
        "dialog": context.dialog,
        "batch_styles": context.batch_styles,
    }
    if not any(v for v in screen.values()):
        return None
    if store:
        _, screen["store_name"] = _resolve_ui_store(store)
        if context.style:
            try:
                with database.cursor() as cursor:
                    cursor.execute(
                        "SELECT max(name) AS name FROM woo.store_product_state "
                        "WHERE fdm4_store = %s AND style_code = %s AND kind = 'parent'",
                        (store, context.style),
                    )
                    row = cursor.fetchone()
                    screen["style_name"] = (row or {}).get("name") or None
                    if context.color:
                        cursor.execute(
                            "SELECT max(color) AS color FROM woo.store_product_state "
                            "WHERE fdm4_store = %s AND style_code = %s AND color_code = %s",
                            (store, context.style, context.color),
                        )
                        row = cursor.fetchone()
                        screen["color_name"] = (row or {}).get("color") or None
            except Exception:
                logger.warning("agent: could not resolve screen names for %s/%s", store, context.style)
    return screen


def _resolve_ui_store(store: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Validate the UI's selected store and look up its display name.

    Anything that is not a well-formed store code is dropped. A lookup
    failure never blocks the turn - the code alone is still useful."""
    code = (store or "").strip()
    if not code or not _UI_STORE_CODE.match(code):
        return None, None
    try:
        with database.cursor() as cursor:
            listing = queries.list_stores(cursor)
        rows = listing.get("stores") if isinstance(listing, dict) else listing
        for row in rows or []:
            if str(row.get("fdm4_store") or row.get("code") or "") == code:
                return code, str(row.get("display_name") or "")
    except Exception:
        logger.warning("agent: could not resolve UI store %s", code)
    return code, None


@router.post("/chat")
async def chat_route(
    body: ChatRequest,
    request: Request,
    user: dict[str, str] = Depends(require_agent_access),
):
    settings = get_settings()
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message is required")
    if len(message) > settings.agent_max_input_chars:
        raise HTTPException(status_code=413, detail="Message is too large")

    context = AccessContext.from_session(user)
    turn_id = uuid.uuid4()
    try:
        await asyncio.wait_for(
            _turn_semaphore.acquire(),
            timeout=CAPACITY_WAIT_SECONDS,
        )
    except TimeoutError:
        log_event(
            "turn_capacity_reject",
            user_login=context.user_login,
            status="busy",
        )
        raise HTTPException(
            status_code=503,
            detail="The assistant is busy; retry shortly",
            headers={"Retry-After": "2"},
        ) from None

    try:
        session, replay = await _prepare_turn_cancellation_safe(
            session_id=body.session_id,
            message=message,
            user_login=context.user_login,
            turn_id=turn_id,
        )
    except LookupError:
        _turn_semaphore.release()
        raise HTTPException(status_code=404, detail="Not found") from None
    except SessionBusy:
        _turn_semaphore.release()
        raise HTTPException(
            status_code=409,
            detail="This conversation already has an active turn",
        ) from None
    except BaseException:
        _turn_semaphore.release()
        raise

    session_id = session["id"]
    screen = await _joinable_to_thread(_resolve_screen, body)
    cleanup = _TurnCleanup(
        session_id,
        context.user_login,
        turn_id,
    )
    log_event(
        "turn_start",
        session_id=session_id,
        turn_id=turn_id,
        user_login=context.user_login,
        status="active",
    )

    async def event_stream() -> AsyncIterator[bytes]:
        assistant_parts: list[str] = []
        terminal_replay: list[dict] = []
        finished = False
        iterator = None
        next_event = None
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        lease_seconds = settings.agent_turn_timeout_seconds + 30
        heartbeat = asyncio.create_task(_lease_heartbeat(
            session_id=session_id,
            user_login=context.user_login,
            turn_id=turn_id,
            lease_seconds=lease_seconds,
            stop=heartbeat_stop,
            lost=lease_lost,
        ))
        try:
            iterator = run_turn(
                context,
                replay,
                settings,
                session_id=session_id,
                screen=screen,
            ).__aiter__()
            next_event = asyncio.create_task(iterator.__anext__())
            while True:
                done, _ = await asyncio.wait(
                    {next_event},
                    timeout=15.0,
                )
                if not done:
                    if lease_lost.is_set():
                        raise SessionBusy("active turn lease was lost")
                    if await request.is_disconnected():
                        raise asyncio.CancelledError()
                    yield b": heartbeat\n\n"
                    continue
                try:
                    if lease_lost.is_set():
                        raise SessionBusy("active turn lease was lost")
                    event = next_event.result()
                except StopAsyncIteration:
                    break

                if event.get("type") == "token":
                    assistant_parts.append(str(event.get("text", "")))
                if event.get("type") == "done":
                    terminal_replay = list(event.get("replay_items") or [])
                    _terminal, cancelled_after_commit = (
                        await _finish_turn_cancellation_safe(
                            session_id=session_id,
                            user_login=context.user_login,
                            turn_id=turn_id,
                            status="complete",
                            content="".join(assistant_parts),
                            replay_items=terminal_replay,
                        )
                    )
                    finished = True
                    if cancelled_after_commit:
                        raise asyncio.CancelledError()
                    log_event(
                        "turn_complete",
                        session_id=session_id,
                        turn_id=turn_id,
                        user_login=context.user_login,
                        status="complete",
                        input_tokens=event.get("input_tokens", 0),
                        output_tokens=event.get("output_tokens", 0),
                        tool_call_count=event.get("tool_call_count", 0),
                    )
                    public_event = {
                        "type": "done",
                        "session_id": str(session_id),
                        "turn_id": str(turn_id),
                        "input_tokens": event.get("input_tokens", 0),
                        "output_tokens": event.get("output_tokens", 0),
                        "tool_call_count": event.get("tool_call_count", 0),
                    }
                else:
                    public_event = dict(event)
                    public_event.setdefault("session_id", str(session_id))
                    public_event.setdefault("turn_id", str(turn_id))
                yield sse(public_event)
                if event.get("type") == "done":
                    break
                if await request.is_disconnected():
                    raise asyncio.CancelledError()
                next_event = asyncio.create_task(iterator.__anext__())
        except asyncio.CancelledError:
            await _close_agent_iterator(next_event, iterator)
            next_event = None
            iterator = None
            if not finished:
                try:
                    await asyncio.shield(_joinable_to_thread(
                        _finish_turn,
                        session_id=session_id,
                        user_login=context.user_login,
                        turn_id=turn_id,
                        status="cancelled",
                        content="".join(assistant_parts),
                        replay_items=terminal_replay,
                    ))
                except SessionBusy:
                    pass
                else:
                    finished = True
                    log_event(
                        "turn_disconnect",
                        session_id=session_id,
                        turn_id=turn_id,
                        user_login=context.user_login,
                        status="cancelled",
                    )
            raise
        except SessionBusy:
            log_event(
                "turn_lease_lost",
                session_id=session_id,
                turn_id=turn_id,
                user_login=context.user_login,
                status="cancelled",
            )
            yield sse({
                "type": "error",
                "message": "This conversation turn lost its active lease.",
                "session_id": str(session_id),
                "turn_id": str(turn_id),
            })
        except (QuotaExceeded, AgentError, TimeoutError) as exc:
            failure_kind = type(exc).__name__
            logger.warning(
                "agent turn failed: session=%s turn=%s user=%s kind=%s detail=%s",
                session_id, turn_id, context.user_login, failure_kind, str(exc)[:300],
            )
            if not finished:
                await _joinable_to_thread(
                    _finish_turn,
                    session_id=session_id,
                    user_login=context.user_login,
                    turn_id=turn_id,
                    status="failed",
                    content="".join(assistant_parts),
                    replay_items=terminal_replay,
                )
                finished = True
            log_event(
                "turn_failure",
                session_id=session_id,
                turn_id=turn_id,
                user_login=context.user_login,
                status="failed",
                kind=failure_kind,
            )
            yield sse({
                "type": "error",
                "message": (
                    "The assistant's usage budget is used up for now; try again later."
                    if isinstance(exc, QuotaExceeded)
                    else "The assistant could not complete that request."
                ),
                "session_id": str(session_id),
                "turn_id": str(turn_id),
            })
        except Exception as exc:
            logger.exception(
                "agent turn crashed: session=%s turn=%s user=%s kind=%s",
                session_id, turn_id, context.user_login, type(exc).__name__,
            )
            if not finished:
                await _joinable_to_thread(
                    _finish_turn,
                    session_id=session_id,
                    user_login=context.user_login,
                    turn_id=turn_id,
                    status="failed",
                    content="".join(assistant_parts),
                    replay_items=terminal_replay,
                )
                finished = True
            log_event(
                "turn_failure",
                session_id=session_id,
                turn_id=turn_id,
                user_login=context.user_login,
                status="failed",
            )
            yield sse({
                "type": "error",
                "message": "The assistant is temporarily unavailable.",
                "session_id": str(session_id),
                "turn_id": str(turn_id),
            })
        finally:
            await _close_agent_iterator(next_event, iterator)
            heartbeat_stop.set()
            await asyncio.shield(heartbeat)
            await asyncio.shield(cleanup())

    return _CleanupStreamingResponse(
        event_stream(),
        cleanup=cleanup,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
