"""Authenticated JSON and file endpoints for the Logo Admin application."""

import csv
import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import io
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import struct
import tempfile
from typing import Any, Dict, Iterator, List, Literal, Optional, Set, Tuple
from urllib.parse import urlencode, urlsplit

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from auth import (
    WordPressRequestError,
    require_csrf,
    require_user,
    wordpress_json_request,
)
from authorization import AccessContext
from commands import (
    ApplyToColorsCommand,
    CopyStyleCommand,
    DeactivateAssignmentCommand,
    DeactivateColorCommand,
    DeleteStorePricingTierCommand,
    HardDeleteAssignmentCommand,
    HardDeleteColorCommand,
    SaveAssignmentCommand,
    SetStorePricingTierCommand,
    SetStyleActiveCommand,
    UpdateStoreSettingsCommand,
)
from config import get_settings
from db import database
from design_resolver import design_available_to_store, validate_design_asset
from domain import Conflict, InvalidCommand, NotFound
import legacy_import as legacy
import mutations
import queries as read_queries
import mix_service
import wp_bridge
from commands import (
    DeletePriceRuleCommand,
    RemoveBrandStockRuleCommand,
    RemoveStockOverrideCommand,
    RemoveSyncBlockCommand,
    SetBrandStockRuleCommand,
    SetLogoCostCommand,
    SetLogoDefaultCostCommand,
    SetLogoNameCommand,
    SetPriceRuleActiveCommand,
    SetStockOverrideCommand,
    SetStoreExtraCustomersCommand,
    SetSyncBlockCommand,
)
from snapshots import lock_scopes


router = APIRouter(prefix="/api", tags=["logo-admin"])
logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "fdm4_store",
    "product_style",
    "garment_color_code",
    "option_row",
    "position",
    "design_id",
    "logo_code",
    "color_scheme_id",
    "location",
    "optional",
    "background",
    "cost_override",
    "sort_order",
    "image_url",
    "active",
]
MAX_CSV_ROWS = 20000
MAX_IMAGE_PIXELS = 40_000_000
CSV_TEXT_COLUMNS = {
    "fdm4_store",
    "product_style",
    "garment_color_code",
    "design_id",
    "logo_code",
    "color_scheme_id",
    "location",
    "background",
    "image_url",
}


class StoreSettingsBody(BaseModel):
    enabled: bool
    allows_none: bool


class AssignmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fdm4_store: str = Field(min_length=1, max_length=100)
    product_style: str = Field(min_length=1, max_length=100)
    garment_color_code: str = Field(min_length=1, max_length=100)
    position: int = Field(ge=1, le=3)
    option_row: int = Field(default=1, ge=1, le=999)
    design_id: str = Field(min_length=1, max_length=100)
    logo_code: str = Field(min_length=1, max_length=100)
    color_scheme_id: str = Field(min_length=1, max_length=100)
    location: str = Field(default="", max_length=200)
    optional: bool = False
    background: str = Field(default="", max_length=200)
    cost_override: Optional[Decimal] = None
    sort_order: int = Field(default=0, ge=-2147483648, le=2147483647)
    image_url: str = Field(default="", max_length=2048)
    # Three-state compatibility contract: absent/NULL preserves, "" clears,
    # and non-empty text sets the override.
    name_override: Optional[str] = Field(default=None, max_length=200)
    expected_updated_at: Optional[datetime.datetime] = None
    active: bool = True


class StyleActiveBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    style: str = Field(min_length=1, max_length=100)
    active: bool


class ApplyAllColorsBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    style: str = Field(min_length=1, max_length=100)
    garment_color_code: str = Field(min_length=1, max_length=100)
    position: int = Field(ge=1, le=3)
    option_row: int = Field(default=1, ge=1, le=999)
    overwrite: bool = False


class CopyStyleBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    source_style: str = Field(min_length=1, max_length=100)
    target_style: str = Field(min_length=1, max_length=100)
    overwrite: bool = False


class SyncBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    styles: List[str] = Field(default_factory=list, max_length=100)


class StoreTierBody(BaseModel):
    fdm4_store: str = Field(min_length=1, max_length=100)
    tier_name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class LogoNameBody(BaseModel):
    design_id: str = Field(min_length=1, max_length=64)
    color_scheme_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    fdm4_store: str = Field(default="", max_length=100)


class RepullBody(BaseModel):
    design_id: str = Field(min_length=1, max_length=64)
    force: bool = False


