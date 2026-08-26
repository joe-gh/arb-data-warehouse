"""Token-authenticated machine feed of the product projection.

External consumers (Emblem ingest, a future Shopify adapter) page
woo.store_product_state deltas by row_version. These are MACHINE endpoints:
auth is a bearer token checked against woo.feed_consumer (sha256 at rest,
constant-time compare) - no session, no CSRF, and nginx exposes /feed/
without the operator IP allowlist. Rows include is_active=false tombstones
so consumers can retire products; every refresh is an atomic generation, so
a consumer that pages to the current ceiling always sees a consistent
snapshot boundary.
"""

import hashlib
import hmac
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query

from db import database

router = APIRouter(prefix="/feed", tags=["feed"])

MAX_PAGE_LIMIT = 5000
DEFAULT_PAGE_LIMIT = 1000


def _authenticate(authorization: Optional[str]) -> Dict[str, Any]:
    """Resolve the bearer token to an active feed consumer row."""

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization[len("Bearer "):].strip()
    if not token or len(token) > 512:
        raise HTTPException(status_code=401, detail="Invalid feed token")
    supplied_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    with database.cursor() as cursor:
        cursor.execute(
            "SELECT name, token_hash FROM woo.feed_consumer WHERE active = true"
        )
        rows = cursor.fetchall()
    matched: Optional[Dict[str, Any]] = None
    # Compare against every active row so timing does not reveal which (if
    # any) consumer name matched.
    for row in rows:
        if hmac.compare_digest(supplied_hash, str(row["token_hash"])):
            matched = row
    if matched is None:
        raise HTTPException(status_code=401, detail="Invalid feed token")
    return matched


