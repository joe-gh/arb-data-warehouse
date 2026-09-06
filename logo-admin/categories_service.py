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

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import Json, execute_values

from snapshots import dumps_exact


def _json(value):
    """jsonb read back through the pool decodes numbers as Decimal; write them
    back as numbers, never as quoted strings."""
    return Json(value, dumps=dumps_exact)

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
        (actor[:100], action, entity, entity_key, _json(detail)),
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


def set_freeze(env: str, on: bool) -> Dict[str, Any]:
    """Switch the WordPress-side product_cat edit freeze (network option)."""
    result = _broker(env, "/freeze", method="POST", payload={"on": bool(on)})
    if not isinstance(result, dict):
        raise BrokerError("WordPress returned an unexpected /freeze response")
    return result


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


# 5,000 membership rows is ~300 KB of JSON: comfortably inside the broker
# client's response cap and the WP request budget even for blog 1.
_EXPORT_PAGE_LIMIT = 5000
_EXPORT_MAX_PAGES = 200
# A membership edit landing between two export pages shifts rows; the export
# is re-pulled from scratch this many times before giving up.
_EXPORT_ATTEMPTS = 3


class ExportInconsistent(BrokerError):
    """Two pages of one export disagreed (the live data moved underneath)."""


def _export_page(env: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """One /export call. The broker's 409 for a moved generation is the same
    condition as a page that disagrees with page 1, so it is raised as
    ExportInconsistent and the whole export is re-pulled."""

    try:
        page = _broker(env, "/export", method="POST", payload=payload)
    except BrokerError as exc:
        if getattr(exc, "status", 0) == 409 and "arb_catmgr_export_moved" in str(exc):
            raise ExportInconsistent(
                f"blog {payload.get('blog_id')}: the categories changed"
                f" mid-export ({exc})"
            ) from exc
        raise
    if not isinstance(page, dict):
        raise BrokerError("WordPress returned an unexpected /export page")
    return page


def _fetch_export_once(env: str, blog_id: int) -> Dict[str, Any]:
    first_payload = {
        "blog_id": int(blog_id),
        "products_offset": 0,
        "products_limit": _EXPORT_PAGE_LIMIT,
        "include_uncategorized": True,
    }
    first = _export_page(env, first_payload)
    if not isinstance(first.get("terms"), list):
        raise BrokerError("WordPress returned an unexpected /export response")
    # Broker v3 stamps every page with a digest of the blog's whole category
    # state. Sent back as expected_generation it makes WordPress refuse a page
    # that no longer belongs with page 1 (counts alone cannot see an edit that
    # keeps the row count).
    generation = str(first.get("export_generation") or "")

    def check_generation(page: Dict[str, Any]) -> None:
        seen = str(page.get("export_generation") or "")
        if generation and seen and seen != generation:
            raise ExportInconsistent(
                f"blog {blog_id}: the categories changed mid-export"
                f" (generation {generation} -> {seen})"
            )

    first_products = list(first.get("products") or [])
    first_uncategorized = list(first.get("uncategorized") or [])
    products = list(first_products)
    total = int(first.get("products_total") or len(products))
    keyset = "next_after" in first          # broker v2: keyset pages + counts
    cursor = first.get("next_after")
    pages = 1
    while len(products) < total:
        if pages >= _EXPORT_MAX_PAGES:
            raise BrokerError(
                f"blog {blog_id}: /export paging exceeded {_EXPORT_MAX_PAGES} pages"
            )
        if keyset:
            if not isinstance(cursor, dict):
                raise ExportInconsistent(
                    f"blog {blog_id}: /export ended after {len(products)} of {total} rows"
                )
            payload = {
                "blog_id": int(blog_id),
                "after_term_id": int(cursor.get("term_id") or 0),
                "after_product_id": int(cursor.get("product_id") or 0),
                "products_limit": _EXPORT_PAGE_LIMIT,
            }
        else:
            payload = {
                "blog_id": int(blog_id),
                "products_offset": len(products),
                "products_limit": _EXPORT_PAGE_LIMIT,
            }
        if generation:
            payload["expected_generation"] = generation
        page = _export_page(env, payload)
        check_generation(page)
        if keyset and int(page.get("products_total") or 0) != total:
            raise ExportInconsistent(
                f"blog {blog_id}: membership count changed mid-export"
                f" ({total} -> {page.get('products_total')})"
            )
        chunk = list(page.get("products") or [])
        if not chunk:
            raise ExportInconsistent(
                f"blog {blog_id}: /export returned an empty page after"
                f" {len(products)} of {total} rows"
            )
        products.extend(chunk)
        cursor = page.get("next_after")
        pages += 1
    unique = {(int(r.get("term_id") or 0), int(r.get("product_id") or 0))
              for r in products if isinstance(r, dict)}
    if len(products) != total or len(unique) != total:
        raise ExportInconsistent(
            f"blog {blog_id}: /export delivered {len(products)} rows"
            f" ({len(unique)} unique) but declared {total}"
        )

    # Products with no category at all have their own cursor (they used to
    # stop silently at the page cap, hiding them from planning AND restore).
    uncategorized = list(first_uncategorized)
    uncat_total = first.get("uncategorized_total")
    uncat_after = first.get("next_uncategorized_after")
    uncat_pages = 1
    while uncat_after is not None:
        if uncat_pages >= _EXPORT_MAX_PAGES:
            raise BrokerError(
                f"blog {blog_id}: /export uncategorized paging exceeded"
                f" {_EXPORT_MAX_PAGES} pages"
            )
        payload = {
            "blog_id": int(blog_id),
            "include_uncategorized": True,
            "after_uncategorized_id": int(uncat_after),
            # The membership rows of a continuation page are not used; ask for
            # the smallest one WordPress will build.
            "products_limit": 1,
        }
        if generation:
            payload["expected_generation"] = generation
        page = _export_page(env, payload)
        check_generation(page)
        chunk = list(page.get("uncategorized") or [])
        if not chunk:
            # A page that happened to fill exactly still carries a cursor; the
            # empty page after it is the end. The total check below is what
            # decides whether the set really arrived.
            break
        uncategorized.extend(chunk)
        uncat_after = page.get("next_uncategorized_after")
        uncat_pages += 1
    if uncat_total is not None and len(uncategorized) < int(uncat_total):
        # An older broker cannot page this set: refuse rather than import a
        # copy that would plan and restore only part of the store. Not an
        # ExportInconsistent - retrying cannot make it complete.
        raise BrokerError(
            f"blog {blog_id}: WordPress listed only {len(uncategorized)} of"
            f" {int(uncat_total)} products that have no category, so the copy"
            " would be incomplete - ask a developer to update the WordPress"
            " side before importing this store",
            500,
        )

    if (pages > 1 or uncat_pages > 1) and not generation:
        # An older broker has no generation to fence with: re-read page 1 and
        # check that the terms, its own membership rows and its uncategorized
        # rows are still the ones the walk started from.
        before = export_fingerprint(
            first.get("terms") or [], first_products, first_uncategorized,
        )
        again = _export_page(env, first_payload)
        after = export_fingerprint(
            again.get("terms") or [], again.get("products") or [],
            again.get("uncategorized") or [],
        )
        if before != after:
            raise ExportInconsistent(
                f"blog {blog_id}: the categories changed between the first and"
                " the last page of the export"
            )
    first["products"] = products
    first["uncategorized"] = uncategorized
    return first


def fetch_export(env: str, blog_id: int) -> Dict[str, Any]:
    """Pull one blog's terms + memberships (+ uncategorized products).

    Pages are keyset-ordered by (term_id, product_id) and every page repeats
    the membership count AND (broker v3) the generation digest of the blog's
    whole category state: a change underneath the export is detected and the
    whole export is re-pulled, so an imported snapshot is never a torn read.
    The final unique row count must equal the declared count."""

    last: Optional[Exception] = None
    for _ in range(_EXPORT_ATTEMPTS):
        try:
            return _fetch_export_once(env, blog_id)
        except ExportInconsistent as exc:
            last = exc
    raise BrokerError(
        f"{last} - the live memberships kept changing during {_EXPORT_ATTEMPTS}"
        " export attempts; try again once product saves have settled"
    )


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
        # The original slug of a term the broker parked on catmgrtmp-<id>
        # (from its _arb_catmgr_parked term meta); '' for every other term.
        "parked_from": str(row.get("parked_from") or ""),
    }