class ValidationMiss(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _read_service(service, /, **arguments):
    """Run a shared read service and preserve the existing HTTP contract."""

    try:
        with database.cursor() as cursor:
            return service(cursor, **arguments)
    except read_queries.QueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except read_queries.QueryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _execute_mutation(command, user: Dict[str, str]) -> dict:
    """Run the shared mutation kernel while preserving HTTP error contracts."""

    context = AccessContext.from_session(user)
    try:
        with database.cursor(
            write=True,
            actor=context.user_login,
        ) as cursor:
            lock_scopes(cursor, mutations.affected_scopes(command))
            result = mutations.dispatch_mutation(
                cursor,
                context.user_login,
                command,
            )
        return result.value
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except InvalidCommand as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _clean(value: str, field: str, maximum: int = 100) -> str:
    cleaned = str(value).strip()
    if not cleaned or len(cleaned) > maximum or "\x00" in cleaned:
        raise ValidationMiss("invalid_value", f"{field} is invalid")
    return cleaned


def _optional_text(value: Any, field: str, maximum: int) -> str:
    if value is None:
        return ""
    cleaned = str(value).strip()
    if len(cleaned) > maximum or "\x00" in cleaned:
        raise ValidationMiss("invalid_value", f"{field} is invalid")
    return cleaned


def _parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValidationMiss("invalid_boolean", f"{field} must be true or false")


def _parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValidationMiss("invalid_cost", "cost_override must be numeric") from None
    if not parsed.is_finite() or parsed < Decimal("-9999999999.99") or parsed > Decimal("9999999999.99"):
        raise ValidationMiss("invalid_cost", "cost_override is outside the supported range")
    if parsed.as_tuple().exponent < -2:
        raise ValidationMiss("invalid_cost", "cost_override may have at most two decimal places")
    return parsed


def _parse_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationMiss("invalid_integer", f"{field} must be an integer") from None
    if str(value).strip() != str(parsed) or parsed < minimum or parsed > maximum:
        raise ValidationMiss("invalid_integer", f"{field} is outside the supported range")
    return parsed


def _validate_image_url(value: Any) -> str:
    url = _optional_text(value, "image_url", 2048)
    if not url:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise ValidationMiss("invalid_image_url", "image_url must be an absolute HTTP(S) URL")
    try:
        parsed.port
    except ValueError:
        raise ValidationMiss("invalid_image_url", "image_url has an invalid port") from None
    return url


def _csv_safe_text(value: Any) -> str:
    """Escape formula-leading text while preserving CSV round-trip fidelity."""

    rendered = "" if value is None else str(value)
    if rendered.startswith("'"):
        return "'" + rendered
    probe = rendered.lstrip(" \t\r\n")
    if rendered.startswith(("\t", "\r", "\n")) or probe.startswith(("=", "+", "-", "@")):
        return "'" + rendered
    return rendered


def _csv_restore_text(value: Any) -> Any:
    rendered = "" if value is None else str(value)
    if rendered.startswith("''"):
        return rendered[1:]
    if len(rendered) > 1 and rendered[0] == "'":
        candidate = rendered[1:]
        probe = candidate.lstrip(" \t\r\n")
        if candidate.startswith(("\t", "\r", "\n")) or probe.startswith(("=", "+", "-", "@")):
            return candidate
    return rendered


def _catalog_for_store(cursor, store: str) -> Optional[str]:
    cursor.execute(
        """
        SELECT catalog_id
          FROM woo.store_catalog
         WHERE fdm4_store = %s
           AND suggested = true
         ORDER BY suggested DESC, products DESC, catalog_id
         LIMIT 1
        """,
        (store,),
    )
    row = cursor.fetchone()
    return str(row["catalog_id"]) if row else None


def _style_exists(cursor, store: str, catalog: str, style: str) -> bool:
    cursor.execute(
        """
        SELECT 1
          FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s
           AND style_code = %s AND is_active = true
         LIMIT 1
        """,
        (store, catalog, style),
    )
    return cursor.fetchone() is not None


def _color_exists(cursor, store: str, catalog: str, style: str, color: str) -> bool:
    cursor.execute(
        """
        SELECT 1
          FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s
           AND style_code = %s AND kind = 'variation' AND is_active = true
           AND color_code = %s
         LIMIT 1
        """,
        (store, catalog, style, color),
    )
    return cursor.fetchone() is not None


def _design_exists(cursor, design_id: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM fdm4.dec_design WHERE btrim(design_id) = %s LIMIT 1",
        (design_id,),
    )
    return cursor.fetchone() is not None


def _active_primary_exists(cursor, values: Dict[str, Any]) -> bool:
    cursor.execute(
        """
        SELECT 1
          FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
           AND garment_color_code = %s AND option_row = %s
           AND position = 1 AND active = true
         LIMIT 1
        """,
        (
            values["fdm4_store"],
            values["product_style"],
            values["garment_color_code"],
            values["option_row"],
        ),
    )
    return cursor.fetchone() is not None


def _validate_primary_anchor(cursor, values: Dict[str, Any]) -> None:
    if values["position"] > 1 and values.get("active", True):
        if not _active_primary_exists(cursor, values):
            raise ValidationMiss(
                "orphaned_companion",
                "position 2/3 requires an active position-1 assignment in the same option row",
            )


# NOTE: per-row placement differences within a style/position are VALID data
# (e.g. left-pocket vs right-pocket logo rows on one style - live on
# daveyrcsafety 205542). The product_logos placements[] label collapses to the
# first non-empty location (legacy AVNL parity); per-row truth rides on the
# logo posts and the location-keyed design map, so no conflict gate here.


def _validate_warehouse_keys(cursor, values: Dict[str, Any]) -> None:
    store = values["fdm4_store"]
    style = values["product_style"]
    color = values["garment_color_code"]
    design_id = values["design_id"]
    scheme = values["color_scheme_id"]

    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise ValidationMiss("no_store", f"unknown FDM4 store {store}")
    cursor.execute(
        """
        SELECT 1
          FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
           AND garment_color_code = %s AND option_row = %s AND position = %s
         LIMIT 1
        """,
        (store, style, color, values["option_row"], values["position"]),
    )
    existing_assignment = cursor.fetchone() is not None
    if not _style_exists(cursor, store, catalog, style) and not existing_assignment:
        raise ValidationMiss("no_style", f"style {style} is not active in store {store}")
    if not _color_exists(cursor, store, catalog, style, color) and not existing_assignment:
        raise ValidationMiss(
            "no_color_code", f"color {color} is not active for store/style"
        )
    if not _design_exists(cursor, design_id):
        raise ValidationMiss("no_design", f"unknown design_id {design_id}")
    _validate_primary_anchor(cursor, values)
    # Image-only legacy assignments: the Phase A seeder deliberately accepts
    # rows that carry a working sheet/media image but no matching FDM4 art for
    # this (design, scheme). With an explicit image_url the FDM4 scheme and
    # filename checks are advisory, not blocking, so those rows stay editable.
    if values.get("image_url"):
        return
    if not validate_design_asset(
        cursor, store=store, design_id=design_id, scheme=scheme
    ):
        if not design_available_to_store(cursor, store, design_id):
            raise ValidationMiss(
                "foreign_design",
                f"design {design_id} belongs to a different FDM4 customer"
                " account and is not available to this store",
            )
        raise ValidationMiss(
            "no_art", f"design {design_id} has no color scheme {scheme}"
        )
    if not validate_design_asset(
        cursor,
        store=store,
        design_id=design_id,
        scheme=scheme,
        logo_code=values["logo_code"],
    ):
        raise ValidationMiss(
            "no_design",
            f"logo_code {values['logo_code']} does not match design {design_id} / scheme {scheme}",
        )


def _assignment_values(body: AssignmentBody) -> Dict[str, Any]:
    values = {
        "fdm4_store": _clean(body.fdm4_store, "fdm4_store"),
        "product_style": _clean(body.product_style, "product_style"),
        "garment_color_code": _clean(
            body.garment_color_code, "garment_color_code"
        ),
        "position": body.position,
        "option_row": body.option_row,
        "design_id": _clean(body.design_id, "design_id"),
        "logo_code": _clean(body.logo_code, "logo_code").upper(),
        "color_scheme_id": _clean(
            body.color_scheme_id, "color_scheme_id"
        ).upper(),
        "location": _optional_text(body.location, "location", 200),
        "optional": body.optional,
        "background": _optional_text(body.background, "background", 200),
        "cost_override": _parse_decimal(body.cost_override),
        "sort_order": body.sort_order,
        "image_url": _validate_image_url(body.image_url),
        "active": body.active,
    }
    return values


def _upsert_assignment(cursor, values: Dict[str, Any], user_login: str, overwrite: bool = True) -> bool:
    conflict = """
        DO UPDATE SET
            design_id = EXCLUDED.design_id,
            logo_code = EXCLUDED.logo_code,
            color_scheme_id = EXCLUDED.color_scheme_id,
            location = EXCLUDED.location,
            optional = EXCLUDED.optional,
            background = EXCLUDED.background,
            cost_override = EXCLUDED.cost_override,
            sort_order = EXCLUDED.sort_order,
            image_url = EXCLUDED.image_url,
            active = EXCLUDED.active,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
    """ if overwrite else "DO NOTHING"
    cursor.execute(
        f"""
        INSERT INTO logo.assignment (
            fdm4_store, product_style, garment_color_code, option_row, position,
            design_id, logo_code, color_scheme_id, location, optional,
            background, cost_override, sort_order, image_url, active, updated_by
        ) VALUES (
            %(fdm4_store)s, %(product_style)s, %(garment_color_code)s, %(option_row)s,
            %(position)s, %(design_id)s, %(logo_code)s, %(color_scheme_id)s,
            %(location)s, %(optional)s, %(background)s, %(cost_override)s,
            %(sort_order)s, %(image_url)s, %(active)s, %(updated_by)s
        )
        ON CONFLICT (fdm4_store, product_style, garment_color_code, option_row, position)
        {conflict}
        """,
        {**values, "updated_by": user_login},
    )
    changed = cursor.rowcount > 0
    # A selectable row cannot survive without its primary logo. Deactivating
    # position 1 therefore deactivates every companion in the option atomically.
    if changed and values["position"] == 1 and not values.get("active", True):
        cursor.execute(
            """
            UPDATE logo.assignment
               SET active = false, updated_by = %s, updated_at = now()
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s
               AND position > 1 AND active = true
            """,
            (
                user_login,
                values["fdm4_store"],
                values["product_style"],
                values["garment_color_code"],
                values["option_row"],
            ),
        )
    return changed


def _store_display_name(code: Any, catalog: Any) -> str:
    """Human-readable store name derived from the catalog slug.

    Catalogs are named like ``S_032813_daveyrcsafety`` - the suffix after the
    store code is the storefront slug. Falls back to the raw code.
    """
    code_s = str(code or "")
    catalog_s = str(catalog or "")
    slug = ""
    if catalog_s and code_s and catalog_s.startswith(code_s + "_"):
        slug = catalog_s[len(code_s) + 1 :]
    elif catalog_s.count("_") >= 2:
        slug = catalog_s.split("_", 2)[2]
    if not slug:
        return code_s
    pretty = re.sub(r"[-_]+", " ", slug).strip()
    # Junk slugs (mostly digits, e.g. "01 1") read worse than the code itself.
    if sum(ch.isalpha() for ch in pretty) < 3:
        return code_s
    return pretty[:1].upper() + pretty[1:]


@router.get("/stores")
def stores(user: Dict[str, str] = Depends(require_user)):
    del user
    return _read_service(read_queries.list_stores)


@router.get("/styles")
def styles(
    store: str = Query(..., min_length=1, max_length=100),
    q: str = Query("", max_length=100),
    active_only: bool = Query(True),
    assigned_only: bool = Query(True),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    return _read_service(
        read_queries.list_styles,
        store=store,
        q=q,
        active_only=active_only,
        assigned_only=assigned_only,
    )


@router.get("/style")
def style_detail(
    store: str = Query(..., min_length=1, max_length=100),
    style: str = Query(..., min_length=1, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    return _read_service(read_queries.get_style, store=store, style=style)


@router.get("/styles/similar")
def similar_styles(
    store: str = Query(..., min_length=1, max_length=100),
    style: str = Query(..., min_length=1, max_length=100),
    mode: str = Query("exact", pattern="^(exact|overlap)$"),
    user: Dict[str, str] = Depends(require_user),
):
    """Styles of the store carrying the same logo set as `style` (exact) or
    sharing at least one logo tuple (overlap). Read-only."""
    del user
    return _read_service(
        read_queries.find_similar_styles,
        fdm4_store=store,
        product_style=style,
        mode=mode,
    )


@router.get("/styles/coverage")
def styles_coverage(
    store: str = Query(..., min_length=1, max_length=100),
    unconfigured_only: bool = Query(True),
    user: Dict[str, str] = Depends(require_user),
):
    """Per live style: garment colors with / without active logo assignments."""
    del user
    return _read_service(
        read_queries.store_logo_coverage,
        fdm4_store=store,
        unconfigured_only=unconfigured_only,
    )


class FillGapsPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: str = Field(min_length=1, max_length=100)
    styles: Optional[List[str]] = Field(default=None, max_length=500)


@router.post("/styles/fill-gaps/preview")
def fill_gaps_preview(body: FillGapsPreviewBody,
                      user: Dict[str, str] = Depends(require_csrf)):
    """Read-only plan: which gap styles can be filled from their own
    configured colors (auto source only when every configured color carries
    an identical logo set), and which have no source at all."""
    del user
    styles = None
    if body.styles:
        styles = _clean_list(body.styles, upper=True, maxitems=500, field="styles")
    return _read_service(read_queries.fill_gaps_plan, fdm4_store=body.store, styles=styles)


class FillGapsEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str = Field(min_length=1, max_length=100)
    source_color: str = Field(min_length=1, max_length=100)
    colors: Optional[List[str]] = Field(default=None, max_length=500)


class FillGapsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: str = Field(min_length=1, max_length=100)
    entries: List[FillGapsEntry] = Field(min_length=1, max_length=200)
    overwrite: bool = False


@router.post("/styles/fill-gaps")
def fill_gaps(body: FillGapsBody, user: Dict[str, str] = Depends(require_csrf)):
    """Copy each entry style's own source-color logos onto its logo-less
    colors. Journaled as ONE batch; undo via /api/bulk-apply/undo."""
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        lock_scopes(cursor, [mutations.assignment_style_scope(body.store, entry.style)
                             for entry in body.entries])
        try:
            return mutations.fill_gaps(
                cursor,
                fdm4_store=body.store,
                entries=[entry.model_dump() for entry in body.entries],
                overwrite=body.overwrite,
                actor=user["user_login"],
            )
        except (NotFound, Conflict, InvalidCommand) as exc:
            raise _editor_errors(exc) from None


@router.get("/designs")
def designs(
    q: str = Query("", max_length=100),
    store: Optional[str] = Query(None, max_length=100),
    used_only: bool = Query(False),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    return _read_service(read_queries.search_designs, q=q, store=store, used_only=used_only)


@router.get("/vocab")
def assignment_vocab(user: Dict[str, str] = Depends(require_user)):
    """Real-world field vocabularies drawn from existing assignments."""
    del user
    return _read_service(read_queries.get_assignment_vocab)


@router.get("/designs/{design_id}")
def design_detail(
    design_id: str,
    user: Dict[str, str] = Depends(require_user),
):
    del user
    return _read_service(
        read_queries.get_design,
        design_id=design_id,
        fdm4_art_base=get_settings().fdm4_art_base,
    )


@router.get("/settings/{store}")
def read_settings(
    store: str,
    user: Dict[str, str] = Depends(require_user),
):
    """Store-level logo settings, readable without loading any style."""
    del user
    return _read_service(read_queries.get_store_settings, store=store)


@router.put("/settings/{store}")
def update_settings(
    store: str,
    body: StoreSettingsBody,
    user: Dict[str, str] = Depends(require_csrf),
):
    return _execute_mutation(
        UpdateStoreSettingsCommand(
            store=store,
            enabled=body.enabled,
            allows_none=body.allows_none,
        ),
        user,
    )


@router.put("/assignments")
def put_assignment(
    body: AssignmentBody,
    user: Dict[str, str] = Depends(require_csrf),
):
    return _execute_mutation(
        SaveAssignmentCommand.model_validate(body.model_dump()),
        user,
    )


@router.delete("/assignments")
def delete_assignment(
    fdm4_store: str = Query(..., min_length=1, max_length=100),
    product_style: str = Query(..., min_length=1, max_length=100),
    garment_color_code: str = Query(..., min_length=1, max_length=100),
    position: int = Query(..., ge=1, le=3),
    option_row: int = Query(1, ge=1, le=999),
    hard: bool = Query(False),
    user: Dict[str, str] = Depends(require_csrf),
):
    values = {
        "fdm4_store": fdm4_store,
        "product_style": product_style,
        "garment_color_code": garment_color_code,
        "position": position,
        "option_row": option_row,
    }
    command = (
        HardDeleteAssignmentCommand(**values)
        if hard
        else DeactivateAssignmentCommand(**values)
    )
    return _execute_mutation(command, user)


@router.delete("/assignments-by-color")
def delete_assignments_by_color(
    fdm4_store: str = Query(..., min_length=1, max_length=100),
    product_style: str = Query(..., min_length=1, max_length=100),
    garment_color_code: str = Query(..., min_length=1, max_length=100),
    hard: bool = Query(False),
    user: Dict[str, str] = Depends(require_csrf),
):
    """Clear a whole color channel - every option row on (store, style, color).

    Built for retiring old data (including colors no longer in the FDM4
    catalog). Row-level audit triggers record each removed assignment.
    """
    values = {
        "fdm4_store": fdm4_store,
        "product_style": product_style,
        "garment_color_code": garment_color_code,
    }
    command = (
        HardDeleteColorCommand(**values)
        if hard
        else DeactivateColorCommand(**values)
    )
    return _execute_mutation(command, user)


@router.post("/style-active")
def set_style_active(
    body: StyleActiveBody,
    user: Dict[str, str] = Depends(require_csrf),
):
    return _execute_mutation(
        SetStyleActiveCommand.model_validate(body.model_dump()),
        user,
    )


@router.post("/apply-all-colors")
def apply_all_colors(
    body: ApplyAllColorsBody,
    user: Dict[str, str] = Depends(require_csrf),
):
    return _execute_mutation(
        ApplyToColorsCommand.model_validate(body.model_dump()),
        user,
    )


@router.post("/copy-style")
def copy_style(
    body: CopyStyleBody,
    user: Dict[str, str] = Depends(require_csrf),
):
    return _execute_mutation(
        CopyStyleCommand.model_validate(body.model_dump()),
        user,
    )


# ---------------------------------------------------------------------------
# Editor batch operations (reorder / paste / copy-to-many / design swap).
# Standalone mutations following the bulk-apply precedent: explicit
# lock_scopes here, journaled into logo.bulk_batch, undo via
# POST /api/bulk-apply/undo. Not commands (the agent write surface is closed).
# ---------------------------------------------------------------------------

class ReorderBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    style: str = Field(min_length=1, max_length=100)
    garment_color_code: str = Field(min_length=1, max_length=100)
    option_rows: List[int] = Field(min_length=1, max_length=999)
    apply_to: Literal["color", "style"] = "color"


def _editor_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, Conflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/assignments/reorder")
def reorder_assignments(body: ReorderBody, user: Dict[str, str] = Depends(require_csrf)):
    """Drag-and-drop persistence: renumber sort_order for one color's option
    rows (or every color with the same rows). Journaled as a bulk batch."""
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        lock_scopes(cursor, (mutations.assignment_style_scope(body.store, body.style),))
        try:
            return mutations.reorder_option_rows(
                cursor,
                fdm4_store=body.store,
                product_style=body.style,
                garment_color_code=body.garment_color_code,
                option_rows=body.option_rows,
                apply_to=body.apply_to,
                actor=user["user_login"],
            )
        except (NotFound, Conflict, InvalidCommand) as exc:
            raise _editor_errors(exc) from None


class StyleColorOrderBody(BaseModel):
    style: str = Field(min_length=1, max_length=100)
    colors: List[str] = Field(default_factory=list, max_length=500)


@router.put("/style-color-order")
def set_style_color_order(body: StyleColorOrderBody, user: Dict[str, str] = Depends(require_csrf)):
    """Editor-only garment color order for one style (all stores). Full
    replace: colors omitted here fall back to alphabetical after the ordered
    ones. Never synced anywhere."""
    style = _clean(body.style, "style")
    colors = _clean_list(body.colors, maxitems=500, field="colors")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute("DELETE FROM logo.style_color_order WHERE product_style = %s", (style,))
        for index, code in enumerate(colors):
            cursor.execute(
                """
                INSERT INTO logo.style_color_order
                    (product_style, garment_color_code, sort_order, updated_by, updated_at)
                VALUES (%s, %s, %s, %s, now())
                """,
                (style, code, (index + 1) * 10, user["user_login"]),
            )
    return {"ok": True, "style": style, "colors": colors}


class PasteRow(BaseModel):
    option_row: int = Field(ge=1, le=999)
    position: int = Field(ge=1, le=3)
    design_id: str = Field(min_length=1, max_length=100)
    logo_code: str = Field(default="", max_length=100)
    color_scheme_id: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=200)
    optional: bool = False
    background: str = Field(default="", max_length=200)
    cost_override: Optional[float] = None
    sort_order: int = Field(default=0, ge=-2147483648, le=2147483647)
    image_url: str = Field(default="", max_length=2000)
    name_override: Optional[str] = Field(default=None, max_length=200)
    active: bool = True


class PasteBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    style: str = Field(min_length=1, max_length=100)
    colors: List[str] = Field(min_length=1, max_length=500)
    rows: List[PasteRow] = Field(min_length=1, max_length=2000)
    overwrite: bool = False
    as_new_rows: bool = False


@router.post("/assignments/paste")
def paste_assignments(body: PasteBody, user: Dict[str, str] = Depends(require_csrf)):
    """Clipboard paste onto colors of one style. Journaled; undo via
    /api/bulk-apply/undo."""
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        lock_scopes(cursor, (mutations.assignment_style_scope(body.store, body.style),))
        try:
            return mutations.paste_assignments(
                cursor,
                fdm4_store=body.store,
                product_style=body.style,
                colors=_clean_list(body.colors, maxitems=500, field="colors"),
                rows=[row.model_dump() for row in body.rows],
                overwrite=body.overwrite,
                as_new_rows=body.as_new_rows,
                actor=user["user_login"],
            )
        except (NotFound, Conflict, InvalidCommand) as exc:
            raise _editor_errors(exc) from None


class PasteBatchBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    styles: List[str] = Field(min_length=1, max_length=200)
    color_scope: Literal["match", "all", "light", "dark"] = "match"
    match_color: Optional[str] = Field(default=None, max_length=100)
    rows: List[PasteRow] = Field(min_length=1, max_length=2000)
    overwrite: bool = False
    as_new_rows: bool = False


@router.post("/assignments/paste-batch")
def paste_assignments_batch(body: PasteBatchBody, user: Dict[str, str] = Depends(require_csrf)):
    styles = _clean_list(body.styles, upper=True, maxitems=200, field="styles")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        lock_scopes(cursor, [mutations.assignment_style_scope(body.store, s) for s in styles])
        try:
            return mutations.paste_batch(
                cursor, fdm4_store=body.store, styles=styles,
                color_scope=body.color_scope, match_color=body.match_color,
                rows=[row.model_dump() for row in body.rows],
                overwrite=body.overwrite, as_new_rows=body.as_new_rows,
                actor=user["user_login"],
            )
        except (NotFound, Conflict, InvalidCommand) as exc:
            raise _editor_errors(exc) from None


class StyleActiveBatchBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    styles: List[str] = Field(min_length=1, max_length=200)
    active: bool


@router.post("/style-active-batch")
def style_active_batch(body: StyleActiveBatchBody, user: Dict[str, str] = Depends(require_csrf)):
    """Activate/deactivate many styles in one transaction (same handler as
    /api/style-active, per style)."""
    styles = _clean_list(body.styles, upper=True, maxitems=200, field="styles")
    results = []
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        lock_scopes(cursor, [mutations.assignment_style_scope(body.store, s) for s in styles])
        for style in styles:
            command = SetStyleActiveCommand(store=body.store, style=style, active=body.active)
            try:
                outcome = mutations.set_style_active(cursor, user["user_login"], command).value
                results.append({"style": style, "updated": outcome["updated"]})
            except NotFound as exc:
                results.append({"style": style, "updated": 0, "error": str(exc)})
    return {"ok": True, "results": results}


class CopyStyleBatchBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    source_style: str = Field(min_length=1, max_length=100)
    target_styles: List[str] = Field(min_length=1, max_length=200)
    color_match: Literal["exact", "like"] = "exact"
    mode: Literal["merge", "overwrite", "replace"] = "merge"


@router.post("/copy-style-batch/preview")
def copy_style_batch_preview(body: CopyStyleBatchBody, user: Dict[str, str] = Depends(require_csrf)):
    """Read-only plan: per target style, which source color feeds each
    target color (exact code, or like light/dark class) and what it costs."""
    del user
    targets = _clean_list(body.target_styles, upper=True, maxitems=200, field="target_styles")
    with database.cursor() as cursor:
        try:
            plan, _channels = mutations.plan_copy_style_batch(
                cursor, fdm4_store=body.store, source_style=body.source_style,
                target_styles=targets, color_match=body.color_match,
            )
        except (NotFound, InvalidCommand) as exc:
            raise _editor_errors(exc) from None
    plan["mode"] = body.mode
    return plan


@router.post("/copy-style-batch")
def copy_style_batch(body: CopyStyleBatchBody, user: Dict[str, str] = Depends(require_csrf)):
    targets = _clean_list(body.target_styles, upper=True, maxitems=200, field="target_styles")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        lock_scopes(cursor, [mutations.assignment_style_scope(body.store, s) for s in targets])
        try:
            return mutations.copy_style_batch(
                cursor, fdm4_store=body.store, source_style=body.source_style,
                target_styles=targets, color_match=body.color_match, mode=body.mode,
                actor=user["user_login"],
            )
        except (NotFound, Conflict, InvalidCommand) as exc:
            raise _editor_errors(exc) from None


class DesignSwapBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    from_design_id: str = Field(min_length=1, max_length=100)
    from_color_scheme_id: Optional[str] = Field(default=None, max_length=100)
    to_design_id: str = Field(min_length=1, max_length=100)
    to_color_scheme_id: str = Field(min_length=1, max_length=100)
    to_logo_code: Optional[str] = Field(default=None, max_length=100)
    styles: Optional[List[str]] = None


def _design_swap_kwargs(body: DesignSwapBody) -> Dict[str, Any]:
    # Style codes are matched exactly (no upper-casing): they only narrow.
    return {
        "fdm4_store": body.store,
        "from_design_id": body.from_design_id,
        "from_color_scheme_id": body.from_color_scheme_id,
        "to_design_id": body.to_design_id,
        "to_color_scheme_id": body.to_color_scheme_id,
        "to_logo_code": body.to_logo_code,
        "styles": _clean_list(body.styles, maxitems=200, field="styles") or None,
    }


@router.post("/design-swap/preview")
def design_swap_preview(body: DesignSwapBody, user: Dict[str, str] = Depends(require_csrf)):
    """Dry run of a design swap: every assignment on the old design (and
    optionally one scheme) with the verdict a manual save would give the new
    values. Read-only; POSTed because the body is structured (same as
    /bulk-apply/preview)."""
    del user
    with database.cursor() as cursor:
        try:
            return mutations.plan_design_swap(cursor, **_design_swap_kwargs(body))
        except (NotFound, Conflict, InvalidCommand) as exc:
            raise _editor_errors(exc) from None


@router.post("/design-swap")
def design_swap(body: DesignSwapBody, user: Dict[str, str] = Depends(require_csrf)):
    """Move every assignment on one design (optionally one color scheme) to a
    re-issued design / scheme / logo code. One journaled bulk batch; undo via
    /api/bulk-apply/undo. Locks every affected style before planning."""
    kwargs = _design_swap_kwargs(body)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        try:
            styles = mutations.design_swap_styles(
                cursor, fdm4_store=kwargs["fdm4_store"],
                from_design_id=kwargs["from_design_id"],
                from_color_scheme_id=kwargs["from_color_scheme_id"],
                styles=kwargs["styles"],
            )
            lock_scopes(cursor, [mutations.assignment_style_scope(kwargs["fdm4_store"], s)
                                 for s in styles])
            return mutations.design_swap(
                cursor, **kwargs, actor=user["user_login"], locked_styles=styles,
            )
        except (NotFound, Conflict, InvalidCommand) as exc:
            raise _editor_errors(exc) from None


@router.get("/export")
def export_assignments(
    store: Optional[str] = Query(None, max_length=100),
    style: Optional[str] = Query(None, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    clauses = []
    params: List[Any] = []
    if store:
        store = _clean(store, "store")
        clauses.append("fdm4_store = %s")
        params.append(store)
    if style:
        style = _clean(style, "style")
        clauses.append("product_style = %s")
        params.append(style)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    select_sql = f"""
        SELECT {', '.join(CSV_COLUMNS)}
          FROM logo.assignment
          {where}
         ORDER BY fdm4_store, product_style, garment_color_code, option_row, position
    """

    def chunks() -> Iterator[str]:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output, fieldnames=CSV_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        with database.streaming_cursor(batch_size=500) as cursor:
            cursor.execute(select_sql, tuple(params))
            while rows := cursor.fetchmany(500):
                for db_row in rows:
                    row = dict(db_row)
                    row["optional"] = "true" if row["optional"] else "false"
                    row["active"] = "true" if row["active"] else "false"
                    row["cost_override"] = (
                        "" if row["cost_override"] is None
                        else str(row["cost_override"])
                    )
                    for column in CSV_TEXT_COLUMNS:
                        row[column] = _csv_safe_text(row.get(column))
                    writer.writerow(row)
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
    filename_parts = ["logo-assignments"]
    if store:
        filename_parts.append(re.sub(r"[^A-Za-z0-9._-]", "_", store))
    if style:
        filename_parts.append(re.sub(r"[^A-Za-z0-9._-]", "_", style))
    filename = "-".join(filename_parts) + ".csv"
    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_assignment(row: Dict[str, Any], forced_store: str) -> Dict[str, Any]:
    row = {
        key: _csv_restore_text(value) if key in CSV_TEXT_COLUMNS else value
        for key, value in row.items()
    }
    row_store = _optional_text(row.get("fdm4_store"), "fdm4_store", 100)
    if forced_store and row_store and row_store != forced_store:
        raise ValidationMiss("store_mismatch", "row store does not match selected store")
    store = forced_store or _clean(row_store, "fdm4_store")
    values = {
        "fdm4_store": store,
        "product_style": _clean(row.get("product_style", ""), "product_style"),
        "garment_color_code": _clean(
            row.get("garment_color_code", ""), "garment_color_code"
        ),
        "position": _parse_int(row.get("position"), "position", 1, 3),
        "option_row": _parse_int(row.get("option_row"), "option_row", 1, 999),
        "design_id": _clean(row.get("design_id", ""), "design_id"),
        "logo_code": _clean(row.get("logo_code", ""), "logo_code").upper(),
        "color_scheme_id": _clean(
            row.get("color_scheme_id", ""), "color_scheme_id"
        ).upper(),
        "location": _optional_text(row.get("location"), "location", 200),
        "optional": _parse_bool(row.get("optional"), "optional"),
        "background": _optional_text(row.get("background"), "background", 200),
        "cost_override": _parse_decimal(row.get("cost_override")),
        "sort_order": _parse_int(
            row.get("sort_order"), "sort_order", -2147483648, 2147483647
        ),
        "image_url": _validate_image_url(row.get("image_url")),
        "active": _parse_bool(row.get("active"), "active"),
    }
    return values


def _insert_import_report(
    cursor,
    row: Dict[str, Any],
    reason: str,
    detail: str,
    user_login: str,
    row_number: int,
) -> None:
    cursor.execute(
        """
        INSERT INTO logo.import_report (
            fdm4_store, product_style, product_color, logo_code, reason, detail
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            str(row.get("fdm4_store") or "")[:100] or None,
            str(row.get("product_style") or "")[:100] or None,
            str(row.get("garment_color_code") or "")[:100] or None,
            str(row.get("logo_code") or "")[:100] or None,
            reason[:100],
            f"row={row_number}; updated_by={user_login}; {detail}"[:4000],
        ),
    )


@router.post("/import")
def import_assignments(
    file: UploadFile = File(...),
    store: str = Form(""),
    user: Dict[str, str] = Depends(require_csrf),
):
    settings = get_settings()
    raw = file.file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"This file is too large (limit: {settings.max_upload_bytes // (1024 * 1024)} MB)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = ""
        decode_error = "CSV must be UTF-8 encoded"
    else:
        decode_error = ""

    forced_store = store.strip()
    if forced_store:
        forced_store = _clean(forced_store, "store")

    if decode_error:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            _insert_import_report(
                cursor,
                {"fdm4_store": forced_store},
                "invalid_csv",
                decode_error,
                user["user_login"],
                0,
            )
        return JSONResponse(status_code=400, content={"ok": False, "error": decode_error, "reported": 1})

    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = reader.fieldnames or []
        if headers != CSV_COLUMNS:
            header_error = "CSV headers must exactly match the export format"
            parsed_rows: List[Dict[str, Any]] = []
        else:
            header_error = ""
            parsed_rows = list(reader)
    except csv.Error:
        header_error = "CSV syntax is invalid"
        parsed_rows = []

    if header_error or len(parsed_rows) > MAX_CSV_ROWS:
        error = header_error or f"CSV may contain at most {MAX_CSV_ROWS} rows"
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            _insert_import_report(
                cursor,
                {"fdm4_store": forced_store},
                "invalid_csv",
                error,
                user["user_login"],
                0,
            )
        return JSONResponse(status_code=400, content={"ok": False, "error": error, "reported": 1})

    imported = 0
    misses = 0
    seen: Set[Tuple[str, str, str, int, int]] = set()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        if forced_store and _catalog_for_store(cursor, forced_store) is None:
            _insert_import_report(
                cursor,
                {"fdm4_store": forced_store},
                "no_store",
                "selected store does not exist",
                user["user_login"],
                0,
            )
            return JSONResponse(
                status_code=422,
                content={"ok": False, "error": "Selected store not found", "reported": 1},
            )

        for row_number, row in enumerate(parsed_rows, start=2):
            try:
                if None in row:
                    raise ValidationMiss(
                        "invalid_csv", "row contains more fields than the CSV header"
                    )
                values = _csv_assignment(row, forced_store)
                key = (
                    values["fdm4_store"],
                    values["product_style"],
                    values["garment_color_code"],
                    values["option_row"],
                    values["position"],
                )
                if key in seen:
                    raise ValidationMiss("duplicate_row", "duplicate assignment key in CSV")
                _validate_warehouse_keys(cursor, values)
                seen.add(key)
            except ValidationMiss as exc:
                report_row = dict(row)
                if forced_store:
                    report_row["fdm4_store"] = forced_store
                _insert_import_report(
                    cursor,
                    report_row,
                    exc.reason,
                    exc.detail,
                    user["user_login"],
                    row_number,
                )
                misses += 1
                continue

            cursor.execute("SAVEPOINT logo_import_row")
            try:
                _upsert_assignment(cursor, values, user["user_login"])
                cursor.execute("RELEASE SAVEPOINT logo_import_row")
                imported += 1
            except Exception as exc:
                cursor.execute("ROLLBACK TO SAVEPOINT logo_import_row")
                cursor.execute("RELEASE SAVEPOINT logo_import_row")
                report_row = dict(row)
                if forced_store:
                    report_row["fdm4_store"] = forced_store
                _insert_import_report(
                    cursor,
                    report_row,
                    "database_error",
                    f"database rejected row ({type(exc).__name__})",
                    user["user_login"],
                    row_number,
                )
                misses += 1

    return {"ok": True, "imported": imported, "misses": misses}


@router.get("/import-report")
def import_report(
    store: Optional[str] = Query(None, max_length=100),
    reason: Optional[str] = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    return _read_service(
        read_queries.get_import_report,
        store=store,
        reason=reason,
        limit=limit,
        offset=offset,
    )


@router.get("/import-report/export")
def export_import_report(
    store: Optional[str] = Query(None, max_length=100),
    reason: Optional[str] = Query(None, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    """CSV export of the import punch list, honoring the same filters. The
    dialog shows at most 500 rows; this is how the full backlog is reached."""
    del user
    clauses = []
    params: List[Any] = []
    if store:
        clauses.append("fdm4_store = %s")
        params.append(_clean(store, "store"))
    if reason:
        clauses.append("reason = %s")
        params.append(_clean(reason, "reason"))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    columns = [
        "id", "imported_at", "fdm4_store", "product_style", "product_color",
        "logo_code", "reason", "detail",
    ]
    select_sql = f"""
        SELECT id, imported_at, fdm4_store, product_style, product_color,
               logo_code, reason, detail
          FROM logo.import_report
          {where}
         ORDER BY imported_at DESC, id DESC
         LIMIT 100000
    """

    def chunks() -> Iterator[str]:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(columns)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        with database.streaming_cursor(batch_size=500) as cursor:
            cursor.execute(select_sql, tuple(params))
            while rows := cursor.fetchmany(500):
                for db_row in rows:
                    row = dict(db_row)
                    writer.writerow([
                        row["id"],
                        row["imported_at"].isoformat() if row["imported_at"] is not None else "",
                        _csv_safe_text(row["fdm4_store"]),
                        _csv_safe_text(row["product_style"]),
                        _csv_safe_text(row["product_color"]),
                        _csv_safe_text(row["logo_code"]),
                        _csv_safe_text(row["reason"]),
                        _csv_safe_text(row["detail"]),
                    ])
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

    filename_parts = ["import-punch-list"]
    if store:
        filename_parts.append(re.sub(r"[^A-Za-z0-9._-]", "_", store))
    filename = "-".join(filename_parts) + ".csv"
    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/product-link")
def product_link(
    store: str = Query(..., min_length=1, max_length=100),
    style: str = Query(..., min_length=1, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    """The WordPress front-end/admin URLs for a style's product, looked up on
    the configured sync target. Soft-fails so a WordPress hiccup never breaks
    the editor. Shared with MCP and the assistant via wp_bridge."""
    del user
    return wp_bridge.product_link(_clean(store, "store"), _clean(style, "style"))


# ---- Logo sync ownership -------------------------------------------------
# Which stores the warehouse is allowed to sync logos to. The gate itself
# lives in WordPress (a network option read by ARB_Logo_Reconcile); these
# endpoints let operators see and flip it from this app, with a mandatory
# safety check so enabling a store can never silently wipe live logos.


class OwnershipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fdm4_store: str = Field(min_length=1, max_length=32)
    owned: bool
    # When enabling a store whose warehouse data does not cover every
    # currently-logo'd style, the caller must acknowledge the exact count of
    # styles that will lose logos. Prevents blind/scripted enables.
    acknowledge_missing: int = Field(0, ge=0, le=100000)


_wp_admin_call = wp_bridge.wp_admin_call


def _warehouse_logo_styles(store: str) -> set:
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT upper(btrim(product_style)) AS style
              FROM logo.assignment
             WHERE fdm4_store = %s AND active
            """,
            (store,),
        )
        return {row["style"] for row in cursor.fetchall() if row["style"]}


def _ownership_preview(store: str) -> dict:
    settings = get_settings()
    wp = _wp_admin_call(
        f"/logo-styles?{urlencode({'fdm4_store': store})}",
        timeout=settings.wp_sync_timeout,
    )
    wp_styles = [dict(s) for s in (wp.get("styles") or []) if isinstance(s, dict)]
    warehouse = _warehouse_logo_styles(store)
    missing = [s for s in wp_styles if str(s.get("style", "")).upper() not in warehouse]
    return {
        "blog_id": int(wp.get("blog_id") or 0),
        "wp_logo_styles": len(wp_styles),
        "warehouse_styles": len(warehouse),
        "covered": len(wp_styles) - len(missing),
        "missing": missing,
        "safe": not missing,
    }


@router.get("/logo-ownership")
def logo_ownership(user: Dict[str, str] = Depends(require_user)):
    """Every mapped store with whether this app may sync its logos."""
    del user
    try:
        resp = _wp_admin_call("/ownership")
    except WordPressRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {
        "stores": resp.get("stores") or [],
        "owned_blogs": resp.get("owned_blogs") or [],
    }


@router.get("/logo-ownership/preview")
def logo_ownership_preview(
    store: str = Query(..., min_length=1, max_length=32),
    user: Dict[str, str] = Depends(require_user),
):
    """Pre-enable safety check: styles that carry logos on the website today
    but have no active warehouse rows - enabling sync would remove those."""
    del user
    try:
        return _ownership_preview(store.strip())
    except WordPressRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.post("/logo-ownership")
def set_logo_ownership(body: OwnershipBody, user: Dict[str, str] = Depends(require_csrf)):
    store = body.fdm4_store.strip()
    preview = None
    if body.owned:
        try:
            preview = _ownership_preview(store)
        except WordPressRequestError as exc:
            raise HTTPException(status_code=502, detail=f"Safety check failed: {exc}") from None
        if preview["missing"] and body.acknowledge_missing != len(preview["missing"]):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{len(preview['missing'])} style(s) on this store currently have logos the warehouse "
                    "does not cover - enabling sync would remove them from the website. "
                    "Import their sheets first, or acknowledge the removal to proceed."
                ),
            )
    try:
        resp = _wp_admin_call(
            "/ownership",
            method="POST",
            payload={"fdm4_store": store, "owned": body.owned},
        )
    except WordPressRequestError as exc:
        status = exc.status if 400 <= exc.status < 500 else 502
        raise HTTPException(status_code=status, detail=str(exc)) from None
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            """
            INSERT INTO logo.audit_log (actor, action, fdm4_store, detail)
            VALUES (%s, %s, %s, %s)
            """,
            (
                user["user_login"],
                "ownership_enabled" if body.owned else "ownership_disabled",
                store,
                json.dumps({
                    "blog_id": resp.get("blog_id"),
                    "acknowledged_missing": body.acknowledge_missing if body.owned else None,
                    "missing_styles": [str(m.get("style", "")) for m in (preview or {}).get("missing", [])][:200],
                }),
            ),
        )
    return {
        "ok": True,
        "fdm4_store": store,
        "blog_id": resp.get("blog_id"),
        "owned": bool(resp.get("owned")),
    }


@router.get("/audit-log")
def audit_log(
    store: Optional[str] = Query(None, max_length=100),
    style: Optional[str] = Query(None, max_length=100),
    actor: Optional[str] = Query(None, max_length=100),
    action: Optional[str] = Query(None, max_length=100),
    before_id: Optional[int] = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: Dict[str, str] = Depends(require_user),
):
    """Read-only change history (trigger-fed logo.audit_log; keyset paging)."""
    del user
    return _read_service(
        read_queries.get_audit_log,
        store=store,
        style=style,
        actor=actor,
        action=action,
        before_id=before_id,
        limit=limit,
    )


@router.get("/audit-log/export")
def export_audit_log(
    store: Optional[str] = Query(None, max_length=100),
    style: Optional[str] = Query(None, max_length=100),
    actor: Optional[str] = Query(None, max_length=100),
    action: Optional[str] = Query(None, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    """CSV export of the change history, honoring the same filters."""
    del user
    clauses = []
    params: List[Any] = []
    if store:
        clauses.append("fdm4_store = %s")
        params.append(_clean(store, "store"))
    if style:
        clauses.append("product_style = %s")
        params.append(_clean(style, "style"))
    if actor:
        clauses.append("actor = %s")
        params.append(_clean(actor, "actor"))
    if action:
        clauses.append("action = %s")
        params.append(_clean(action, "action"))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    columns = [
        "id", "at", "actor", "action", "fdm4_store", "product_style",
        "garment_color_code", "option_row", "position", "detail",
    ]
    select_sql = f"""
        SELECT id, at, actor, action, fdm4_store, product_style,
               garment_color_code, option_row, position, detail
          FROM logo.audit_log
          {where}
         ORDER BY id DESC
         LIMIT 100000
    """

    def chunks() -> Iterator[str]:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(columns)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        with database.streaming_cursor(batch_size=500) as cursor:
            cursor.execute(select_sql, tuple(params))
            while rows := cursor.fetchmany(500):
                for db_row in rows:
                    row = dict(db_row)
                    writer.writerow([
                        row["id"],
                        row["at"].isoformat() if row["at"] is not None else "",
                        _csv_safe_text(row["actor"]),
                        _csv_safe_text(row["action"]),
                        _csv_safe_text(row["fdm4_store"]),
                        _csv_safe_text(row["product_style"]),
                        _csv_safe_text(row["garment_color_code"]),
                        "" if row["option_row"] is None else row["option_row"],
                        "" if row["position"] is None else row["position"],
                        _csv_safe_text(
                            json.dumps(row["detail"], separators=(",", ":"))
                            if row["detail"] is not None
                            else ""
                        ),
                    ])
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
    filename_parts = ["logo-activity"]
    if store:
        filename_parts.append(re.sub(r"[^A-Za-z0-9._-]", "_", store))
    filename = "-".join(filename_parts) + ".csv"
    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Pricing-tier fallback: assign a store a price tier so the transform uses it
# when FDM4's catalog price is blank/0. Config only - the warehouse transform
# consumes woo.store_pricing_tier; nothing here touches prices directly.
# ---------------------------------------------------------------------------

@router.get("/pricing/tiers")
def pricing_tiers(user: Dict[str, str] = Depends(require_user)):
    del user
    return _read_service(read_queries.list_pricing_tiers)


@router.get("/pricing/store-tiers")
def pricing_store_tiers(user: Dict[str, str] = Depends(require_user)):
    """Current store -> tier assignments, with human-readable store names."""
    del user
    return _read_service(read_queries.list_store_pricing_tiers)


@router.put("/pricing/store-tier")
def set_store_tier(body: StoreTierBody, user: Dict[str, str] = Depends(require_csrf)):
    return _execute_mutation(
        SetStorePricingTierCommand.model_validate(body.model_dump()),
        user,
    )


@router.delete("/pricing/store-tier")
def delete_store_tier(
    fdm4_store: str = Query(..., min_length=1, max_length=100),
    user: Dict[str, str] = Depends(require_csrf),
):
    return _execute_mutation(
        DeleteStorePricingTierCommand(fdm4_store=fdm4_store),
        user,
    )


# ---------------------------------------------------------------------------
# Color classification: light/dark classification for garment color codes.
# Seeded by AI; operators can override individual entries here.
# ---------------------------------------------------------------------------

class ColorClassBody(BaseModel):
    color_code: str = Field(min_length=1, max_length=100)
    light_dark: str = Field(min_length=1, max_length=10)


@router.get("/colors")
def colors(
    q: str = Query("", max_length=100),
    cls: str = Query("", max_length=10),
    needs_review: bool = Query(False),
    sort: str = Query("", max_length=16),
    direction: str = Query("asc", max_length=4),
    limit: int = Query(200, le=1000),
    offset: int = Query(0, ge=0),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    return _read_service(
        read_queries.list_colors,
        q=q,
        cls=cls,
        needs_review=needs_review,
        sort=sort,
        direction="desc" if direction == "desc" else "asc",
        limit=limit,
        offset=offset,
    )


@router.put("/colors")
def put_color(
    body: ColorClassBody,
    user: Dict[str, str] = Depends(require_csrf),
):
    with database.cursor(write=True, actor=user["user_login"]) as cur:
        try:
            return mutations.set_color_class(
                cur,
                color_code=body.color_code,
                light_dark=body.light_dark,
                actor=user["user_login"],
            )
        except (ValueError, LookupError) as e:
            raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Sync blocks: freeze whole stores or specific styles from the product-sync
# engine (skipped for updates AND exempt from deactivation). style_code '' =
# whole store. Engine reads woo.sync_exclusion fail-open.
# ---------------------------------------------------------------------------

class SyncBlockBody(BaseModel):
    fdm4_store: str = Field(min_length=1, max_length=100)
    whole_store: bool = False
    styles: List[str] = []
    note: str = Field(default="", max_length=1000)
    # 'full' skips the store entirely; 'pricing' lets the sync run (creates,
    # stock, status) but never writes a price to an existing variation.
    # Only meaningful for whole-store blocks; style rows are always full.
    scope: str = Field(default="full", pattern="^(full|pricing)$")


class StockOverrideBody(BaseModel):
    style_code: str = Field(min_length=1, max_length=100)
    mode: str = Field(pattern="^(fake|real)$")
    note: str = Field(default="", max_length=1000)


class StockOverrideToggleBody(BaseModel):
    style_code: str = Field(min_length=1, max_length=100)
    active: bool


class SyncBlockToggleBody(BaseModel):
    fdm4_store: str = Field(min_length=1, max_length=100)
    style_code: str = Field(default="", max_length=100)
    active: bool


@router.get("/sync-blocks")
def sync_blocks(user: Dict[str, str] = Depends(require_user)):
    del user
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT fdm4_store, style_code, note, active, scope, updated_at, updated_by
              FROM woo.sync_exclusion ORDER BY fdm4_store, style_code
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return {"blocks": rows}


@router.put("/sync-blocks")
def save_sync_block(body: SyncBlockBody, user: Dict[str, str] = Depends(require_csrf)):
    styles = _clean_list(body.styles, upper=True, field="styles")
    if body.whole_store and styles:
        raise HTTPException(
            status_code=400,
            detail="Untick 'entire store' if you are listing specific style numbers")
    if not body.whole_store and not styles:
        raise HTTPException(status_code=400, detail="Choose whole-store or provide at least one style")
    result = _execute_mutation(
        SetSyncBlockCommand(
            store=body.fdm4_store, styles=[] if body.whole_store else styles,
            scope=body.scope if body.whole_store else "full", note=body.note, active=True,
        ),
        user,
    )
    return {"ok": True, "saved": result["saved"], "per_style": result["per_style"]}


def save_sync_block(body: SyncBlockBody, user: Dict[str, str] = Depends(require_csrf)):
    store = " ".join(body.fdm4_store.split()).upper()
    styles = _clean_list(body.styles, upper=True, field="styles")
    if body.whole_store and styles:
        raise HTTPException(
            status_code=400,
            detail="Untick 'entire store' if you are listing specific style numbers")
    if not body.whole_store and not styles:
        raise HTTPException(status_code=400, detail="Choose whole-store or provide at least one style")
    note = body.note.strip()
    rows = [""] if body.whole_store else styles
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        # Same universe the UI store dropdown offers (woo.store_catalog); a
        # typo'd code would otherwise create a block that freezes nothing.
        cursor.execute("SELECT 1 FROM woo.store_catalog WHERE fdm4_store = %s LIMIT 1", (store,))
        if not cursor.fetchone():
            raise HTTPException(status_code=400, detail=f"Unknown store code: {store}")
        scope = body.scope if body.whole_store else "full"
        for style in rows:
            cursor.execute(
                """
                INSERT INTO woo.sync_exclusion (fdm4_store, style_code, note, active, scope, updated_by)
                VALUES (%s, %s, %s, true, %s, %s)
                ON CONFLICT (fdm4_store, style_code) DO UPDATE SET
                    note = EXCLUDED.note, active = true, scope = EXCLUDED.scope,
                    updated_at = now(), updated_by = EXCLUDED.updated_by
                """,
                (store, style, note, scope, user["user_login"]),
            )
        # Tell the caller what each style actually freezes right now - a style
        # matching zero products is almost always a typo.
        per_style = []
        if styles:
            cursor.execute(
                """
                SELECT upper(btrim(style_code)) AS style, count(*) AS products
                  FROM woo.store_product_state
                 WHERE fdm4_store = %s AND is_active AND kind = 'variation'
                   AND upper(btrim(style_code)) = ANY(%s)
                 GROUP BY 1
                """,
                (store, styles),
            )
            counts = {r["style"]: r["products"] for r in cursor.fetchall()}
            per_style = [{"style": s, "products": counts.get(s, 0)} for s in styles]
    return {"ok": True, "saved": len(rows), "per_style": per_style}


class BrandStockRuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mill_code: str = Field(min_length=1, max_length=32)
    mode: str = Field(pattern="^(real|fake)$")


@router.get("/stock-overrides/brands")
def brand_stock_rules(
    q: str = Query("", max_length=100),
    mode: str = Query("", max_length=8),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: Dict[str, str] = Depends(require_user),
):
    """Every FDM4 brand (mill) with its stock behavior, paged and searchable:
    an explicit rule from woo.brand_stock_rule, or the automatic default."""
    del user
    if mode not in ("", "real", "fake", "auto"):
        mode = ""
    term = q.strip()
    like = f"%{term}%"
    mode_sql = {
        "": "",
        "real": "AND r.mode = 'real'",
        "fake": "AND r.mode = 'fake'",
        "auto": "AND r.mode IS NULL",
    }[mode]
    base_sql = f"""
        FROM fdm4.mill m
        LEFT JOIN woo.brand_stock_rule r
               ON r.mill_code = btrim(m."mill-code") AND r.active
        LEFT JOIN (
              SELECT btrim("mill-code") AS mc,
                     count(DISTINCT btrim("style-code")) AS styles
                FROM fdm4.style GROUP BY 1
             ) sc ON sc.mc = btrim(m."mill-code")
       WHERE NULLIF(btrim(m."mill-code"), '') IS NOT NULL
         AND COALESCE(sc.styles, 0) > 0
         AND ( %(term)s = ''
            OR btrim(COALESCE(m.description, '')) ILIKE %(like)s
            OR btrim(m."mill-code") ILIKE %(like)s )
         {mode_sql}
    """
    params = {"term": term, "like": like, "limit": limit, "offset": offset}
    with database.cursor() as cursor:
        cursor.execute(f"SELECT count(*) AS total {base_sql}", params)
        total = int(cursor.fetchone()["total"])
        cursor.execute(
            f"""
            SELECT btrim(m."mill-code") AS mill_code,
                   btrim(COALESCE(m.description, '')) AS brand_name,
                   r.mode, r.updated_by, r.updated_at,
                   COALESCE(sc.styles, 0) AS styles
            {base_sql}
             ORDER BY (r.mode IS NULL), lower(btrim(COALESCE(m.description, ''))), btrim(m."mill-code")
             LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return {"brands": rows, "total": total, "limit": limit, "offset": offset}


@router.put("/stock-overrides/brands")
def set_brand_stock_rule(body: BrandStockRuleBody, user: Dict[str, str] = Depends(require_csrf)):
    return _execute_mutation(
        SetBrandStockRuleCommand(mill_code=body.mill_code, mode=body.mode, active=True), user,
    )


def set_brand_stock_rule(body: BrandStockRuleBody, user: Dict[str, str] = Depends(require_csrf)):
    mill = _clean(body.mill_code, "mill_code")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            'SELECT btrim(COALESCE(description, \'\')) AS name FROM fdm4.mill WHERE btrim("mill-code") = %s LIMIT 1',
            (mill,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail=f"No FDM4 brand with mill code {mill}")
        cursor.execute(
            """
            INSERT INTO woo.brand_stock_rule (mill_code, brand_name, mode, active, updated_by, updated_at)
            VALUES (%s, %s, %s, true, %s, now())
            ON CONFLICT (mill_code) DO UPDATE SET
                mode = EXCLUDED.mode, brand_name = EXCLUDED.brand_name,
                active = true, updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            (mill, row["name"], body.mode, user["user_login"]),
        )
        cursor.execute(
            'SELECT count(DISTINCT btrim("style-code")) AS n FROM fdm4.style WHERE btrim("mill-code") = %s',
            (mill,),
        )
        styles = int(cursor.fetchone()["n"])
    return {"ok": True, "mill_code": mill, "brand_name": row["name"], "mode": body.mode, "styles": styles}


@router.delete("/stock-overrides/brands")
def delete_brand_stock_rule(
    mill: str = Query(..., min_length=1, max_length=32),
    user: Dict[str, str] = Depends(require_csrf),
):
    _execute_mutation(RemoveBrandStockRuleCommand(mill_code=mill), user)
    return {"ok": True}


def delete_brand_stock_rule(
    mill: str = Query(..., min_length=1, max_length=32),
    user: Dict[str, str] = Depends(require_csrf),
):
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute("DELETE FROM woo.brand_stock_rule WHERE mill_code = %s RETURNING mill_code", (_clean(mill, "mill"),))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="That brand has no rule to remove")
    return {"ok": True}


@router.get("/stock-overrides")
def stock_overrides(
    q: str = Query("", max_length=100),
    mode: str = Query("", max_length=8),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    if mode not in ("", "real", "fake"):
        mode = ""
    term = q.strip()
    like = f"%{term}%"
    mode_sql = {"": "", "real": "AND o.mode = 'real'", "fake": "AND o.mode = 'fake'"}[mode]
    # One set-based enrichment pass instead of two correlated subqueries per
    # row - name and brand stay searchable without the per-row scans.
    base_sql = f"""
        FROM woo.stock_override o
        LEFT JOIN (
              SELECT upper(btrim(style_code)) AS style,
                     max(brand) AS brand, max(name) AS product_name
                FROM woo.store_product_state
               WHERE kind = 'parent'
                 AND upper(btrim(style_code)) IN
                     (SELECT upper(btrim(style_code)) FROM woo.stock_override)
               GROUP BY 1
             ) e ON e.style = upper(btrim(o.style_code))
       WHERE ( %(term)s = ''
          OR o.style_code ILIKE %(like)s
          OR o.note ILIKE %(like)s
          OR COALESCE(e.brand, '') ILIKE %(like)s
          OR COALESCE(e.product_name, '') ILIKE %(like)s )
         {mode_sql}
    """
    params = {"term": term, "like": like, "limit": limit, "offset": offset}
    with database.cursor() as cursor:
        cursor.execute(f"SELECT count(*) AS total {base_sql}", params)
        total = int(cursor.fetchone()["total"])
        cursor.execute(
            f"""
            SELECT o.style_code, o.mode, o.note, o.active, o.updated_at, o.updated_by,
                   e.brand, e.product_name
            {base_sql}
             ORDER BY o.style_code
             LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return {"overrides": rows, "total": total, "limit": limit, "offset": offset}


@router.put("/stock-overrides")
def save_stock_override(body: StockOverrideBody, user: Dict[str, str] = Depends(require_csrf)):
    return _execute_mutation(
        SetStockOverrideCommand(style_code=body.style_code, mode=body.mode, note=body.note, active=True),
        user,
    )


def save_stock_override(body: StockOverrideBody, user: Dict[str, str] = Depends(require_csrf)):
    style = " ".join(body.style_code.split()).upper()
    note = body.note.strip()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            """
            SELECT max(brand) AS brand, max(name) AS product_name,
                   count(*) FILTER (WHERE kind = 'variation' AND is_active) AS variants
              FROM woo.store_product_state
             WHERE upper(btrim(style_code)) = %s
            """,
            (style,),
        )
        info = cursor.fetchone()
        if not info or not info["variants"]:
            raise HTTPException(status_code=400, detail=f"Style {style} has no active variations in the warehouse - check the style number")
        cursor.execute(
            """
            INSERT INTO woo.stock_override (style_code, mode, note, active, updated_by)
            VALUES (%s, %s, %s, true, %s)
            ON CONFLICT (style_code) DO UPDATE SET
                mode = EXCLUDED.mode, note = EXCLUDED.note, active = true,
                updated_at = now(), updated_by = EXCLUDED.updated_by
            """,
            (style, body.mode, note, user["user_login"]),
        )
    return {
        "ok": True,
        "style_code": style,
        "brand": info["brand"] or "",
        "product_name": info["product_name"] or "",
        "variants": int(info["variants"]),
    }


@router.put("/stock-overrides/toggle")
def toggle_stock_override(body: StockOverrideToggleBody, user: Dict[str, str] = Depends(require_csrf)):
    style = " ".join(body.style_code.split()).upper()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            "UPDATE woo.stock_override SET active = %s, updated_at = now(), updated_by = %s WHERE style_code = %s",
            (body.active, user["user_login"], style),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Override not found")
    return {"ok": True}


@router.delete("/stock-overrides")
def delete_stock_override(style: str = Query(min_length=1, max_length=100), user: Dict[str, str] = Depends(require_csrf)):
    _execute_mutation(RemoveStockOverrideCommand(style_code=style), user)
    return {"ok": True}


def delete_stock_override(style: str, user: Dict[str, str] = Depends(require_csrf)):
    style = " ".join(style.split()).upper()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute("DELETE FROM woo.stock_override WHERE style_code = %s", (style,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Override not found")
    return {"ok": True}


@router.put("/sync-blocks/toggle")
def toggle_sync_block(body: SyncBlockToggleBody, user: Dict[str, str] = Depends(require_csrf)):
    store = " ".join(body.fdm4_store.split()).upper()
    style = " ".join(body.style_code.split()).upper()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            """
            UPDATE woo.sync_exclusion SET active=%s, updated_at=now(), updated_by=%s
             WHERE fdm4_store=%s AND style_code=%s RETURNING fdm4_store
            """,
            (body.active, user["user_login"], store, style),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Block not found")
    return {"ok": True}


@router.delete("/sync-blocks")
def delete_sync_block(
    store: str = Query(min_length=1, max_length=100),
    style: str = Query(default="", max_length=100),
    user: Dict[str, str] = Depends(require_csrf),
):
    _execute_mutation(RemoveSyncBlockCommand(store=store, styles=[style] if style.strip() else []), user)
    return {"ok": True}


def delete_sync_block(
    store: str = Query(min_length=1, max_length=100),
    style: str = Query(default="", max_length=100),
    user: Dict[str, str] = Depends(require_csrf),
):
    store = " ".join(store.split()).upper()
    style = " ".join(style.split()).upper()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            "DELETE FROM woo.sync_exclusion WHERE fdm4_store=%s AND style_code=%s RETURNING fdm4_store",
            (store, style),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Block not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Price rules: Arborwear-managed pricing outside FDM4. Evaluated hourly by the
# warehouse transform via woo.eval_price_rules - the preview below calls the
# SAME function, so preview == reality by construction.
# ---------------------------------------------------------------------------

PRICE_EFFECT_TYPES = ("percent", "flat", "set_price", "price_level", "margin_over_cost")
PRICE_LEVEL_KEYS = ("msrp", "corp1", "corp2", "corp3", "wholesale", "employee", "base")


class PriceRuleBody(BaseModel):
    rule_id: Optional[int] = None
    name: str = Field(min_length=1, max_length=200)
    active: bool = False
    priority: int = Field(default=100, ge=1, le=100000)
    stackable: bool = False
    stores: List[str] = []
    store_tiers: List[str] = []
    styles: List[str] = []
    brands: List[str] = []
    categories: List[str] = []
    excl_stores: List[str] = []
    excl_styles: List[str] = []
    excl_brands: List[str] = []
    excl_categories: List[str] = []
    effect_type: str
    effect_value: Optional[Decimal] = None
    price_level_key: Optional[str] = None
    basis: str = Field(default="current", max_length=16)
    rounding: str = Field(default="none", max_length=8)
    floor_price: Optional[Decimal] = None
    ceiling_price: Optional[Decimal] = None
    cap_at_msrp: bool = False
    effective_from: Optional[str] = None    # YYYY-MM-DD or empty
    effective_until: Optional[str] = None
    note: str = Field(default="", max_length=2000)


def _clean_list(values, upper=False, maxlen=100, maxitems=5000, field="list"):
    out = []
    seen = set()
    for v in values or []:
        v = " ".join(str(v).split())
        if len(v) > maxlen:
            raise HTTPException(
                status_code=400,
                detail=f"{field}: entry exceeds {maxlen} characters: {v[:40]}...")
        if upper:
            v = v.upper()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    if len(out) > maxitems:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: too many entries ({len(out)}; max {maxitems})")
    return out


def _parse_rule_date(value, field):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD")


@router.get("/price-rules")
def price_rules(user: Dict[str, str] = Depends(require_user)):
    del user
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT rule_id, name, active, priority, stackable,
                   COALESCE(stores, '{}') AS stores, COALESCE(store_tiers, '{}') AS store_tiers,
                   COALESCE(styles, '{}') AS styles, COALESCE(brands, '{}') AS brands,
                   COALESCE(categories, '{}') AS categories,
                   COALESCE(excl_stores, '{}') AS excl_stores, COALESCE(excl_styles, '{}') AS excl_styles,
                   COALESCE(excl_brands, '{}') AS excl_brands, COALESCE(excl_categories, '{}') AS excl_categories,
                   effect_type, effect_value, price_level_key, basis, rounding,
                   floor_price, ceiling_price, cap_at_msrp,
                   effective_from, effective_until, note, updated_at, updated_by,
                   last_previewed_at
              FROM woo.price_rule ORDER BY priority, rule_id
            """
        )
        rules = [dict(r) for r in cursor.fetchall()]
        # Stores whose prices the sync will not touch (whole-store freezes of
        # either scope) - rules targeting them have no storefront effect.
        cursor.execute(
            "SELECT DISTINCT fdm4_store FROM woo.sync_exclusion WHERE active AND style_code = ''")
        frozen = [r["fdm4_store"] for r in cursor.fetchall()]
    return {"rules": rules, "effect_types": list(PRICE_EFFECT_TYPES),
            "price_level_keys": list(PRICE_LEVEL_KEYS),
            "frozen_stores": frozen}


@router.get("/price-rules/dimensions")
def price_rule_dimensions(user: Dict[str, str] = Depends(require_user)):
    del user
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT brand FROM woo.store_product_state WHERE NULLIF(btrim(brand),'') IS NOT NULL ORDER BY 1 LIMIT 1000")
        brands = [r["brand"] for r in cursor.fetchall()]
        cursor.execute(
            "SELECT DISTINCT category FROM woo.store_product_state WHERE NULLIF(btrim(category),'') IS NOT NULL ORDER BY 1 LIMIT 200")
        categories = [r["category"] for r in cursor.fetchall()]
        cursor.execute("SELECT tier_name FROM woo.pricing_tier ORDER BY sort_order, tier_name")
        tiers = [r["tier_name"] for r in cursor.fetchall()]
    return {"brands": brands, "categories": categories, "tiers": tiers}


# Widest magnitude woo.price_rule's numeric(12,4) columns can hold.
MAX_RULE_VALUE = Decimal("9999999")

# Fields whose change alters WHAT the rule does (vs name/note labeling). A
# material edit clears the preview stamp and forces the rule inactive - the
# server-side counterpart of "preview required before activating".
MATERIAL_RULE_FIELDS = (
    "priority", "stackable", "stores", "store_tiers", "styles", "brands",
    "categories", "excl_stores", "excl_styles", "excl_brands",
    "excl_categories", "effect_type", "effect_value", "price_level_key",
    "basis", "rounding", "floor_price", "ceiling_price", "cap_at_msrp",
    "effective_from", "effective_until",
)


def _rule_material_changed(old, new) -> bool:
    for field in MATERIAL_RULE_FIELDS:
        a, b = old.get(field), new[field]
        if isinstance(a, list) or isinstance(b, list):
            if set(a or []) != set(b or []):
                return True
        elif field in ("effect_value", "floor_price", "ceiling_price"):
            if (a is None) != (b is None):
                return True
            if a is not None and Decimal(a) != Decimal(b):
                return True
        elif a != b:
            return True
    return False


@router.put("/price-rules")
def save_price_rule(body: PriceRuleBody, user: Dict[str, str] = Depends(require_csrf)):
    if body.effect_type not in PRICE_EFFECT_TYPES:
        raise HTTPException(status_code=400, detail="Unknown effect type")
    if body.effect_type == "price_level":
        if (body.price_level_key or "") not in PRICE_LEVEL_KEYS:
            raise HTTPException(status_code=400, detail="price_level_key required for price_level effect")
    elif body.effect_value is None:
        raise HTTPException(status_code=400, detail="effect_value required for this effect type")
    if body.effect_value is not None:
        if abs(body.effect_value) > MAX_RULE_VALUE:
            raise HTTPException(status_code=400, detail="effect_value is out of range")
        if body.effect_type == "set_price" and body.effect_value <= 0:
            raise HTTPException(status_code=400, detail="set_price must be greater than 0")
        if body.effect_type == "margin_over_cost" and body.effect_value <= 0:
            raise HTTPException(status_code=400, detail="margin_over_cost multiplier must be greater than 0")
        if body.effect_type == "percent" and (body.effect_value <= -100 or body.effect_value > 1000):
            raise HTTPException(status_code=400, detail="percent must be above -100 and at most 1000")
    if body.floor_price is not None and (body.floor_price < 0 or body.floor_price > MAX_RULE_VALUE):
        raise HTTPException(status_code=400, detail="floor_price must be between 0 and 9,999,999")
    if body.ceiling_price is not None and (body.ceiling_price < 0 or body.ceiling_price > MAX_RULE_VALUE):
        raise HTTPException(status_code=400, detail="ceiling_price must be between 0 and 9,999,999")
    if (body.floor_price is not None and body.ceiling_price is not None
            and body.ceiling_price < body.floor_price):
        raise HTTPException(status_code=400, detail="The never-above price is below the never-below price")
    basis = (body.basis or "current").strip().lower()
    if body.effect_type not in ("percent", "flat"):
        basis = "current"
    if basis != "current" and basis not in PRICE_LEVEL_KEYS:
        raise HTTPException(status_code=400, detail="Unknown price basis")
    rounding = (body.rounding or "none").strip().lower()
    if rounding not in ("none", "99", "95", "00"):
        raise HTTPException(status_code=400, detail="Unknown rounding choice")
    frm = _parse_rule_date(body.effective_from, "effective_from")
    until = _parse_rule_date(body.effective_until, "effective_until")
    if frm and until and until < frm:
        raise HTTPException(status_code=400, detail="effective_until is before effective_from")
    values = {
        "rule_id": body.rule_id,
        "name": " ".join(body.name.split()),
        "priority": body.priority,
        "stackable": body.stackable,
        "stores": _clean_list(body.stores, upper=True, field="stores") or None,
        "store_tiers": _clean_list(body.store_tiers, field="store_tiers") or None,
        "styles": _clean_list(body.styles, upper=True, field="styles") or None,
        "brands": _clean_list(body.brands, field="brands") or None,
        "categories": _clean_list(body.categories, field="categories") or None,
        "excl_stores": _clean_list(body.excl_stores, upper=True, field="excl_stores") or None,
        "excl_styles": _clean_list(body.excl_styles, upper=True, field="excl_styles") or None,
        "excl_brands": _clean_list(body.excl_brands, field="excl_brands") or None,
        "excl_categories": _clean_list(body.excl_categories, field="excl_categories") or None,
        "effect_type": body.effect_type,
        "effect_value": body.effect_value,
        "price_level_key": body.price_level_key if body.effect_type == "price_level" else None,
        "basis": basis,
        "rounding": rounding,
        "floor_price": body.floor_price,
        "ceiling_price": body.ceiling_price,
        "cap_at_msrp": bool(body.cap_at_msrp),
        "effective_from": frm,
        "effective_until": until,
        "note": body.note.strip(),
    }
    deactivated = False
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        if values["rule_id"]:
            cursor.execute(
                "SELECT * FROM woo.price_rule WHERE rule_id=%s FOR UPDATE",
                (values["rule_id"],),
            )
            old = cursor.fetchone()
            if not old:
                raise HTTPException(status_code=404, detail="Rule not found")
            material = _rule_material_changed(old, values)
            # Save can KEEP a rule active (label-only edits) or deactivate it,
            # but never activate - activation goes through /toggle, which
            # checks the preview stamp.
            new_active = bool(body.active) and bool(old["active"]) and not material
            deactivated = bool(old["active"]) and bool(body.active) and not new_active
            cursor.execute(
                """
                UPDATE woo.price_rule SET
                    name=%(name)s, active=%(active)s, priority=%(priority)s, stackable=%(stackable)s,
                    stores=%(stores)s, store_tiers=%(store_tiers)s, styles=%(styles)s,
                    brands=%(brands)s, categories=%(categories)s,
                    excl_stores=%(excl_stores)s, excl_styles=%(excl_styles)s,
                    excl_brands=%(excl_brands)s, excl_categories=%(excl_categories)s,
                    effect_type=%(effect_type)s, effect_value=%(effect_value)s,
                    price_level_key=%(price_level_key)s, basis=%(basis)s, rounding=%(rounding)s,
                    floor_price=%(floor_price)s, ceiling_price=%(ceiling_price)s, cap_at_msrp=%(cap_at_msrp)s,
                    effective_from=%(effective_from)s, effective_until=%(effective_until)s,
                    last_previewed_at=CASE WHEN %(material)s THEN NULL ELSE last_previewed_at END,
                    note=%(note)s, updated_at=now(), updated_by=%(actor)s
                 WHERE rule_id=%(rule_id)s RETURNING rule_id, active
                """,
                {**values, "active": new_active, "material": material,
                 "actor": user["user_login"]},
            )
            row = cursor.fetchone()
        else:
            # New rules are born inactive with no preview stamp.
            cursor.execute(
                """
                INSERT INTO woo.price_rule (name, active, priority, stackable, stores, store_tiers,
                    styles, brands, categories, excl_stores, excl_styles, excl_brands, excl_categories,
                    effect_type, effect_value, price_level_key, basis, rounding,
                    floor_price, ceiling_price, cap_at_msrp, effective_from, effective_until, note, updated_by)
                VALUES (%(name)s, false, %(priority)s, %(stackable)s, %(stores)s, %(store_tiers)s,
                    %(styles)s, %(brands)s, %(categories)s, %(excl_stores)s, %(excl_styles)s,
                    %(excl_brands)s, %(excl_categories)s, %(effect_type)s, %(effect_value)s,
                    %(price_level_key)s, %(basis)s, %(rounding)s, %(floor_price)s, %(ceiling_price)s,
                    %(cap_at_msrp)s, %(effective_from)s, %(effective_until)s, %(note)s, %(actor)s)
                RETURNING rule_id, active
                """,
                {**values, "actor": user["user_login"]},
            )
            row = cursor.fetchone()
    return {"ok": True, "rule_id": row["rule_id"], "active": row["active"],
            "deactivated": deactivated}


class PriceRuleToggleBody(BaseModel):
    rule_id: int = Field(ge=1)
    active: bool


@router.put("/price-rules/toggle")
def toggle_price_rule(body: PriceRuleToggleBody, user: Dict[str, str] = Depends(require_csrf)):
    """Flip active only - never touches rule content. Activation requires a
    preview stamp newer than the last material edit (shared rule in
    mutations.set_price_rule_active; the UI keys off preview_required)."""
    try:
        _execute_mutation(SetPriceRuleActiveCommand(rule_id=body.rule_id, active=body.active), user)
    except HTTPException as exc:
        if exc.status_code == 422 and "Preview" in str(exc.detail):
            return JSONResponse(
                status_code=409,
                content={"error": "preview_required",
                         "message": "Preview this rule before activating - "
                                    "its settings changed since the last preview."})
        raise
    return {"ok": True, "rule_id": body.rule_id, "active": body.active}


def toggle_price_rule(body: PriceRuleToggleBody, user: Dict[str, str] = Depends(require_csrf)):
    """Flip active only - never touches rule content, so a stale client list
    can't clobber another operator's edits. Activation requires a preview
    stamp newer than the last material edit (the save path clears it)."""
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            "SELECT active, last_previewed_at FROM woo.price_rule WHERE rule_id=%s FOR UPDATE",
            (body.rule_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Rule not found")
        if body.active and row["last_previewed_at"] is None:
            return JSONResponse(
                status_code=409,
                content={"error": "preview_required",
                         "message": "Preview this rule before activating - "
                                    "its settings changed since the last preview."})
        cursor.execute(
            "UPDATE woo.price_rule SET active=%s, updated_at=now(), updated_by=%s WHERE rule_id=%s",
            (body.active, user["user_login"], body.rule_id),
        )
    return {"ok": True, "rule_id": body.rule_id, "active": body.active}


@router.delete("/price-rules")
def delete_price_rule(rule_id: int = Query(ge=1), user: Dict[str, str] = Depends(require_csrf)):
    _execute_mutation(DeletePriceRuleCommand(rule_id=rule_id), user)
    return {"ok": True}


def delete_price_rule(rule_id: int = Query(ge=1), user: Dict[str, str] = Depends(require_csrf)):
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute("DELETE FROM woo.price_rule WHERE rule_id=%s RETURNING rule_id", (rule_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Rule not found")
    return {"ok": True}


class PriceRulePreviewBody(BaseModel):
    rule_id: int = Field(ge=1)
    sample_limit: int = Field(default=200, ge=1, le=1000)


@router.post("/price-rules/preview")
def preview_price_rule(body: PriceRulePreviewBody, user: Dict[str, str] = Depends(require_csrf)):
    with database.cursor() as cursor:
        cursor.execute("SELECT * FROM woo.price_rule WHERE rule_id=%s", (body.rule_id,))
        rule = cursor.fetchone()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        # Candidate pre-filter from the rule's own targeting keeps the preview
        # fast; the evaluator then applies the FULL active chain per candidate.
        # Base = base_price (the PRE-rule price the transform preserves), never
        # the projected `price` column - that one already has active rules
        # baked in, and evaluating from it would double-apply them.
        where = ["s.is_active", "s.kind = 'variation'", "s.price IS NOT NULL"]
        params: dict = {"rid": body.rule_id, "lim": 50001}
        if rule["stores"] or rule["store_tiers"]:
            where.append(
                "(s.fdm4_store = ANY(%(stores)s) OR EXISTS (SELECT 1 FROM woo.store_pricing_tier spt"
                " WHERE spt.fdm4_store = s.fdm4_store AND spt.tier_name = ANY(%(tiers)s)))")
            params["stores"] = rule["stores"] or []
            params["tiers"] = rule["store_tiers"] or []
        if rule["styles"]:
            where.append("upper(btrim(s.style_code)) = ANY(%(styles)s)")
            params["styles"] = rule["styles"]
        if rule["brands"]:
            where.append("s.brand = ANY(%(brands)s)")
            params["brands"] = rule["brands"]
        if rule["categories"]:
            where.append("s.category = ANY(%(cats)s)")
            params["cats"] = rule["categories"]
        if rule.get("excl_stores"):
            where.append("NOT (s.fdm4_store = ANY(%(xstores)s))")
            params["xstores"] = rule["excl_stores"]
        if rule.get("excl_styles"):
            where.append("NOT (upper(btrim(s.style_code)) = ANY(%(xstyles)s))")
            params["xstyles"] = rule["excl_styles"]
        if rule.get("excl_brands"):
            where.append("NOT (s.brand = ANY(%(xbrands)s))")
            params["xbrands"] = rule["excl_brands"]
        if rule.get("excl_categories"):
            where.append("NOT (s.category = ANY(%(xcats)s))")
            params["xcats"] = rule["excl_categories"]
        cursor.execute("SET LOCAL statement_timeout = '120s'")
        cursor.execute(
            f"""
            WITH cand AS (
                SELECT s.fdm4_store, s.style_code, s.sku, s.color, s.size,
                       COALESCE(s.base_price, s.price) AS price,
                       s.price_levels, s.brand, s.category, s.def_cost,
                       (s.price_levels ->> 'msrp')::numeric AS msrp
                  FROM woo.store_product_state s
                 WHERE {' AND '.join(where)}
                 LIMIT %(lim)s
            ), hits AS (
                SELECT c.*, rp.final_price, rp.applied_rule_ids
                  FROM cand c
                  CROSS JOIN LATERAL woo.eval_price_rules(
                      c.fdm4_store, c.style_code, c.brand, c.category,
                      c.price, c.price_levels, c.def_cost,
                      current_date, ARRAY[%(rid)s]::bigint[], NULL) rp
                 WHERE rp.final_price IS NOT NULL AND %(rid)s = ANY(rp.applied_rule_ids)
            )
            SELECT (SELECT count(*) FROM cand)                                    AS candidates,
                   count(*)                                                        AS affected,
                   count(DISTINCT fdm4_store)                                      AS stores,
                   count(*) FILTER (WHERE msrp IS NOT NULL AND final_price > msrp) AS above_msrp,
                   count(*) FILTER (WHERE final_price <> price)                    AS changed,
                   round(min(final_price - price), 4)                              AS min_delta,
                   round(max(final_price - price), 4)                              AS max_delta,
                   round(avg(final_price - price), 4)                              AS avg_delta
              FROM hits
            """,
            params,
        )
        summary = dict(cursor.fetchone())
        cursor.execute(
            f"""
            WITH cand AS (
                SELECT s.fdm4_store, s.style_code, s.sku, s.color, s.size,
                       COALESCE(s.base_price, s.price) AS price,
                       s.price_levels, s.brand, s.category, s.def_cost,
                       (s.price_levels ->> 'msrp')::numeric AS msrp
                  FROM woo.store_product_state s
                 WHERE {' AND '.join(where)}
                 LIMIT %(lim)s
            )
            SELECT c.fdm4_store, c.style_code, c.sku, c.color, c.size,
                   c.price AS before_price, rp.final_price AS after_price, c.msrp,
                   (c.msrp IS NOT NULL AND rp.final_price > c.msrp) AS over_msrp,
                   rp.applied_rule_ids
              FROM cand c
              CROSS JOIN LATERAL woo.eval_price_rules(
                  c.fdm4_store, c.style_code, c.brand, c.category,
                  c.price, c.price_levels, c.def_cost,
                  current_date, ARRAY[%(rid)s]::bigint[], NULL) rp
             WHERE rp.final_price IS NOT NULL AND %(rid)s = ANY(rp.applied_rule_ids)
             ORDER BY abs(rp.final_price - c.price) DESC
             LIMIT %(sample)s
            """,
            {**params, "sample": body.sample_limit},
        )
        sample = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            f"""
            WITH cand AS (
                SELECT s.fdm4_store, s.style_code,
                       COALESCE(s.base_price, s.price) AS price,
                       s.price_levels, s.brand,
                       s.category, s.def_cost
                  FROM woo.store_product_state s
                 WHERE {' AND '.join(where)}
                 LIMIT %(lim)s
            )
            SELECT c.fdm4_store, count(*) AS affected
              FROM cand c
              CROSS JOIN LATERAL woo.eval_price_rules(
                  c.fdm4_store, c.style_code, c.brand, c.category,
                  c.price, c.price_levels, c.def_cost,
                  current_date, ARRAY[%(rid)s]::bigint[], NULL) rp
             WHERE rp.final_price IS NOT NULL AND %(rid)s = ANY(rp.applied_rule_ids)
             GROUP BY 1 ORDER BY 2 DESC LIMIT 30
            """,
            params,
        )
        per_store = [dict(r) for r in cursor.fetchall()]
        # Price-frozen stores among the affected: the hourly sync will not
        # change their live prices, so the rule has no visible effect there.
        cursor.execute(
            "SELECT DISTINCT fdm4_store FROM woo.sync_exclusion WHERE active AND style_code = ''")
        frozen_all = {r["fdm4_store"] for r in cursor.fetchall()}
        affected_stores = {p["fdm4_store"] for p in per_store} | set(rule["stores"] or [])
        frozen_targets = sorted(affected_stores & frozen_all)
    summary["truncated"] = summary.get("candidates", 0) is not None and summary["candidates"] >= 50001
    # Record the preview server-side - this is what /toggle checks before
    # allowing activation. Guarded by updated_at so a rule edited while the
    # preview ran does not get a stamp for numbers it no longer matches.
    preview_recorded = False
    with database.cursor(write=True, actor=user["user_login"]) as wcur:
        wcur.execute(
            """
            UPDATE woo.price_rule SET last_previewed_at = now()
             WHERE rule_id = %s AND updated_at = %s RETURNING rule_id
            """,
            (body.rule_id, rule["updated_at"]),
        )
        preview_recorded = wcur.fetchone() is not None
    return {"ok": True, "rule_id": body.rule_id, "summary": summary,
            "store_count": summary.get("stores"),
            "per_store": per_store, "sample": sample,
            "frozen_targets": frozen_targets,
            "preview_recorded": preview_recorded}


@router.get("/price-rules/check")
def check_price(
    store: str = Query(..., min_length=1, max_length=100),
    style: str = Query(..., min_length=1, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    """The composed answer: what will this style cost on this store with every
    active rule, priority, and stacking applied - the same math as the sync."""
    del user
    store_v = _clean(store, "store").upper()
    style_v = _clean(style, "style").upper()
    with database.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '30s'")
        cursor.execute(
            """
            SELECT s.sku, s.color, s.size,
                   COALESCE(s.base_price, s.price) AS base_price,
                   s.price AS current_price,
                   (s.price_levels ->> 'msrp')::numeric AS msrp,
                   rp.final_price, rp.applied_rule_ids
              FROM woo.store_product_state s
              CROSS JOIN LATERAL woo.eval_price_rules(
                  s.fdm4_store, s.style_code, s.brand, s.category,
                  COALESCE(s.base_price, s.price), s.price_levels, s.def_cost,
                  current_date, NULL, NULL) rp
             WHERE s.fdm4_store = %s AND upper(btrim(s.style_code)) = %s
               AND s.is_active AND s.kind = 'variation' AND s.price IS NOT NULL
             ORDER BY s.color, s.size
             LIMIT 500
            """,
            (store_v, style_v),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        rule_ids = sorted({rid for r in rows for rid in (r.get("applied_rule_ids") or [])})
        names = {}
        if rule_ids:
            cursor.execute(
                "SELECT rule_id, name FROM woo.price_rule WHERE rule_id = ANY(%s)",
                (rule_ids,),
            )
            names = {r["rule_id"]: r["name"] for r in cursor.fetchall()}
        cursor.execute(
            "SELECT 1 FROM woo.sync_exclusion WHERE active AND style_code = '' AND fdm4_store = %s LIMIT 1",
            (store_v,),
        )
        frozen = cursor.fetchone() is not None
    return {"store": store_v, "style": style_v, "rows": rows,
            "rule_names": names, "frozen": frozen}


# ---------------------------------------------------------------------------
# Product mix overrides: opt-in per-store control of WHICH products a store
# carries. Stores absent from woo.store_mix_store follow FDM4 untouched.
# mode 'all'  = registered but follows FDM4 (new FDM4 products auto-included);
# mode 'list' = woo.store_mix_item is authoritative - the hourly transform
# filters the store's projection to listed styles, their included color
# channels (colors, NULL = all), minus per-color size excludes. Style, color,
# and size identifiers are canonical CODES, stored upper(btrim()) - the same
# normalization the transform compares with.
# ---------------------------------------------------------------------------

MIX_MODES = ("all", "list")


class MixStoreBody(BaseModel):
    fdm4_store: str = Field(min_length=1, max_length=100)
    mode: str = Field(default="list")
    note: str = Field(default="", max_length=1000)


class MixStoreModeBody(BaseModel):
    fdm4_store: str = Field(min_length=1, max_length=100)
    mode: str


class MixExternalBody(BaseModel):
    fdm4_store: str = Field(min_length=1, max_length=100)


class MixStyleBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    style_code: str = Field(min_length=1, max_length=100)
    colors: Optional[List[str]] = None
    size_excludes: Optional[Dict[str, List[str]]] = None


class MixStylesBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    styles: List[str] = []


class MixImportBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    mode: str


class MixPreviewBody(BaseModel):
    store: str = Field(min_length=1, max_length=100)
    action: str
    styles: List[str] = []
    style_code: str = Field(default="", max_length=100)
    colors: Optional[List[str]] = None
    size_excludes: Optional[Dict[str, List[str]]] = None


def _mix_norm(value: str) -> str:
    return mix_service.norm(value)


def _mix_registry(cursor, store: str, *, required: bool = True):
    try:
        return mix_service.registry(cursor, store, required=required)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _mix_require_list_mode(registry_row) -> None:
    try:
        mix_service.require_list_mode(registry_row)
    except InvalidCommand as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _mix_known_store(cursor, store: str) -> None:
    try:
        mix_service.known_store(cursor, store)
    except NotFound as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _mix_seed_items(cursor, store: str, actor: str) -> int:
    return mix_service.seed_items(cursor, store, actor)


def _mix_item_count(cursor, store: str) -> int:
    return mix_service.item_count(cursor, store)


def _mix_style_universe(cursor, store: str, style: str):
    """Known color channels and sizes for one style: state rows (any activity,
    so channels removed from the mix stay editable) plus candidate colors the
    filtered state no longer carries."""
    cursor.execute(
        """
        SELECT upper(btrim(COALESCE(s.color_code, ''))) AS color,
               max(COALESCE(NULLIF(btrim(s.color), ''), s.color_code)) AS color_name,
               count(*) FILTER (WHERE s.is_active) AS variations,
               jsonb_agg(DISTINCT jsonb_build_object(
                   'code', upper(btrim(COALESCE(s.size_code, ''))),
                   'label', COALESCE(NULLIF(btrim(s.size), ''), s.size_code)
               )) FILTER (WHERE COALESCE(btrim(s.size_code), '') <> '') AS sizes
          FROM woo.store_product_state s
         WHERE s.fdm4_store = %s AND upper(btrim(s.style_code)) = %s
           AND s.kind = 'variation'
           AND COALESCE(btrim(s.color_code), '') <> ''
         GROUP BY 1
         ORDER BY 1
        """,
        (store, style),
    )
    available = []
    seen = set()
    for row in cursor.fetchall():
        available.append({
            "color": row["color"],
            "color_name": row["color_name"],
            "variations": int(row["variations"]),
            "sizes": row["sizes"] or [],
        })
        seen.add(row["color"])
    cursor.execute(
        "SELECT colors FROM woo.store_mix_candidate WHERE fdm4_store=%s AND upper(btrim(style_code))=%s",
        (store, style),
    )
    cand = cursor.fetchone()
    for color in (cand["colors"] if cand and cand["colors"] else []):
        color = _mix_norm(color)
        if color and color not in seen:
            seen.add(color)
            available.append({
                "color": color, "color_name": color, "variations": 0, "sizes": [],
            })
    return available


def _mix_clean_style_config(colors, size_excludes, available):
    """Normalize + validate one style's colors/size_excludes against its known
    universe. Returns (colors_or_none, size_excludes_json_or_none)."""
    known_colors = {a["color"] for a in available}
    known_sizes = {
        a["color"]: {s["code"] for s in a["sizes"]} for a in available
    }
    cleaned_colors = None
    if colors is not None:
        cleaned_colors = _clean_list(colors, upper=True, field="colors")
        if not cleaned_colors:
            raise HTTPException(
                status_code=400,
                detail="An empty color list is not storable - every color "
                       "channel would be excluded. To drop this product "
                       "entirely, remove the style from the mix instead.")
        unknown = [c for c in cleaned_colors if c not in known_colors]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown color channel(s) for this style: {', '.join(unknown)}")
    cleaned_sx: Dict[str, List[str]] = {}
    for raw_color, raw_sizes in (size_excludes or {}).items():
        color = _mix_norm(raw_color)
        if not color:
            continue
        allowed_keys = set(cleaned_colors) if cleaned_colors is not None else known_colors
        if color not in allowed_keys:
            raise HTTPException(
                status_code=400,
                detail=f"size_excludes color {color} is not an included color channel")
        sizes = _clean_list(raw_sizes, upper=True, field=f"size_excludes[{color}]")
        if not sizes:
            continue
        unknown_sizes = [s for s in sizes if s not in known_sizes.get(color, set())]
        if unknown_sizes:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown size(s) for {color}: {', '.join(unknown_sizes)}")
        if set(sizes) >= known_sizes.get(color, {None}) and known_sizes.get(color):
            raise HTTPException(
                status_code=400,
                detail=f"size_excludes for {color} would exclude every size - "
                       "remove the color channel instead")
        cleaned_sx[color] = sizes
    return cleaned_colors, (json.dumps(cleaned_sx) if cleaned_sx else None)


@router.get("/product-mix/stores")
def mix_stores(user: Dict[str, str] = Depends(require_user)):
    del user
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.fdm4_store, m.mode, m.active, m.note, m.imported_at,
                   m.created_by, m.created_at,
                   CASE WHEN m.mode = 'all' THEN 0 ELSE COALESCE(i.n, 0) END AS style_count,
                   (v.fdm4_store IS NOT NULL) AS external,
                   v.catalog_id AS external_catalog
              FROM woo.store_mix_store m
              LEFT JOIN (
                    SELECT fdm4_store, count(*) AS n
                      FROM woo.store_mix_item GROUP BY 1
                   ) i USING (fdm4_store)
              LEFT JOIN woo.virtual_catalog_store v USING (fdm4_store)
             ORDER BY m.fdm4_store
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return {"ok": True, "stores": rows}


@router.put("/product-mix/stores")
def enable_mix_store(body: MixStoreBody, user: Dict[str, str] = Depends(require_csrf)):
    store = _mix_norm(body.fdm4_store)
    mode = body.mode.strip().lower()
    if mode not in MIX_MODES:
        raise HTTPException(status_code=400, detail="mode must be 'all' or 'list'")
    note = body.note.strip()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        _mix_known_store(cursor, store)
        existing = _mix_registry(cursor, store, required=False)
        if existing and existing["active"]:
            raise HTTPException(
                status_code=400,
                detail=f"{store} already has a product-mix override")
        cursor.execute(
            """
            INSERT INTO woo.store_mix_store
                (fdm4_store, mode, active, note, created_by, updated_by)
            VALUES (%s, %s, true, %s, %s, %s)
            ON CONFLICT (fdm4_store) DO UPDATE SET
                mode = EXCLUDED.mode, active = true, note = EXCLUDED.note,
                updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            (store, mode, note, user["user_login"], user["user_login"]),
        )
        imported = 0
        if mode == "list":
            imported = _mix_seed_items(cursor, store, user["user_login"])
            if _mix_item_count(cursor, store) == 0:
                # An enabled list-mode store with zero items would remove every
                # product on the next transform. Refuse; the txn rolls back.
                raise HTTPException(
                    status_code=400,
                    detail=f"{store} has no products to seed - cannot enable "
                           "list mode (an empty list would remove everything)")
            cursor.execute(
                "UPDATE woo.store_mix_store SET imported_at = now() WHERE fdm4_store = %s",
                (store,),
            )
    return {"ok": True, "mode": mode, "imported": imported}


@router.put("/product-mix/stores/mode")
def switch_mix_mode(body: MixStoreModeBody, user: Dict[str, str] = Depends(require_csrf)):
    store = _mix_norm(body.fdm4_store)
    mode = body.mode.strip().lower()
    if mode not in MIX_MODES:
        raise HTTPException(status_code=400, detail="mode must be 'all' or 'list'")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        registry = _mix_registry(cursor, store)
        if registry["mode"] == mode:
            return {"ok": True, "mode": mode, "imported": 0, "note": "Mode unchanged"}
        imported = 0
        if mode == "list":
            # Snapshot the current FDM4 mix BEFORE flipping the mode so the
            # transform can never see a list-mode store with an empty list.
            imported = _mix_seed_items(cursor, store, user["user_login"])
            if _mix_item_count(cursor, store) == 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"{store} has no products to snapshot - cannot "
                           "switch to list mode")
            note = "Snapshotted the current FDM4 mix; the list is now authoritative"
        else:
            note = ("Store now follows FDM4 (new products auto-included). "
                    "The saved list is kept but inactive")
        cursor.execute(
            """
            UPDATE woo.store_mix_store
               SET mode = %s, updated_by = %s, updated_at = now(),
                   imported_at = CASE WHEN %s = 'list' THEN now() ELSE imported_at END
             WHERE fdm4_store = %s
            """,
            (mode, user["user_login"], mode, store),
        )
    return {"ok": True, "mode": mode, "imported": imported, "note": note}


@router.delete("/product-mix/stores")
def disable_mix_store(
    store: str = Query(min_length=1, max_length=100),
    user: Dict[str, str] = Depends(require_csrf),
):
    store = _mix_norm(store)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            """
            UPDATE woo.store_mix_store
               SET active = false, updated_by = %s, updated_at = now()
             WHERE fdm4_store = %s AND active RETURNING fdm4_store
            """,
            (user["user_login"], store),
        )
        if not cursor.fetchone():
            raise HTTPException(
                status_code=404,
                detail=f"{store} is not using a custom product list")
    return {"ok": True}


@router.put("/product-mix/external")
def enroll_external_store(body: MixExternalBody, user: Dict[str, str] = Depends(require_csrf)):
    """Enroll a store as an external (BrightSites/POS-fronted) store: the
    warehouse projects EVERY priced FDM4 style for it, always in stock (9999),
    via woo.virtual_catalog_store. The transform ranks that catalog as the
    store's suggested-primary, so the Woo sync engine picks it up on its own -
    no WP-side sync-map edit needed (a brand-new blog still needs its one-time
    Store Sync Map row in WP).
    """
    store = _mix_norm(body.fdm4_store)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        _mix_known_store(cursor, store)
        cursor.execute(
            "SELECT catalog_id FROM woo.virtual_catalog_store WHERE fdm4_store = %s",
            (store,))
        if cursor.fetchone():
            raise HTTPException(
                status_code=400,
                detail=f"{store} is already an external all-products store")
        registry = _mix_registry(cursor, store, required=False)
        if registry and registry["active"] and registry["mode"] == "list":
            raise HTTPException(
                status_code=400,
                detail=f"{store} uses a curated list; switch it to "
                       "'All products - follow FDM4' before making it external")
        catalog_id = f"{store}_Woo_1"
        cursor.execute(
            """
            INSERT INTO woo.virtual_catalog_store
                (fdm4_store, catalog_id, note, stock_override)
            VALUES (%s, %s, %s, 9999)
            """,
            (store, catalog_id,
             f"External all-products store; enrolled via Warehouse Ops by {user['user_login']}"),
        )
        cursor.execute(
            """
            INSERT INTO woo.store_mix_store
                (fdm4_store, mode, active, note, created_by, updated_by)
            VALUES (%s, 'all', true, %s, %s, %s)
            ON CONFLICT (fdm4_store) DO UPDATE SET
                mode = 'all', active = true,
                updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            (store, "External store: all products, always in stock",
             user["user_login"], user["user_login"]),
        )
    return {
        "ok": True,
        "catalog_id": catalog_id,
        "note": ("Supply builds on the next hourly warehouse refresh; the "
                 "storefront follows on its next sync (within ~1h15 total)."),
    }


@router.delete("/product-mix/external")
def unenroll_external_store(
    store: str = Query(min_length=1, max_length=100),
    user: Dict[str, str] = Depends(require_csrf),
):
    store = _mix_norm(store)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            "DELETE FROM woo.virtual_catalog_store WHERE fdm4_store = %s RETURNING catalog_id",
            (store,))
        if not cursor.fetchone():
            raise HTTPException(
                status_code=404,
                detail=f"{store} is not an external all-products store")
    return {
        "ok": True,
        "note": ("The all-products supply retires on the next hourly refresh; "
                 "the store's blog then re-syncs from its regular FDM4 catalog, "
                 "which can deactivate most of its products. The product-mix "
                 "registry entry is kept."),
    }


@router.get("/product-mix")
def mix_styles_list(
    store: str = Query(min_length=1, max_length=100),
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    store = _mix_norm(store)
    q = " ".join(q.split()).upper()
    with database.cursor() as cursor:
        registry = _mix_registry(cursor, store)
        cursor.execute(
            """
            SELECT count(*) AS n FROM woo.store_product_state
             WHERE fdm4_store = %s AND is_active AND kind = 'variation'
            """,
            (store,),
        )
        products_live = int(cursor.fetchone()["n"])
        if registry["mode"] == "all":
            return {
                "ok": True, "mode": "all", "total": 0, "styles": [],
                "summary": {"in_mix": None, "new_in_fdm4": 0,
                            "products_live": products_live},
            }
        in_mix = _mix_item_count(cursor, store)
        cursor.execute(
            """
            SELECT count(*) AS n
              FROM woo.store_mix_candidate c
             WHERE c.fdm4_store = %s
               AND NOT EXISTS (
                   SELECT 1 FROM woo.store_mix_item i
                    WHERE i.fdm4_store = c.fdm4_store
                      AND i.style_code = upper(btrim(c.style_code)))
            """,
            (store,),
        )
        new_in_fdm4 = int(cursor.fetchone()["n"])
        like = f"%{q}%"
        cursor.execute(
            """
            SELECT count(*) AS n FROM woo.store_mix_item
             WHERE fdm4_store = %s AND (%s = '' OR style_code ILIKE %s)
            """,
            (store, q, like),
        )
        total = int(cursor.fetchone()["n"])
        cursor.execute(
            """
            SELECT i.style_code, i.colors, i.size_excludes, i.source,
                   i.added_by, i.added_at, i.updated_by, i.updated_at,
                   COALESCE(p.products, 0) AS products_live,
                   COALESCE(p.style_name, '') AS name
              FROM woo.store_mix_item i
              LEFT JOIN (
                    SELECT upper(btrim(style_code)) AS style,
                           count(*) FILTER (WHERE kind = 'variation') AS products,
                           max(name) FILTER (WHERE kind = 'parent') AS style_name
                      FROM woo.store_product_state
                     WHERE fdm4_store = %s AND is_active
                     GROUP BY 1
                   ) p ON p.style = i.style_code
             WHERE i.fdm4_store = %s AND (%s = '' OR i.style_code ILIKE %s)
             ORDER BY i.style_code
             LIMIT %s OFFSET %s
            """,
            (store, store, q, like, limit, offset),
        )
        styles = [dict(r) for r in cursor.fetchall()]
    return {
        "ok": True, "mode": "list", "total": total, "styles": styles,
        "summary": {"in_mix": in_mix, "new_in_fdm4": new_in_fdm4,
                    "products_live": products_live},
    }


@router.get("/product-mix/style")
def mix_style_detail(
    store: str = Query(min_length=1, max_length=100),
    style: str = Query(min_length=1, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    store = _mix_norm(store)
    style = _mix_norm(style)
    with database.cursor() as cursor:
        _mix_registry(cursor, store)
        available = _mix_style_universe(cursor, store, style)
        cursor.execute(
            """
            SELECT colors, size_excludes, source, added_by, added_at
              FROM woo.store_mix_item
             WHERE fdm4_store = %s AND style_code = %s
            """,
            (store, style),
        )
        item = cursor.fetchone()
    return {
        "ok": True, "store": store, "style_code": style,
        "in_mix": item is not None,
        "colors": item["colors"] if item else None,
        "size_excludes": item["size_excludes"] if item else None,
        "available": available,
    }


@router.put("/product-mix/style")
def save_mix_style(body: MixStyleBody, user: Dict[str, str] = Depends(require_csrf)):
    store = _mix_norm(body.store)
    style = _mix_norm(body.style_code)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        registry = _mix_registry(cursor, store)
        _mix_require_list_mode(registry)
        available = _mix_style_universe(cursor, store, style)
        colors, sx_json = _mix_clean_style_config(
            body.colors, body.size_excludes, available)
        cursor.execute(
            """
            INSERT INTO woo.store_mix_item
                (fdm4_store, style_code, colors, size_excludes, source,
                 added_by, updated_by)
            VALUES (%s, %s, %s, %s::jsonb, 'manual', %s, %s)
            ON CONFLICT (fdm4_store, style_code) DO UPDATE SET
                colors = EXCLUDED.colors, size_excludes = EXCLUDED.size_excludes,
                updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            (store, style, colors, sx_json, user["user_login"], user["user_login"]),
        )
    return {"ok": True, "store": store, "style_code": style,
            "colors": colors, "size_excludes": json.loads(sx_json) if sx_json else None}


@router.put("/product-mix")
def add_mix_styles(body: MixStylesBody, user: Dict[str, str] = Depends(require_csrf)):
    store = _mix_norm(body.store)
    styles = _clean_list(body.styles, upper=True, field="styles")
    if not styles:
        raise HTTPException(status_code=400, detail="Provide at least one style")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        registry = _mix_registry(cursor, store)
        _mix_require_list_mode(registry)
        saved = 0
        for style in styles:
            cursor.execute(
                """
                INSERT INTO woo.store_mix_item
                    (fdm4_store, style_code, colors, source, added_by, updated_by)
                VALUES (%s, %s, NULL, 'manual', %s, %s)
                ON CONFLICT (fdm4_store, style_code) DO NOTHING
                """,
                (store, style, user["user_login"], user["user_login"]),
            )
            saved += cursor.rowcount
        cursor.execute(
            """
            SELECT upper(btrim(style_code)) AS style, count(*) AS products
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND is_active AND kind = 'variation'
               AND upper(btrim(style_code)) = ANY(%s)
             GROUP BY 1
            """,
            (store, styles),
        )
        counts = {r["style"]: r["products"] for r in cursor.fetchall()}
        per_style = [{"style": s, "products": counts.get(s, 0)} for s in styles]
    return {"ok": True, "saved": saved, "per_style": per_style}


@router.delete("/product-mix")
def remove_mix_styles(body: MixStylesBody, user: Dict[str, str] = Depends(require_csrf)):
    store = _mix_norm(body.store)
    styles = _clean_list(
        body.styles, upper=True, maxitems=500,
        field="styles (max 500 removals per request)")
    if not styles:
        raise HTTPException(status_code=400, detail="Provide at least one style")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        registry = _mix_registry(cursor, store)
        _mix_require_list_mode(registry)
        cursor.execute(
            """
            DELETE FROM woo.store_mix_item
             WHERE fdm4_store = %s AND style_code = ANY(%s)
            """,
            (store, styles),
        )
        removed = cursor.rowcount
        if removed and _mix_item_count(cursor, store) == 0:
            # Never leave an active list-mode store empty - the transform
            # would remove every product. The txn rolls back.
            raise HTTPException(
                status_code=400,
                detail="This would leave the mix empty and remove every "
                       "product from the store. Disable the override instead")
    return {"ok": True, "removed": removed}


@router.post("/product-mix/import")
def import_mix(body: MixImportBody, user: Dict[str, str] = Depends(require_csrf)):
    store = _mix_norm(body.store)
    mode = body.mode.strip().lower()
    if mode not in ("merge", "reset"):
        raise HTTPException(status_code=400, detail="mode must be 'merge' or 'reset'")
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        registry = _mix_registry(cursor, store)
        _mix_require_list_mode(registry)
        removed = 0
        if mode == "reset":
            removed = _mix_item_count(cursor, store)
            cursor.execute(
                "DELETE FROM woo.store_mix_item WHERE fdm4_store = %s", (store,))
        added = _mix_seed_items(cursor, store, user["user_login"])
        if _mix_item_count(cursor, store) == 0:
            raise HTTPException(
                status_code=400,
                detail="Import found no products for this store - nothing was changed")
        cursor.execute(
            "UPDATE woo.store_mix_store SET imported_at = now() WHERE fdm4_store = %s",
            (store,),
        )
    return {"ok": True, "mode": mode, "added": added, "removed": removed}


@router.post("/product-mix/preview")
def preview_mix(body: MixPreviewBody, user: Dict[str, str] = Depends(require_csrf)):
    del user
    store = _mix_norm(body.store)
    # Frontend ships the short names; the long forms are accepted aliases.
    aliases = {"remove": "remove_styles", "style": "set_style", "mode": "switch_all"}
    action = body.action.strip().lower()
    action = aliases.get(action, action)
    if action not in ("remove_styles", "set_style", "reset", "disable", "switch_all"):
        raise HTTPException(status_code=400, detail="Unknown preview action")
    retired = restored = affected = 0
    approximate = False
    note = ""
    with database.cursor() as cursor:
        registry = _mix_registry(cursor, store)
        if action == "remove_styles":
            styles = _clean_list(body.styles, upper=True, field="styles")
            if not styles:
                raise HTTPException(status_code=400, detail="Provide styles to preview")
            _mix_require_list_mode(registry)
            cursor.execute(
                """
                SELECT count(*) AS n, count(DISTINCT upper(btrim(style_code))) AS s
                  FROM woo.store_product_state
                 WHERE fdm4_store = %s AND is_active AND kind = 'variation'
                   AND upper(btrim(style_code)) = ANY(%s)
                """,
                (store, styles),
            )
            row = cursor.fetchone()
            retired, affected = int(row["n"]), int(row["s"])
        elif action == "set_style":
            style = _mix_norm(body.style_code)
            if not style:
                raise HTTPException(status_code=400, detail="style_code is required")
            _mix_require_list_mode(registry)
            available = _mix_style_universe(cursor, store, style)
            colors, sx_json = _mix_clean_style_config(
                body.colors, body.size_excludes, available)
            cursor.execute(
                """
                WITH v AS (
                    SELECT s.is_active,
                           ((%(colors)s::text[] IS NULL
                             OR upper(btrim(COALESCE(s.color_code, ''))) = ANY(%(colors)s::text[]))
                            AND NOT COALESCE(jsonb_exists(
                                %(sx)s::jsonb -> upper(btrim(COALESCE(s.color_code, ''))),
                                upper(btrim(COALESCE(s.size_code, '')))), false)
                           ) AS passes
                      FROM woo.store_product_state s
                     WHERE s.fdm4_store = %(store)s AND s.kind = 'variation'
                       AND upper(btrim(s.style_code)) = %(style)s
                )
                SELECT count(*) FILTER (WHERE is_active AND NOT passes) AS retired,
                       count(*) FILTER (WHERE NOT is_active AND passes) AS restored
                  FROM v
                """,
                {"store": store, "style": style, "colors": colors, "sx": sx_json},
            )
            row = cursor.fetchone()
            retired, restored = int(row["retired"]), int(row["restored"])
            affected = 1
            approximate = restored > 0
            note = ("Restored counts are approximate - inactive products may "
                    "be inactive for other reasons (e.g. dropped by FDM4)")
        elif action == "reset":
            _mix_require_list_mode(registry)
            cursor.execute(
                """
                SELECT array_agg(upper(btrim(c.style_code))) AS drift
                  FROM woo.store_mix_candidate c
                 WHERE c.fdm4_store = %s
                   AND NOT EXISTS (
                       SELECT 1 FROM woo.store_mix_item i
                        WHERE i.fdm4_store = c.fdm4_store
                          AND i.style_code = upper(btrim(c.style_code)))
                """,
                (store,),
            )
            drift = cursor.fetchone()["drift"] or []
            affected = len(drift)
            if drift:
                cursor.execute(
                    """
                    SELECT count(*) AS n FROM woo.store_product_state
                     WHERE fdm4_store = %s AND NOT is_active AND kind = 'variation'
                       AND upper(btrim(style_code)) = ANY(%s)
                    """,
                    (store, drift),
                )
                restored = int(cursor.fetchone()["n"])
            approximate = True
            note = ("Reset also restores any removed color channels and size "
                    "excludes on existing styles; per-channel counts are not "
                    "included here")
        else:  # disable / switch_all - store reverts to full FDM4 control
            affected = _mix_item_count(cursor, store)
            cursor.execute(
                """
                SELECT count(*) AS n FROM woo.store_product_state
                 WHERE fdm4_store = %s AND NOT is_active AND kind = 'variation'
                """,
                (store,),
            )
            restored = int(cursor.fetchone()["n"])
            approximate = True
            note = ("Everything FDM4 currently offers returns on the next "
                    "sync; restored counts are approximate")
    return {"ok": True, "action": action, "styles_affected": affected,
            "products_retired": retired, "products_restored": restored,
            "approximate": approximate, "note": note}


# ---------------------------------------------------------------------------
# Bulk-apply preview: dry-run that returns which products a logo variant would
# apply to in one store. Read-only but POSTed because the body is structured.
# ---------------------------------------------------------------------------

class BulkPreviewBody(BaseModel):
    fdm4_store: str = Field(min_length=1, max_length=100)
    logo_code: str = Field(min_length=1, max_length=100)
    color_scheme: str = Field(min_length=1, max_length=100)
    target: dict
    style_codes: Optional[List[str]] = None
    option_row: int = Field(default=1, ge=1, le=999)


@router.post("/bulk-apply/preview")
def bulk_preview(body: BulkPreviewBody, user: Dict[str, str] = Depends(require_csrf)):
    del user
    with database.cursor() as cur:
        try:
            return read_queries.compute_bulk_preview(
                cur, fdm4_store=body.fdm4_store, logo_code=body.logo_code,
                color_scheme=body.color_scheme, target=body.target, style_codes=body.style_codes,
                option_row=body.option_row)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


class BulkExecuteRow(BaseModel):
    style_code: str
    color_code: str


class BulkExecuteBody(BaseModel):
    fdm4_store: str
    logo_code: str
    color_scheme: str
    placement: str
    rows: List[BulkExecuteRow]
    option_row: int = Field(default=1, ge=1, le=999)
    cost_override: Optional[Decimal] = None
    image_url: Optional[str] = None


@router.post("/bulk-apply/execute")
def bulk_execute(body: BulkExecuteBody, user: Dict[str, str] = Depends(require_csrf)):
    with database.cursor(write=True, actor=user["user_login"]) as cur:
        try:
            lock_scopes(
                cur,
                (
                    mutations.MutationScope(
                        "assignment_option_row",
                        {
                            "fdm4_store": body.fdm4_store,
                            "product_style": row.style_code,
                            "garment_color_code": row.color_code,
                            "option_row": body.option_row,
                        },
                    )
                    for row in body.rows
                ),
            )
            return mutations.bulk_apply_execute(
                cur,
                fdm4_store=body.fdm4_store,
                logo_code=body.logo_code,
                color_scheme=body.color_scheme,
                placement=body.placement,
                rows=[r.model_dump() for r in body.rows],
                actor=user["user_login"],
                option_row=body.option_row,
                cost_override=body.cost_override,
                image_url=body.image_url,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


class BulkUndoBody(BaseModel):
    batch_id: int


@router.get("/bulk-apply/batches")
def bulk_batches(
    store: str = Query("", max_length=100),
    limit: int = Query(10, ge=1, le=50),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    clauses = []
    params: List[Any] = []
    if store:
        clauses.append("b.fdm4_store = %s")
        params.append(_clean(store, "store"))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit)
    with database.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT b.batch_id, b.fdm4_store, b.logo_code,
                   b.color_scheme, b.placement, b.target, b.applied,
                   b.created_at, b.created_by, b.undone_at,
                   count(br.batch_id)::integer AS recorded_rows
              FROM logo.bulk_batch b
              LEFT JOIN logo.bulk_batch_row br
                ON br.batch_id = b.batch_id
              {where}
             GROUP BY b.batch_id
             ORDER BY b.batch_id DESC
             LIMIT %s
            """,
            tuple(params),
        )
        return {"batches": [dict(row) for row in cursor.fetchall()]}


@router.post("/bulk-apply/undo")
def bulk_undo(body: BulkUndoBody, user: Dict[str, str] = Depends(require_csrf)):
    with database.cursor(write=True, actor=user["user_login"]) as cur:
        try:
            cur.execute(
                """
                SELECT fdm4_store, product_style, garment_color_code, option_row
                  FROM logo.bulk_batch_row
                 WHERE batch_id = %s
                """,
                (body.batch_id,),
            )
            scope_rows = cur.fetchall()
            lock_scopes(
                cur,
                (
                    mutations.MutationScope(
                        "assignment_option_row",
                        {
                            "fdm4_store": row["fdm4_store"],
                            "product_style": row["product_style"],
                            "garment_color_code": row["garment_color_code"],
                            "option_row": row["option_row"],
                        },
                    )
                    for row in scope_rows
                ),
            )
            return mutations.bulk_apply_undo(cur, batch_id=body.batch_id, actor=user["user_login"])
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))


# ---------------------------------------------------------------------------
# Logo display names: the customer-facing name per (design_id, color scheme).
# Seeded from FDM4 design_pool + filename parse; editable here; re-pullable
# from FDM4 when their descriptions change. The Woo reconcile reads this table
# (logo.display_name) as the authoritative shopper-facing logo name.
# ---------------------------------------------------------------------------

@router.get("/logo-names")
def logo_names(
    q: str = Query("", max_length=200),
    store: str = Query("", max_length=100),
    flt: str = Query("", alias="filter", max_length=16),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    term = q.strip()
    like = f"%{term}%"
    store = store.strip()
    if flt not in ("", "store", "global", "unnamed"):
        flt = ""
    # Fixed snippets chosen by exact match - never user text.
    store_flt_sql = {
        "": "",
        "store": "AND s.design_id IS NOT NULL",
        "global": "AND s.design_id IS NULL AND g.design_id IS NOT NULL",
        "unnamed": "AND s.design_id IS NULL AND g.design_id IS NULL",
    }[flt]
    global_flt_sql = {
        "": "",
        "store": "AND dn.fdm4_store <> ''",
        "global": "AND dn.fdm4_store = ''",
        "unnamed": "AND false",
    }[flt]
    with database.cursor() as cursor:
        if store:
            # Store-scoped: exactly the (design, scheme) pairs this store's
            # ACTIVE assignments use - including still-unnamed ones so they can
            # be named. Store-specific rows beat the global ('') defaults.
            cursor.execute(
                f"""
                WITH used AS (
                    SELECT btrim(a.design_id) AS design_id,
                           upper(btrim(a.color_scheme_id)) AS color_scheme_id,
                           min(btrim(a.logo_code)) AS logo_code,
                           count(*) AS n_assign
                      FROM logo.assignment a
                     WHERE a.fdm4_store = %(store)s AND a.active
                       AND NULLIF(btrim(a.design_id), '') IS NOT NULL
                     GROUP BY 1, 2
                )
                SELECT u.design_id, u.color_scheme_id,
                       COALESCE(s.name, g.name, '') AS name,
                       COALESCE(s.source, g.source, '') AS source,
                       COALESCE(s.locked, g.locked, false) AS locked,
                       COALESCE(s.uses, g.uses, 0) AS uses,
                       COALESCE(s.fdm4_description, g.fdm4_description)
                           AS fdm4_description,
                       COALESCE(s.updated_at, g.updated_at) AS updated_at,
                       COALESCE(s.updated_by, g.updated_by) AS updated_by,
                       u.logo_code, u.n_assign,
                       (SELECT btrim(dp.art_id) FROM fdm4.design_pool dp
                         WHERE btrim(dp.design_id) = u.design_id
                           AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
                         ORDER BY btrim(dp.design_pool_num) LIMIT 1) AS art_id,
                       (s.design_id IS NOT NULL) AS store_specific,
                       count(*) OVER() AS total_count
                  FROM used u
                  LEFT JOIN logo.display_name s
                         ON s.design_id = u.design_id
                        AND s.color_scheme_id = u.color_scheme_id
                        AND s.fdm4_store = %(store)s
                  LEFT JOIN logo.display_name g
                         ON g.design_id = u.design_id
                        AND g.color_scheme_id = u.color_scheme_id
                        AND g.fdm4_store = ''
                 WHERE ( %(term)s = ''
                    OR COALESCE(s.name, g.name, '') ILIKE %(like)s
                    OR u.design_id ILIKE %(like)s
                    OR u.color_scheme_id ILIKE %(like)s
                    OR u.logo_code ILIKE %(like)s
                    OR EXISTS (SELECT 1 FROM fdm4.design_pool dp
                                WHERE btrim(dp.design_id) = u.design_id
                                  AND btrim(dp.art_id) ILIKE %(like)s) )
                 {store_flt_sql}
                 ORDER BY u.n_assign DESC, u.design_id, u.color_scheme_id
                 LIMIT %(limit)s OFFSET %(offset)s
                """,
                {
                    "term": term,
                    "like": like,
                    "store": store,
                    "limit": limit,
                    "offset": offset,
                },
            )
        else:
            cursor.execute(
                f"""
                SELECT dn.design_id, dn.color_scheme_id, dn.name, dn.source,
                       dn.locked, dn.uses, dn.fdm4_description, dn.updated_at,
                       dn.updated_by, dn.fdm4_store, la.logo_code, la.n_assign,
                       (SELECT btrim(dp.art_id) FROM fdm4.design_pool dp
                         WHERE btrim(dp.design_id) = dn.design_id
                           AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
                         ORDER BY btrim(dp.design_pool_num) LIMIT 1) AS art_id,
                       (dn.fdm4_store <> '') AS store_specific,
                       count(*) OVER() AS total_count
                  FROM logo.display_name dn
                  LEFT JOIN LATERAL (
                      SELECT min(btrim(a.logo_code)) AS logo_code, count(*) AS n_assign
                        FROM logo.assignment a
                       WHERE btrim(a.design_id) = dn.design_id
                         AND upper(btrim(a.color_scheme_id)) = dn.color_scheme_id
                  ) la ON true
                 WHERE ( %(term)s = ''
                    OR dn.name ILIKE %(like)s
                    OR dn.design_id ILIKE %(like)s
                    OR dn.color_scheme_id ILIKE %(like)s
                    OR la.logo_code ILIKE %(like)s
                    OR EXISTS (SELECT 1 FROM fdm4.design_pool dp
                                WHERE btrim(dp.design_id) = dn.design_id
                                  AND btrim(dp.art_id) ILIKE %(like)s) )
                 {global_flt_sql}
                 ORDER BY dn.uses DESC, dn.design_id, dn.color_scheme_id
                 LIMIT %(limit)s OFFSET %(offset)s
                """,
                {"term": term, "like": like, "limit": limit, "offset": offset},
            )
        rows = [dict(row) for row in cursor.fetchall()]
    total = int(rows[0]["total_count"]) if rows else 0
    for row in rows:
        row.pop("total_count", None)
    return {
        "names": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "store": store,
    }


@router.put("/logo-names")
def set_logo_name(body: LogoNameBody, user: Dict[str, str] = Depends(require_csrf)):
    """Shared with the assistant (mutations.set_logo_name): '' = the global
    default row; a store code writes a store-specific row that beats it."""
    result = _execute_mutation(
        SetLogoNameCommand(
            design_id=body.design_id, color_scheme_id=body.color_scheme_id,
            name=body.name, store=body.fdm4_store.strip() or None,
        ),
        user,
    )
    return {"ok": True, "name": result}


def set_logo_name(body: LogoNameBody, user: Dict[str, str] = Depends(require_csrf)):
    design_id = _clean(body.design_id, "design_id", 64)
    scheme = _clean(body.color_scheme_id, "color_scheme_id", 64).upper()
    name = _clean(body.name, "name", 200)
    # '' = the global default row; a store code writes a store-specific row
    # that beats the global one everywhere that store is concerned.
    store = body.fdm4_store.strip()
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            """
            INSERT INTO logo.display_name
                (design_id, color_scheme_id, fdm4_store, name, source, locked,
                 uses, updated_at, updated_by)
            VALUES (%s, %s, %s, %s, 'manual', true, 0, now(), %s)
            ON CONFLICT (design_id, color_scheme_id, fdm4_store) DO UPDATE SET
                name = EXCLUDED.name, source = 'manual', locked = true,
                updated_at = now(), updated_by = EXCLUDED.updated_by
            RETURNING design_id, color_scheme_id, fdm4_store, name, source,
                      locked, uses
            """,
            (design_id, scheme, store, name, user["user_login"]),
        )
        result = dict(cursor.fetchone())
    return {"ok": True, "name": result}


@router.post("/logo-names/repull")
def repull_logo_name(body: RepullBody, user: Dict[str, str] = Depends(require_csrf)):
    """Re-pull one design's names from FDM4 design_pool. Locked (hand-edited)
    rows are preserved unless force=true."""
    design_id = _clean(body.design_id, "design_id", 64)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            "SELECT logo.repull_display_name(%s, %s) AS changed",
            (design_id, bool(body.force)),
        )
        changed = int(cursor.fetchone()["changed"])
        cursor.execute(
            """
            SELECT design_id, color_scheme_id, name, source, locked, uses, fdm4_description
              FROM logo.display_name WHERE design_id = %s ORDER BY color_scheme_id
            """,
            (design_id,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    return {"ok": True, "changed": changed, "names": rows}


def _jpeg_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 7:
                return None
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return width, height
        offset += length
    return None


def _detect_image(data: bytes) -> Optional[Tuple[str, str, int, int]]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        if (
            data[12:16] != b"IHDR"
            or data[-12:-8] != b"\x00\x00\x00\x00"
            or data[-8:-4] != b"IEND"
        ):
            return None
        width, height = struct.unpack(">II", data[16:24])
        result = ("png", "image/png", width, height)
    elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 14:
        if data[-1:] != b";":
            return None
        width, height = struct.unpack("<HH", data[6:10])
        result = ("gif", "image/gif", width, height)
    elif data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        dimensions = _jpeg_dimensions(data)
        if dimensions is None:
            return None
        result = ("jpg", "image/jpeg", dimensions[0], dimensions[1])
    elif len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        declared = int.from_bytes(data[4:8], "little") + 8
        if declared != len(data):
            return None
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
        else:
            return None
        result = ("webp", "image/webp", width, height)
    else:
        return None

    if result[2] <= 0 or result[3] <= 0 or result[2] * result[3] > MAX_IMAGE_PIXELS:
        return None
    return result


@router.post("/upload")
def upload_image(
    file: UploadFile = File(...),
    user: Dict[str, str] = Depends(require_csrf),
):
    del user
    settings = get_settings()
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"This image is too large (limit: {settings.max_upload_bytes // (1024 * 1024)} MB)")
    detected = _detect_image(data)
    if detected is None:
        raise HTTPException(
            status_code=422,
            detail="File must be a valid PNG, JPEG, GIF, or WebP image",
        )
    extension, media_type, width, height = detected
    declared_type = (file.content_type or "").lower()
    if declared_type not in {"", "application/octet-stream", media_type}:
        raise HTTPException(status_code=422, detail="Image content type does not match its data")

    settings.upload_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(24)}.{extension}"
    final_path = settings.upload_dir / filename
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".upload-", dir=settings.upload_dir, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o640)
        os.replace(temporary_path, final_path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Unable to store image") from None

    return {
        "ok": True,
        "filename": filename,
        "url": settings.media_base + filename,
        "media_type": media_type,
        "size": len(data),
        "width": width,
        "height": height,
    }


@router.post("/sync")
def sync_now(
    body: SyncBody,
    user: Dict[str, str] = Depends(require_csrf),
):
    settings = get_settings()
    store = _clean(body.store, "store")
    styles = []
    for style in body.styles:
        cleaned = _clean(style, "style")
        if cleaned not in styles:
            styles.append(cleaned)

    with database.cursor() as cursor:
        catalog = _catalog_for_store(cursor, store)
        if catalog is None:
            raise HTTPException(status_code=404, detail="Store not found")
        for style in styles:
            if not _style_exists(cursor, store, catalog, style):
                cursor.execute(
                    "SELECT 1 FROM logo.assignment WHERE fdm4_store = %s AND product_style = %s LIMIT 1",
                    (store, style),
                )
                if cursor.fetchone() is None:
                    raise HTTPException(status_code=422, detail=f"Unknown style {style}")

    # Commit durable intent before crossing the WordPress boundary. Completion
    # is a separate append-only row, so a local post-call failure can never
    # make a successful external reconciliation look like it did not run.
    request_id = secrets.token_hex(16)
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        cursor.execute(
            """
            INSERT INTO logo.audit_log (
                actor, action, fdm4_store, detail
            ) VALUES (%s, 'sync_requested', %s, %s)
            RETURNING id
            """,
            (
                user["user_login"],
                store,
                json.dumps({"request_id": request_id, "styles": styles}),
            ),
        )
        intent_id = int(cursor.fetchone()["id"])

    try:
        result = wordpress_json_request(
            settings.wp_sync_url,
            settings.wp_sync_user,
            settings.wp_sync_app_password,
            method="POST",
            timeout=settings.wp_sync_timeout,
            payload={"fdm4_store": store, "styles": styles},
        )
    except WordPressRequestError as exc:
        try:
            with database.cursor(write=True, actor=user["user_login"]) as cursor:
                cursor.execute(
                    """
                    INSERT INTO logo.audit_log (
                        actor, action, fdm4_store, detail
                    ) VALUES (%s, 'sync_failed', %s, %s)
                    """,
                    (
                        user["user_login"],
                        store,
                        json.dumps({
                            "request_id": request_id,
                            "intent_id": intent_id,
                            "error": str(exc)[:1000],
                        }),
                    ),
                )
        except Exception:
            logger.exception("unable to record failed WordPress sync completion")
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    reconcile = result.get("reconcile")
    stats = dict(reconcile.get("stats", {})) if isinstance(reconcile, dict) else {}
    design_map = result.get("design_map")
    if isinstance(design_map, dict) and isinstance(design_map.get("rows"), int):
        stats["design_map_rows"] = design_map["rows"]

    try:
        with database.cursor(write=True, actor=user["user_login"]) as cursor:
            cursor.execute(
                """
                INSERT INTO logo.audit_log (
                    actor, action, fdm4_store, detail
                ) VALUES (%s, 'sync_succeeded', %s, %s)
                """,
                (
                    user["user_login"],
                    store,
                    json.dumps({
                        "request_id": request_id,
                        "intent_id": intent_id,
                        "styles": styles,
                        "owned": result.get("owned"),
                        "stats": {
                            key: value
                            for key, value in stats.items()
                            if isinstance(value, (int, float))
                        },
                    }),
                ),
            )
    except Exception:
        logger.exception("unable to record successful WordPress sync completion")
    return {"ok": True, **result, "stats": stats}


# ---------------------------------------------------------------------------
# Legacy migration: NDJSON logo-sheet import + media-server image mirroring.
# Both are re-runnable: the import upserts (preserving manual edits by
# default), and the mirror is keyed by logo.image_import (source_url ->
# locally stored, content-hash-named file).
# ---------------------------------------------------------------------------

MAX_LEGACY_IMPORT_BYTES = 64 * 1024 * 1024
MAX_LEGACY_IMPORT_ROWS = 100_000
IMAGE_FETCH_TIMEOUT = 20


@router.post("/legacy-import")
def legacy_import_sheets(
    file: UploadFile = File(...),
    preserve_manual: str = Form("true"),
    user: Dict[str, str] = Depends(require_csrf),
):
    raw = file.file.read(MAX_LEGACY_IMPORT_BYTES + 1)
    if len(raw) > MAX_LEGACY_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Import exceeds the 64MB limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Import must be UTF-8 NDJSON") from None
    preserve = _parse_bool(preserve_manual, "preserve_manual")

    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        def report(row: Dict[str, Any], reason: str, detail: str, row_number: int) -> None:
            _insert_import_report(cursor, {
                "fdm4_store": row.get("fdm4_store"),
                "product_style": row.get("product_style"),
                "garment_color_code": row.get("product_color") or row.get("garment_color_code"),
                "logo_code": row.get("logo_code"),
            }, reason, detail, user["user_login"], row_number)

        try:
            counts = legacy.import_rows(
                cursor,
                io.StringIO(text),
                user["user_login"],
                preserve,
                report,
                MAX_LEGACY_IMPORT_ROWS,
            )
        except legacy.RowMiss as exc:
            raise HTTPException(status_code=413, detail=exc.detail) from None
    return {"ok": True, **counts}


def _resolve_safe_url(url: str):
    """Parse a legacy image URL and resolve only globally routable addresses."""

    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise ValueError("unsafe URL")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValueError("invalid URL port") from None
    try:
        infos = socket.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise ValueError("unresolvable URL") from None
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise ValueError("invalid resolved address") from None
        if not address.is_global:
            raise ValueError("URL resolves to a non-public address")
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    if not addresses:
        raise ValueError("unresolvable URL")
    return parsed, port, addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose socket is pinned to a prevalidated DNS result."""

    def __init__(self, host: str, port: int, address: str, timeout: int) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._resolved_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._resolved_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an IP while verifying the original host."""

    def __init__(self, host: str, port: int, address: str, timeout: int) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._resolved_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._resolved_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _fetch_legacy_image(url: str, max_bytes: int) -> bytes:
    parsed, port, addresses = _resolve_safe_url(url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    default_port = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host_header = f"[{host}]"
    else:
        host_header = host
    if port != default_port:
        host_header += f":{port}"

    last_error: Optional[BaseException] = None
    for address in addresses:
        connection = (
            _PinnedHTTPSConnection(host, port, address, IMAGE_FETCH_TIMEOUT)
            if parsed.scheme == "https"
            else _PinnedHTTPConnection(host, port, address, IMAGE_FETCH_TIMEOUT)
        )
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "image/*",
                    "Host": host_header,
                    "User-Agent": "arb-logo-admin/1.0",
                },
            )
            response = connection.getresponse()
            # Manual http.client requests do not follow redirects. Rejecting all
            # non-2xx responses prevents a public host from bouncing to metadata.
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"unexpected HTTP status {response.status}")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    raise ValueError("invalid Content-Length") from None
                if declared_length < 0:
                    raise ValueError("invalid Content-Length")
                if declared_length > max_bytes:
                    raise ValueError("image exceeds the upload size limit")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("image exceeds the upload size limit")
            return data
        except (http.client.HTTPException, OSError, ValueError) as exc:
            last_error = exc
        finally:
            connection.close()
    if last_error is not None:
        raise last_error
    raise ValueError("unresolvable URL")


@router.post("/legacy-import-images")
def legacy_import_images(
    limit: int = Form(50),
    user: Dict[str, str] = Depends(require_csrf),
):
    settings = get_settings()
    limit = max(1, min(200, int(limit)))

    # Phase 1 (read): distinct legacy URLs still referenced by assignments.
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT image_url
              FROM logo.assignment
             WHERE image_url ~* '^https?://'
               AND image_url NOT LIKE %s
             ORDER BY image_url
             LIMIT %s
            """,
            (settings.media_base + "%", limit + 1),
        )
        candidates = [str(row["image_url"]) for row in cursor.fetchall()]
        remaining_hint = len(candidates) > limit
        candidates = candidates[:limit]
        cursor.execute(
            """
            SELECT source_url, filename, bytes
              FROM logo.image_import
             WHERE source_url = ANY(%s)
            """,
            (candidates,),
        )
        already: Dict[str, str] = {}
        for row in cursor.fetchall():
            filename = str(row["filename"])
            expected_size = int(row["bytes"] or 0)
            mapped_path = settings.upload_dir / filename
            if (
                filename == Path(filename).name
                and filename not in {"", ".", ".."}
                and not mapped_path.is_symlink()
                and mapped_path.is_file()
                and expected_size > 0
                and mapped_path.stat().st_size == expected_size
            ):
                already[str(row["source_url"])] = filename

    # Phase 2 (network + disk, outside any transaction): download, validate,
    # store under a content-hash name so identical assets dedupe naturally.
    downloaded: Dict[str, str] = {}
    failures: List[Dict[str, str]] = []
    settings.upload_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    for url in candidates:
        if url in already:
            continue
        try:
            data = _fetch_legacy_image(url, settings.max_upload_bytes)
        except (http.client.HTTPException, OSError, ValueError) as exc:
            failures.append({"url": url, "reason": type(exc).__name__})
            continue
        detected = _detect_image(data)
        if detected is None:
            failures.append({"url": url, "reason": "not a valid image"})
            continue
        extension = detected[0]
        filename = hashlib.sha256(data).hexdigest()[:32] + "." + extension
        final_path = settings.upload_dir / filename
        existing_file_is_exact = False
        if not final_path.is_symlink() and final_path.is_file():
            try:
                existing_file_is_exact = (
                    final_path.stat().st_size == len(data)
                    and hashlib.sha256(final_path.read_bytes()).digest()
                    == hashlib.sha256(data).digest()
                )
            except OSError:
                existing_file_is_exact = False
        if not existing_file_is_exact:
            temporary_path: Optional[Path] = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=".import-", dir=settings.upload_dir, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.chmod(temporary_path, 0o640)
                os.replace(temporary_path, final_path)
            except OSError:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                failures.append({"url": url, "reason": "unable to store image"})
                continue
        downloaded[url] = filename

    # Phase 3 (write): record the mapping and repoint every assignment that
    # still uses the legacy URL. Failures are reported to the punch list.
    repointed = 0
    with database.cursor(write=True, actor=user["user_login"]) as cursor:
        for url, filename in {**already, **downloaded}.items():
            cursor.execute(
                """
                INSERT INTO logo.image_import (source_url, filename, bytes, imported_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_url) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    bytes = EXCLUDED.bytes,
                    imported_at = now(),
                    imported_by = EXCLUDED.imported_by
                """,
                (
                    url,
                    filename,
                    (settings.upload_dir / filename).stat().st_size,
                    user["user_login"],
                ),
            )
            cursor.execute(
                "UPDATE logo.assignment SET image_url = %s, updated_by = %s, updated_at = now() WHERE image_url = %s",
                (settings.media_base + filename, user["user_login"], url),
            )
            repointed += cursor.rowcount
        for failure in failures:
            _insert_import_report(
                cursor,
                {"fdm4_store": None, "product_style": None, "garment_color_code": None, "logo_code": None},
                "image_import_failed",
                f"{failure['reason']}: {failure['url']}"[:4000],
                user["user_login"],
                0,
            )
        cursor.execute(
            """
            SELECT count(DISTINCT image_url) AS remaining
              FROM logo.assignment
             WHERE image_url ~* '^https?://' AND image_url NOT LIKE %s
            """,
            (settings.media_base + "%",),
        )
        remaining = int(cursor.fetchone()["remaining"])

    return {
        "ok": True,
        "processed": len(candidates),
        "downloaded": len(downloaded),
        "reused": len(already),
        "repointed_assignments": repointed,
        "failed": len(failures),
        "remaining": remaining if not remaining_hint else max(remaining, 1),
    }


# ---------------------------------------------------------------------------
# System health (read-only overview for the Health tab)
# ---------------------------------------------------------------------------


@router.get("/health/overview")
def health_overview(user: Dict[str, str] = Depends(require_user)):
    del user
    now = datetime.datetime.now(datetime.timezone.utc)
    out: Dict[str, Any] = {"ok": True, "generated_at": now.isoformat()}
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, status, started_at, finished_at,
                   EXTRACT(EPOCH FROM (finished_at - started_at))::int AS duration_s,
                   rows_loaded, refresh_version,
                   left(COALESCE(note, ''), 200) AS note,
                   left(COALESCE(error, ''), 400) AS error
              FROM woo.sync_control
             WHERE op = 'pull'
             ORDER BY id DESC
             LIMIT 12
            """
        )
        runs = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'success') AS ok_24h,
                   count(*) FILTER (WHERE status NOT IN ('success', 'running', 'requested')) AS failed_24h
              FROM woo.sync_control
             WHERE op = 'pull' AND requested_at > now() - interval '24 hours'
            """
        )
        day = dict(cursor.fetchone())
        out["pipeline"] = {"runs": runs, "ok_24h": int(day["ok_24h"] or 0), "failed_24h": int(day["failed_24h"] or 0)}

        cursor.execute(
            """
            SELECT max(row_version) AS max_version,
                   max(changed_at) AS latest_change,
                   count(*) FILTER (WHERE is_active) AS active_rows,
                   count(*) AS total_rows,
                   count(*) FILTER (WHERE is_active AND kind = 'parent') AS parents,
                   count(*) FILTER (WHERE is_active AND kind = 'variation') AS variations,
                   count(*) FILTER (WHERE changed_at > now() - interval '24 hours') AS changed_24h
              FROM woo.store_product_state
            """
        )
        out["state"] = dict(cursor.fetchone())

        cursor.execute(
            "SELECT rule_id, left(name, 120) AS name FROM woo.price_rule WHERE active ORDER BY priority, rule_id LIMIT 20"
        )
        rule_rows = [dict(r) for r in cursor.fetchall()]
        cursor.execute("SELECT count(*) AS n FROM woo.price_rule WHERE active")
        active_rule_count = int(cursor.fetchone()["n"])
        cursor.execute(
            """
            SELECT count(DISTINCT fdm4_store) AS stores,
                   count(*) FILTER (WHERE style_code = '') AS whole_store,
                   count(*) FILTER (WHERE style_code <> '') AS styles
              FROM woo.sync_exclusion
             WHERE active
            """
        )
        blocks = dict(cursor.fetchone())
        cursor.execute(
            "SELECT fdm4_store, mode FROM woo.store_mix_store WHERE active ORDER BY fdm4_store LIMIT 50"
        )
        mix_rows = [dict(r) for r in cursor.fetchall()]
        cursor.execute("SELECT count(*) AS n FROM woo.store_pricing_tier")
        tiers = int(cursor.fetchone()["n"] or 0)
        out["features"] = {
            "price_rules": {"active": active_rule_count, "rules": rule_rows},
            "sync_blocks": blocks,
            "mix_stores": mix_rows,
            "tier_assignments": tiers,
        }

        cursor.execute(
            """
            SELECT (SELECT count(*) FROM pim.ingest_event) AS events,
                   (SELECT max(received_at) FROM pim.ingest_event) AS latest_event,
                   (SELECT count(*) FROM pim.product_state) AS products
            """
        )
        out["pim"] = dict(cursor.fetchone())

        # The feed registry ships separately; degrade gracefully until the
        # table exists on this database.
        cursor.execute("SELECT to_regclass('woo.feed_consumer') IS NOT NULL AS present")
        feeds_present = bool(cursor.fetchone()["present"])
        consumers: List[Dict[str, Any]] = []
        if feeds_present:
            cursor.execute(
                """
                SELECT name, left(COALESCE(url, ''), 300) AS url, active,
                       last_ping_at, left(COALESCE(last_ping_status, ''), 60) AS last_ping_status,
                       last_pull_at, last_pull_version,
                       left(COALESCE(note, ''), 200) AS note
                  FROM woo.feed_consumer
                 ORDER BY name
                """
            )
            consumers = [dict(r) for r in cursor.fetchall()]
        out["feeds"] = {"available": feeds_present, "consumers": consumers}
    return out


# ---- Shared with the assistant: logo cost, extra customers, default cost,
# sync status and design usage. Same handlers, session/CSRF auth here.


class LogoCostBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store: str = Field(min_length=1, max_length=100)
    design_id: str = Field(min_length=1, max_length=100)
    color_scheme_id: Optional[str] = Field(default=None, max_length=100)
    cost_override: Optional[Decimal] = None
    styles: List[str] = Field(min_length=1, max_length=50)


@router.post("/assignments/logo-cost")
def set_logo_cost(body: LogoCostBody, user: Dict[str, str] = Depends(require_csrf)):
    """One shopper charge (or none) for a logo across the named styles of a store."""
    return _execute_mutation(SetLogoCostCommand.model_validate(body.model_dump()), user)


class ExtraCustomersBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customers: List[str] = Field(default=[], max_length=20)


@router.put("/settings/{store}/extra-customers")
def set_store_extra_customers(store: str, body: ExtraCustomersBody, user: Dict[str, str] = Depends(require_csrf)):
    return _execute_mutation(SetStoreExtraCustomersCommand(store=store, customers=body.customers), user)


class DefaultCostBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logo_code: str = Field(min_length=1, max_length=100)
    color_scheme_id: str = Field(min_length=1, max_length=100)
    cost: Decimal = Field(ge=0)
    locked: bool = True


@router.put("/default-costs")
def set_logo_default_cost(body: DefaultCostBody, user: Dict[str, str] = Depends(require_csrf)):
    return _execute_mutation(SetLogoDefaultCostCommand.model_validate(body.model_dump()), user)


@router.get("/sync-status")
def sync_status(
    store: Optional[str] = Query(default=None, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    """Pipeline liveness (latest pull / reconcile) and, with a store, its
    logo-sync ownership, freezes and recent sync events."""
    del user
    with database.cursor() as cursor:
        return wp_bridge.sync_status_report(cursor, store)


@router.get("/design-usage")
def design_usage(
    store: str = Query(..., min_length=1, max_length=100),
    design_id: str = Query(..., min_length=1, max_length=100),
    color_scheme_id: Optional[str] = Query(default=None, max_length=100),
    user: Dict[str, str] = Depends(require_user),
):
    del user
    return _read_service(read_queries.list_design_usage, store=store, design_id=design_id, color_scheme_id=color_scheme_id)
