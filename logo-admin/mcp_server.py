"""MCP server for the Logo Admin tool (stdio; run on the warehouse box).

Full admin parity for AI-driven operation: every tool calls the FastAPI app's
own routes IN-PROCESS (TestClient), so all validation, orphan cascades, and
audit-trigger attribution behave exactly as they do for a human operator.
Writes are attributed in logo.audit_log to the invoking operator
(ARB_MCP_OPERATOR, exported by mcp-run.sh from the SSH login); with no
identifiable operator the session is "CLI connection", which no apply or
agent allowlist contains.

No network surface: transport is stdio, intended to be launched over SSH via
/opt/arb-logo-admin/mcp-run.sh (root wrapper loads /etc/arb-logo-admin.env).
Nothing in the web UI references this server.
"""

import base64
import binascii
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

import main as app_main
from auth import require_csrf, require_user
from config import get_settings

# The PERSON behind this MCP session. mcp-run.sh exports the invoking Unix
# login (the SSH user that ran `sudo mcp-run.sh`) as ARB_MCP_OPERATOR; every
# authorization tier (CATMGR_APPLY_USERS, CATMGR_VIEW_USERS, agent allowlists)
# and every audit row is evaluated against that login, never against a shared
# process label. Without an identifiable operator the session runs as the
# nondescript "CLI connection", which no allowlist ever contains.
_OPERATOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")


def _operator_login() -> str:
    raw = (os.environ.get("ARB_MCP_OPERATOR") or "").strip().lower()
    if raw and _OPERATOR_RE.match(raw):
        return raw
    return (os.environ.get("ARB_MCP_ACTOR") or "CLI connection")[:100]


ACTOR = _operator_login()


def _identity() -> Dict[str, str]:
    return {"user_login": ACTOR, "display_name": f"{ACTOR} (MCP)", "csrf": "mcp"}


app_main.app.dependency_overrides[require_user] = _identity
app_main.app.dependency_overrides[require_csrf] = _identity

client = TestClient(app_main.app)
client.__enter__()  # run lifespan: opens the logo_admin DB pool

mcp = FastMCP(
    "arb-logo-admin",
    instructions=(
        "Administer the Arborwear warehouse-driven logo system. Stores are "
        "FDM4 codes like S_032813; styles are product style codes like 408045; "
        "garment colors are FDM4 color codes like 0445. An assignment is one "
        "logo on (store, style, color, option_row, position): option_row = one "
        "selectable choice for the customer, position 1-3 = placement slots "
        "within that choice. Also manage store pricing tiers: a tier fills a "
        "store's price from the FDM4 price list only where its catalog price is "
        "blank/0 (real prices are never changed). All logo writes are recorded "
        "in the audit log."
    ),
)


def _call(method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
          json_body: Optional[Dict[str, Any]] = None, files: Any = None,
          data: Optional[Dict[str, Any]] = None, expect_text: bool = False) -> Any:
    response = client.request(method, path, params=params, json=json_body, files=files, data=data)
    if response.status_code >= 300:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]
        raise RuntimeError(f"{method} {path} -> HTTP {response.status_code}: {detail}")
    return response.text if expect_text else response.json()