@router.get("/version")
def feed_version(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _authenticate(authorization)
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(max(row_version), 0) AS version,
                   max(changed_at) AS refreshed_at,
                   count(*) FILTER (WHERE is_active) AS active_rows
              FROM woo.store_product_state
            """
        )
        row = cursor.fetchone()
        # Ask E: the logo feed has its own version domain (operator edits are
        # continuous, unlike the hourly product refresh).
        cursor.execute(
            """
            SELECT GREATEST(
                       (SELECT COALESCE(max(row_version), 0) FROM logo.assignment),
                       (SELECT COALESCE(max(row_version), 0) FROM logo.assignment_tombstone)
                   ) AS logo_version,
                   (SELECT count(*) FROM logo.assignment WHERE active)
                       AS logo_active_rows
            """
        )
        logo = cursor.fetchone()
    return {
        "version": int(row["version"]),
        "refreshed_at": row["refreshed_at"],
        "active_rows": int(row["active_rows"]),
        "logo_version": int(logo["logo_version"]),
        "logo_active_rows": int(logo["logo_active_rows"]),
    }


@router.get("/products")
def feed_products(
    since_version: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    store: Optional[str] = Query(None, max_length=32),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    consumer = _authenticate(authorization)
    store_filter = (store or "").strip().upper()
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(max(row_version), 0) AS ceiling"
            "  FROM woo.store_product_state"
        )
        version_ceiling = int(cursor.fetchone()["ceiling"])
        cursor.execute(
            """
            SELECT s.fdm4_store, s.catalog_id, s.sku, s.kind, s.style_code,
                   s.row_version, s.is_active, s.changed_at,
                   -- Ask B (Emblem): additive serve-time enrichment - warehouse
                   -- columns merged into the payload without touching stored
                   -- rows or hashes. jsonb_strip_nulls keeps absent fields out.
                   -- Ask C: parent rows additionally carry a `pim` object
                   -- (content + canonical CDN image urls) when PIM data exists.
                   s.payload || jsonb_strip_nulls(jsonb_build_object(
                       'brand', s.brand,
                       'category', s.category,
                       'item_name', s.item_name,
                       'price_levels', s.price_levels,
                       'def_cost', s.def_cost,
                       'origin_country', s.origin_country,
                       'harmonization', s.harmonization,
                       'design_id', s.design_id,
                       'design_name', s.design_name,
                       'color_code', s.color_code,
                       'size_code', s.size_code,
                       'weight', s.weight,
                       'ean_code', s.ean_code,
                       'mill_code', s.mill_code
                   )) || CASE
                       WHEN pim.pim_obj IS NOT NULL AND pim.pim_obj <> '{}'::jsonb
                       THEN jsonb_build_object('pim', pim.pim_obj)
                       ELSE '{}'::jsonb
                   END || CASE
                       -- Production-tuned logo overlay geometry (percent units:
                       -- left/top/width/height + angle) for parent rows.
                       WHEN pp.placement IS NOT NULL
                       THEN jsonb_build_object('logo_placement',
                                pp.placement
                                    || jsonb_build_object('fallback', pp.fallback))
                       ELSE '{}'::jsonb
                   END AS payload
              FROM woo.store_product_state s
              LEFT JOIN LATERAL (
                    SELECT jsonb_strip_nulls(jsonb_build_object(
                               'name', NULLIF(p.name, ''),
                               'short_description', NULLIF(p.short_description, ''),
                               'description', NULLIF(p.description, ''),
                               'images', (
                                   SELECT jsonb_agg(
                                              jsonb_build_object(
                                                  'url', COALESCE(mo.cdn_url, i.img->>'src'),
                                                  'position', COALESCE(NULLIF(i.img->>'position', '')::int, i.ord::int)
                                              )
                                              ORDER BY COALESCE(NULLIF(i.img->>'position', '')::int, i.ord::int)
                                          )
                                     FROM jsonb_array_elements(
                                              coalesce(p.payload->'parent'->'images', '[]'::jsonb)
                                          ) WITH ORDINALITY i(img, ord)
                                     LEFT JOIN pim.media_object mo
                                            ON mo.source_url = i.img->>'src'
                               )
                           )) AS pim_obj
                      FROM pim.product_state p
                      JOIN woo.store_blog_map bm
                            ON bm.fdm4_store = s.fdm4_store
                     WHERE s.kind = 'parent'
                       AND p.blog_id = bm.blog_id
                       AND p.sku_parent = s.sku
                     ORDER BY p.blog_id
                     LIMIT 1
                   ) pim ON true
              LEFT JOIN LATERAL (
                    SELECT pp0.placement, pp0.fallback
                      FROM pim.product_placement pp0
                      JOIN woo.store_blog_map bm2
                            ON bm2.fdm4_store = s.fdm4_store
                     WHERE s.kind = 'parent'
                       AND pp0.blog_id = bm2.blog_id
                       AND pp0.sku_parent = s.sku
                     ORDER BY pp0.blog_id
                     LIMIT 1
                   ) pp ON true
             WHERE s.row_version > %s
               AND (%s = '' OR s.fdm4_store = %s)
             ORDER BY s.row_version
             LIMIT %s
            """,
            (since_version, store_filter, store_filter, limit),
        )
        rows = cursor.fetchall()

    next_since = int(rows[-1]["row_version"]) if len(rows) == limit else None
    reached = int(rows[-1]["row_version"]) if rows else since_version
    # Progress stamp is best-effort telemetry for the Health view; a failure
    # here must never fail a successful pull.
    try:
        with database.cursor(write=True) as cursor:
            cursor.execute(
                """
                UPDATE woo.feed_consumer
                   SET last_pull_at = now(),
                       last_pull_version = GREATEST(
                           COALESCE(last_pull_version, 0), %s)
                 WHERE name = %s
                """,
                (reached, consumer["name"]),
            )
    except Exception:  # noqa: BLE001 - telemetry only
        pass
    return {
        "version_ceiling": version_ceiling,
        "rows": rows,
        "next_since_version": next_since,
    }


@router.get("/logos")
def feed_logos(
    since_version: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    store: Optional[str] = Query(None, max_length=32),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Ask E (Emblem): logo assignments as a keyset feed with tombstones.

    Grain: (fdm4_store, style_code, color_code|null, option_row, position).
    color_code null = applies to every colorway of the style. Soft-retired
    assignments (active=false) arrive as is_active=false rows with full
    payloads; hard deletes arrive as is_active=false rows with empty payloads
    (from logo.assignment_tombstone). logo_name/price are resolved with the
    same precedence the storefront uses (name_override > store display name >
    global display name > logo code; cost_override > default cost). Absent
    price means no upcharge (0.00).
    """
    _authenticate(authorization)
    store_filter = (store or "").strip().upper()
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT GREATEST(
                       (SELECT COALESCE(max(row_version), 0) FROM logo.assignment),
                       (SELECT COALESCE(max(row_version), 0) FROM logo.assignment_tombstone)
                   ) AS ceiling
            """
        )
        version_ceiling = int(cursor.fetchone()["ceiling"])
        cursor.execute(
            """
            SELECT u.fdm4_store, u.style_code, u.color_code, u.option_row,
                   u.position, u.catalog_id, u.row_version, u.is_active,
                   u.changed_at, u.payload
              FROM (
                SELECT a.fdm4_store,
                       a.product_style AS style_code,
                       NULLIF(a.garment_color_code, '') AS color_code,
                       a.option_row,
                       a.position,
                       a.catalog_id,
                       a.row_version,
                       a.active AS is_active,
                       a.updated_at AS changed_at,
                       jsonb_strip_nulls(jsonb_build_object(
                           'logo_name', COALESCE(
                               NULLIF(a.name_override, ''),
                               dn_s.name, dn_g.name,
                               NULLIF(a.logo_code, ''), a.design_id),
                           'design_id', NULLIF(a.design_id, ''),
                           'art_id', COALESCE(
                               NULLIF(btrim(dp.art_id), ''),
                               NULLIF(a.design_id, '')),
                           'logo_code', NULLIF(a.logo_code, ''),
                           'placement', NULLIF(a.location, ''),
                           'price', COALESCE(a.cost_override, dc.cost)::text,
                           'color_scheme', NULLIF(a.color_scheme_id, ''),
                           'image_url', NULLIF(a.image_url, ''),
                           'background', NULLIF(a.background, ''),
                           'optional', a.optional,
                           'sort_order', a.sort_order
                       )) AS payload
                  FROM logo.assignment a
                  LEFT JOIN logo.display_name dn_s
                        ON dn_s.design_id = a.design_id
                       AND dn_s.color_scheme_id = a.color_scheme_id
                       AND dn_s.fdm4_store = a.fdm4_store
                  LEFT JOIN logo.display_name dn_g
                        ON dn_g.design_id = a.design_id
                       AND dn_g.color_scheme_id = a.color_scheme_id
                       AND dn_g.fdm4_store = ''
                  LEFT JOIN logo.default_cost dc
                        ON dc.logo_code = a.logo_code
                       AND dc.color_scheme_id = a.color_scheme_id
                  LEFT JOIN LATERAL (
                        SELECT dp0.art_id
                          FROM fdm4.design_pool dp0
                         WHERE btrim(dp0.design_id) = btrim(a.design_id)
                         LIMIT 1
                       ) dp ON true
                UNION ALL
                SELECT t.fdm4_store, t.product_style,
                       NULLIF(t.garment_color_code, ''), t.option_row,
                       t.position, NULL::text, t.row_version, false,
                       t.deleted_at, '{}'::jsonb
                  FROM logo.assignment_tombstone t
              ) u
             WHERE u.row_version > %s
               AND (%s = '' OR u.fdm4_store = %s)
             ORDER BY u.row_version
             LIMIT %s
            """,
            (since_version, store_filter, store_filter, limit),
        )
        rows = cursor.fetchall()
    next_since = int(rows[-1]["row_version"]) if len(rows) == limit else None
    return {
        "version_ceiling": version_ceiling,
        "rows": rows,
        "next_since_version": next_since,
    }


@router.get("/stores")
def feed_stores(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _authenticate(authorization)
    with database.cursor() as cursor:
        cursor.execute(
            """
            -- Ask F (Emblem): one row per (fdm4_store, catalog_id) - the real
            -- storefront grain - with the suggested-catalog marker and the
            -- store's pricing tier for price-list assignment.
            SELECT s.fdm4_store,
                   s.catalog_id,
                   count(*) FILTER (WHERE s.is_active) AS active_rows,
                   max(s.row_version) AS version,
                   COALESCE(sc.suggested, false) AS suggested,
                   b.blog_id,
                   COALESCE(b.blog_ids, '') AS blog_ids,
                   COALESCE(b.blog_path, '') AS blog_path,
                   COALESCE(pt.tier_name, '') AS pricing_tier,
                   COALESCE(la.logo_assignments, 0) AS logo_assignments
              FROM woo.store_product_state s
              LEFT JOIN woo.store_catalog sc
                    ON sc.fdm4_store = s.fdm4_store
                   AND sc.catalog_id = s.catalog_id
              LEFT JOIN (
                    SELECT fdm4_store,
                           min(blog_id) AS blog_id,
                           string_agg(blog_id::text, ', ' ORDER BY blog_id)
                               AS blog_ids,
                           min(blog_path) AS blog_path
                      FROM woo.store_blog_map
                     GROUP BY fdm4_store
                   ) b ON b.fdm4_store = s.fdm4_store
              LEFT JOIN woo.store_pricing_tier pt
                    ON pt.fdm4_store = s.fdm4_store
              LEFT JOIN (
                    SELECT fdm4_store, count(*) AS logo_assignments
                      FROM logo.assignment
                     WHERE active
                     GROUP BY fdm4_store
                   ) la ON la.fdm4_store = s.fdm4_store
             GROUP BY s.fdm4_store, s.catalog_id, sc.suggested,
                      b.blog_id, b.blog_ids, b.blog_path, pt.tier_name,
                      la.logo_assignments
             ORDER BY s.fdm4_store, s.catalog_id
            """
        )
        rows = cursor.fetchall()
    return {"stores": rows}


@router.get("/categories")
def feed_categories(
    blog_id: int = Query(1, ge=1),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Manually curated Woo category tree + product memberships (additive,
    2026-08-26). Snapshot semantics, NOT row_version paging: the source is a
    full-replace import from the production blog's hand-curated product_cat
    taxonomy (curated.* tables), so consumers replace their copy whenever
    imported_at moves. Names arrive entity-decoded; the same name may appear
    under different parents - (blog_id, term_id) is the identity and `path`
    disambiguates. Memberships are keyed by parent-product SKU (style code);
    rows with product_count 0 and no memberships are curation debris safe to
    ignore. Entirely independent of FDM4's flat category vocabulary."""
    _authenticate(authorization)
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT term_id, slug, name, parent_term_id, depth, path,
                   sort_order, product_count, imported_at
              FROM curated.category
             WHERE blog_id = %s
             ORDER BY depth, path
            """,
            (blog_id,),
        )
        categories = cursor.fetchall()
        cursor.execute(
            """
            SELECT term_id, sku, product_id
              FROM curated.category_product
             WHERE blog_id = %s
             ORDER BY term_id, sku
            """,
            (blog_id,),
        )
        memberships = cursor.fetchall()
    imported_at = max((c["imported_at"] for c in categories), default=None)
    return {
        "blog_id": blog_id,
        "imported_at": imported_at,
        "categories": categories,
        "memberships": memberships,
    }