def normalize_export(terms: List[Dict[str, Any]], products: List[Dict[str, Any]],
                     uncategorized: Optional[List[Dict[str, Any]]] = None,
                     ) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int, str]],
                                List[Tuple[int, str]]]:
    """The exact rows an import stores: cleaned terms, (term_id, product_id,
    sku) memberships of known terms, (product_id, sku) uncategorized products.
    Shared by the import and the live fingerprint so both see one shape."""

    clean_terms = [_clean_term(t) for t in terms]
    known_term_ids = {t["term_id"] for t in clean_terms}
    clean_products: List[Tuple[int, int, str]] = []
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
    categorized = {p for _, p, _ in clean_products}
    clean_uncategorized: List[Tuple[int, str]] = []
    for row in uncategorized or []:
        if not isinstance(row, dict):
            raise ValueError(f"malformed uncategorized row: {row!r}")
        product_id = int(row.get("product_id") or 0)
        if product_id <= 0 or product_id in categorized:
            continue
        clean_uncategorized.append(
            (product_id, str(row.get("sku") or "").strip().upper())
        )
    return clean_terms, clean_products, clean_uncategorized


# Fields of a term that a plan reads or an apply writes; everything the
# fingerprint must notice. Counts are derived and change with product saves
# that do not touch categories, so they stay out.
_FINGERPRINT_TERM_FIELDS = ("term_id", "slug", "name", "parent", "description",
                            "sort_order", "thumbnail_id", "name_locked",
                            "parked_from")


