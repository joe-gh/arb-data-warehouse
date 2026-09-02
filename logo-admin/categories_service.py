"""Category editor: snapshot pipeline + WP broker transport.

The category editor keeps per-environment (dev/prod) copies of the live
WooCommerce product_cat state in catmgr.wp_term / catmgr.wp_term_product,
imported through the arb-admin broker (arb-category-apply.php) and
re-importable at any time. Imports are FULL-REPLACE per (env, blog) inside the
caller's transaction and bump catmgr.snapshot.version, so anything computed
from an older snapshot is detectably stale.

Transport functions (fetch_*) live here so routes and MCP share one path and
tests can monkeypatch them; the DB functions take an open cursor and perform
no commits themselves.
"""

from typing import Any, Dict, List, Optional

from psycopg2.extras import Json, execute_values

from auth import WordPressRequestError, wordpress_json_request
from config import CATMGR_ENVS, CatmgrTarget, get_settings


class TargetNotConfigured(LookupError):
    """The requested environment has no configured WP target."""


def record_audit(cursor, *, actor: str, action: str, entity: str,
                 entity_key: str, detail: Dict[str, Any]) -> None:
    """Append one catmgr audit row (explicit, sync-intent style)."""

    cursor.execute(
        """
        INSERT INTO catmgr.audit_log (actor, action, entity, entity_key, detail)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (actor[:100], action, entity, entity_key, Json(detail)),
    )


class BrokerError(RuntimeError):
    """The WP broker call failed; status carries the upstream HTTP status."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- targets


def configured_targets() -> List[Dict[str, str]]:
    settings = get_settings()
    return [
        {"env": env, "host": settings.catmgr_targets[env].host}
        for env in CATMGR_ENVS
        if env in settings.catmgr_targets
    ]


def get_target(env: str) -> CatmgrTarget:
    target = get_settings().catmgr_targets.get(env)
    if target is None:
        raise TargetNotConfigured(env)
    return target


def _broker(env: str, path: str, *, method: str = "GET",
            payload: Optional[Dict[str, Any]] = None) -> Any:
    target = get_target(env)
    settings = get_settings()
    try:
        return wordpress_json_request(
            f"{target.base_url}{path}",
            target.user,
            target.app_password,
            method=method,
            timeout=settings.catmgr_wp_timeout,
            payload=payload,
        )
    except WordPressRequestError as exc:
        raise BrokerError(str(exc), getattr(exc, "status", 502)) from exc


def fetch_blogs(env: str) -> List[Dict[str, Any]]:
    result = _broker(env, "/blogs")
    blogs = result.get("blogs")
    if not isinstance(blogs, list):
        raise BrokerError("WordPress returned an unexpected /blogs response")
    return blogs


def fetch_wp_status(env: str) -> Dict[str, Any]:
    result = _broker(env, "/status")
    if not isinstance(result, dict):
        raise BrokerError("WordPress returned an unexpected /status response")
    return result


_EXPORT_PAGE_LIMIT = 20000
_EXPORT_MAX_PAGES = 50


def fetch_export(env: str, blog_id: int) -> Dict[str, Any]:
    """Pull one blog's terms + memberships, following product paging."""

    first = _broker(env, "/export", method="POST", payload={
        "blog_id": int(blog_id),
        "products_offset": 0,
        "products_limit": _EXPORT_PAGE_LIMIT,
    })
    if not isinstance(first, dict) or not isinstance(first.get("terms"), list):
        raise BrokerError("WordPress returned an unexpected /export response")
    products = list(first.get("products") or [])
    total = int(first.get("products_total") or len(products))
    pages = 1
    while len(products) < total:
        if pages >= _EXPORT_MAX_PAGES:
            raise BrokerError(
                f"blog {blog_id}: /export paging exceeded {_EXPORT_MAX_PAGES} pages"
            )
        page = _broker(env, "/export", method="POST", payload={
            "blog_id": int(blog_id),
            "products_offset": len(products),
            "products_limit": _EXPORT_PAGE_LIMIT,
        })
        chunk = list(page.get("products") or [])
        if not chunk:
            break
        products.extend(chunk)
        pages += 1
    first["products"] = products
    return first


# ---------------------------------------------------------------- snapshots


def _require_env(env: str) -> str:
    if env not in CATMGR_ENVS:
        raise ValueError(f"unknown environment: {env!r}")
    return env


def _clean_term(row: Any) -> Dict[str, Any]:
    if not isinstance(row, dict) or "term_id" not in row or "slug" not in row:
        raise ValueError(f"malformed term row: {row!r}")
    return {
        "term_id": int(row["term_id"]),
        "slug": str(row["slug"]),
        "name": str(row.get("name") or ""),
        "parent": int(row.get("parent") or 0),
        "description": str(row.get("description") or ""),
        "count": int(row.get("count") or 0),
        "sort_order": int(row.get("sort_order") or 0),
        "thumbnail_id": int(row.get("thumbnail_id") or 0),
        "name_locked": bool(row.get("name_locked")),
    }


def snapshot_status(cursor, env: str) -> List[Dict[str, Any]]:
    _require_env(env)
    cursor.execute(
        """
        SELECT blog_id, blog_path, version, imported_at, imported_by,
               term_count, membership_count
          FROM catmgr.snapshot
         WHERE env = %s
         ORDER BY blog_id
        """,
        (env,),
    )
    return [dict(row) for row in cursor.fetchall()]


def import_blog_snapshot(cursor, *, env: str, blog_id: int, blog_path: str,
                         terms: List[Dict[str, Any]],
                         products: List[Dict[str, Any]],
                         actor: str) -> Dict[str, Any]:
    """Full-replace one (env, blog) snapshot. Caller owns the transaction."""

    _require_env(env)
    blog_id = int(blog_id)
    clean_terms = [_clean_term(t) for t in terms]
    known_term_ids = {t["term_id"] for t in clean_terms}
    clean_products = []
    for row in products:
        if not isinstance(row, dict):
            raise ValueError(f"malformed product row: {row!r}")
        term_id = int(row.get("term_id") or 0)
        product_id = int(row.get("product_id") or 0)
        if term_id not in known_term_ids or product_id <= 0:
            continue  # membership of an unknown term (or junk) is dropped
        clean_products.append(
            (term_id, product_id, str(row.get("sku") or "").strip().upper())
        )

    cursor.execute(
        "SELECT version FROM catmgr.snapshot WHERE env=%s AND blog_id=%s FOR UPDATE",
        (env, blog_id),
    )
    row = cursor.fetchone()
    version = (int(row["version"]) + 1) if row else 1

    cursor.execute(
        "DELETE FROM catmgr.wp_term_product WHERE env=%s AND blog_id=%s",
        (env, blog_id),
    )
    cursor.execute(
        "DELETE FROM catmgr.wp_term WHERE env=%s AND blog_id=%s",
        (env, blog_id),
    )
    if clean_terms:
        execute_values(
            cursor,
            """
            INSERT INTO catmgr.wp_term
                (env, blog_id, term_id, slug, name, parent_term_id, description,
                 count, sort_order, thumbnail_id, name_locked, snapshot_version)
            VALUES %s
            """,
            [
                (env, blog_id, t["term_id"], t["slug"], t["name"], t["parent"],
                 t["description"], t["count"], t["sort_order"],
                 t["thumbnail_id"], t["name_locked"], version)
                for t in clean_terms
            ],
        )
    if clean_products:
        execute_values(
            cursor,
            """
            INSERT INTO catmgr.wp_term_product
                (env, blog_id, term_id, product_id, sku, snapshot_version)
            VALUES %s
            """,
            [(env, blog_id, t, p, sku, version) for t, p, sku in clean_products],
        )

    cursor.execute(
        """
        INSERT INTO catmgr.snapshot AS snapshot
            (env, blog_id, version, blog_path, imported_at, imported_by,
             term_count, membership_count)
        VALUES (%s, %s, %s, %s, now(), %s, %s, %s)
        ON CONFLICT (env, blog_id) DO UPDATE
           SET version = EXCLUDED.version,
               blog_path = EXCLUDED.blog_path,
               imported_at = EXCLUDED.imported_at,
               imported_by = EXCLUDED.imported_by,
               term_count = EXCLUDED.term_count,
               membership_count = EXCLUDED.membership_count
        """,
        (env, blog_id, version, str(blog_path or ""), actor[:100],
         len(clean_terms), len(clean_products)),
    )
    record_audit(
        cursor,
        actor=actor,
        action="snapshot_import",
        entity="snapshot",
        entity_key=f"{env}:{blog_id}",
        detail={
            "version": version,
            "blog_path": str(blog_path or ""),
            "term_count": len(clean_terms),
            "membership_count": len(clean_products),
        },
    )
    return {
        "blog_id": blog_id,
        "version": version,
        "term_count": len(clean_terms),
        "membership_count": len(clean_products),
    }