@mcp.tool()
def get_product_state(store: str, style: Optional[str] = None, sku: Optional[str] = None, limit: int = 500) -> Any:
    """Read get product state through the shared authenticated route."""
    params = {"store": store, "style": style, "sku": sku, "limit": limit}
    return _call("GET", "/api/product-state", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def get_change_history(store: Optional[str] = None, style: Optional[str] = None, logo_code: Optional[str] = None, rule_id: Optional[int] = None, since_days: int = 7, actor: Optional[str] = None, limit: int = 100) -> Any:
    """Read get change history through the shared authenticated route."""
    params = {"store": store, "style": style, "logo_code": logo_code, "rule_id": rule_id, "since_days": since_days, "actor": actor, "limit": limit}
    return _call("GET", "/api/change-history", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def get_stock(style: str, color_code: Optional[str] = None, size_code: Optional[str] = None) -> Any:
    """Read get stock through the shared authenticated route."""
    params = {"style": style, "color_code": color_code, "size_code": size_code}
    return _call("GET", "/api/stock", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def audit_store_prices(store: str, limit: int = 50) -> Any:
    """Read audit store prices through the shared authenticated route."""
    params = {"store": store, "limit": limit}
    return _call("GET", "/api/store-price-audit", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def wp_product_check(store: str, style: Optional[str] = None, sku: Optional[str] = None) -> Any:
    """Read wp product check through the shared authenticated route."""
    params = {"store": store, "style": style, "sku": sku}
    return _call("GET", "/api/wordpress/product-check", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def wp_store_check(store: str) -> Any:
    """Read wp store check through the shared authenticated route."""
    params = {"store": store}
    return _call("GET", "/api/wordpress/store-check", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def get_order_status(order_id: int, store: Optional[str] = None, blog_id: Optional[int] = None) -> Any:
    """Read get order status through the shared authenticated route."""
    params = {"order_id": order_id, "store": store, "blog_id": blog_id}
    return _call("GET", "/api/order-status", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def find_issues(store: Optional[str] = None, checks: Optional[List[str]] = None, limit: int = 50) -> Any:
    """Read find issues through the shared authenticated route."""
    params = {"store": store, "checks": checks, "limit": limit}
    return _call("GET", "/api/issues", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def explain_product(store: str, style: str) -> Any:
    """Read explain product through the shared authenticated route."""
    params = {"store": store, "style": style}
    return _call("GET", "/api/product-explanation", params={k: v for k, v in params.items() if v is not None})

# ---------------------------------------------------------------- read tools

@mcp.tool()
def list_stores() -> Any:
    """List all stores (FDM4 code, display name, catalog, product counts)."""
    return _call("GET", "/api/stores")


@mcp.tool()
def list_styles(store: str, q: str = "", active_only: bool = True, assigned_only: bool = True) -> Any:
    """Search a store's product styles. active_only limits to the live FDM4
    catalog; assigned_only limits to styles that already have logo rows."""
    return _call("GET", "/api/styles", params={
        "store": store, "q": q, "active_only": active_only, "assigned_only": assigned_only,
    })


@mcp.tool()
def get_style(store: str, style: str) -> Any:
    """Full logo grid for one style: garment colors, every assignment
    (option rows x positions 1-3), and store settings."""
    return _call("GET", "/api/style", params={"store": store, "style": style})


@mcp.tool()
def find_similar_styles(fdm4_store: str, product_style: str, mode: str = "exact") -> Any:
    """Styles of a store whose logo set (distinct design/scheme/position/
    location tuples over active assignments, any garment color) matches the
    given style: mode='exact' = identical set, mode='overlap' = shares at
    least one tuple, with shared/only_in_source/only_in_target counts."""
    return _call("GET", "/api/styles/similar", params={
        "store": fdm4_store, "style": product_style, "mode": mode,
    })


@mcp.tool()
def store_logo_coverage(fdm4_store: str, unconfigured_only: bool = True) -> Any:
    """Per live style of a store: colors_total, colors_configured (>= 1 active
    logo) and the unconfigured color codes. unconfigured_only=True lists only
    styles with at least one color lacking logos."""
    return _call("GET", "/api/styles/coverage", params={
        "store": fdm4_store, "unconfigured_only": unconfigured_only,
    })


@mcp.tool()
def fill_gaps_preview(fdm4_store: str, styles: Optional[List[str]] = None) -> Any:
    """Plan for copying each style's own configured logos onto its logo-less
    colors: `copyable` styles (with auto_source when every configured color
    matches, else needs_choice + per-color sources) and `no_source` styles
    (no logos anywhere). Read-only."""
    body: Dict[str, Any] = {"store": fdm4_store}
    if styles:
        body["styles"] = styles
    return _call("POST", "/api/styles/fill-gaps/preview", json_body=body)


@mcp.tool()
def fill_gaps_execute(fdm4_store: str, entries: List[Dict[str, Any]],
                      overwrite: bool = False) -> Any:
    """Fill logo-less colors from each style's own source color. entries =
    [{style, source_color, colors?}] (colors defaults to every logo-less
    color of the style). Occupied slots are skipped unless overwrite. One
    journal batch for the whole run - undo via bulk_apply_undo."""
    return _call("POST", "/api/styles/fill-gaps", json_body={
        "store": fdm4_store, "entries": entries, "overwrite": overwrite,
    })


@mcp.tool()
def search_designs(q: str = "", store: Optional[str] = None) -> Any:
    """Search FDM4 designs by description/id/logo code. With a store and empty
    q, browses designs used by that store and its owning FDM4 customers."""
    params: Dict[str, Any] = {"q": q}
    if store:
        params["store"] = store
    return _call("GET", "/api/designs", params=params)


@mcp.tool()
def get_design(design_id: str) -> Any:
    """Design detail: color schemes (colorways), FDM4 art assets, placements."""
    return _call("GET", f"/api/designs/{design_id}")


@mcp.tool()
def get_vocab() -> Any:
    """Placement vocabulary (canonical FDM4 names + ad-hoc in-use values with
    usage counts) and background classes (lb-white / lb-black)."""
    return _call("GET", "/api/vocab")


@mcp.tool()
def get_store_settings(store: str) -> Any:
    """Store-level logo settings: enabled, allows_none ('No logo' choice)."""
    return _call("GET", f"/api/settings/{store}")


@mcp.tool()
def get_import_report(store: Optional[str] = None, reason: Optional[str] = None,
                      limit: int = 100, offset: int = 0) -> Any:
    """Punch list: legacy sheet rows that could not be imported, with reasons
    (no_color_code, no_design, no_art, orphaned_companion, ...)."""
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if store:
        params["store"] = store
    if reason:
        params["reason"] = reason
    return _call("GET", "/api/import-report", params=params)


@mcp.tool()
def get_audit_log(store: Optional[str] = None, style: Optional[str] = None,
                  actor: Optional[str] = None, action: Optional[str] = None,
                  before_id: Optional[int] = None, limit: int = 50) -> Any:
    """Read-only change history (who changed what, when; field-level diffs).
    Keyset paging via before_id from the previous response."""
    params: Dict[str, Any] = {"limit": limit}
    for key, value in (("store", store), ("style", style), ("actor", actor), ("action", action)):
        if value:
            params[key] = value
    if before_id is not None:
        params["before_id"] = before_id
    return _call("GET", "/api/audit-log", params=params)


@mcp.tool()
def export_assignments_csv(store: Optional[str] = None, style: Optional[str] = None) -> str:
    """Export logo assignments as CSV text (round-trips with import_assignments_csv)."""
    params = {k: v for k, v in (("store", store), ("style", style)) if v}
    return _call("GET", "/api/export", params=params or None, expect_text=True)


@mcp.tool()
def get_product_link(store: str, style: str) -> Any:
    """The WordPress front-end + admin URLs for a style's product on the sync
    target (dev now, prod after cutover). Soft-fails if WordPress is unreachable."""
    return _call("GET", "/api/product-link", params={"store": store, "style": style})


@mcp.tool()
def export_audit_log_csv(store: Optional[str] = None, style: Optional[str] = None,
                         actor: Optional[str] = None, action: Optional[str] = None) -> str:
    """CSV export of the change history, honoring the same filters as
    get_audit_log. Returns CSV text."""
    params = {k: v for k, v in (("store", store), ("style", style),
                                ("actor", actor), ("action", action)) if v}
    return _call("GET", "/api/audit-log/export", params=params or None, expect_text=True)


@mcp.tool()
def list_pricing_tiers() -> Any:
    """List available pricing tiers (tier_name, price-level key, is_msrp,
    sort_order). A store on an is_msrp tier gets NO override (behaves as
    unconfigured). Pair with list_store_tiers / set_store_tier."""
    return _call("GET", "/api/pricing/tiers")


@mcp.tool()
def list_store_tiers() -> Any:
    """List every store's pricing-tier assignment (store code, tier, note,
    updated_at, display name). A tier fills a store's price from the FDM4 price
    list ONLY where the catalog price is blank/0; real prices are never changed."""
    return _call("GET", "/api/pricing/store-tiers")


# --------------------------------------------------------------- write tools

@mcp.tool()
def save_assignment(fdm4_store: str, product_style: str, garment_color_code: str,
                    position: int, design_id: str, logo_code: str, color_scheme_id: str,
                    option_row: int = 1, location: str = "", optional: bool = False,
                    background: str = "", cost_override: Optional[float] = None,
                    sort_order: int = 0, image_url: str = "",
                    name_override: Optional[str] = None,
                    expected_updated_at: Optional[str] = None,
                    active: bool = True) -> Any:
    """Create or update one logo assignment. position 2/3 requires an active
    position-1 in the same option_row. Validated against the FDM4 warehouse
    (design, scheme, logo code, store/style/color)."""
    body = {
        "fdm4_store": fdm4_store, "product_style": product_style,
        "garment_color_code": garment_color_code, "position": position,
        "option_row": option_row, "design_id": design_id, "logo_code": logo_code,
        "color_scheme_id": color_scheme_id, "location": location, "optional": optional,
        "background": background, "cost_override": cost_override,
        "sort_order": sort_order, "image_url": image_url, "active": active,
    }
    if name_override is not None:
        body["name_override"] = name_override
    if expected_updated_at is not None:
        body["expected_updated_at"] = expected_updated_at
    return _call("PUT", "/api/assignments", json_body=body)


@mcp.tool()
def delete_assignment(fdm4_store: str, product_style: str, garment_color_code: str,
                      position: int, option_row: int = 1, hard: bool = False) -> Any:
    """Deactivate (hard=False) or permanently delete (hard=True) one assignment.
    Removing position 1 cascades to its option row's companions."""
    return _call("DELETE", "/api/assignments", params={
        "fdm4_store": fdm4_store, "product_style": product_style,
        "garment_color_code": garment_color_code, "position": position,
        "option_row": option_row, "hard": hard,
    })


@mcp.tool()
def clear_color(fdm4_store: str, product_style: str, garment_color_code: str,
                hard: bool = True) -> Any:
    """Clear a whole color channel: every option row on (store, style, color).
    hard=True deletes; hard=False deactivates. Use for retiring old data."""
    return _call("DELETE", "/api/assignments-by-color", params={
        "fdm4_store": fdm4_store, "product_style": product_style,
        "garment_color_code": garment_color_code, "hard": hard,
    })


@mcp.tool()
def set_style_active(store: str, style: str, active: bool) -> Any:
    """Bulk-activate or deactivate every assignment on a style."""
    return _call("POST", "/api/style-active", json_body={"store": store, "style": style, "active": active})


@mcp.tool()
def apply_to_all_colors(store: str, style: str, garment_color_code: str, position: int,
                        option_row: int = 1, overwrite: bool = False) -> Any:
    """Copy one assignment to every live color of the style (same row/position).
    Occupied slots are preserved unless overwrite=True."""
    return _call("POST", "/api/apply-all-colors", json_body={
        "store": store, "style": style, "garment_color_code": garment_color_code,
        "position": position, "option_row": option_row, "overwrite": overwrite,
    })


@mcp.tool()
def copy_style(store: str, source_style: str, target_style: str, overwrite: bool = False) -> Any:
    """Copy a style's whole logo configuration onto another style in the same
    store (matching color codes only)."""
    return _call("POST", "/api/copy-style", json_body={
        "store": store, "source_style": source_style, "target_style": target_style,
        "overwrite": overwrite,
    })


@mcp.tool()
def update_store_settings(store: str, enabled: bool, allows_none: bool) -> Any:
    """Set store-wide logo behavior: enabled (project logos at all) and
    allows_none (customers may pick 'No logo')."""
    return _call("PUT", f"/api/settings/{store}", json_body={"enabled": enabled, "allows_none": allows_none})


@mcp.tool()
def set_store_tier(fdm4_store: str, tier_name: str, note: str = "") -> Any:
    """Assign a store a pricing tier - the fallback used ONLY when FDM4's catalog
    price is blank/0 (real catalog prices are never overridden). tier_name must be
    one returned by list_pricing_tiers (e.g. 'Level 3 (Corp 3)', 'MSRP'). Validated
    against the store list and tier list."""
    return _call("PUT", "/api/pricing/store-tier", json_body={
        "fdm4_store": fdm4_store, "tier_name": tier_name, "note": note,
    })


@mcp.tool()
def delete_store_tier(fdm4_store: str) -> Any:
    """Remove a store's pricing-tier assignment. Its blank-catalog items revert
    to the retail (MSRP) fallback."""
    return _call("DELETE", "/api/pricing/store-tier", params={"fdm4_store": fdm4_store})


@mcp.tool()
def sync_to_wordpress(store: str, styles: Optional[List[str]] = None) -> Any:
    """Push a store's logo configuration to WordPress (rebuilds the design map,
    reconciles product_logos). styles=[] or omitted syncs every in-scope style.
    WordPress refuses stores not switched to warehouse ownership (HTTP 409)."""
    return _call("POST", "/api/sync", json_body={"store": store, "styles": styles or []})


@mcp.tool()
def import_assignments_csv(csv_text: str, store: str = "") -> Any:
    """Import assignments from CSV text (the export_assignments_csv format).
    Invalid rows go to the punch list; valid rows import."""
    return _call("POST", "/api/import", data={"store": store},
                 files={"file": ("import.csv", csv_text.encode("utf-8"), "text/csv")})


@mcp.tool()
def import_legacy_ndjson(server_path: str, preserve_manual: bool = True) -> Any:
    """Re-run the legacy sheet import from an NDJSON file already on the
    warehouse box (produced by `wp arb_product_sync logo-export-current`).
    preserve_manual keeps rows edited in the app since the last import."""
    import_root = Path(
        os.environ.get("MCP_IMPORT_DIR", "/var/lib/arb-logo-admin/imports")
    ).resolve(strict=True)
    requested = Path(server_path)
    path = requested if requested.is_absolute() else import_root / requested
    if path.is_symlink():
        raise RuntimeError("Legacy import path must not be a symbolic link")
    try:
        path = path.resolve(strict=True)
        path.relative_to(import_root)
    except (FileNotFoundError, ValueError):
        raise RuntimeError(
            "Legacy import path must be a file inside MCP_IMPORT_DIR"
        ) from None
    if not path.is_file():
        raise RuntimeError("Legacy import path must be a regular file")
    with path.open("rb") as fh:
        return _call("POST", "/api/legacy-import",
                     data={"preserve_manual": "true" if preserve_manual else "false"},
                     files={"file": (path.name, fh, "application/x-ndjson")})


@mcp.tool()
def mirror_legacy_images(limit: int = 50) -> Any:
    """Mirror a batch of legacy media-server logo images into warehouse-owned
    storage (SSRF-guarded; idempotent; repoints assignments). Call repeatedly
    until it reports nothing left to mirror."""
    return _call("POST", "/api/legacy-import-images", data={"limit": str(limit)})


@mcp.tool()
def upload_image(content_base64: str, filename: str) -> Any:
    """Upload a logo image (PNG/JPEG/WebP/GIF, base64-encoded). Returns the
    public media URL to use as an assignment's image_url. Publishing to the
    media server happens automatically within a minute."""
    max_bytes = get_settings().max_upload_bytes
    encoded_limit = ((max_bytes + 2) // 3) * 4
    if len(content_base64) > encoded_limit:
        raise RuntimeError("Base64 image exceeds MAX_UPLOAD_BYTES")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("Image content is not valid base64") from exc
    if len(data) > max_bytes:
        raise RuntimeError("Decoded image exceeds MAX_UPLOAD_BYTES")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif",
    }.get(extension, "image/png")
    return _call("POST", "/api/upload", files={"file": (filename, data, mime)})


@mcp.tool()
def list_colors(q: str = "", cls: str = "", needs_review: bool = False,
                limit: int = 200, offset: int = 0) -> Any:
    """List/search garment color light/dark classifications. q matches color
    code or name; cls filters by class (dark/light); needs_review=True returns
    only colors not yet classified."""
    return _call("GET", "/api/colors", params={
        "q": q, "cls": cls, "needs_review": needs_review,
        "limit": limit, "offset": offset,
    })


@mcp.tool()
def set_color_class(color_code: str, light_dark: str) -> Any:
    """Set a garment color's light/dark classification and mark it manual so
    automatic re-classification will not overwrite it."""
    return _call("PUT", "/api/colors", json_body={
        "color_code": color_code, "light_dark": light_dark,
    })


@mcp.tool()
def bulk_apply_preview(fdm4_store: str, logo_code: str, color_scheme: str,
                       target: Dict[str, Any],
                       style_codes: Optional[List[str]] = None) -> Any:
    """Dry-run: which products a logo variant would apply to in one store.
    target must have mode='light_dark' (with class='dark'|'light') or
    mode='colors' (with color_codes=[...]). style_codes narrows to specific
    styles. Returns rows (style_code, color_code, was, new) and counts."""
    body: Dict[str, Any] = {
        "fdm4_store": fdm4_store, "logo_code": logo_code,
        "color_scheme": color_scheme, "target": target,
    }
    if style_codes is not None:
        body["style_codes"] = style_codes
    return _call("POST", "/api/bulk-apply/preview", json_body=body)


@mcp.tool()
def bulk_apply_execute(fdm4_store: str, logo_code: str, color_scheme: str,
                       placement: str,
                       rows: List[Dict[str, str]]) -> Any:
    """Apply a logo variant to selected products in one store (undoable).
    rows is a list of {style_code, color_code} dicts - typically the rows
    returned by bulk_apply_preview. Returns batch_id for undo."""
    return _call("POST", "/api/bulk-apply/execute", json_body={
        "fdm4_store": fdm4_store, "logo_code": logo_code,
        "color_scheme": color_scheme, "placement": placement,
        "rows": rows,
    })


@mcp.tool()
def bulk_apply_undo(batch_id: int) -> Any:
    """Undo a bulk-apply batch by batch_id (from bulk_apply_execute). Restores
    every assignment to its pre-batch state. Raises 409 if already undone."""
    return _call("POST", "/api/bulk-apply/undo", json_body={"batch_id": batch_id})


# -------------------------------------------------------- editor batch tools
# Reorder / paste / copy-to-many / design swap: journaled like bulk apply,
# undone through bulk_apply_undo(batch_id).

@mcp.tool()
def reorder_option_rows(fdm4_store: str, product_style: str, garment_color_code: str,
                        option_rows: List[int], apply_to: str = "color") -> Any:
    """Reorder the selectable logo rows on one color (the storefront order
    follows). apply_to='style' applies the same order to every color of the
    style that has the same rows. Undo through bulk_apply_undo(batch_id)."""
    return _call("POST", "/api/assignments/reorder", json_body={
        "store": fdm4_store, "style": product_style,
        "garment_color_code": garment_color_code,
        "option_rows": option_rows, "apply_to": apply_to,
    })


@mcp.tool()
def set_style_color_order(product_style: str, colors: List[str]) -> Any:
    """Editor-only order of garment colors in one style's logo grid (all
    stores). Full replace; an empty list restores alphabetical order."""
    return _call("PUT", "/api/style-color-order", json_body={
        "style": product_style, "colors": colors,
    })


@mcp.tool()
def paste_assignments(fdm4_store: str, product_style: str, colors: List[str],
                      rows: List[Dict[str, Any]], overwrite: bool = False,
                      as_new_rows: bool = False) -> Any:
    """Paste clipboard rows (option_row, position, design_id, logo_code,
    color_scheme_id, location, optional, background, cost_override,
    sort_order, image_url, name_override, active) onto the listed colors of one
    style. Each row is validated like a manual save; invalid rows are
    reported. Undo through bulk_apply_undo(batch_id)."""
    return _call("POST", "/api/assignments/paste", json_body={
        "store": fdm4_store, "style": product_style, "colors": colors,
        "rows": rows, "overwrite": overwrite, "as_new_rows": as_new_rows,
    })


@mcp.tool()
def paste_assignments_batch(fdm4_store: str, styles: List[str], rows: List[Dict[str, Any]],
                            color_scope: str = "match", match_color: Optional[str] = None,
                            overwrite: bool = False, as_new_rows: bool = False) -> Any:
    """Paste the same rows onto many styles of one store. color_scope: match
    (only match_color), all, light or dark (logo.color_class). One undoable
    batch; per-style problems are reported in results."""
    body = {"store": fdm4_store, "styles": styles, "rows": rows, "color_scope": color_scope,
            "overwrite": overwrite, "as_new_rows": as_new_rows}
    if match_color is not None:
        body["match_color"] = match_color
    return _call("POST", "/api/assignments/paste-batch", json_body=body)


@mcp.tool()
def set_styles_active(fdm4_store: str, styles: List[str], active: bool) -> Any:
    """Activate or deactivate every valid assignment on many styles at once."""
    return _call("POST", "/api/style-active-batch", json_body={
        "store": fdm4_store, "styles": styles, "active": active,
    })


@mcp.tool()
def copy_style_batch_preview(fdm4_store: str, source_style: str, target_styles: List[str],
                             color_match: str = "exact") -> Any:
    """Plan copying one style's logos to many styles. color_match 'exact'
    copies colors the target shares with the source; 'like' also maps the
    rest by light/dark class. Read-only."""
    return _call("POST", "/api/copy-style-batch/preview", json_body={
        "store": fdm4_store, "source_style": source_style,
        "target_styles": target_styles, "color_match": color_match,
    })


@mcp.tool()
def copy_style_batch(fdm4_store: str, source_style: str, target_styles: List[str],
                     color_match: str = "exact", mode: str = "merge") -> Any:
    """Copy one style's logos to many styles in one undoable batch. mode:
    merge (skip occupied), overwrite, or replace (clear each mapped color
    first). Undo through bulk_apply_undo(batch_id)."""
    return _call("POST", "/api/copy-style-batch", json_body={
        "store": fdm4_store, "source_style": source_style,
        "target_styles": target_styles, "color_match": color_match, "mode": mode,
    })


def _design_swap_body(fdm4_store: str, from_design_id: str, to_design_id: str,
                      to_color_scheme_id: str, from_color_scheme_id: Optional[str],
                      to_logo_code: Optional[str], styles: Optional[List[str]]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "store": fdm4_store, "from_design_id": from_design_id,
        "to_design_id": to_design_id, "to_color_scheme_id": to_color_scheme_id,
    }
    if from_color_scheme_id is not None:
        body["from_color_scheme_id"] = from_color_scheme_id
    if to_logo_code is not None:
        body["to_logo_code"] = to_logo_code
    if styles is not None:
        body["styles"] = styles
    return body


@mcp.tool()
def design_swap_preview(fdm4_store: str, from_design_id: str, to_design_id: str,
                        to_color_scheme_id: str, from_color_scheme_id: Optional[str] = None,
                        to_logo_code: Optional[str] = None,
                        styles: Optional[List[str]] = None) -> Any:
    """Dry run of replacing a design across one store: every assignment on
    from_design_id (all schemes, or only from_color_scheme_id) with the
    verdict (ok / unchanged / invalid + reason) it would get on to_design_id /
    to_color_scheme_id. to_logo_code is derived from FDM4 art when the scheme
    has exactly one code on file; otherwise it is required. styles narrows."""
    return _call("POST", "/api/design-swap/preview", json_body=_design_swap_body(
        fdm4_store, from_design_id, to_design_id, to_color_scheme_id,
        from_color_scheme_id, to_logo_code, styles))


@mcp.tool()
def design_swap(fdm4_store: str, from_design_id: str, to_design_id: str,
                to_color_scheme_id: str, from_color_scheme_id: Optional[str] = None,
                to_logo_code: Optional[str] = None,
                styles: Optional[List[str]] = None) -> Any:
    """Replace a design across one store (same arguments as
    design_swap_preview). Invalid rows are skipped and reported, never fatal.
    Placement, price, order and name overrides are kept; the storefront image
    is reused from the store's newest image for the new design/scheme or
    cleared. Undo through bulk_apply_undo(batch_id)."""
    return _call("POST", "/api/design-swap", json_body=_design_swap_body(
        fdm4_store, from_design_id, to_design_id, to_color_scheme_id,
        from_color_scheme_id, to_logo_code, styles))


@mcp.tool()
def list_logo_names(q: str = "", limit: int = 50, offset: int = 0) -> Any:
    """List/search the customer-facing logo names (logo.display_name), one row
    per (design_id, color scheme). Search matches name, design id, color scheme,
    or logo code. 'source' is pool_exact/pool_design/filename/code/manual;
    'locked' rows were hand-edited and are protected from re-pull."""
    return _call("GET", "/api/logo-names", params={"q": q, "limit": limit, "offset": offset})


@mcp.tool()
def set_logo_name(design_id: str, color_scheme_id: str, name: str, fdm4_store: str = "") -> Any:
    """Set the shopper-facing name for one (design, color scheme): the shared
    default (fdm4_store '') or one store's own name. Marks the row manual +
    locked so a later FDM4 re-pull will not overwrite it. Takes effect on the
    next store logo sync."""
    return _call("PUT", "/api/logo-names", json_body={
        "design_id": design_id, "color_scheme_id": color_scheme_id, "name": name,
        "fdm4_store": fdm4_store,
    })


@mcp.tool()
def repull_logo_name(design_id: str, force: bool = False) -> Any:
    """Re-pull a design's names from FDM4 design_pool (use after FDM4 updates a
    description). Locked/hand-edited rows are preserved unless force=True.
    Returns how many rows changed and the refreshed rows."""
    return _call("POST", "/api/logo-names/repull", json_body={
        "design_id": design_id, "force": force,
    })


# ------------------------------------------------------- category editor tools


@mcp.tool()
def cat_targets() -> Any:
    """List the category editor's configured WordPress environments (dev/prod)
    with their hosts. The category editor is env-scoped: every other cat_*
    tool takes one of these env values. 404 = the feature is disabled
    (CATMGR_ENABLED)."""
    return _call("GET", "/api/categories/targets")


@mcp.tool()
def cat_snapshot_status(env: str) -> Any:
    """Per-blog category snapshot status for one environment: version,
    imported_at, term/membership counts. A blog absent here has never been
    imported."""
    return _call("GET", "/api/categories/snapshots", params={"env": env})


@mcp.tool()
def cat_list_blogs(env: str) -> Any:
    """Live blog list from the target WordPress environment (blog_id, path,
    store name). Use to choose blog_ids for cat_snapshot_import."""
    return _call("GET", "/api/categories/blogs", params={"env": env})


@mcp.tool()
def cat_wp_status(env: str) -> Any:
    """Target WordPress preflight: freeze flag, Redirection/WP Rocket
    presence, WP version."""
    return _call("GET", "/api/categories/wp-status", params={"env": env})


@mcp.tool()
def cat_snapshot_import(env: str, blog_ids: List[int]) -> Any:
    """Import (or re-import) live category snapshots for the given blogs from
    the target WordPress environment. Full-replace per blog + version bump;
    READS WordPress, never changes it. Returns per-blog results; a failed
    blog does not stop the rest."""
    return _call("POST", "/api/categories/snapshots/import", json_body={
        "env": env, "blog_ids": blog_ids,
    })


@mcp.tool()
def cat_tree_get(blog_id: Optional[int] = None) -> Any:
    """The draft category tree (flat node list; build hierarchy by parent_id).
    With blog_id: that store's EFFECTIVE tree - global nodes minus its
    excludes, renames applied, store-local extra nodes appended - plus the
    store's override rows."""
    if blog_id is None:
        return _call("GET", "/api/categories/tree")
    return _call("GET", "/api/categories/tree/effective", params={"blog_id": blog_id})


@mcp.tool()
def cat_node_create(name: str, parent_id: Optional[int] = None,
                    slug: Optional[str] = None, description: str = "",
                    position: Optional[int] = None) -> Any:
    """Create a draft category node. slug defaults to a normalized form of the
    name (unique; -2 suffix on clash). position = index among siblings
    (default: append)."""
    body: Dict[str, Any] = {"name": name, "parent_id": parent_id,
                            "description": description}
    if slug is not None:
        body["slug"] = slug
    if position is not None:
        body["position"] = position
    return _call("POST", "/api/categories/nodes", json_body=body)


@mcp.tool()
def cat_node_update(node_id: int, name: Optional[str] = None,
                    slug: Optional[str] = None,
                    description: Optional[str] = None) -> Any:
    """Rename / re-slug / re-describe a draft node. Only provided fields
    change. Slug collisions return 409."""
    body: Dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if slug is not None:
        body["slug"] = slug
    if description is not None:
        body["description"] = description
    return _call("PUT", f"/api/categories/nodes/{node_id}", json_body=body)


@mcp.tool()
def cat_node_move(node_id: int, parent_id: Optional[int],
                  position: Optional[int] = None) -> Any:
    """Move a draft node under a new parent (None = top level) at the given
    sibling index. Refuses cycles."""
    return _call("POST", f"/api/categories/nodes/{node_id}/move", json_body={
        "parent_id": parent_id, "position": position,
    })


@mcp.tool()
def cat_node_delete(node_id: int, cascade: bool = False) -> Any:
    """Delete a draft node. Refuses when it has children unless cascade=True
    (which deletes the whole subtree). Store overrides on deleted nodes are
    removed automatically."""
    return _call("DELETE", f"/api/categories/nodes/{node_id}",
                 params={"cascade": cascade})


@mcp.tool()
def cat_draft_seed(env: str, blog_id: int, force: bool = False) -> Any:
    """Replace the draft tree with one blog's imported snapshot (usually
    prod blog 1) so editing starts from live reality. Refuses when the draft
    is non-empty unless force=True (force also clears all store overrides)."""
    return _call("POST", "/api/categories/draft/seed", json_body={
        "env": env, "blog_id": blog_id, "force": force,
    })


@mcp.tool()
def cat_overrides_list(blog_id: Optional[int] = None) -> Any:
    """List per-store overrides (extra_node / rename / exclude), optionally
    for one blog."""
    params = {} if blog_id is None else {"blog_id": blog_id}
    return _call("GET", "/api/categories/overrides", params=params)


@mcp.tool()
def cat_override_set(blog_id: int, kind: str, node_id: Optional[int] = None,
                     name: Optional[str] = None, slug: Optional[str] = None,
                     parent_node_id: Optional[int] = None,
                     include_descendants: bool = True, sort_order: int = 0,
                     override_id: Optional[int] = None) -> Any:
    """Create or update one store override. kind: extra_node (store-local
    category; name required, parent_node_id = graft point), rename (node_id +
    name), exclude (node_id; include_descendants hides the subtree). Pass
    override_id to update an existing row."""
    return _call("PUT", "/api/categories/overrides", json_body={
        "override_id": override_id, "blog_id": blog_id, "kind": kind,
        "node_id": node_id, "name": name, "slug": slug,
        "parent_node_id": parent_node_id,
        "include_descendants": include_descendants, "sort_order": sort_order,
    })


@mcp.tool()
def cat_override_delete(override_id: int) -> Any:
    """Delete one store override by id."""
    return _call("DELETE", f"/api/categories/overrides/{override_id}")


@mcp.tool()
def cat_mapping_status(env: str) -> Any:
    """The slug-map workbench data for one environment: every live slug with
    its blog/product counts and current disposition (map / delete /
    store_custom), plus a progress summary. Preview cannot run until every
    slug has a disposition."""
    return _call("GET", "/api/categories/mapping", params={"env": env})


@mcp.tool()
def cat_mapping_suggest(env: str) -> Any:
    """Auto-match proposals for unmapped slugs whose NAME matches exactly one
    draft node. Slugs whose name matches several nodes come back under
    `ambiguous` with every candidate's full path and must be mapped
    explicitly. Nothing is persisted - accept via cat_mapping_bulk."""
    return _call("GET", "/api/categories/mapping/suggest", params={"env": env})


@mcp.tool()
def cat_mapping_set(old_slug: str, action: str,
                    target_node_id: Optional[int] = None,
                    is_primary: Optional[bool] = None,
                    note: str = "") -> Any:
    """Set one live slug's disposition. action=map requires target_node_id
    (first map into a node auto-becomes primary = the in-place survivor
    keeping its term_id); delete = debris; store_custom = preserved as a
    store-local category."""
    return _call("PUT", "/api/categories/mapping", json_body={"rows": [{
        "old_slug": old_slug, "action": action,
        "target_node_id": target_node_id, "is_primary": is_primary,
        "note": note,
    }]})


@mcp.tool()
def cat_mapping_bulk(rows: List[Dict[str, Any]]) -> Any:
    """Bulk slug dispositions (up to 500 rows of
    {old_slug, action, target_node_id?, is_primary?, note?}); per-row results,
    failures don't stop the rest."""
    return _call("PUT", "/api/categories/mapping", json_body={"rows": rows})


@mcp.tool()
def cat_mapping_clear(old_slug: str) -> Any:
    """Remove a slug's disposition (back to unmapped)."""
    from urllib.parse import quote
    return _call("DELETE", f"/api/categories/mapping/{quote(old_slug, safe='')}")


@mcp.tool()
def cat_rules_list(node_id: Optional[int] = None) -> Any:
    """List assignment rules (optionally for one node)."""
    params = {} if node_id is None else {"node_id": node_id}
    return _call("GET", "/api/categories/rules", params=params)


@mcp.tool()
def cat_rule_evaluate(env: str, spec: Dict[str, Any], limit: int = 50) -> Any:
    """Dry-run a rule spec against the env's product universe:
    {from: 'all'|[old_slug,...], field: name|brand|mill_code|category|sku,
    op: equals|prefix|regex, value}. Returns match count + sample skus."""
    return _call("POST", "/api/categories/rules/evaluate", json_body={
        "env": env, "spec": spec, "limit": limit,
    })


@mcp.tool()
def cat_rule_set(node_id: int, spec: Dict[str, Any], priority: int = 0,
                 note: str = "", rule_id: Optional[int] = None) -> Any:
    """Create/update an assignment rule on a node (spec as in
    cat_rule_evaluate). Rule matches join the node's membership."""
    return _call("PUT", "/api/categories/rules", json_body={
        "rule_id": rule_id, "node_id": node_id, "spec": spec,
        "priority": priority, "note": note,
    })


@mcp.tool()
def cat_rule_delete(rule_id: int) -> Any:
    """Delete an assignment rule."""
    return _call("DELETE", f"/api/categories/rules/{rule_id}")


@mcp.tool()
def cat_assignments_list(node_id: int) -> Any:
    """Explicit style-level add/remove assignments on one node."""
    return _call("GET", "/api/categories/assignments", params={"node_id": node_id})


@mcp.tool()
def cat_assign(node_id: int, skus: List[str], mode: str = "add",
               source: str = "manual", note: str = "") -> Any:
    """Add or remove styles (SKUs) on a node explicitly. Opposite-mode rows
    for the same skus are replaced."""
    return _call("PUT", "/api/categories/assignments", json_body={
        "node_id": node_id, "skus": skus, "mode": mode, "source": source,
        "note": note,
    })



@mcp.tool()
def cat_assignment_delete(assignment_id: int) -> Any:
    """Delete one explicit product assignment row (add or remove) by id, as
    listed by cat_assignments_list - the way to undo a mistaken cat_assign."""
    return _call("DELETE", f"/api/categories/assignments/{int(assignment_id)}")

@mcp.tool()
def cat_membership(env: str, node_id: int) -> Any:
    """A node's effective style membership in one env:
    carried (from mapped old slugs) ∪ rule matches ∪ adds − removes,
    with per-source counts and a sample."""
    return _call("GET", "/api/categories/membership",
                 params={"env": env, "node_id": node_id})


@mcp.tool()
def cat_preview(env: str, blog_ids: Optional[List[int]] = None) -> Any:
    """Full migration preview for one environment (all snapshotted blogs, or a
    subset): blockers (unmapped slugs, slug collisions, zero-category styles),
    warnings (code items, blog-1 slug changes, redirect count), per-blog stats
    and totals. ok=true means apply is possible. Read-only."""
    return _call("POST", "/api/categories/preview", json_body={
        "env": env, "blog_ids": blog_ids,
    })


@mcp.tool()
def cat_preview_blog(env: str, blog_id: int) -> Any:
    """One blog's full declarative plan: term updates (in-place, fenced by
    expected_slug), creates, deletes (merge/delete/excluded), membership
    changes (per product final slug sets), redirects (blog 1). This exact
    payload is what apply will converge WordPress toward."""
    return _call("GET", "/api/categories/preview/blog",
                 params={"env": env, "blog_id": blog_id})


@mcp.tool()
def cat_ack_list() -> Any:
    """List intentionally-uncategorized acknowledgements (skus excluded from
    the zero-category blocker)."""
    return _call("GET", "/api/categories/uncategorized-ack")


@mcp.tool()
def cat_ack_set(skus: List[str], note: str = "") -> Any:
    """Acknowledge styles as intentionally uncategorized (converts their
    zero-category blocker into a warning)."""
    return _call("PUT", "/api/categories/uncategorized-ack", json_body={
        "skus": skus, "note": note,
    })


@mcp.tool()
def cat_ack_delete(sku: str) -> Any:
    """Remove an intentionally-uncategorized acknowledgement."""
    return _call("DELETE", f"/api/categories/uncategorized-ack/{sku}")


@mcp.tool()
def cat_runs(env: Optional[str] = None) -> Any:
    """List apply runs (newest first), optionally for one environment."""
    params = {} if env is None else {"env": env}
    return _call("GET", "/api/categories/runs", params=params)


@mcp.tool()
def cat_run_status(run_id: int) -> Any:
    """One run with its per-blog jobs (status, attempts, progress, stats,
    results, pre-apply snapshot presence)."""
    return _call("GET", f"/api/categories/runs/{run_id}")


@mcp.tool()
def cat_run_create(env: str, blog_ids: Optional[List[int]] = None,
                   stop_on_failure: bool = True, start: bool = True) -> Any:
    """Create (and by default start) an apply run: freezes per-blog plans
    (blog 1 first) and works through them via the WP broker. Refused while the
    preview has blockers or another run is active for the env. APPLY-GATED:
    requires the CATMGR_APPLY_USERS allowlist."""
    return _call("POST", "/api/categories/runs", json_body={
        "env": env, "blog_ids": blog_ids, "stop_on_failure": stop_on_failure,
        "start": start,
    })


@mcp.tool()
def cat_run_start(run_id: int) -> Any:
    """(Re)start the worker for a queued run. Apply-gated."""
    return _call("POST", f"/api/categories/runs/{run_id}/start")


@mcp.tool()
def cat_run_pause(run_id: int) -> Any:
    """Pause a run after the current job finishes. Apply-gated."""
    return _call("POST", f"/api/categories/runs/{run_id}/pause")


@mcp.tool()
def cat_run_resume(run_id: int) -> Any:
    """Resume a paused/failed run (pending jobs continue). Apply-gated."""
    return _call("POST", f"/api/categories/runs/{run_id}/resume")


@mcp.tool()
def cat_run_cancel(run_id: int) -> Any:
    """Cancel a paused/queued run; pending jobs become cancelled. Apply-gated."""
    return _call("POST", f"/api/categories/runs/{run_id}/cancel")


@mcp.tool()
def cat_job_retry(run_id: int, job_id: int) -> Any:
    """Re-queue a failed/skipped job and restart the worker (progress made
    before the failure is not repeated). Apply-gated."""
    return _call("POST", f"/api/categories/runs/{run_id}/jobs/{job_id}/retry")


@mcp.tool()
def cat_job_skip(run_id: int, job_id: int) -> Any:
    """Mark a failed job skipped so the run can complete without that blog.
    Apply-gated."""
    return _call("POST", f"/api/categories/runs/{run_id}/jobs/{job_id}/skip")


@mcp.tool()
def cat_restore_blog(run_id: int, job_id: int) -> Any:
    """EMERGENCY: converge one blog back to its pre-apply snapshot (deleted
    terms recreate with new ids). Apply-gated."""
    return _call("POST", f"/api/categories/runs/{run_id}/jobs/{job_id}/restore")


@mcp.tool()
def cat_freeze_set(env: str, on: bool) -> Any:
    """Toggle the WordPress-native category-edit freeze for an environment
    (blocks wp-admin product_cat changes during the migration window).
    Apply-gated."""
    return _call("POST", "/api/categories/freeze", json_body={
        "env": env, "on": on,
    })


@mcp.tool()
def cat_drift_audit(env: str, blog_ids: Optional[List[int]] = None) -> Any:
    """Re-import every snapshotted blog live (or only blog_ids, for a phased
    rollout), then report what a plan would still change. converged=true and
    empty pending = WordPress matches the draft exactly."""
    body: Dict[str, Any] = {"env": env}
    if blog_ids:
        body["blog_ids"] = [int(b) for b in blog_ids]
    return _call("POST", "/api/categories/drift-audit", json_body=body)


# ---------------------------------------------------- shared-kernel parity
# Every tool below calls a route that runs the same mutations.* handler (or
# queries.* read) the in-app assistant stages, with MCP's identity instead of
# a browser session.


@mcp.tool()
def get_sync_status(store: Optional[str] = None) -> Any:
    """Pipeline liveness: latest FDM4 pull and website reconcile with timing
    and errors, 24h counts; with a store also its logo-sync ownership, freezes,
    recent sync events and last logo edit."""
    return _call("GET", "/api/sync-status", params={"store": store} if store else None)


@mcp.tool()
def list_design_usage(store: str, design_id: str, color_scheme_id: Optional[str] = None) -> Any:
    """Styles of a store carrying a design (optionally one scheme): rows,
    colors, schemes, logo codes and style_codes for a swap."""
    params = {"store": store, "design_id": design_id}
    if color_scheme_id:
        params["color_scheme_id"] = color_scheme_id
    return _call("GET", "/api/design-usage", params=params)


@mcp.tool()
def set_logo_cost(store: str, design_id: str, styles: List[str], cost_override: Optional[str] = None,
                  color_scheme_id: Optional[str] = None) -> Any:
    """One shopper charge for a logo on every row of the named styles in a
    store (cost_override as a decimal string; None clears the override so the
    logo's default cost applies)."""
    return _call("POST", "/api/assignments/logo-cost", json_body={
        "store": store, "design_id": design_id, "color_scheme_id": color_scheme_id,
        "cost_override": cost_override, "styles": styles,
    })


@mcp.tool()
def set_store_extra_customers(store: str, customers: List[str]) -> Any:
    """Replace the other FDM4 customer numbers whose designs a store may use."""
    return _call("PUT", f"/api/settings/{store}/extra-customers", json_body={"customers": customers})


@mcp.tool()
def set_logo_default_cost(logo_code: str, color_scheme_id: str, cost: str, locked: bool = True) -> Any:
    """A logo variant's default shopper charge (every store without a row
    override); cost as a decimal string."""
    return _call("PUT", "/api/default-costs", json_body={
        "logo_code": logo_code, "color_scheme_id": color_scheme_id, "cost": cost, "locked": locked,
    })


@mcp.tool()
def set_price_rule_active(rule_id: int, active: bool) -> Any:
    """Switch a price rule on or off (on requires an app preview since its last edit)."""
    return _call("PUT", "/api/price-rules/toggle", json_body={"rule_id": rule_id, "active": active})


@mcp.tool()
def delete_price_rule(rule_id: int) -> Any:
    """Remove a price rule entirely."""
    return _call("DELETE", "/api/price-rules", params={"rule_id": rule_id})


@mcp.tool()
def list_price_rules(store: Optional[str] = None) -> Any:
    """Price rules (optionally only those that can affect one store)."""
    return _call("GET", "/api/price-rules", params={"store": store} if store else None)


@mcp.tool()
def set_stock_override(style_code: str, mode: str, note: str = "") -> Any:
    """Fake Inventory style exception: mode 'fake' (always in stock) or 'real'."""
    return _call("PUT", "/api/stock-overrides", json_body={"style_code": style_code, "mode": mode, "note": note})


@mcp.tool()
def remove_stock_override(style_code: str) -> Any:
    """Remove a style's Fake Inventory exception."""
    return _call("DELETE", "/api/stock-overrides", params={"style": style_code})


@mcp.tool()
def set_brand_stock_rule(mill_code: str, mode: str) -> Any:
    """Fake Inventory brand rule by FDM4 mill code: mode 'real' or 'fake'."""
    return _call("PUT", "/api/stock-overrides/brands", json_body={"mill_code": mill_code, "mode": mode})


@mcp.tool()
def remove_brand_stock_rule(mill_code: str) -> Any:
    """Remove a brand's Fake Inventory rule."""
    return _call("DELETE", "/api/stock-overrides/brands", params={"mill": mill_code})


@mcp.tool()
def set_sync_block(fdm4_store: str, styles: Optional[List[str]] = None, scope: str = "full", note: str = "") -> Any:
    """Freeze the hourly update for a whole store (styles empty; scope full|pricing)
    or for named styles."""
    styles = styles or []
    return _call("PUT", "/api/sync-blocks", json_body={
        "fdm4_store": fdm4_store, "whole_store": not styles, "styles": styles, "scope": scope, "note": note,
    })


@mcp.tool()
def remove_sync_block(fdm4_store: str, style_code: str = "") -> Any:
    """Remove a whole-store freeze (style_code '') or one style's freeze."""
    return _call("DELETE", "/api/sync-blocks", params={"store": fdm4_store, "style": style_code})


@mcp.tool()
def set_product_mix(fdm4_store: str, mode: str, note: str = "") -> Any:
    """Enrol a store in Product Mix ('all' follows FDM4; 'list' = curated list,
    seeded from the current mix) or switch its mode."""
    try:
        return _call("PUT", "/api/product-mix/stores", json_body={"fdm4_store": fdm4_store, "mode": mode, "note": note})
    except RuntimeError as exc:
        if "already has a product-mix override" not in str(exc):
            raise
        return _call("PUT", "/api/product-mix/stores/mode", json_body={"fdm4_store": fdm4_store, "mode": mode})


@mcp.tool()
def disable_product_mix(fdm4_store: str) -> Any:
    """Switch a store's Product Mix override off (it follows FDM4 again)."""
    return _call("DELETE", "/api/product-mix/stores", params={"store": fdm4_store})


@mcp.tool()
def add_mix_styles(fdm4_store: str, styles: List[str]) -> Any:
    """Add styles (all colors) to a list-mode store's curated product list."""
    return _call("PUT", "/api/product-mix", json_body={"store": fdm4_store, "styles": styles})


@mcp.tool()
def remove_mix_styles(fdm4_store: str, styles: List[str]) -> Any:
    """Drop styles from a list-mode store's curated list (never empties it)."""
    return _call("DELETE", "/api/product-mix", json_body={"store": fdm4_store, "styles": styles})


@mcp.tool()
def get_stock_rules(q: str = "", limit: int = 200) -> Any:
    """Read brand inventory rules and a bounded page of style exceptions."""
    return {
        "brands": _call("GET", "/api/stock-overrides/brands"),
        "style_exceptions": _call("GET", "/api/stock-overrides", params={"q": q, "limit": limit}),
    }


@mcp.tool()
def list_sync_blocks() -> Any:
    """Read the existing store and style sync freezes."""
    return _call("GET", "/api/sync-blocks")


@mcp.tool()
def get_product_mix(store: str, q: str = "", limit: int = 50, offset: int = 0) -> Any:
    """Read a store's product-mix mode and a page of curated styles."""
    return _call("GET", "/api/product-mix", params={"store": store, "q": q, "limit": limit, "offset": offset})


def tool_names() -> List[str]:
    """Return names of all registered MCP tools. Used by parity tests only."""
    try:
        # FastMCP 1.x: tools are stored in _tool_manager._tools keyed by name.
        return list(mcp._tool_manager._tools)
    except AttributeError:
        pass
    # Fallback: parse this file's AST and collect @mcp.tool()-decorated fns.
    # @mcp.tool() is ast.Call(func=ast.Attribute(..., attr='tool')), not bare Attribute.
    import ast
    from pathlib import Path as _Path
    src = _Path(__file__).read_text()
    tree = ast.parse(src)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                attr = None
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    attr = dec.func
                elif isinstance(dec, ast.Attribute):
                    attr = dec
                if attr is not None and attr.attr == "tool":
                    names.append(node.name)
    return names




@mcp.tool()
def cat_node_lookup(env: str, slug: Optional[str] = None, path: Optional[str] = None) -> Any:
    """Confirm a draft category by its exact path or web address before proposing a move."""
    params = {"env": env, "slug": slug, "path": path}
    return _call("GET", "/api/categories/node-lookup", params={k: v for k, v in params.items() if v is not None})


@mcp.tool()
def cat_mapping_rows(env: str, filter: str = "undecided", slugs: Optional[List[str]] = None,
                     limit: int = 100, offset: int = 0) -> Any:
    """Read bounded decision rows: undecided, empty, store_only, or explicit old slugs."""
    params = {"env": env, "filter": filter, "slugs": slugs, "limit": limit, "offset": offset}
    return _call("GET", "/api/categories/mapping-rows", params={k: v for k, v in params.items() if v is not None})


if __name__ == "__main__":
    mcp.run()