def export_fingerprint(terms: List[Dict[str, Any]], products: List[Dict[str, Any]],
                       uncategorized: Optional[List[Dict[str, Any]]] = None) -> str:
    """sha256 of the normalized export. Equal fingerprints = identical
    category state (terms, their attributes, every membership, every
    uncategorized product)."""

    clean_terms, clean_products, clean_uncategorized = normalize_export(
        terms, products, uncategorized,
    )
    body = {
        "terms": sorted(
            [[t[f] for f in _FINGERPRINT_TERM_FIELDS] for t in clean_terms],
            key=lambda row: row[0],
        ),
        "memberships": sorted(set(clean_products)),
        "uncategorized": sorted(set(clean_uncategorized)),
    }
    digest = hashlib.sha256(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest


def snapshot_status(cursor, env: str) -> List[Dict[str, Any]]:
    _require_env(env)
    cursor.execute(
        """
        SELECT blog_id, blog_path, version, imported_at, imported_by,
               term_count, membership_count, fingerprint
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
                         actor: str,
                         uncategorized: Optional[List[Dict[str, Any]]] = None,
                         ) -> Dict[str, Any]:
    """Full-replace one (env, blog) snapshot. Caller owns the transaction."""

    _require_env(env)
    blog_id = int(blog_id)
    clean_terms, clean_products, clean_uncategorized = normalize_export(
        terms, products, uncategorized,
    )
    fingerprint = export_fingerprint(terms, products, uncategorized)

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
        "DELETE FROM catmgr.wp_uncategorized_product WHERE env=%s AND blog_id=%s",
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
                 count, sort_order, thumbnail_id, name_locked, parked_from,
                 snapshot_version)
            VALUES %s
            """,
            [
                (env, blog_id, t["term_id"], t["slug"], t["name"], t["parent"],
                 t["description"], t["count"], t["sort_order"],
                 t["thumbnail_id"], t["name_locked"], t["parked_from"], version)
                for t in clean_terms
            ],
        )
    if clean_uncategorized:
        execute_values(
            cursor,
            """
            INSERT INTO catmgr.wp_uncategorized_product
                (env, blog_id, product_id, sku, snapshot_version)
            VALUES %s
            """,
            [(env, blog_id, p, sku, version) for p, sku in clean_uncategorized],
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
             term_count, membership_count, fingerprint)
        VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s)
        ON CONFLICT (env, blog_id) DO UPDATE
           SET version = EXCLUDED.version,
               blog_path = EXCLUDED.blog_path,
               imported_at = EXCLUDED.imported_at,
               imported_by = EXCLUDED.imported_by,
               term_count = EXCLUDED.term_count,
               membership_count = EXCLUDED.membership_count,
               fingerprint = EXCLUDED.fingerprint
        """,
        (env, blog_id, version, str(blog_path or ""), actor[:100],
         len(clean_terms), len(clean_products), fingerprint),
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
            "uncategorized_count": len(clean_uncategorized),
            "fingerprint": fingerprint,
        },
    )
    return {
        "blog_id": blog_id,
        "version": version,
        "term_count": len(clean_terms),
        "membership_count": len(clean_products),
        "uncategorized_count": len(clean_uncategorized),
        "fingerprint": fingerprint,
    }


def import_export(cursor, *, env: str, blog_id: int, export: Dict[str, Any],
                  actor: str) -> Dict[str, Any]:
    """import_blog_snapshot() straight from a fetch_export() payload."""

    return import_blog_snapshot(
        cursor, env=env, blog_id=blog_id,
        blog_path=str(export.get("blog_path") or ""),
        terms=export.get("terms") or [],
        products=export.get("products") or [],
        uncategorized=export.get("uncategorized") or [],
        actor=actor,
    )
