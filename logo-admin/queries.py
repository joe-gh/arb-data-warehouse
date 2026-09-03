"""Bounded, cursor-owned read services shared by HTTP and the agent."""

import html
import re
import threading
import time
from typing import Any, Optional

import legacy_import


# Every collection-returning query has a fixed service-owned cap.  Callers
# cannot raise these limits, and each query asks PostgreSQL for only one row
# beyond the cap so it can report truncation without materializing the rest.
STORE_RESULT_LIMIT = 500
STYLE_SEARCH_RESULT_LIMIT = 100
STYLE_COLOR_RESULT_LIMIT = 500
STYLE_ASSIGNMENT_RESULT_LIMIT = 5_000
DESIGN_SEARCH_RESULT_LIMIT = 100
ASSIGNMENT_PLACEMENT_RESULT_LIMIT = 500
ASSIGNMENT_BACKGROUND_RESULT_LIMIT = 500
DESIGN_ASSET_RESULT_LIMIT = 500
COLOR_CLASS_SCAN_LIMIT = 5_000
DESIGN_PLACEMENT_RESULT_LIMIT = 500
PRICING_TIER_RESULT_LIMIT = 500
STORE_PRICING_TIER_RESULT_LIMIT = 500
COLOR_CLASS_RESULT_LIMIT = 500
SIMILAR_STYLE_RESULT_LIMIT = 500
COVERAGE_STYLE_RESULT_LIMIT = 2_000
LOGO_SET_RESULT_LIMIT = 500
READ_TEXT_CHAR_LIMIT = 1_024
READ_URL_CHAR_LIMIT = 2_048
READ_MAX_ROW_BYTES = 128 * 1024
READ_MAX_RESULT_BYTES = 8 * 1024 * 1024


class QueryServiceError(ValueError):
    """Base class for safe read-service failures."""


class QueryNotFound(QueryServiceError):
    """The requested warehouse entity does not exist."""


class QueryValidationError(QueryServiceError):
    """A direct service caller supplied an invalid value."""


def _bounded_query(
    cursor,
    query: str,
    params,
    limit: int,
) -> tuple[list[dict], bool, bool]:
    """Return an ordered JSON rowset only when DB-computed bytes are bounded."""

    cursor.execute(
        f"""
        WITH bounded_result AS MATERIALIZED (
            {query}
        ), encoded AS MATERIALIZED (
            SELECT row_number() OVER () AS ordinal,
                   to_jsonb(bounded_result) AS row_json
              FROM bounded_result
        ), measured AS MATERIALIZED (
            SELECT ordinal, row_json,
                   octet_length(row_json::text)::bigint AS row_bytes
              FROM encoded
        ), stats AS MATERIALIZED (
            SELECT count(*)::integer AS row_count,
                   coalesce(max(row_bytes), 0)::bigint AS max_row_bytes,
                   CASE
                       WHEN count(*) = 0 THEN 2::bigint
                       ELSE (
                           coalesce(sum(row_bytes), 0)
                           + ((count(*) - 1) * 2)
                           + 2
                       )::bigint
                   END AS result_bytes
              FROM measured
        ), payload AS MATERIALIZED (
            SELECT CASE
                       WHEN max_row_bytes <= {READ_MAX_ROW_BYTES}
                        AND result_bytes <= {READ_MAX_RESULT_BYTES}
                       THEN coalesce(
                           (
                               SELECT jsonb_agg(row_json ORDER BY ordinal)
                                 FROM measured
                           ),
                           '[]'::jsonb
                       )
                       ELSE '[]'::jsonb
                   END AS rows,
                   row_count,
                   max_row_bytes,
                   result_bytes
              FROM stats
        )
        SELECT rows,
               row_count, max_row_bytes, result_bytes
          FROM payload
        """,
        params,
    )
    result = cursor.fetchone()
    row_count = int(result["row_count"])
    byte_truncated = (
        int(result["max_row_bytes"]) > READ_MAX_ROW_BYTES
        or int(result["result_bytes"]) > READ_MAX_RESULT_BYTES
    )
    rows = [dict(row) for row in (result["rows"] or [])]
    return rows[:limit], row_count > limit, byte_truncated


def _clean(value: Any, field: str, maximum: int = 100) -> str:
    cleaned = str(value).strip()
    if not cleaned or len(cleaned) > maximum or "\x00" in cleaned:
        raise QueryValidationError(f"{field} is invalid")
    return cleaned


