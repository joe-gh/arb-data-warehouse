"""Category editor HTTP surface (Phase 1: targets + snapshots).

Every route is gated by ``require_catmgr``: while CATMGR_ENABLED is false the
whole surface answers 404, mirroring the assistant's ship-dark convention.
Snapshot imports run one WordPress fetch + one transaction PER BLOG, so the UI
can drive per-blog progress and a mid-list failure never poisons finished
blogs.
"""

import csv as csv_module
import io

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

import categories_draft
import categories_mapping
import categories_planner
import categories_runs
import categories_service
from auth import require_csrf, require_user
from categories_draft import DraftConflict, DraftError
from categories_service import BrokerError, TargetNotConfigured
from config import get_settings
from db import database


router = APIRouter(prefix="/api/categories", tags=["categories"])


def catmgr_visible(user_login: str) -> bool:
    """The category editor exists for an operator only when the feature is on
    AND, if CATMGR_VIEW_USERS is set, their login is on it (empty = everyone).
    Used by the page context (nav) and by every /api/categories route."""

    settings = get_settings()
    if not settings.catmgr_enabled:
        return False
    if not settings.catmgr_view_users:
        return True
    return str(user_login or "").strip().lower() in settings.catmgr_view_users


def require_catmgr(user: Dict[str, str] = Depends(require_user)) -> None:
    if not catmgr_visible(user.get("user_login", "")):
        raise HTTPException(status_code=404, detail="Not found")


def _configured_env(env: str) -> str:
    try:
        categories_service.get_target(env)
    except TargetNotConfigured:
        raise HTTPException(
            status_code=404,
            detail=f"Environment '{env}' is not configured",
        ) from None
    return env


