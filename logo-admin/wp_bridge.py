"""WordPress broker calls shared by the HTTP routes, the MCP server and the
in-app assistant. One implementation per call; the callers differ only in
how they authenticate the person asking."""

from typing import Any, Dict, Optional
from urllib.parse import urlencode

from auth import WordPressRequestError, wordpress_json_request
from config import get_settings
import queries


def wp_admin_call(path: str, *, method: str = "GET", payload: Optional[dict] = None,
                  timeout: Optional[int] = None) -> Dict[str, Any]:
    settings = get_settings()
    base = settings.wp_sync_url.rsplit("/sync", 1)[0]
    return wordpress_json_request(
        f"{base}{path}",
        settings.wp_sync_user,
        settings.wp_sync_app_password,
        method=method,
        timeout=timeout or settings.wp_http_timeout,
        payload=payload,
    )


def product_link(store: str, style: str) -> Dict[str, Any]:
    """Front-end + admin URLs of a style's product on the sync target.
    Soft-fails (ok=false, empty URLs) so a WordPress hiccup never breaks a
    caller."""
    settings = get_settings()
    query = urlencode({"fdm4_store": store, "style": style})
    try:
        result = wp_admin_call(f"/product-link?{query}", timeout=min(settings.wp_http_timeout, 10))
    except WordPressRequestError:
        return {"ok": False, "view_url": "", "edit_url": ""}
    return {
        "ok": bool(result.get("ok")),
        "view_url": str(result.get("view_url") or ""),
        "edit_url": str(result.get("edit_url") or ""),
    }


def logo_ownership() -> Dict[str, Any]:
    """Every mapped store with whether this app may sync its logos (the gate
    is a WordPress network option). Raises WordPressRequestError."""
    resp = wp_admin_call("/ownership")
    return {
        "stores": resp.get("stores") or [],
        "owned_blogs": resp.get("owned_blogs") or [],
    }


def store_ownership(store: str) -> Dict[str, Any]:
    """One store's logo-sync ownership, soft-failing when WordPress is down:
    {available, owned, blog_id, store}."""
    wanted = str(store).strip().upper()
    try:
        data = logo_ownership()
    except WordPressRequestError as exc:
        return {"available": False, "owned": None, "blog_id": None, "store": wanted, "error": str(exc)}
    for entry in data["stores"]:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("fdm4_store") or entry.get("store") or "").strip().upper()
        if code == wanted:
            return {
                "available": True,
                "owned": bool(entry.get("owned")),
                "blog_id": entry.get("blog_id"),
                "store": wanted,
            }
    return {"available": True, "owned": None, "blog_id": None, "store": wanted,
            "note": "store is not mapped to a WordPress site"}


def sync_status_report(cursor, store: Optional[str] = None) -> Dict[str, Any]:
    """queries.get_sync_status plus the store's logo-sync ownership from
    WordPress; the assistant read tool, the HTTP route and MCP all use this."""
    result = queries.get_sync_status(cursor, store=store)
    if result.get("store"):
        result["logo_sync_ownership"] = store_ownership(result["store"])
    return result