def _optional(value: Any, field: str, maximum: int = 100) -> str:
    cleaned = "" if value is None else str(value).strip()
    if len(cleaned) > maximum or "\x00" in cleaned:
        raise QueryValidationError(f"{field} is invalid")
    return cleaned


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _catalog_for_store(cursor, store: str) -> Optional[str]:
    cursor.execute(
        """
        SELECT left(catalog_id, 256) AS catalog_id
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


# Friendly names for stores whose catalog slug is meaningless (e.g. virtual
# catalogs like S_015883_Woo_1, which would otherwise render as "Woo 1").
STORE_DISPLAY_OVERRIDES = {
    # Virtual store codes with no WordPress blog of their own (external
    # all-products stores); everything else takes its blog's site title.
    "S_015883": "Square",
    "S_DAVEYTREE": "Davey Tree",
}


def _store_display_name(code: Any, catalog: Any, blog_name: Any = "") -> str:
    code_s = str(code or "")
    override = STORE_DISPLAY_OVERRIDES.get(code_s.strip().upper())
    if override:
        return override
    # The WordPress site title (woo.store_blog_map.blog_name) is the name
    # operators know a store by; the catalog slug is only a fallback for
    # stores with no blog mapping.
    blog_s = html.unescape(str(blog_name or "")).strip()
    if blog_s:
        return blog_s[:120]
    catalog_s = str(catalog or "")
    slug = ""
    if catalog_s and code_s and catalog_s.startswith(code_s + "_"):
        slug = catalog_s[len(code_s) + 1 :]
    elif catalog_s.count("_") >= 2:
        slug = catalog_s.split("_", 2)[2]
    if not slug:
        return code_s
    pretty = re.sub(r"[-_]+", " ", slug).strip()
    if sum(character.isalpha() for character in pretty) < 3:
        return code_s
    return pretty[:1].upper() + pretty[1:]


def list_stores(cursor) -> dict:
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        """
        WITH chosen AS (
            SELECT DISTINCT ON (fdm4_store)
                   fdm4_store, catalog_id, products, suggested
              FROM woo.store_catalog
             WHERE suggested = true
             ORDER BY fdm4_store, suggested DESC, products DESC, catalog_id
        ), assignment_counts AS (
            SELECT fdm4_store,
                   count(*) AS assignment_count,
                   count(DISTINCT product_style) AS assigned_styles
              FROM logo.assignment
             GROUP BY fdm4_store
        ), blogs AS (
            SELECT fdm4_store,
                   min(blog_id) AS blog_id,
                   string_agg(blog_id::text, ', ' ORDER BY blog_id) AS blog_ids,
                   min(blog_path) AS blog_path
              FROM woo.store_blog_map
             GROUP BY fdm4_store
        ), blog_names AS (
            SELECT DISTINCT ON (fdm4_store) fdm4_store, blog_name
              FROM woo.store_blog_map
             ORDER BY fdm4_store, blog_id
        )
        SELECT left(c.fdm4_store, 256) AS fdm4_store,
               left(c.catalog_id, 256) AS catalog_id,
               COALESCE(c.products, 0) AS products,
               COALESCE(a.assignment_count, 0) AS assignment_count,
               COALESCE(a.assigned_styles, 0) AS assigned_styles,
               COALESCE(s.enabled, true) AS enabled,
               COALESCE(s.allows_none, false) AS allows_none,
               b.blog_id,
               left(COALESCE(b.blog_ids, ''), 64) AS blog_ids,
               left(COALESCE(b.blog_path, ''), 128) AS blog_path,
               left(COALESCE(bn.blog_name, ''), 200) AS blog_name
          FROM chosen c
          LEFT JOIN assignment_counts a USING (fdm4_store)
          LEFT JOIN logo.store_settings s USING (fdm4_store)
          LEFT JOIN blogs b USING (fdm4_store)
          LEFT JOIN blog_names bn USING (fdm4_store)
         ORDER BY c.fdm4_store
         LIMIT %s
        """,
        (STORE_RESULT_LIMIT + 1,),
        STORE_RESULT_LIMIT,
    )
    for row in rows:
        row["display_name"] = _store_display_name(
            row.get("fdm4_store"), row.get("catalog_id"), row.get("blog_name")
        )
    rows.sort(key=lambda row: str(row.get("display_name", "")).lower())
    return {
        "stores": rows,
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


def list_styles(
    cursor,
    *,
    store: str,
    q: str = "",
    active_only: bool = True,
    assigned_only: bool = True,
) -> dict:
    store = _clean(store, "store")
    q = _optional(q, "q")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise QueryNotFound("Store not found")
    active_clauses = []
    if active_only:
        active_clauses.append("AND s.product_style IS NOT NULL")
    if assigned_only:
        active_clauses.append(
            "AND COALESCE(c.active_count, c.assignment_count, 0) > 0"
        )
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        f"""
        WITH state_styles AS (
            SELECT style_code AS product_style,
                   max(name) FILTER (WHERE kind = 'parent') AS name,
                   count(DISTINCT color_code) FILTER (
                       WHERE kind = 'variation' AND color_code IS NOT NULL
                   ) AS color_count
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND catalog_id = %s
               AND is_active = true AND style_code IS NOT NULL
             GROUP BY style_code
        ), configured AS (
            SELECT product_style,
                   count(*) AS assignment_count,
                   count(*) FILTER (WHERE active) AS active_count
              FROM logo.assignment
             WHERE fdm4_store = %s
             GROUP BY product_style
        )
        SELECT left(COALESCE(s.product_style, c.product_style), 256)
                   AS product_style,
               left(COALESCE(s.name, ''), {READ_TEXT_CHAR_LIMIT}) AS name,
               COALESCE(s.color_count, 0) AS color_count,
               COALESCE(c.assignment_count, 0) AS assignment_count,
               COALESCE(c.active_count, 0) AS active_count
          FROM state_styles s
          FULL OUTER JOIN configured c USING (product_style)
         WHERE (
                COALESCE(s.product_style, c.product_style) ILIKE %s ESCAPE '\\'
             OR COALESCE(s.name, '') ILIKE %s ESCAPE '\\'
         )
         {' '.join(active_clauses)}
         ORDER BY COALESCE(s.product_style, c.product_style)
         LIMIT %s
        """,
        (
            store,
            catalog,
            store,
            _like(q),
            _like(q),
            STYLE_SEARCH_RESULT_LIMIT + 1,
        ),
        STYLE_SEARCH_RESULT_LIMIT,
    )
    return {
        "store": store,
        "styles": rows,
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


def get_style(cursor, *, store: str, style: str) -> dict:
    store = _clean(store, "store")
    style = _clean(style, "style")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise QueryNotFound("Store not found")
    if not _style_exists(cursor, store, catalog, style):
        cursor.execute(
            "SELECT 1 FROM logo.assignment "
            "WHERE fdm4_store = %s AND product_style = %s LIMIT 1",
            (store, style),
        )
        if cursor.fetchone() is None:
            raise QueryNotFound("Style not found")

    colors, colors_truncated, colors_byte_truncated = _bounded_query(
        cursor,
        f"""
        WITH active_colors AS (
            SELECT color_code AS code, max(color) AS name
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND catalog_id = %s AND style_code = %s
               AND kind = 'variation' AND is_active = true
               AND NULLIF(btrim(color_code), '') IS NOT NULL
             GROUP BY color_code
        ), configured_colors AS (
            SELECT DISTINCT garment_color_code AS code
              FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s
        )
        SELECT left(COALESCE(a.code, c.code), 256) AS code,
               left(
                   COALESCE(NULLIF(a.name, ''), c.code),
                   {READ_TEXT_CHAR_LIMIT}
               ) AS name,
               (a.code IS NOT NULL) AS warehouse_active,
               o.sort_order AS editor_order
          FROM active_colors a
          FULL OUTER JOIN configured_colors c USING (code)
          LEFT JOIN logo.style_color_order o
            ON o.product_style = %s
           AND o.garment_color_code = COALESCE(a.code, c.code)
         ORDER BY o.sort_order NULLS LAST,
                  COALESCE(NULLIF(a.name, ''), c.code), COALESCE(a.code, c.code)
         LIMIT %s
        """,
        (
            store,
            catalog,
            style,
            store,
            style,
            style,
            STYLE_COLOR_RESULT_LIMIT + 1,
        ),
        STYLE_COLOR_RESULT_LIMIT,
    )
    assignment_rows, assignments_truncated, assignments_byte_truncated = (
        _bounded_query(
            cursor,
            f"""
            SELECT left(a.fdm4_store, 256) AS fdm4_store,
                   left(a.product_style, 256) AS product_style,
                   left(a.garment_color_code, 256) AS garment_color_code,
                   a.option_row, a.position,
                   left(a.design_id, 256) AS design_id,
                   left(a.logo_code, 256) AS logo_code,
                   left(a.color_scheme_id, 256) AS color_scheme_id,
                   left(dn.name, {READ_TEXT_CHAR_LIMIT}) AS display_name,
                   left(a.location, {READ_TEXT_CHAR_LIMIT}) AS location,
                   a.optional,
                   left(a.background, {READ_TEXT_CHAR_LIMIT}) AS background,
                   a.cost_override, a.sort_order,
                   dc.cost AS default_cost,
                   left(a.image_url, {READ_URL_CHAR_LIMIT}) AS image_url,
                   left(COALESCE(a.name_override, ''), {READ_TEXT_CHAR_LIMIT})
                       AS name_override,
                   a.active,
                   left(a.updated_by, 256) AS updated_by, a.updated_at,
                   bool_and(a.active) OVER () AS _style_active
              FROM logo.assignment a
              LEFT JOIN logo.default_cost dc
                     ON upper(btrim(dc.logo_code)) = upper(btrim(a.logo_code))
                    AND upper(btrim(dc.color_scheme_id)) = upper(btrim(a.color_scheme_id))
              LEFT JOIN LATERAL (
                  SELECT candidate.name
                    FROM logo.display_name candidate
                   WHERE candidate.design_id = a.design_id
                     AND candidate.color_scheme_id = a.color_scheme_id
                     AND candidate.fdm4_store IN (a.fdm4_store, '')
                   ORDER BY (candidate.fdm4_store = a.fdm4_store) DESC
                   LIMIT 1
              ) dn ON true
             WHERE a.fdm4_store = %s AND a.product_style = %s
             ORDER BY a.garment_color_code, a.sort_order, a.option_row, a.position
             LIMIT %s
            """,
            (store, style, STYLE_ASSIGNMENT_RESULT_LIMIT + 1),
            STYLE_ASSIGNMENT_RESULT_LIMIT,
        )
    )
    style_active = (
        bool(assignment_rows[0]["_style_active"])
        if assignment_rows
        else True
    )
    # What the shopper actually pays for the row: the explicit override wins,
    # then the automatic default for (logo code, scheme); None = only the
    # FDM4 design upcharge (not visible here) or free.
    for _row in assignment_rows:
        _override = _row.get("cost_override")
        _default = _row.get("default_cost")
        if _override is not None:
            _row["effective_cost"], _row["effective_cost_source"] = _override, "override"
        elif _default is not None:
            _row["effective_cost"], _row["effective_cost_source"] = _default, "default"
        else:
            _row["effective_cost"], _row["effective_cost_source"] = None, "none"
    assignments = []
    for row in assignment_rows:
        row.pop("_style_active", None)
        assignments.append(row)
    cursor.execute(
        """
        SELECT COALESCE(enabled, true) AS enabled,
               COALESCE(allows_none, false) AS allows_none,
               updated_at
          FROM logo.store_settings
         WHERE fdm4_store = %s
        """,
        (store,),
    )
    settings_row = cursor.fetchone()
    settings = dict(settings_row) if settings_row else {
        "enabled": True,
        "allows_none": False,
        "updated_at": None,
    }
    return {
        "store": store,
        "style": style,
        "settings": settings,
        "style_active": style_active,
        "colors": colors,
        "assignments": assignments,
        "truncated": (
            colors_truncated
            or assignments_truncated
            or colors_byte_truncated
            or assignments_byte_truncated
        ),
        "truncation": {
            "rows": colors_truncated or assignments_truncated,
            "bytes": colors_byte_truncated or assignments_byte_truncated,
            "colors": colors_truncated,
            "assignments": assignments_truncated,
            "colors_bytes": colors_byte_truncated,
            "assignments_bytes": assignments_byte_truncated,
        },
    }


def find_similar_styles(cursor, *, fdm4_store: str, product_style: str,
                        mode: str = "exact") -> dict:
    """Styles of one store whose LOGO SET matches the source style's.

    A style's logo set is the DISTINCT (design_id, upper(color_scheme_id),
    position, location) tuples across its ACTIVE assignments - garment colors
    and option rows are ignored, so "the same logos, on whatever colors"
    matches. mode='exact' returns styles whose set equals the source's;
    mode='overlap' returns every style sharing at least one tuple, most
    shared first, with shared / only_in_source / only_in_target counts.
    Read-only and bounded (SIMILAR_STYLE_RESULT_LIMIT)."""
    store = _clean(fdm4_store, "fdm4_store")
    style = _clean(product_style, "product_style")
    if mode not in ("exact", "overlap"):
        raise QueryValidationError("mode must be 'exact' or 'overlap'")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise QueryNotFound("Store not found")
    source_live = _style_exists(cursor, store, catalog, style)
    if not source_live:
        cursor.execute(
            "SELECT 1 FROM logo.assignment "
            "WHERE fdm4_store = %s AND product_style = %s LIMIT 1",
            (store, style),
        )
        if cursor.fetchone() is None:
            raise QueryNotFound("Style not found")
    logo_set, set_truncated, set_byte_truncated = _bounded_query(
        cursor,
        f"""
        SELECT DISTINCT left(a.design_id, 256) AS design_id,
               left(upper(a.color_scheme_id), 256) AS color_scheme_id,
               a.position,
               left(a.location, {READ_TEXT_CHAR_LIMIT}) AS location
          FROM logo.assignment a
         WHERE a.fdm4_store = %s AND a.product_style = %s AND a.active
         ORDER BY 3, 1, 2, 4
         LIMIT %s
        """,
        (store, style, LOGO_SET_RESULT_LIMIT + 1),
        LOGO_SET_RESULT_LIMIT,
    )
    exact_clause = (
        "AND sc.only_in_target = 0 AND sc.shared = ss.n" if mode == "exact" else ""
    )
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        f"""
        WITH source AS (
            SELECT DISTINCT a.design_id,
                   upper(a.color_scheme_id) AS color_scheme_id,
                   a.position, a.location
              FROM logo.assignment a
             WHERE a.fdm4_store = %s AND a.product_style = %s AND a.active
        ), source_size AS (
            SELECT count(*)::integer AS n FROM source
        ), candidate AS (
            SELECT DISTINCT a.product_style, a.design_id,
                   upper(a.color_scheme_id) AS color_scheme_id,
                   a.position, a.location
              FROM logo.assignment a
             WHERE a.fdm4_store = %s AND a.product_style <> %s AND a.active
        ), scored AS (
            SELECT c.product_style,
                   count(s.design_id)::integer AS shared,
                   (count(*) - count(s.design_id))::integer AS only_in_target
              FROM candidate c
              LEFT JOIN source s
                ON s.design_id = c.design_id
               AND s.color_scheme_id = c.color_scheme_id
               AND s.position = c.position
               AND s.location = c.location
             GROUP BY c.product_style
        ), live AS (
            SELECT style_code AS product_style,
                   max(name) FILTER (WHERE kind = 'parent') AS name
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND catalog_id = %s AND is_active = true
               AND style_code IS NOT NULL
             GROUP BY style_code
        )
        SELECT left(sc.product_style, 256) AS style,
               left(COALESCE(l.name, ''), {READ_TEXT_CHAR_LIMIT}) AS name,
               sc.shared,
               (ss.n - sc.shared) AS only_in_source,
               sc.only_in_target,
               (l.product_style IS NOT NULL) AS warehouse_active
          FROM scored sc
         CROSS JOIN source_size ss
          LEFT JOIN live l ON l.product_style = sc.product_style
         WHERE sc.shared > 0
           {exact_clause}
         ORDER BY sc.shared DESC, sc.only_in_target, (ss.n - sc.shared), sc.product_style
         LIMIT %s
        """,
        (store, style, store, style, store, catalog, SIMILAR_STYLE_RESULT_LIMIT + 1),
        SIMILAR_STYLE_RESULT_LIMIT,
    )
    return {
        "store": store,
        "mode": mode,
        "source": {
            "style": style,
            "warehouse_active": source_live,
            "logo_set": logo_set,
        },
        "styles": rows,
        "truncated": truncated or byte_truncated or set_truncated or set_byte_truncated,
        "truncation": {
            "rows": truncated or set_truncated,
            "bytes": byte_truncated or set_byte_truncated,
        },
    }


def store_logo_coverage(cursor, *, fdm4_store: str,
                        unconfigured_only: bool = True) -> dict:
    """Per style in the store's live catalog: how many of its active garment
    colors carry at least one ACTIVE logo assignment, and which do not.
    unconfigured_only keeps only styles with >= 1 color lacking logos.
    Read-only and bounded (COVERAGE_STYLE_RESULT_LIMIT styles; the
    unconfigured list per style is capped at STYLE_COLOR_RESULT_LIMIT)."""
    store = _clean(fdm4_store, "fdm4_store")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise QueryNotFound("Store not found")
    where_clause = "WHERE cardinality(unconfigured) > 0" if unconfigured_only else ""
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        f"""
        WITH live AS (
            SELECT style_code AS product_style,
                   max(name) FILTER (WHERE kind = 'parent') AS name,
                   COALESCE(array_agg(DISTINCT color_code) FILTER (
                       WHERE kind = 'variation'
                         AND NULLIF(btrim(color_code), '') IS NOT NULL
                   ), '{{}}'::text[]) AS colors
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND catalog_id = %s AND is_active = true
               AND style_code IS NOT NULL
             GROUP BY style_code
        ), configured AS (
            SELECT product_style,
                   array_agg(DISTINCT garment_color_code) AS colors
              FROM logo.assignment
             WHERE fdm4_store = %s AND active
             GROUP BY product_style
        ), scored AS (
            SELECT l.product_style, l.name, l.colors,
                   ARRAY(
                       SELECT c FROM unnest(l.colors) AS c
                        WHERE c <> ALL (COALESCE(cf.colors, '{{}}'::text[]))
                        ORDER BY c
                   ) AS unconfigured
              FROM live l
              LEFT JOIN configured cf USING (product_style)
        )
        SELECT left(product_style, 256) AS style,
               left(COALESCE(name, ''), {READ_TEXT_CHAR_LIMIT}) AS name,
               cardinality(colors) AS colors_total,
               (cardinality(colors) - cardinality(unconfigured)) AS colors_configured,
               unconfigured[1:{STYLE_COLOR_RESULT_LIMIT}] AS unconfigured
          FROM scored
         {where_clause}
         ORDER BY product_style
         LIMIT %s
        """,
        (store, catalog, store, COVERAGE_STYLE_RESULT_LIMIT + 1),
        COVERAGE_STYLE_RESULT_LIMIT,
    )
    return {
        "store": store,
        "unconfigured_only": bool(unconfigured_only),
        "styles": rows,
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


def fill_gaps_plan(cursor, *, fdm4_store: str, styles=None) -> dict:
    """Plan for copying a style's own configured logos onto its logo-less
    colors. Splits gap styles into `copyable` (>= 1 configured live color)
    and `no_source` (no logos anywhere). A copyable style gets an
    `auto_source` only when every configured color carries an IDENTICAL
    logo set; otherwise `needs_choice` is true and the operator picks the
    source color. Read-only."""
    store = _clean(fdm4_store, "fdm4_store")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise QueryNotFound("Store not found")
    style_filter = ""
    params: list = [store, catalog]
    if styles:
        style_filter = "AND style_code = ANY(%s)"
        params.append([_clean(s, "style") for s in styles])
    params.append(COVERAGE_STYLE_RESULT_LIMIT)
    cursor.execute(
        f"""
        SELECT style_code AS product_style,
               max(name) FILTER (WHERE kind = 'parent') AS name,
               COALESCE(array_agg(DISTINCT color_code) FILTER (
                   WHERE kind = 'variation'
                     AND NULLIF(btrim(color_code), '') IS NOT NULL
               ), '{{}}'::text[]) AS colors
          FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s AND is_active = true
           AND style_code IS NOT NULL {style_filter}
         GROUP BY style_code
         ORDER BY style_code
         LIMIT %s
        """,
        params,
    )
    live = {str(r["product_style"]): (str(r["name"] or ""), sorted(str(c) for c in r["colors"]))
            for r in cursor.fetchall()}
    if not live:
        return {"store": store, "copyable": [], "no_source": [], "truncated": False}
    cursor.execute(
        """
        SELECT product_style, garment_color_code, option_row, position, design_id,
               logo_code, color_scheme_id, location, optional, background,
               cost_override, sort_order, image_url, name_override
          FROM logo.assignment
         WHERE fdm4_store = %s AND active AND product_style = ANY(%s)
         ORDER BY product_style, garment_color_code, option_row, position
        """,
        (store, list(live)),
    )
    assigned: dict = {}
    for row in cursor.fetchall():
        style = str(row["product_style"])
        color = str(row["garment_color_code"])
        assigned.setdefault(style, {}).setdefault(color, []).append((
            int(row["option_row"]), int(row["position"]), str(row["design_id"]),
            str(row["logo_code"] or ""), str(row["color_scheme_id"] or ""),
            str(row["location"] or ""), bool(row["optional"]),
            str(row["background"] or ""),
            None if row["cost_override"] is None else float(row["cost_override"]),
            int(row["sort_order"] or 0), str(row["image_url"] or ""),
            row["name_override"],
        ))
    copyable, no_source = [], []
    for style in sorted(live):
        name, colors = live[style]
        by_color = assigned.get(style, {})
        configured = sorted(c for c in colors if c in by_color)
        targets = sorted(c for c in colors if c not in by_color)
        if not targets:
            continue
        if not configured:
            no_source.append({"style": style, "name": name, "unconfigured": targets})
            continue
        signatures = {c: tuple(by_color[c]) for c in configured}
        identical = len(set(signatures.values())) == 1
        auto_source = configured[0] if identical else None
        copyable.append({
            "style": style,
            "name": name,
            "targets": targets,
            "sources": [{"color": c, "rows": len(by_color[c])} for c in configured],
            "auto_source": auto_source,
            "needs_choice": not identical,
            "slots": len(by_color[auto_source]) * len(targets) if auto_source else None,
        })
    return {"store": store, "copyable": copyable, "no_source": no_source,
            "truncated": len(live) >= COVERAGE_STYLE_RESULT_LIMIT}


def search_designs(
    cursor,
    *,
    q: str = "",
    store: Optional[str] = None,
    used_only: bool = False,
) -> dict:
    q = _optional(q, "q")
    store_value = _optional(store, "store")
    where = """( btrim(d.design_id) ILIKE %(pattern)s ESCAPE '\\'
              OR COALESCE(d.description, '') ILIKE %(pattern)s ESCAPE '\\'
              OR COALESCE(d.web_description, '') ILIKE %(pattern)s ESCAPE '\\'
              OR EXISTS (
                  SELECT 1 FROM art a2
                   WHERE a2.design_id = btrim(d.design_id)
                     AND a2.logo_code ILIKE %(pattern)s ESCAPE '\\'
              )
              OR EXISTS (
                  SELECT 1 FROM logo.display_name dnx
                   WHERE dnx.design_id = btrim(d.design_id)
                     AND dnx.fdm4_store IN (%(store)s, '')
                     AND dnx.name ILIKE %(pattern)s ESCAPE '\\'
              ) )"""
    if not q:
        if used_only and store_value:
            # Bulk Apply's browse list: exactly the designs this store already
            # uses. The customer expansion below drags in every design owned
            # by shared art customers, which reads as "all stores' logos".
            where = "COALESCE(u.uses, 0) > 0"
        else:
            where = (
                "(COALESCE(u.uses, 0) > 0 OR "
                "btrim(d.cust_number) IN (SELECT cn FROM store_custs))"
                if store_value
                else "COALESCE(g.uses, 0) > 0"
            )
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        f"""
        WITH art AS (
            SELECT btrim(art_id) AS design_id,
                   upper(regexp_replace(
                       regexp_replace(target_filename, '^.*/', ''),
                       '[^A-Za-z0-9].*$', ''
                   )) AS logo_code,
                   upper(btrim(color_scheme_id)) AS color_scheme_id
              FROM fdm4.cust_art_file
             WHERE NULLIF(btrim(art_id), '') IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM fdm4.design_pool collision
                    WHERE btrim(collision.art_id) =
                          btrim(cust_art_file.art_id)
                      AND NULLIF(btrim(collision.art_id), '') IS NOT NULL
               )
             UNION
            -- Newer designs carry an art number different from the design
            -- number; that link lives only in design_pool.
            SELECT btrim(dp.design_id) AS design_id,
                   upper(regexp_replace(
                       regexp_replace(caf.target_filename, '^.*/', ''),
                       '[^A-Za-z0-9].*$', ''
                   )) AS logo_code,
                   upper(btrim(caf.color_scheme_id)) AS color_scheme_id
              FROM fdm4.design_pool dp
              JOIN fdm4.cust_art_file caf
                ON btrim(caf.art_id) = btrim(dp.art_id)
             WHERE NULLIF(btrim(dp.design_id), '') IS NOT NULL
        ), store_usage AS (
            SELECT design_id, count(*) AS uses
              FROM logo.assignment
             WHERE fdm4_store = %(store)s AND active = true
             GROUP BY design_id
        ), global_usage AS (
            SELECT design_id, count(*) AS uses
              FROM logo.assignment
             WHERE active = true
             GROUP BY design_id
        ), store_custs AS (
            SELECT DISTINCT btrim(d2.cust_number) AS cn
              FROM logo.assignment a
              JOIN fdm4.dec_design d2 ON btrim(d2.design_id) = a.design_id
             WHERE a.fdm4_store = %(store)s
               AND NULLIF(btrim(d2.cust_number), '') IS NOT NULL
        ), design_name AS (
            -- Curated shopper-facing logo name (from FDM4 design_pool); one
            -- representative per design = the most-used color scheme's name.
            SELECT DISTINCT ON (design_id)
                   design_id, name
              FROM logo.display_name
             WHERE NULLIF(btrim(name), '') IS NOT NULL
               AND fdm4_store IN (%(store)s, '')
             ORDER BY design_id,
                      (fdm4_store = %(store)s) DESC,
                      uses DESC NULLS LAST,
                      color_scheme_id
        )
        SELECT left(btrim(d.design_id), 256) AS design_id,
               left(
                   COALESCE(
                       NULLIF(btrim(dname.name), ''),
                       NULLIF(btrim(d.web_description), ''),
                       btrim(d.description),
                       ''
                   ),
                   {READ_TEXT_CHAR_LIMIT}
               ) AS description,
               left(COALESCE(btrim(d.methods_used), ''), 512) AS methods_used,
               left(COALESCE(btrim(d.cust_number), ''), 256) AS cust_number,
               COALESCE(u.uses, 0) AS store_uses,
               COALESCE(g.uses, 0) AS total_uses,
               (array_remove(
                   array_agg(DISTINCT left(art.logo_code, 256)), ''
               ))[1:100] AS logo_codes,
               -- Bulk Apply's dropdown offers one option per (logo code,
               -- color scheme) pair; an assignment stores the scheme's OWN
               -- logo code (e.g. TBXNV for NV), so the pair must stay linked.
               COALESCE(
                   jsonb_agg(DISTINCT jsonb_build_object(
                       'color_scheme_id', left(art.color_scheme_id, 64),
                       'logo_code', left(art.logo_code, 256)
                   )) FILTER (
                       WHERE COALESCE(art.color_scheme_id, '') <> ''
                         AND COALESCE(art.logo_code, '') <> ''
                   ),
                   '[]'::jsonb
               ) AS schemes
          FROM fdm4.dec_design d
          LEFT JOIN art ON art.design_id = btrim(d.design_id)
          LEFT JOIN store_usage u ON u.design_id = btrim(d.design_id)
          LEFT JOIN global_usage g ON g.design_id = btrim(d.design_id)
          LEFT JOIN design_name dname ON dname.design_id = btrim(d.design_id)
         WHERE {where}
         GROUP BY d.design_id, d.web_description, d.description, d.methods_used,
                  d.cust_number, u.uses, g.uses, dname.name
         ORDER BY
             CASE WHEN btrim(d.design_id) = %(exact)s THEN 0 ELSE 1 END,
             COALESCE(u.uses, 0) DESC,
             COALESCE(g.uses, 0) DESC,
             COALESCE(NULLIF(btrim(dname.name), ''), NULLIF(btrim(d.web_description), ''), btrim(d.description), ''),
             btrim(d.design_id)
         LIMIT %(limit)s
        """,
        {
            "pattern": _like(q),
            "store": store_value,
            "exact": q,
            "limit": DESIGN_SEARCH_RESULT_LIMIT + 1,
        },
        DESIGN_SEARCH_RESULT_LIMIT,
    )
    return {
        "designs": rows,
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


def get_assignment_vocab(cursor) -> dict:
    placements, placements_truncated, placements_byte_truncated = _bounded_query(
        cursor,
        f"""
        WITH usage AS (
            SELECT lower(btrim(location)) AS key, count(*) AS uses
              FROM logo.assignment
             WHERE btrim(location) <> ''
             GROUP BY 1
        ), vocab AS (
            SELECT btrim(name) AS location, lower(btrim(name)) AS key,
                   true AS canonical
              FROM logo.placement_vocab
             WHERE active
        ), ad_hoc AS (
            SELECT DISTINCT ON (lower(btrim(location)))
                   btrim(location) AS location,
                   lower(btrim(location)) AS key,
                   false AS canonical
              FROM logo.assignment
             WHERE btrim(location) <> ''
               AND lower(btrim(location)) NOT IN (SELECT key FROM vocab)
             ORDER BY lower(btrim(location)), location
        )
        SELECT left(entries.location, {READ_TEXT_CHAR_LIMIT}) AS location,
               coalesce(u.uses, 0)::bigint AS uses,
               entries.canonical
          FROM (SELECT * FROM vocab UNION ALL SELECT * FROM ad_hoc) entries
          LEFT JOIN usage u ON u.key = entries.key
         ORDER BY entries.canonical DESC, uses DESC, entries.location
         LIMIT %s
        """,
        (ASSIGNMENT_PLACEMENT_RESULT_LIMIT + 1,),
        ASSIGNMENT_PLACEMENT_RESULT_LIMIT,
    )
    backgrounds, backgrounds_truncated, backgrounds_byte_truncated = _bounded_query(
        cursor,
        f"""
        SELECT left(background, {READ_TEXT_CHAR_LIMIT}) AS background, uses FROM (
            SELECT DISTINCT ON (lower(btrim(background)))
                   btrim(background) AS background,
                   (sum(count(*)) OVER (
                       PARTITION BY lower(btrim(background))
                   ))::bigint AS uses
              FROM logo.assignment
             WHERE btrim(background) <> ''
             GROUP BY btrim(background)
             ORDER BY lower(btrim(background)), count(*) DESC, btrim(background)
        ) b
        ORDER BY uses DESC, background
        LIMIT %s
        """,
        (ASSIGNMENT_BACKGROUND_RESULT_LIMIT + 1,),
        ASSIGNMENT_BACKGROUND_RESULT_LIMIT,
    )
    return {
        "placements": placements,
        "backgrounds": backgrounds,
        "truncated": (
            placements_truncated
            or backgrounds_truncated
            or placements_byte_truncated
            or backgrounds_byte_truncated
        ),
        "truncation": {
            "rows": placements_truncated or backgrounds_truncated,
            "bytes": (
                placements_byte_truncated or backgrounds_byte_truncated
            ),
            "placements": placements_truncated,
            "backgrounds": backgrounds_truncated,
            "placements_bytes": placements_byte_truncated,
            "backgrounds_bytes": backgrounds_byte_truncated,
        },
    }


# Per-color live style counts, computed in ONE grouped scan and cached in
# process memory. The underlying data only changes on the hourly warehouse
# refresh, while the old per-row correlated subquery cost ~130ms x 200 rows
# per Colors page (25s+, tripping the 30s statement timeout during refreshes
# and surfacing as "warehouse database unavailable").
_COLOR_STYLE_COUNTS: dict[str, Any] = {"at": 0.0, "map": None}
_COLOR_STYLE_COUNTS_TTL = 600.0
_COLOR_STYLE_COUNTS_LOCK = threading.Lock()


def _color_style_counts(cursor) -> dict:
    now = time.monotonic()
    cached = _COLOR_STYLE_COUNTS["map"]
    if cached is not None and now - _COLOR_STYLE_COUNTS["at"] < _COLOR_STYLE_COUNTS_TTL:
        return cached
    with _COLOR_STYLE_COUNTS_LOCK:
        cached = _COLOR_STYLE_COUNTS["map"]
        if cached is not None and time.monotonic() - _COLOR_STYLE_COUNTS["at"] < _COLOR_STYLE_COUNTS_TTL:
            return cached
        cursor.execute(
            """
            SELECT color_code, count(DISTINCT style_code) AS style_count
              FROM woo.store_product_state
             WHERE is_active AND kind = 'variation'
               AND NULLIF(btrim(color_code), '') IS NOT NULL
             GROUP BY color_code
            """
        )
        fresh = {
            str(row["color_code"]): int(row["style_count"])
            for row in cursor.fetchall()
        }
        _COLOR_STYLE_COUNTS["map"] = fresh
        _COLOR_STYLE_COUNTS["at"] = time.monotonic()
        return fresh


def list_colors(
    cursor,
    *,
    q: str = "",
    cls: str = "",
    needs_review: bool = False,
    sort: str = "",
    direction: str = "asc",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    q = _optional(q, "q")
    safe_limit = max(1, min(int(limit), COLOR_CLASS_RESULT_LIMIT))
    safe_offset = max(int(offset), 0)
    where_parts = ["1=1"]
    params: list[Any] = []
    if q:
        where_parts.append(
            "(cc.color_name ILIKE %s ESCAPE '\\' OR cc.color_code ILIKE %s ESCAPE '\\')"
        )
        pat = _like(q)
        params.extend([pat, pat])
    if cls in ("light", "dark", "both"):
        where_parts.append("cc.light_dark = %s")
        params.append(cls)
    if needs_review:
        where_parts.append("cc.source = 'ai' AND COALESCE(cc.confidence, 0) < 0.7")
    where_sql = " AND ".join(where_parts)
    # The classification table is small (~1.5k rows), so fetch the whole
    # matching set (hard-capped) and sort/paginate in Python - the only way to
    # sort by the cached live style counts, and it keeps totals/summary exact.
    cursor.execute(
        f"""
        SELECT left(cc.color_code, 256) AS color_code,
               left(cc.color_name, {READ_TEXT_CHAR_LIMIT}) AS color_name,
               cc.light_dark, cc.source, cc.confidence
          FROM logo.color_class cc
         WHERE {where_sql}
         LIMIT {COLOR_CLASS_SCAN_LIMIT + 1}
        """,
        tuple(params),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    scan_truncated = len(rows) > COLOR_CLASS_SCAN_LIMIT
    rows = rows[:COLOR_CLASS_SCAN_LIMIT]
    counts = _color_style_counts(cursor)
    for row in rows:
        row["style_count"] = counts.get(str(row["color_code"]), 0)

    def _needs_review(row: dict) -> bool:
        return row["source"] == "ai" and (row["confidence"] or 0) < 0.7

    sort_keys = {
        "name": lambda r: (r["color_name"] or "").lower(),
        "code": lambda r: r["color_code"] or "",
        "styles": lambda r: r["style_count"],
        "class": lambda r: r["light_dark"] or "",
        "source": lambda r: r["source"] or "",
        "confidence": lambda r: r["confidence"] if r["confidence"] is not None else -1,
    }
    if sort in sort_keys:
        rows.sort(key=sort_keys[sort], reverse=(direction == "desc"))
    else:
        # Default: review-needed first, least-confident first, then name.
        rows.sort(
            key=lambda r: (
                not _needs_review(r),
                r["confidence"] if r["confidence"] is not None else -1,
                (r["color_name"] or "").lower(),
            )
        )

    summary = {
        "light": sum(1 for r in rows if r["light_dark"] == "light"),
        "dark": sum(1 for r in rows if r["light_dark"] == "dark"),
        "both": sum(1 for r in rows if r["light_dark"] == "both"),
        "review": sum(1 for r in rows if _needs_review(r)),
    }
    total = len(rows)
    page = rows[safe_offset : safe_offset + safe_limit]
    return {
        "colors": page,
        "total": total,
        "summary": summary,
        "truncated": scan_truncated,
        "truncation": {"rows": scan_truncated, "bytes": False},
    }


def get_design(
    cursor,
    *,
    design_id: str,
    fdm4_art_base: str,
) -> dict:
    design_id = _clean(design_id, "design_id")
    cursor.execute(
        """
        SELECT left(btrim(design_id), 256) AS design_id,
               left(
                   COALESCE(
                       NULLIF(btrim(web_description), ''),
                       btrim(description),
                       ''
                   ),
                   1024
               ) AS description,
               left(COALESCE(btrim(methods_used), ''), 512) AS methods_used,
               left(COALESCE(btrim(design_categ_id), ''), 256) AS category,
               left(COALESCE(btrim(cust_number), ''), 256) AS customer
          FROM fdm4.dec_design
         WHERE btrim(design_id) = %s
         LIMIT 1
        """,
        (design_id,),
    )
    design = cursor.fetchone()
    if design is None:
        raise QueryNotFound("Design not found")
    asset_rows, assets_truncated, assets_byte_truncated = _bounded_query(
        cursor,
        """
        SELECT left(upper(btrim(caf.color_scheme_id)), 256)
                   AS color_scheme_id,
               left(upper(btrim(caf.resource_type)), 256) AS resource_type,
               left(
                   COALESCE(NULLIF(btrim(caf.target_web_path), ''), ''),
                   2048
               ) AS target_web_path,
               left(
                   COALESCE(NULLIF(btrim(caf.target_filename), ''), ''),
                   2048
               ) AS target_filename,
               left(
                   COALESCE(NULLIF(a.image_url, ''), ''),
                   2048
               ) AS assignment_image_url
          FROM fdm4.cust_art_file caf
          LEFT JOIN LATERAL (
              SELECT image_url
                FROM logo.assignment la
               WHERE (btrim(la.design_id) = btrim(caf.art_id)
                      OR btrim(la.design_id) IN (
                          SELECT btrim(dp.design_id) FROM fdm4.design_pool dp
                           WHERE btrim(dp.art_id) = btrim(caf.art_id)
                      ))
                 AND upper(btrim(la.color_scheme_id)) = upper(btrim(caf.color_scheme_id))
                 AND NULLIF(la.image_url, '') IS NOT NULL
               ORDER BY la.updated_at DESC
               LIMIT 1
          ) a ON true
         WHERE btrim(caf.art_id) IN (
                   -- The design's art number(s) per design_pool, falling back
                   -- to the design number itself (legacy same-number art).
                   SELECT btrim(dp.art_id) FROM fdm4.design_pool dp
                    WHERE btrim(dp.design_id) = %s
                      AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
                   UNION ALL
                   SELECT %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM fdm4.design_pool mapped
                         WHERE btrim(mapped.design_id) = %s
                           AND NULLIF(btrim(mapped.art_id), '') IS NOT NULL
                    )
               )
         ORDER BY upper(btrim(caf.color_scheme_id)),
                  upper(btrim(caf.resource_type)), caf.target_filename
         LIMIT %s
        """,
        (
            design_id, design_id, design_id,
            DESIGN_ASSET_RESULT_LIMIT + 1,
        ),
        DESIGN_ASSET_RESULT_LIMIT,
    )
    scheme_map: dict[str, dict[str, Any]] = {}
    for row in asset_rows:
        scheme_id = str(row["color_scheme_id"] or "")
        scheme = scheme_map.setdefault(
            scheme_id,
            {
                "color_scheme_id": scheme_id,
                "warehouse_image_url": str(row["assignment_image_url"] or ""),
                "preview_url": str(row["assignment_image_url"] or ""),
                "assets": [],
            },
        )
        if not scheme["warehouse_image_url"] and row["assignment_image_url"]:
            scheme["warehouse_image_url"] = str(row["assignment_image_url"])
        asset_path = str(row["target_web_path"] or row["target_filename"] or "")
        asset_url = (
            (fdm4_art_base + asset_path.lstrip("/"))[:READ_URL_CHAR_LIMIT]
            if asset_path
            else ""
        )
        asset = {
            "resource_type": row["resource_type"],
            "target_web_path": row["target_web_path"],
            "target_filename": row["target_filename"],
            "url": asset_url,
        }
        scheme["assets"].append(asset)
        if (
            not scheme["preview_url"]
            and str(row["resource_type"] or "").upper() in {"PREVIEW", "THUMB"}
            and asset_url
        ):
            scheme["preview_url"] = asset_url
    placements, placements_truncated, placements_byte_truncated = _bounded_query(
        cursor,
        """
        SELECT DISTINCT
               left(COALESCE(btrim(location_id), ''), 1024) AS location,
               left(COALESCE(btrim(method_id), ''), 256) AS method,
               left(COALESCE(btrim(design_color_scheme_id), ''), 256)
                   AS color_scheme_id,
               left(COALESCE(btrim(description), ''), 1024) AS description,
               CASE
                   WHEN btrim(stitch_count) ~ '^[0-9]+$'
                   THEN btrim(stitch_count)::integer
                   ELSE NULL
               END AS stitch_count
         FROM fdm4.design_pool
         WHERE btrim(design_id) = %s
         ORDER BY location, method, color_scheme_id
         LIMIT %s
        """,
        (design_id, DESIGN_PLACEMENT_RESULT_LIMIT + 1),
        DESIGN_PLACEMENT_RESULT_LIMIT,
    )
    schemes = list(scheme_map.values())
    for scheme in schemes:
        scheme["is_colorway"] = bool(scheme["color_scheme_id"])
        scheme["name"] = (
            scheme["color_scheme_id"]
            if scheme["color_scheme_id"]
            else "No colorway on file - production art only"
        )
    colorways = [scheme for scheme in schemes if scheme["is_colorway"]]
    if colorways:
        schemes = colorways
    return {
        "design": dict(design),
        "schemes": schemes,
        "placements": placements,
        "truncated": (
            assets_truncated
            or placements_truncated
            or assets_byte_truncated
            or placements_byte_truncated
        ),
        "truncation": {
            "rows": assets_truncated or placements_truncated,
            "bytes": assets_byte_truncated or placements_byte_truncated,
            "assets": assets_truncated,
            "placements": placements_truncated,
            "assets_bytes": assets_byte_truncated,
            "placements_bytes": placements_byte_truncated,
        },
    }


def get_store_settings(cursor, *, store: str) -> dict:
    store = _clean(store, "store")
    if _catalog_for_store(cursor, store) is None:
        raise QueryNotFound("Store not found")
    cursor.execute(
        """
        SELECT enabled, allows_none,
               left(updated_by, 256) AS updated_by, updated_at
          FROM logo.store_settings
         WHERE fdm4_store = %s
        """,
        (store,),
    )
    row = cursor.fetchone()
    if row is None:
        return {
            "settings": {"enabled": True, "allows_none": False},
            "exists": False,
        }
    return {"settings": dict(row), "exists": True}


def get_import_report(
    cursor,
    *,
    store: Optional[str] = None,
    reason: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    if not 1 <= int(limit) <= 500 or int(offset) < 0:
        raise QueryValidationError("invalid report page")
    clauses = []
    params: list[Any] = []
    if store:
        clauses.append("fdm4_store = %s")
        params.append(_clean(store, "store"))
    if reason:
        clauses.append("reason = %s")
        params.append(_clean(reason, "reason"))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    cursor.execute(
        f"SELECT count(*) AS total FROM logo.import_report{where}",
        tuple(params),
    )
    total = int(cursor.fetchone()["total"])
    rows, rows_truncated, bytes_truncated = _bounded_query(
        cursor,
        f"""
        SELECT id, imported_at,
               left(fdm4_store, 256) AS fdm4_store,
               left(product_style, 256) AS product_style,
               left(product_color, 256) AS product_color,
               left(logo_code, 256) AS logo_code,
               left(reason, 256) AS reason,
               left(detail, {READ_TEXT_CHAR_LIMIT}) AS detail
          FROM logo.import_report
          {where}
         ORDER BY imported_at DESC, id DESC
         LIMIT %s OFFSET %s
        """,
        tuple(params + [int(limit) + 1, int(offset)]),
        int(limit),
    )
    return {
        "reports": rows,
        "total": total,
        "limit": int(limit),
        "offset": int(offset),
        "truncated": rows_truncated or bytes_truncated,
        "truncation": {"rows": rows_truncated, "bytes": bytes_truncated},
    }


def get_audit_log(
    cursor,
    *,
    store: Optional[str] = None,
    style: Optional[str] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> dict:
    if not 1 <= int(limit) <= 200:
        raise QueryValidationError("invalid audit-log limit")
    clauses = []
    params: list[Any] = []
    for column, value, field in (
        ("fdm4_store", store, "store"),
        ("product_style", style, "style"),
        ("actor", actor, "actor"),
        ("action", action, "action"),
    ):
        if value:
            clauses.append(f"{column} = %s")
            params.append(_clean(value, field))
    if before_id is not None:
        if int(before_id) < 1:
            raise QueryValidationError("before_id is invalid")
        clauses.append("id < %s")
        params.append(int(before_id))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows, rows_truncated, bytes_truncated = _bounded_query(
        cursor,
        f"""
        SELECT id, at,
               left(actor, 256) AS actor,
               left(action, 256) AS action,
               left(fdm4_store, 256) AS fdm4_store,
               left(product_style, 256) AS product_style,
               left(garment_color_code, 256) AS garment_color_code,
               option_row, position, detail
          FROM logo.audit_log
          {where}
         ORDER BY id DESC
         LIMIT %s
        """,
        tuple(params + [int(limit) + 1]),
        int(limit),
    )
    return {
        "entries": rows,
        "next_before_id": rows[-1]["id"] if rows and rows_truncated else None,
        "truncated": rows_truncated or bytes_truncated,
        "truncation": {"rows": rows_truncated, "bytes": bytes_truncated},
    }


def list_pricing_tiers(cursor) -> dict:
    rows, rows_truncated, bytes_truncated = _bounded_query(
        cursor,
        "SELECT left(tier_name, 256) AS tier_name, "
        "left(price_levels_key, 256) AS price_levels_key, "
        "is_msrp, sort_order FROM woo.pricing_tier "
        "ORDER BY sort_order, tier_name LIMIT %s",
        (PRICING_TIER_RESULT_LIMIT + 1,),
        PRICING_TIER_RESULT_LIMIT,
    )
    return {
        "tiers": rows,
        "truncated": rows_truncated or bytes_truncated,
        "truncation": {"rows": rows_truncated, "bytes": bytes_truncated},
    }


def list_store_pricing_tiers(cursor) -> dict:
    rows, rows_truncated, bytes_truncated = _bounded_query(
        cursor,
        f"""
        SELECT left(spt.fdm4_store, 256) AS fdm4_store,
               left(spt.tier_name, 256) AS tier_name,
               left(spt.note, {READ_TEXT_CHAR_LIMIT}) AS note,
               spt.updated_at,
               left(sc.catalog_id, 256) AS catalog_id,
               left(COALESCE(bm.blog_name, ''), 200) AS blog_name
          FROM woo.store_pricing_tier spt
          LEFT JOIN LATERAL (
              SELECT blog_name
                FROM woo.store_blog_map
               WHERE fdm4_store = spt.fdm4_store
               ORDER BY blog_id
               LIMIT 1
         ) bm ON true
          LEFT JOIN LATERAL (
              SELECT catalog_id
                FROM woo.store_catalog
               WHERE fdm4_store = spt.fdm4_store
               ORDER BY suggested DESC, catalog_id
               LIMIT 1
         ) sc ON true
         ORDER BY spt.fdm4_store
         LIMIT %s
        """,
        (STORE_PRICING_TIER_RESULT_LIMIT + 1,),
        STORE_PRICING_TIER_RESULT_LIMIT,
    )
    for row in rows:
        row["display_name"] = _store_display_name(
            row["fdm4_store"], row.get("catalog_id"), row.get("blog_name")
        )
    return {
        "assignments": rows,
        "truncated": rows_truncated or bytes_truncated,
        "truncation": {"rows": rows_truncated, "bytes": bytes_truncated},
    }


def compute_bulk_preview(
    cursor,
    *,
    fdm4_store: str,
    logo_code: str,
    color_scheme: str,
    target: dict,
    style_codes=None,
    option_row: int = 1,
    design_id: Optional[str] = None,
) -> dict:
    """Compute, for one store + one logo variant + a target filter, the set of
    (style, color) -> this variant changes with a 'was' diff.  Read-only.

    target must be one of:
      {"mode": "light_dark", "class": "light"|"dark"}
      {"mode": "colors", "color_codes": <iterable of color_code strings>}

    style_codes: optional iterable of style_code strings to narrow the scope.

    Returns a dict with:
      "rows":    list of {style_code, color_code, color, was, new} dicts
      "counts":  {"total": int, "unclassified": int}
      "design_id": str (resolved design)
    or on failure:
      {"rows": [], "counts": {"total": 0}, "unresolved_reason": "<reason>"}
    """
    logo_code = logo_code.upper()
    scheme = color_scheme.upper()

    design_index = legacy_import.load_design_lookup(cursor)
    designs = set(design_index.candidates(fdm4_store, logo_code, scheme))
    wanted = str(design_id).strip() if design_id not in (None, "") else ""
    if wanted:
        # A logo code is an art-file prefix and can be shared by several
        # designs of one customer; an explicit design id settles it.
        if designs and wanted not in designs:
            return {
                "rows": [],
                "counts": {"total": 0},
                "unresolved_reason": (
                    f"design {wanted} does not carry {logo_code}/{scheme}"
                    f" (candidates: {', '.join(sorted(designs))})"
                ),
            }
        designs = {wanted}
    if not designs:
        return {
            "rows": [],
            "counts": {"total": 0},
            "unresolved_reason": f"no design for {logo_code}/{scheme}",
        }
    if len(designs) > 1:
        return {
            "rows": [],
            "counts": {"total": 0},
            "unresolved_reason": (
                f"ambiguous design {logo_code}/{scheme}: {', '.join(sorted(designs))};"
                " pass design_id"
            ),
        }
    design_id = next(iter(designs))

    where = [
        "s.fdm4_store=%(store)s",
        "s.is_active",
        "s.kind='variation'",
        "NULLIF(btrim(s.color_code),'') IS NOT NULL",
    ]
    params: dict[str, Any] = {
        "store": fdm4_store,
        "design_id": design_id,
        "logo_code": logo_code,
        "scheme": scheme,
        "option_row": option_row,
    }

    mode = target.get("mode")
    if mode == "light_dark":
        if target.get("class") not in ("light", "dark"):
            raise ValueError("target.class must be 'light' or 'dark'")
        # 'both'-classified colors match either target.
        where.append("cc.light_dark IN (%(cls)s, 'both')")
        params["cls"] = target["class"]
    elif mode == "colors":
        where.append("s.color_code = ANY(%(codes)s)")
        params["codes"] = list(target["color_codes"])
    else:
        raise ValueError("target.mode must be 'light_dark' or 'colors'")

    if style_codes:
        where.append("s.style_code = ANY(%(styles)s)")
        params["styles"] = list(style_codes)

    cursor.execute(
        f"""
        WITH tgt AS (
          SELECT DISTINCT s.style_code, s.color_code, max(s.color) AS color
            FROM woo.store_product_state s
            -- Classification resolves by the store's actual color NAME first,
            -- then by code. FDM4 color codes are NOT globally unique (code
            -- 0002 is "Black" at Lewis but "White" in the global class table),
            -- so a bare code join can flip a color's class entirely.
            LEFT JOIN LATERAL (
                SELECT c2.light_dark
                  FROM logo.color_class c2
                 WHERE lower(btrim(c2.color_name)) = lower(btrim(s.color))
                    OR c2.color_code = s.color_code
                 ORDER BY (lower(btrim(c2.color_name)) = lower(btrim(s.color))) DESC,
                          (c2.source = 'manual') DESC
                 LIMIT 1
            ) cc ON true
           WHERE {' AND '.join(where)}
           GROUP BY s.style_code, s.color_code)
        SELECT t.style_code, t.color_code, t.color,
               a.logo_code AS was_logo, a.color_scheme_id AS was_scheme
          FROM tgt t
          LEFT JOIN logo.assignment a
            ON a.fdm4_store=%(store)s AND a.product_style=t.style_code
           AND a.garment_color_code=t.color_code AND a.option_row=%(option_row)s AND a.position=1 AND a.active
         ORDER BY t.style_code, t.color
        """,
        params,
    )
    rows = []
    for r in cursor.fetchall():
        rows.append(
            {
                "style_code": r["style_code"],
                "color_code": r["color_code"],
                "color": r["color"],
                "product_exists": True,
                "was": (
                    {"logo_code": r["was_logo"], "color_scheme": r["was_scheme"]}
                    if r["was_logo"]
                    else None
                ),
                "new": {
                    "logo_code": logo_code,
                    "color_scheme": scheme,
                    "design_id": str(design_id),
                },
            }
        )

    unclassified = 0
    if mode == "light_dark":
        cursor.execute(
            """
            SELECT count(DISTINCT s.color_code) AS n
              FROM woo.store_product_state s
             WHERE s.fdm4_store = %s AND s.is_active AND s.kind = 'variation'
               AND NOT EXISTS (
                   SELECT 1 FROM logo.color_class cc
                    WHERE lower(btrim(cc.color_name)) = lower(btrim(s.color))
                       OR cc.color_code = s.color_code)
            """,
            (fdm4_store,),
        )
        unclassified = cursor.fetchone()["n"]

    return {
        "rows": rows,
        "counts": {"total": len(rows), "unclassified": unclassified},
        "design_id": str(design_id),
    }


# ---------------------------------------------------------------------------
# Extended bounded reads for the in-app assistant (2026-09-03). Every function
# returns {..., "truncated", "truncation"} like the reads above and never
# exceeds the DB-computed byte bounds of _bounded_query.
# ---------------------------------------------------------------------------

LOGO_NAME_RESULT_LIMIT = 200
STOCK_RULE_RESULT_LIMIT = 500
PRICE_RULE_RESULT_LIMIT = 500
SYNC_BLOCK_RESULT_LIMIT = 500
PRODUCT_MIX_ITEM_RESULT_LIMIT = 500


def list_logo_names(cursor, *, store: Optional[str] = None, q: str = "",
                    limit: int = 100) -> dict:
    """Shopper-facing logo names. With a store: one row per (design, scheme)
    that store's ACTIVE assignments use, with the name its shoppers see
    (store-specific row, else the shared default, else '') and whether it is
    store-specific. Without a store: search the shared/global names."""
    term = _clean(q, "q") if q else ""
    like = f"%{term}%"
    safe_limit = max(1, min(int(limit), LOGO_NAME_RESULT_LIMIT))
    if store:
        store = _clean(store, "store")
        rows, truncated, byte_truncated = _bounded_query(
            cursor,
            f"""
            WITH used AS (
                SELECT btrim(a.design_id) AS design_id,
                       upper(btrim(a.color_scheme_id)) AS color_scheme_id,
                       min(btrim(a.logo_code)) AS logo_code,
                       count(*) AS assignments
                  FROM logo.assignment a
                 WHERE a.fdm4_store = %(store)s AND a.active
                   AND NULLIF(btrim(a.design_id), '') IS NOT NULL
                 GROUP BY 1, 2
            )
            SELECT u.design_id, u.color_scheme_id,
                   left(u.logo_code, 256) AS logo_code,
                   u.assignments,
                   left(COALESCE(s.name, g.name, ''), {READ_TEXT_CHAR_LIMIT}) AS name,
                   (s.design_id IS NOT NULL) AS store_specific,
                   COALESCE(s.locked, g.locked, false) AS locked,
                   left(COALESCE(s.fdm4_description, g.fdm4_description, ''),
                        {READ_TEXT_CHAR_LIMIT}) AS fdm4_description
              FROM used u
              LEFT JOIN logo.display_name s
                     ON s.design_id = u.design_id
                    AND upper(btrim(s.color_scheme_id)) = u.color_scheme_id
                    AND s.fdm4_store = %(store)s
              LEFT JOIN logo.display_name g
                     ON g.design_id = u.design_id
                    AND upper(btrim(g.color_scheme_id)) = u.color_scheme_id
                    AND g.fdm4_store = ''
             WHERE %(term)s = ''
                OR COALESCE(s.name, g.name, '') ILIKE %(like)s
                OR u.design_id ILIKE %(like)s
                OR u.color_scheme_id ILIKE %(like)s
                OR u.logo_code ILIKE %(like)s
                OR COALESCE(s.fdm4_description, g.fdm4_description, '') ILIKE %(like)s
             ORDER BY u.assignments DESC, u.design_id, u.color_scheme_id
             LIMIT %(limit)s
            """,
            {"store": store, "term": term, "like": like, "limit": safe_limit + 1},
            safe_limit,
        )
    else:
        rows, truncated, byte_truncated = _bounded_query(
            cursor,
            f"""
            SELECT dn.design_id, upper(btrim(dn.color_scheme_id)) AS color_scheme_id,
                   NULL::text AS logo_code, NULL::bigint AS assignments,
                   left(dn.name, {READ_TEXT_CHAR_LIMIT}) AS name,
                   (dn.fdm4_store <> '') AS store_specific,
                   left(dn.fdm4_store, 256) AS fdm4_store,
                   dn.locked,
                   left(COALESCE(dn.fdm4_description, ''), {READ_TEXT_CHAR_LIMIT}) AS fdm4_description
              FROM logo.display_name dn
             WHERE %(term)s = ''
                OR dn.name ILIKE %(like)s
                OR dn.design_id ILIKE %(like)s
                OR dn.color_scheme_id ILIKE %(like)s
                OR COALESCE(dn.fdm4_description, '') ILIKE %(like)s
             ORDER BY dn.uses DESC, dn.design_id, dn.color_scheme_id
             LIMIT %(limit)s
            """,
            {"term": term, "like": like, "limit": safe_limit + 1},
            safe_limit,
        )
    return {
        "store": store or None,
        "names": rows,
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


def get_stock_rules(cursor, *, store: Optional[str] = None, q: str = "",
                    limit: int = 200) -> dict:
    """Fake Inventory configuration: brand rules (mode real/fake or null =
    automatic) and style exceptions. With a store, brands are limited to the
    ones the store carries and exceptions to its styles."""
    term = _clean(q, "q") if q else ""
    like = f"%{term}%"
    safe_limit = max(1, min(int(limit), STOCK_RULE_RESULT_LIMIT))
    store = _clean(store, "store") if store else None
    store_brand_sql = (
        "AND btrim(m.\"mill-code\") IN (SELECT DISTINCT mill_code FROM woo.store_product_state "
        "WHERE fdm4_store = %(store)s AND mill_code IS NOT NULL)"
        if store else ""
    )
    brands, b_trunc, b_bytes = _bounded_query(
        cursor,
        f"""
        SELECT btrim(m."mill-code") AS mill_code,
               left(btrim(COALESCE(m.description, '')), {READ_TEXT_CHAR_LIMIT}) AS brand_name,
               r.mode,
               COALESCE(sc.styles, 0) AS styles,
               left(r.updated_by, 256) AS updated_by, r.updated_at
          FROM fdm4.mill m
          LEFT JOIN woo.brand_stock_rule r
                 ON r.mill_code = btrim(m."mill-code") AND r.active
          LEFT JOIN (
                SELECT btrim("mill-code") AS mc, count(DISTINCT btrim("style-code")) AS styles
                  FROM fdm4.style GROUP BY 1
               ) sc ON sc.mc = btrim(m."mill-code")
         WHERE NULLIF(btrim(m."mill-code"), '') IS NOT NULL
           AND COALESCE(sc.styles, 0) > 0
           {store_brand_sql}
           AND ( %(term)s = ''
              OR btrim(COALESCE(m.description, '')) ILIKE %(like)s
              OR btrim(m."mill-code") ILIKE %(like)s )
         ORDER BY (r.mode IS NULL), lower(btrim(COALESCE(m.description, ''))), btrim(m."mill-code")
         LIMIT %(limit)s
        """,
        {"store": store, "term": term, "like": like, "limit": safe_limit + 1},
        safe_limit,
    )
    store_style_sql = (
        "AND upper(btrim(o.style_code)) IN (SELECT upper(btrim(style_code)) FROM woo.store_product_state "
        "WHERE fdm4_store = %(store)s)"
        if store else ""
    )
    overrides, o_trunc, o_bytes = _bounded_query(
        cursor,
        f"""
        SELECT left(o.style_code, 256) AS style_code, o.mode, o.active,
               left(COALESCE(o.note, ''), {READ_TEXT_CHAR_LIMIT}) AS note,
               left(o.updated_by, 256) AS updated_by, o.updated_at
          FROM woo.stock_override o
         WHERE o.active
           {store_style_sql}
           AND ( %(term)s = '' OR o.style_code ILIKE %(like)s OR COALESCE(o.note, '') ILIKE %(like)s )
         ORDER BY o.style_code
         LIMIT %(limit)s
        """,
        {"store": store, "term": term, "like": like, "limit": safe_limit + 1},
        safe_limit,
    )
    return {
        "store": store,
        "brand_rules": brands,
        "style_exceptions": overrides,
        "automatic_rule": (
            "Brands with mode null follow the automatic rule: Arborwear and the "
            "stocked premium brands show real stock; other brands show always in "
            "stock; footwear, arborist gear and tools show real stock."
        ),
        "truncated": b_trunc or b_bytes or o_trunc or o_bytes,
        "truncation": {
            "rows": b_trunc or o_trunc,
            "bytes": b_bytes or o_bytes,
            "brands": b_trunc,
            "style_exceptions": o_trunc,
        },
    }


def list_price_rules(cursor, *, store: Optional[str] = None) -> dict:
    """Price rules Arborwear controls directly, in evaluation order. With a
    store: only rules that can touch it (aimed at all stores or naming it,
    and not excluding it). Also lists stores whose prices are frozen."""
    store = _clean(store, "store") if store else None
    store_sql = (
        "WHERE (COALESCE(cardinality(stores), 0) = 0 OR %(store)s = ANY(stores)) "
        "AND NOT (%(store)s = ANY(COALESCE(excl_stores, '{}')))"
        if store else ""
    )
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        f"""
        SELECT rule_id, left(name, {READ_TEXT_CHAR_LIMIT}) AS name, active, priority, stackable,
               COALESCE(stores, '{{}}') AS stores, COALESCE(store_tiers, '{{}}') AS store_tiers,
               COALESCE(styles, '{{}}') AS styles, COALESCE(brands, '{{}}') AS brands,
               COALESCE(categories, '{{}}') AS categories,
               COALESCE(excl_stores, '{{}}') AS excl_stores, COALESCE(excl_styles, '{{}}') AS excl_styles,
               COALESCE(excl_brands, '{{}}') AS excl_brands, COALESCE(excl_categories, '{{}}') AS excl_categories,
               effect_type, effect_value, price_level_key, basis, rounding,
               floor_price, ceiling_price, cap_at_msrp,
               effective_from, effective_until,
               left(COALESCE(note, ''), {READ_TEXT_CHAR_LIMIT}) AS note,
               last_previewed_at, updated_at, left(updated_by, 256) AS updated_by
          FROM woo.price_rule
          {store_sql}
         ORDER BY priority, rule_id
         LIMIT %(limit)s
        """,
        {"store": store, "limit": PRICE_RULE_RESULT_LIMIT + 1},
        PRICE_RULE_RESULT_LIMIT,
    )
    frozen, f_trunc, f_bytes = _bounded_query(
        cursor,
        "SELECT DISTINCT left(fdm4_store, 256) AS fdm4_store, scope "
        "FROM woo.sync_exclusion WHERE active AND style_code = '' "
        "ORDER BY fdm4_store LIMIT %(limit)s",
        {"limit": SYNC_BLOCK_RESULT_LIMIT + 1},
        SYNC_BLOCK_RESULT_LIMIT,
    )
    return {
        "store": store,
        "rules": rows,
        "frozen_stores": frozen,
        "truncated": truncated or byte_truncated or f_trunc or f_bytes,
        "truncation": {"rows": truncated or f_trunc, "bytes": byte_truncated or f_bytes},
    }


def list_sync_blocks(cursor, *, store: Optional[str] = None) -> dict:
    """Sync Blocks (freezes): whole-store, price-only (scope='pricing') or
    single-style rows the hourly update leaves alone."""
    store = _clean(store, "store") if store else None
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        f"""
        SELECT left(fdm4_store, 256) AS fdm4_store,
               left(style_code, 256) AS style_code,
               (style_code = '') AS whole_store,
               scope, active,
               left(COALESCE(note, ''), {READ_TEXT_CHAR_LIMIT}) AS note,
               updated_at, left(updated_by, 256) AS updated_by
          FROM woo.sync_exclusion
         WHERE (%(store)s IS NULL OR fdm4_store = %(store)s)
         ORDER BY fdm4_store, style_code
         LIMIT %(limit)s
        """,
        {"store": store, "limit": SYNC_BLOCK_RESULT_LIMIT + 1},
        SYNC_BLOCK_RESULT_LIMIT,
    )
    return {
        "store": store,
        "blocks": rows,
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


def get_product_mix(cursor, *, store: str, limit: int = 200) -> dict:
    """Product Mix state of one store: not enrolled (follows FDM4), mode
    'all' (follows FDM4 completely) or 'list' (curated) with its styles."""
    store = _clean(store, "store")
    safe_limit = max(1, min(int(limit), PRODUCT_MIX_ITEM_RESULT_LIMIT))
    cursor.execute(
        """
        SELECT m.fdm4_store, m.mode, m.active, m.note, m.imported_at,
               m.created_by, m.created_at,
               (v.fdm4_store IS NOT NULL) AS external,
               v.catalog_id AS external_catalog
          FROM woo.store_mix_store m
          LEFT JOIN woo.virtual_catalog_store v USING (fdm4_store)
         WHERE m.fdm4_store = %s
        """,
        (store,),
    )
    registry = cursor.fetchone()
    registry = dict(registry) if registry else None
    items, truncated, byte_truncated = ([], False, False)
    if registry and registry.get("mode") == "list":
        items, truncated, byte_truncated = _bounded_query(
            cursor,
            f"""
            SELECT left(i.style_code, 256) AS style_code,
                   i.colors, i.size_excludes,
                   left(COALESCE(i.note, ''), {READ_TEXT_CHAR_LIMIT}) AS note,
                   i.updated_at
              FROM woo.store_mix_item i
             WHERE i.fdm4_store = %(store)s
             ORDER BY i.style_code
             LIMIT %(limit)s
            """,
            {"store": store, "limit": safe_limit + 1},
            safe_limit,
        )
    cursor.execute(
        "SELECT count(*) AS n FROM woo.store_mix_candidate WHERE fdm4_store = %s",
        (store,),
    )
    candidates = int((cursor.fetchone() or {"n": 0})["n"])
    return {
        "store": store,
        "enrolled": registry is not None,
        "mode": (registry or {}).get("mode"),
        "registry": registry,
        "curated_styles": items,
        "new_in_fdm4_candidates": candidates,
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


DESIGN_USAGE_RESULT_LIMIT = 500


def list_design_usage(cursor, *, store: str, design_id: str,
                      color_scheme_id: Optional[str] = None) -> dict:
    """Styles in one store whose logo rows carry a design (optionally one
    color scheme): row counts, colors and schemes per style. Feeds
    replace_design, which needs the style list up front."""
    store = _clean(store, "store")
    design = _clean(design_id, "design_id")
    scheme = (
        _clean(color_scheme_id, "color_scheme_id").upper()
        if color_scheme_id not in (None, "") else None
    )
    rows, truncated, byte_truncated = _bounded_query(
        cursor,
        f"""
        SELECT a.product_style,
               left(max(p.name), {READ_TEXT_CHAR_LIMIT}) AS name,
               count(*)::integer AS rows,
               count(*) FILTER (WHERE a.active)::integer AS active_rows,
               array_to_string(array_agg(DISTINCT a.garment_color_code), ', ') AS colors,
               array_to_string(array_agg(DISTINCT upper(btrim(a.color_scheme_id))), ', ') AS schemes,
               array_to_string(array_agg(DISTINCT upper(btrim(a.logo_code))), ', ') AS logo_codes
          FROM logo.assignment a
          LEFT JOIN woo.store_product_state p
                 ON p.fdm4_store = a.fdm4_store AND p.style_code = a.product_style
                AND p.kind = 'parent'
         WHERE a.fdm4_store = %(store)s
           AND btrim(a.design_id) = %(design)s
           AND (%(scheme)s IS NULL OR upper(btrim(a.color_scheme_id)) = %(scheme)s)
         GROUP BY a.product_style
         ORDER BY a.product_style
         LIMIT %(limit)s
        """,
        {"store": store, "design": design, "scheme": scheme,
         "limit": DESIGN_USAGE_RESULT_LIMIT + 1},
        DESIGN_USAGE_RESULT_LIMIT,
    )
    return {
        "store": store,
        "design_id": design,
        "color_scheme_id": scheme,
        "styles": rows,
        "style_codes": [str(r["product_style"]) for r in rows],
        "total_rows": sum(int(r["rows"]) for r in rows),
        "truncated": truncated or byte_truncated,
        "truncation": {"rows": truncated, "bytes": byte_truncated},
    }


SYNC_STATUS_EVENT_LIMIT = 8


def get_sync_status(cursor, *, store: Optional[str] = None) -> dict:
    """Is the pipeline running and when did it last run: the latest FDM4 pull,
    the latest WooCommerce reconcile per environment, 24-hour counts; with a
    store, its logo-sync events, active freezes and the last logo edit.
    (Logo-sync ownership lives in WordPress; the caller adds it.)"""
    store = _clean(store, "store").upper() if store else None
    cursor.execute(
        """
        SELECT op, env, status, requested_by, requested_at, started_at, finished_at,
               EXTRACT(EPOCH FROM (finished_at - started_at))::int AS duration_s,
               rows_loaded, left(COALESCE(note, ''), 200) AS note,
               left(COALESCE(error, ''), 400) AS error
          FROM (
              SELECT *, row_number() OVER (PARTITION BY op, env ORDER BY id DESC) AS rn
                FROM woo.sync_control
          ) latest
         WHERE rn = 1
         ORDER BY op, env
         LIMIT 20
        """
    )
    latest = [dict(r) for r in cursor.fetchall()]
    cursor.execute(
        """
        SELECT op,
               count(*) FILTER (WHERE status = 'success') AS ok_24h,
               count(*) FILTER (WHERE status NOT IN ('success', 'running', 'requested')) AS failed_24h,
               max(finished_at) FILTER (WHERE status = 'success') AS last_success_at
          FROM woo.sync_control
         WHERE requested_at > now() - interval '24 hours'
         GROUP BY op
         ORDER BY op
        """
    )
    day = [dict(r) for r in cursor.fetchall()]
    out: dict = {"pipeline": {"latest": latest, "last_24h": day}, "store": store}
    if store is None:
        return out
    cursor.execute(
        """
        SELECT at, actor, action, left(COALESCE(detail::text, ''), 300) AS detail
          FROM logo.audit_log
         WHERE fdm4_store = %s
           AND action IN ('sync_requested', 'sync_succeeded', 'sync_failed',
                          'ownership_enabled', 'ownership_disabled')
         ORDER BY id DESC
         LIMIT %s
        """,
        (store, SYNC_STATUS_EVENT_LIMIT),
    )
    events = [dict(r) for r in cursor.fetchall()]
    cursor.execute(
        """
        SELECT style_code, scope, note, updated_at
          FROM woo.sync_exclusion
         WHERE fdm4_store = %s AND active
         ORDER BY style_code
         LIMIT 100
        """,
        (store,),
    )
    freezes = [dict(r) for r in cursor.fetchall()]
    cursor.execute(
        """
        SELECT max(updated_at) AS last_logo_change,
               count(*) FILTER (WHERE active) AS active_logo_rows
          FROM logo.assignment
         WHERE fdm4_store = %s
        """,
        (store,),
    )
    logos = dict(cursor.fetchone())
    cursor.execute(
        "SELECT enabled, allows_none FROM logo.store_settings WHERE fdm4_store = %s",
        (store,),
    )
    settings_row = cursor.fetchone()
    out["store_status"] = {
        "logo_sync_events": events,
        "freezes": freezes,
        "whole_store_frozen": any(f["style_code"] == "" for f in freezes),
        "last_logo_change": logos["last_logo_change"],
        "active_logo_rows": int(logos["active_logo_rows"] or 0),
        "logos_enabled": bool(settings_row["enabled"]) if settings_row else True,
    }
    return out
