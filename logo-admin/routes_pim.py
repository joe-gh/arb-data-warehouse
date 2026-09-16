"""PIM (Sales Layer) routes: the read services the assistant and MCP share,
plus on-demand preview / push requests.

A request is an append-only intent row in logo.audit_log
(pim_push_requested); the worker on the warehouse box
(infra/pim_push_worker.py, root cron every minute) picks it up under the
same lock and caps as the hourly push and appends pim_push_started and
pim_push_finished / pim_push_failed rows. The app never holds the PIM API
key and never runs the push itself.
"""
from __future__ import annotations

import json
import secrets
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import pim_queries
from auth import require_csrf, require_user
from db import database
from queries import QueryNotFound, QueryValidationError

router = APIRouter(prefix="/api/pim", tags=["pim"])


def _read(service, /, **arguments):
    try:
        with database.cursor() as cursor:
            return service(cursor, **arguments)
    except QueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except QueryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/status")
def status(user: Dict[str, str] = Depends(require_user)):
    """Everything the PIM view shows: stage freshness, totals, the latest
    push set, what a preview said would go over, and recent requests."""
    del user
    with database.cursor() as cursor:
        out = pim_queries.pim_pipeline_status(cursor)
        out["preview"] = pim_queries.pim_preview(cursor)
        out["requests"] = pim_queries.pim_requests(cursor, limit=10)["requests"]
        return out


@router.get("/lookup")
def lookup(q: str = Query(min_length=1, max_length=100), user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_lookup, q=q)


@router.get("/explain")
def explain(style: str = Query(min_length=1, max_length=100), user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_explain_style, style=style)


@router.get("/history")
def history(q: str = Query(min_length=1, max_length=100), limit: int = Query(20, ge=1, le=200),
            user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_push_history, q=q, limit=limit)


@router.get("/pipeline")
def pipeline(user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_pipeline_status)


@router.get("/summary")
def summary(user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_summary)


@router.get("/pushes")
def pushes(limit: int = Query(10, ge=1, le=50), user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_recent_pushes, limit=limit)


@router.get("/requests")
def requests(limit: int = Query(10, ge=1, le=50), user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_requests, limit=limit)


@router.get("/request/{request_id}")
def request_status(request_id: str, user: Dict[str, str] = Depends(require_user)):
    del user
    return _read(pim_queries.pim_request_status, request_id=request_id)


class PimRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["preview", "push"] = Field(description="preview = dry diff only (nothing sent); push = pull the PIM mirror, diff and send.")
    styles: List[str] = Field(default_factory=list, max_length=100, description="Optional style codes to limit the run to.")


@router.post("/request")
def create_request(body: PimRequestBody, user: Dict[str, str] = Depends(require_csrf)):
    """Queue a preview or a push. Returns the request id to poll."""
    with database.cursor() as cursor:
        try:
            styles = pim_queries.validate_styles(cursor, body.styles)
        except QueryValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        pending = [r for r in pim_queries.pim_requests(cursor, limit=10)["requests"] if r["state"] in ("queued", "running")]
        if pending:
            raise HTTPException(status_code=409, detail=f"A {pending[0]['mode']} request is already {pending[0]['state']}; wait for it to finish.")
    request_id = secrets.token_hex(12)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            "INSERT INTO logo.audit_log (actor, action, fdm4_store, detail) VALUES (%s, %s, '', %s)",
            (user["user_login"], pim_queries.REQUEST_ACTION,
             json.dumps({"request_id": request_id, "mode": body.mode, "styles": styles})),
        )
    return {"request_id": request_id, "mode": body.mode, "styles": styles, "state": "queued"}
