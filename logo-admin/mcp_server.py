"""MCP server for the Logo Admin tool (stdio; run on the warehouse box).

Full admin parity for AI-driven operation: every tool calls the FastAPI app's
own routes IN-PROCESS (TestClient), so all validation, orphan cascades, and
audit-trigger attribution behave exactly as they do for a human operator.
Writes are attributed in logo.audit_log to ARB_MCP_ACTOR (default
"CLI connection" - deliberately nondescript).

No network surface: transport is stdio, intended to be launched over SSH via
/opt/arb-logo-admin/mcp-run.sh (root wrapper loads /etc/arb-logo-admin.env).
Nothing in the web UI references this server.
"""

import base64
import binascii
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

import main as app_main
from auth import require_csrf, require_user
from config import get_settings

ACTOR = (os.environ.get("ARB_MCP_ACTOR") or "CLI connection")[:100]


def _identity() -> Dict[str, str]:
    return {"user_login": ACTOR, "display_name": ACTOR, "csrf": "mcp"}


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


@mcp.tool()
def list_logo_names(q: str = "", limit: int = 50, offset: int = 0) -> Any:
    """List/search the customer-facing logo names (logo.display_name), one row
    per (design_id, color scheme). Search matches name, design id, color scheme,
    or logo code. 'source' is pool_exact/pool_design/filename/code/manual;
    'locked' rows were hand-edited and are protected from re-pull."""
    return _call("GET", "/api/logo-names", params={"q": q, "limit": limit, "offset": offset})


@mcp.tool()
def set_logo_name(design_id: str, color_scheme_id: str, name: str) -> Any:
    """Set the shopper-facing name for one (design, color scheme). Marks the row
    manual + locked so a later FDM4 re-pull will not overwrite it. Takes effect
    on the next store logo sync."""
    return _call("PUT", "/api/logo-names", json_body={
        "design_id": design_id, "color_scheme_id": color_scheme_id, "name": name,
    })


@mcp.tool()
def repull_logo_name(design_id: str, force: bool = False) -> Any:
    """Re-pull a design's names from FDM4 design_pool (use after FDM4 updates a
    description). Locked/hand-edited rows are preserved unless force=True.
    Returns how many rows changed and the refreshed rows."""
    return _call("POST", "/api/logo-names/repull", json_body={
        "design_id": design_id, "force": force,
    })


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


if __name__ == "__main__":
    mcp.run()