@router.get("/targets")
def list_targets(
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    return {"targets": categories_service.configured_targets()}


@router.get("/snapshots")
def list_snapshots(
    env: str = Query(..., min_length=3, max_length=8),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(env)
    with database.cursor() as cursor:
        blogs = categories_service.snapshot_status(cursor, env)
    return {"env": env, "blogs": blogs}


@router.get("/blogs")
def list_blogs(
    env: str = Query(..., min_length=3, max_length=8),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(env)
    try:
        blogs = categories_service.fetch_blogs(env)
    except BrokerError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"env": env, "blogs": blogs}


@router.get("/wp-status")
def wp_status(
    env: str = Query(..., min_length=3, max_length=8),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(env)
    try:
        status = categories_service.fetch_wp_status(env)
    except BrokerError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"env": env, "status": status}


class SnapshotImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(min_length=3, max_length=8)
    blog_ids: List[int] = Field(min_length=1, max_length=200)


@router.post("/snapshots/import")
def import_snapshots(
    body: SnapshotImportRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    _configured_env(body.env)
    actor = user["user_login"]
    results = []
    seen = set()
    for blog_id in body.blog_ids:
        if blog_id in seen:
            continue
        seen.add(blog_id)
        try:
            export = categories_service.fetch_export(body.env, blog_id)
            with database.cursor(write=True, actor=actor) as cursor:
                result = categories_service.import_blog_snapshot(
                    cursor,
                    env=body.env,
                    blog_id=blog_id,
                    blog_path=str(export.get("blog_path") or ""),
                    terms=export.get("terms") or [],
                    products=export.get("products") or [],
                    actor=actor,
                )
            results.append({"ok": True, **result})
        except (BrokerError, ValueError) as exc:
            results.append({
                "ok": False,
                "blog_id": int(blog_id),
                "error": str(exc)[:500],
            })
    return {"env": body.env, "results": results}


# ---------------------------------------------------------------- draft tree


def _draft_errors(exc: Exception):
    if isinstance(exc, DraftConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, DraftError):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.get("/tree")
def get_tree(
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        nodes = categories_draft.list_nodes(cursor)
    return {"nodes": nodes}


@router.get("/tree/effective")
def get_effective_tree(
    blog_id: int = Query(..., ge=1),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        nodes = categories_draft.effective_tree(cursor, blog_id)
        overrides = categories_draft.list_overrides(cursor, blog_id)
    return {"blog_id": blog_id, "nodes": nodes, "overrides": overrides}


class NodeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=5000)
    position: int | None = Field(default=None, ge=0, le=10000)


@router.post("/nodes")
def create_node(
    body: NodeCreateRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            node = categories_draft.create_node(
                cursor, parent_id=body.parent_id, name=body.name,
                slug=body.slug, description=body.description,
                position=body.position, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"node": node}


class NodeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)


@router.put("/nodes/{node_id}")
def update_node(
    node_id: int,
    body: NodeUpdateRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            node = categories_draft.update_node(
                cursor, node_id, name=body.name, slug=body.slug,
                description=body.description, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"node": node}


class NodeMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = None
    position: int | None = Field(default=None, ge=0, le=10000)


@router.post("/nodes/{node_id}/move")
def move_node(
    node_id: int,
    body: NodeMoveRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            node = categories_draft.move_node(
                cursor, node_id, parent_id=body.parent_id,
                position=body.position, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"node": node}


@router.delete("/nodes/{node_id}")
def delete_node(
    node_id: int,
    cascade: bool = Query(default=False),
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            result = categories_draft.delete_node(
                cursor, node_id, cascade=cascade, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return result


class DraftSeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(min_length=3, max_length=8)
    blog_id: int = Field(ge=1)
    force: bool = False


@router.post("/draft/seed")
def seed_draft(
    body: DraftSeedRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    _configured_env(body.env)
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            result = categories_draft.seed_from_snapshot(
                cursor, env=body.env, blog_id=body.blog_id,
                actor=user["user_login"], force=body.force,
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return result


# ---------------------------------------------------------------- overrides


@router.get("/overrides")
def list_overrides(
    blog_id: int | None = Query(default=None, ge=1),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        overrides = categories_draft.list_overrides(cursor, blog_id)
    return {"overrides": overrides}


class OverrideSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    override_id: int | None = None
    blog_id: int = Field(ge=1)
    kind: str = Field(pattern="^(extra_node|rename|exclude)$")
    node_id: int | None = None
    name: str | None = Field(default=None, max_length=200)
    slug: str | None = Field(default=None, max_length=200)
    parent_node_id: int | None = None
    include_descendants: bool = True
    sort_order: int = Field(default=0, ge=0, le=100000)


@router.put("/overrides")
def save_override(
    body: OverrideSaveRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            override = categories_draft.set_override(
                cursor, blog_id=body.blog_id, kind=body.kind,
                override_id=body.override_id, node_id=body.node_id,
                name=body.name, slug=body.slug,
                parent_node_id=body.parent_node_id,
                include_descendants=body.include_descendants,
                sort_order=body.sort_order, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"override": override}


@router.delete("/overrides/{override_id}")
def delete_override(
    override_id: int,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            categories_draft.delete_override(
                cursor, override_id, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"ok": True}


# ---------------------------------------------------------------- slug map


@router.get("/mapping")
def get_mapping(
    env: str = Query(..., min_length=3, max_length=8),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(env)
    with database.cursor() as cursor:
        return categories_mapping.mapping_status(cursor, env)


@router.get("/mapping/suggest")
def get_mapping_suggestions(
    env: str = Query(..., min_length=3, max_length=8),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(env)
    with database.cursor() as cursor:
        return {"suggestions": categories_mapping.auto_suggest(cursor, env)}


class MappingRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_slug: str = Field(min_length=1, max_length=200)
    action: str = Field(pattern="^(map|delete|store_custom)$")
    target_node_id: int | None = None
    is_primary: bool | None = None
    override_id: int | None = None
    note: str = Field(default="", max_length=1000)


class MappingSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: List[MappingRow] = Field(min_length=1, max_length=500)


@router.put("/mapping")
def save_mapping(
    body: MappingSaveRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    actor = user["user_login"]
    if len(body.rows) == 1:
        row = body.rows[0]
        try:
            with database.cursor(write=True, actor=actor) as cursor:
                saved = categories_mapping.set_mapping(
                    cursor, old_slug=row.old_slug, action=row.action,
                    target_node_id=row.target_node_id,
                    is_primary=row.is_primary, override_id=row.override_id,
                    note=row.note, actor=actor,
                )
        except (DraftError, DraftConflict) as exc:
            raise _draft_errors(exc) from exc
        return {"mapping": saved}
    with database.cursor(write=True, actor=actor) as cursor:
        results = categories_mapping.bulk_set(
            cursor, [row.model_dump() for row in body.rows], actor=actor,
        )
    return {"results": results}


@router.delete("/mapping/{old_slug}")
def delete_mapping(
    old_slug: str,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            categories_mapping.clear_mapping(
                cursor, old_slug, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"ok": True}


# ---------------------------------------------------------------- rules


@router.get("/rules")
def get_rules(
    node_id: int | None = Query(default=None, ge=1),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        return {"rules": categories_mapping.list_rules(cursor, node_id)}


class RuleEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(min_length=3, max_length=8)
    spec: Dict[str, Any]
    limit: int = Field(default=50, ge=1, le=500)


@router.post("/rules/evaluate")
def evaluate_rule(
    body: RuleEvaluateRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(body.env)
    try:
        with database.cursor() as cursor:
            return categories_mapping.evaluate_rule(
                cursor, body.env, body.spec, limit=body.limit,
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc


class RuleSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: int | None = None
    node_id: int = Field(ge=1)
    spec: Dict[str, Any]
    priority: int = Field(default=0, ge=0, le=1000)
    note: str = Field(default="", max_length=1000)


@router.put("/rules")
def save_rule(
    body: RuleSaveRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            rule = categories_mapping.set_rule(
                cursor, node_id=body.node_id, spec=body.spec,
                priority=body.priority, note=body.note,
                rule_id=body.rule_id, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"rule": rule}


@router.delete("/rules/{rule_id}")
def delete_rule(
    rule_id: int,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            categories_mapping.delete_rule(
                cursor, rule_id, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"ok": True}


# ---------------------------------------------------------------- assignments


@router.get("/assignments")
def get_assignments(
    node_id: int = Query(..., ge=1),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        return {"assignments": categories_mapping.list_assignments(cursor, node_id)}


class AssignmentsSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: int = Field(ge=1)
    skus: List[str] = Field(min_length=1, max_length=5000)
    mode: str = Field(pattern="^(add|remove)$")
    source: str = Field(default="manual", pattern="^(manual|csv|ai|rule)$")
    note: str = Field(default="", max_length=1000)


@router.put("/assignments")
def save_assignments(
    body: AssignmentsSaveRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            result = categories_mapping.set_assignments(
                cursor, node_id=body.node_id, skus=body.skus, mode=body.mode,
                source=body.source, note=body.note, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return result


@router.delete("/assignments/{assignment_id}")
def delete_assignment(
    assignment_id: int,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            categories_mapping.delete_assignment(
                cursor, assignment_id, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"ok": True}


@router.get("/membership")
def get_membership(
    env: str = Query(..., min_length=3, max_length=8),
    node_id: int = Query(..., ge=1),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(env)
    try:
        with database.cursor() as cursor:
            return categories_mapping.effective_membership(cursor, env, node_id)
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc


@router.get("/assignments/export", response_class=PlainTextResponse)
def export_assignments(
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.sku, string_agg(n.slug, ';' ORDER BY n.slug) AS node_slugs
              FROM catmgr.product_assignment a
              JOIN catmgr.node n ON n.node_id = a.node_id
             WHERE a.mode = 'add'
             GROUP BY a.sku ORDER BY a.sku
            """
        )
        rows = cursor.fetchall()
    buffer = io.StringIO()
    writer = csv_module.writer(buffer)
    writer.writerow(["sku", "node_slugs"])
    for row in rows:
        writer.writerow([row["sku"], row["node_slugs"]])
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv")


class AssignmentsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv: str = Field(min_length=1, max_length=2_000_000)


@router.post("/assignments/import")
def import_assignments(
    body: AssignmentsImportRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    actor = user["user_login"]
    reader = csv_module.reader(io.StringIO(body.csv))
    header = next(reader, None)
    if not header or [h.strip().lower() for h in header[:2]] != ["sku", "node_slugs"]:
        raise HTTPException(status_code=422,
                            detail="CSV header must be: sku,node_slugs")
    by_node: Dict[str, List[str]] = {}
    bad_rows: List[str] = []
    for line_number, row in enumerate(reader, start=2):
        if not row or not "".join(row).strip():
            continue
        if len(row) < 2:
            bad_rows.append(f"line {line_number}: expected 2 columns")
            continue
        sku = row[0].strip().upper()
        for node_slug in row[1].split(";"):
            node_slug = node_slug.strip()
            if sku and node_slug:
                by_node.setdefault(node_slug, []).append(sku)
    results = []
    with database.cursor(write=True, actor=actor) as cursor:
        cursor.execute("SELECT node_id, slug FROM catmgr.node")
        node_by_slug = {r["slug"]: r["node_id"] for r in cursor.fetchall()}
        for node_slug, skus in sorted(by_node.items()):
            if node_slug not in node_by_slug:
                results.append({"ok": False, "node_slug": node_slug,
                                "error": "unknown node slug"})
                continue
            outcome = categories_mapping.set_assignments(
                cursor, node_id=node_by_slug[node_slug], skus=skus,
                mode="add", source="csv", actor=actor,
            )
            results.append({"ok": True, "node_slug": node_slug,
                            "count": outcome["count"]})
    return {"results": results, "bad_rows": bad_rows[:50]}


# ---------------------------------------------------------------- planner


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(min_length=3, max_length=8)
    blog_ids: List[int] | None = Field(default=None, max_length=200)


@router.post("/preview")
def run_preview(
    body: PreviewRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(body.env)
    try:
        with database.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '120s'")
            return categories_planner.preview(cursor, body.env, body.blog_ids)
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc


@router.get("/preview/blog")
def preview_blog(
    env: str = Query(..., min_length=3, max_length=8),
    blog_id: int = Query(..., ge=1),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    _configured_env(env)
    try:
        with database.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '120s'")
            return categories_planner.build_blog_plan(cursor, env, blog_id)
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc


@router.get("/uncategorized-ack")
def list_uncategorized_acks(
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        return {"acks": categories_planner.list_acks(cursor)}


class AckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skus: List[str] = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=1000)


@router.put("/uncategorized-ack")
def save_uncategorized_acks(
    body: AckRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            count = categories_planner.set_acks(
                cursor, skus=body.skus, note=body.note,
                actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"count": count}


@router.delete("/uncategorized-ack/{sku}")
def delete_uncategorized_ack(
    sku: str,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            categories_planner.delete_ack(cursor, sku, actor=user["user_login"])
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"ok": True}


# ---------------------------------------------------------------- runs


def require_apply_user(
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
) -> Dict[str, str]:
    """Apply-tier gate: everyone edits, only allowlisted operators apply."""

    if not categories_runs.apply_allowed(user["user_login"]):
        raise HTTPException(
            status_code=403,
            detail="Applying category changes requires the CATMGR_APPLY_USERS"
                   " allowlist",
        )
    return user


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(min_length=3, max_length=8)
    blog_ids: List[int] | None = Field(default=None, max_length=200)
    stop_on_failure: bool = True
    start: bool = True


@router.post("/runs")
def create_run(
    body: RunCreateRequest,
    user: Dict[str, str] = Depends(require_apply_user),
):
    _configured_env(body.env)
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            cursor.execute("SET LOCAL statement_timeout = '120s'")
            run = categories_runs.create_run(
                cursor, env=body.env, blog_ids=body.blog_ids,
                stop_on_failure=body.stop_on_failure,
                actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    if body.start:
        categories_runs.start_run(run["run_id"], actor=user["user_login"])
    return {"run": run}


@router.get("/runs")
def list_runs(
    env: str | None = Query(default=None, min_length=3, max_length=8),
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    with database.cursor() as cursor:
        return {"runs": categories_runs.list_runs(cursor, env)}


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    user: Dict[str, str] = Depends(require_user),
    _: None = Depends(require_catmgr),
):
    del user
    try:
        with database.cursor() as cursor:
            return {"run": categories_runs.get_run(cursor, run_id)}
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc


def _run_control(run_id: int, user: Dict[str, str], fn) -> Dict[str, Any]:
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            run = fn(cursor, run_id, actor=user["user_login"])
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"run": run}


@router.post("/runs/{run_id}/start")
def start_run(
    run_id: int,
    user: Dict[str, str] = Depends(require_apply_user),
):
    try:
        with database.cursor() as cursor:
            categories_runs.get_run(cursor, run_id)
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    categories_runs.start_run(run_id, actor=user["user_login"])
    with database.cursor() as cursor:
        return {"run": categories_runs.get_run(cursor, run_id)}


@router.post("/runs/{run_id}/pause")
def pause_run(
    run_id: int,
    user: Dict[str, str] = Depends(require_apply_user),
):
    return _run_control(run_id, user, categories_runs.request_pause)


@router.post("/runs/{run_id}/resume")
def resume_run(
    run_id: int,
    user: Dict[str, str] = Depends(require_apply_user),
):
    result = _run_control(run_id, user, categories_runs.resume)
    categories_runs.start_run(run_id, actor=user["user_login"])
    return result


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: int,
    user: Dict[str, str] = Depends(require_apply_user),
):
    return _run_control(run_id, user, categories_runs.cancel)


@router.post("/runs/{run_id}/jobs/{job_id}/retry")
def retry_job(
    run_id: int,
    job_id: int,
    user: Dict[str, str] = Depends(require_apply_user),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            run = categories_runs.retry_job(
                cursor, run_id, job_id, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    categories_runs.start_run(run_id, actor=user["user_login"])
    return {"run": run}


@router.post("/runs/{run_id}/jobs/{job_id}/skip")
def skip_job(
    run_id: int,
    job_id: int,
    user: Dict[str, str] = Depends(require_apply_user),
):
    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            run = categories_runs.skip_job(
                cursor, run_id, job_id, actor=user["user_login"],
            )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    return {"run": run}


@router.post("/runs/{run_id}/jobs/{job_id}/restore")
def restore_job_blog(
    run_id: int,
    job_id: int,
    user: Dict[str, str] = Depends(require_apply_user),
):
    try:
        result = categories_runs.restore_blog(
            run_id, job_id, actor=user["user_login"],
        )
    except (DraftError, DraftConflict) as exc:
        raise _draft_errors(exc) from exc
    except categories_service.BrokerError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"result": result}


class FreezeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(min_length=3, max_length=8)
    on: bool


@router.post("/freeze")
def set_freeze(
    body: FreezeRequest,
    user: Dict[str, str] = Depends(require_apply_user),
):
    _configured_env(body.env)
    try:
        result = categories_service._broker(
            body.env, "/freeze", method="POST", payload={"on": body.on},
        )
    except categories_service.BrokerError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        categories_service.record_audit(
            cursor, actor=user["user_login"], action="freeze_set",
            entity="freeze", entity_key=body.env,
            detail={"on": body.on, "result": result},
        )
    return {"env": body.env, **result}


class DriftAuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env: str = Field(min_length=3, max_length=8)


@router.post("/drift-audit")
def run_drift_audit(
    body: DriftAuditRequest,
    user: Dict[str, str] = Depends(require_csrf),
    _: None = Depends(require_catmgr),
):
    _configured_env(body.env)
    try:
        return categories_runs.drift_audit(body.env, actor=user["user_login"])
    except categories_service.BrokerError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
