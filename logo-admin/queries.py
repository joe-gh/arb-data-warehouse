"""Bounded, cursor-owned read services shared by HTTP and the agent."""

import datetime
import mix_service
import html
import logging
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


_log = logging.getLogger(__name__)

_TIMEOUT_UNITS = {"us": 0.000001, "ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}


def _timeout_seconds(setting) -> Optional[float]:
    """Seconds for a statement_timeout setting; None when it is off or unreadable."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", str(setting or "").strip().lower())
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2) or "ms"
    if value == 0 or unit not in _TIMEOUT_UNITS:
        return None
    return value * _TIMEOUT_UNITS[unit]


def _bound_statement_timeout(cursor, seconds: int) -> None:
    """Cap the transaction's statement timeout at `seconds`, never loosening a
    tighter bound an enclosing section (explain_product, find_issues) set."""
    cursor.execute("SELECT current_setting('statement_timeout') AS timeout")
    current = _timeout_seconds(cursor.fetchone()["timeout"])
    if current is None or current > seconds:
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(seconds)}s",))


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
               left(COALESCE(d.description, ''), 1024) AS fdm4_description,
               left(COALESCE(d.web_description, ''), 1024) AS web_description,
               (lower(btrim(COALESCE(d.description, ''))) = lower(%(exact)s)
                OR lower(btrim(COALESCE(d.web_description, ''))) = lower(%(exact)s)
                OR EXISTS (SELECT 1 FROM logo.display_name exact_name
                            WHERE exact_name.design_id = btrim(d.design_id)
                              AND exact_name.fdm4_store IN (%(store)s, '')
                              AND lower(btrim(exact_name.name)) = lower(%(exact)s))) AS exact_name_match,
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


def price_rule_impact(cursor, rule_id: int, sample_limit: int = 200, *, include_version: bool = False) -> dict:
    sample_limit = max(1, min(int(sample_limit), 1000))
    cursor.execute("SELECT * FROM woo.price_rule WHERE rule_id=%s", (rule_id,))
    rule = cursor.fetchone()
    if not rule:
        raise QueryNotFound("Rule not found")
    # Candidate pre-filter from the rule's own targeting keeps the preview
    # fast; the evaluator then applies the FULL active chain per candidate.
    # Base = base_price (the PRE-rule price the transform preserves), never
    # the projected `price` column - that one already has active rules
    # baked in, and evaluating from it would double-apply them.
    where = ["s.is_active", "s.kind = 'variation'", "s.price IS NOT NULL"]
    params: dict = {"rid": rule_id, "lim": 50001}
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
             ORDER BY s.fdm4_store, s.style_code, s.sku
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
             ORDER BY s.fdm4_store, s.style_code, s.sku
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
         ORDER BY abs(rp.final_price - c.price) DESC, c.fdm4_store, c.style_code, c.sku
         LIMIT %(sample)s
        """,
        {**params, "sample": sample_limit},
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
             ORDER BY s.fdm4_store, s.style_code, s.sku
             LIMIT %(lim)s
        )
        SELECT c.fdm4_store, count(*) AS affected
          FROM cand c
          CROSS JOIN LATERAL woo.eval_price_rules(
              c.fdm4_store, c.style_code, c.brand, c.category,
              c.price, c.price_levels, c.def_cost,
              current_date, ARRAY[%(rid)s]::bigint[], NULL) rp
         WHERE rp.final_price IS NOT NULL AND %(rid)s = ANY(rp.applied_rule_ids)
         GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 30
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
    result = {"ok": True, "rule_id": rule_id, "summary": summary,
              "store_count": summary.get("stores"), "per_store": per_store,
              "sample": sample, "frozen_targets": frozen_targets}
    if include_version:
        result["rule_updated_at"] = rule["updated_at"]
    return result


def list_price_rule_dimensions(cursor) -> dict:
    cursor.execute(
        "SELECT DISTINCT brand FROM woo.store_product_state WHERE NULLIF(btrim(brand),'') IS NOT NULL ORDER BY 1 LIMIT 1000")
    brands = [r["brand"] for r in cursor.fetchall()]
    cursor.execute(
        "SELECT DISTINCT category FROM woo.store_product_state WHERE NULLIF(btrim(category),'') IS NOT NULL ORDER BY 1 LIMIT 200")
    categories = [r["category"] for r in cursor.fetchall()]
    cursor.execute("SELECT tier_name FROM woo.pricing_tier ORDER BY sort_order, tier_name LIMIT 500")
    tiers = [r["tier_name"] for r in cursor.fetchall()]
    return {"brands": brands, "categories": categories, "tiers": tiers}


def check_price_rules(cursor, *, store: str, style: str) -> dict:
    store_v = _clean(store, "store").upper()
    style_v = _clean(style, "style").upper()
    _bound_statement_timeout(cursor, 30)
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


def get_health_overview(cursor) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    out = {"ok": True, "generated_at": now.isoformat()}
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
             ORDER BY name LIMIT 100
            """
        )
        consumers = [dict(r) for r in cursor.fetchall()]
    out["feeds"] = {"available": feeds_present, "consumers": consumers}
    return out


def mix_style_universe(cursor, store: str, style: str):
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
        color = mix_service.norm(color)
        if color and color not in seen:
            seen.add(color)
            available.append({
                "color": color, "color_name": color, "variations": 0, "sizes": [],
            })
    return available

def get_style_mix(cursor, *, style: str, store: Optional[str] = None, limit: int = 100) -> dict:
    style = mix_service.norm(_clean(style, "style"))
    limit = max(1, min(int(limit), 200))
    if store is not None:
        store = mix_service.norm(_clean(store, "store"))
        registry = mix_service.registry(cursor, store)
        available = mix_style_universe(cursor, store, style)
        cursor.execute("SELECT colors, size_excludes, source, added_by, added_at FROM woo.store_mix_item WHERE fdm4_store = %s AND style_code = %s", (store, style))
        item = cursor.fetchone()
        return {"ok": True, "store": store, "style_code": style,
                "in_mix": item is not None, "mode": registry["mode"],
                "colors": item["colors"] if item else None,
                "size_excludes": item["size_excludes"] if item else None,
                "source": item["source"] if item else None,
                "added_by": item["added_by"] if item else None,
                "added_at": item["added_at"] if item else None,
                "available": available[:500], "truncated": len(available) > 500}
    cursor.execute("""
        WITH live AS (
            SELECT fdm4_store, count(*) FILTER (WHERE kind = 'variation') AS products
              FROM woo.store_product_state
             WHERE upper(btrim(style_code)) = %s AND is_active GROUP BY fdm4_store
        ), listed AS (
            SELECT * FROM woo.store_mix_item WHERE style_code = %s
        )
        SELECT COALESCE(l.fdm4_store, i.fdm4_store) AS store,
               COALESCE(l.products, 0) AS live_products,
               COALESCE(m.active, false) AS mix_enabled, m.mode,
               i.style_code IS NOT NULL AS in_saved_list, i.source,
               i.added_by, i.added_at,
               CASE WHEN m.active AND m.mode = 'list' THEN i.style_code IS NOT NULL
                    ELSE l.fdm4_store IS NOT NULL END AS in_mix,
               CASE WHEN m.active AND m.mode = 'list' THEN COALESCE(i.source, 'excluded')
                    ELSE 'fdm4' END AS supplied_by
          FROM live l FULL JOIN listed i USING (fdm4_store)
          LEFT JOIN woo.store_mix_store m ON m.fdm4_store = COALESCE(l.fdm4_store, i.fdm4_store)
         ORDER BY 1 LIMIT %s
    """, (style, style, limit + 1))
    rows = [dict(row) for row in cursor.fetchall()]
    return {"style_code": style, "stores": rows[:limit], "truncated": len(rows) > limit}


def preview_fill_missing_colors(cursor, *, store: str, styles: list[str]) -> dict:
    if not styles or len(styles) > 50:
        raise QueryValidationError("Name between 1 and 50 styles")
    return fill_gaps_plan(cursor, fdm4_store=store, styles=styles)


def _category_env(env):
    import categories_service
    from categories_service import TargetNotConfigured
    try:
        categories_service.get_target(env)
    except TargetNotConfigured as exc:
        raise QueryNotFound(f"Environment '{env}' is not configured") from exc


def _bounded_category_value(value, limit):
    if isinstance(value, dict):
        return {str(k)[:200]: _bounded_category_value(v, limit) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [_bounded_category_value(v, limit) for v in value[:limit]]
    if isinstance(value, str):
        return value[:1024]
    return value


def cat_node_lookup(cursor, *, env: str, slug: Optional[str] = None, path: Optional[str] = None) -> dict:
    import categories_draft
    from commands import CatCategoryTarget
    from mutations import _category_target
    _category_env(env)
    node_id = _category_target(cursor, CatCategoryTarget(slug=slug, path=path))
    node = categories_draft._node(cursor, node_id)
    parts, seen, current = [], set(), node
    while current and current['node_id'] not in seen:
        seen.add(current['node_id'])
        parts.append(current['name'])
        current = categories_draft._node(cursor, current['parent_id']) if current['parent_id'] else None
    stats = categories_draft.slug_stats(cursor, env).get(node['slug'], {'stores': 0, 'products': 0})
    result = {**node, 'path': ' / '.join(reversed(parts)), **stats}
    bounded = _bounded_category_value(result, 200)
    return {'env': env, 'node': bounded, 'truncated': bounded != result}


def cat_mapping_rows(cursor, *, env: str, filter, limit: int = 100, offset: int = 0) -> dict:
    import categories_mapping
    _category_env(env)
    if not 1 <= limit <= 200 or not 0 <= offset <= 100000:
        raise QueryValidationError('Category decision paging is out of bounds')
    if isinstance(filter, list):
        if not 1 <= len(filter) <= 200 or any(not isinstance(s, str) or not s.strip() or len(s) > 200 for s in filter):
            raise QueryValidationError('Supply 1-200 exact old slugs')
        selected = {s.strip() for s in filter}
        accept = lambda r: r['old_slug'] in selected
    elif filter == 'undecided':
        accept = lambda r: not r['action']
    elif filter == 'empty':
        accept = lambda r: not r['products']
    elif filter == 'store_only':
        accept = lambda r: r['action'] == 'store_custom'
    else:
        raise QueryValidationError('Unknown category decision filter')
    rows = [r for r in categories_mapping.mapping_status(cursor, env)['slugs'] if accept(r)]
    page = rows[offset:offset + limit]
    bounded = _bounded_category_value(page, 200)
    more = offset + len(page) < len(rows)
    return {'env': env, 'rows': bounded, 'total': len(rows), 'offset': offset,
            'next_offset': offset + len(page) if more else None,
            'truncated': more or bounded != page}


def cat_tree(cursor, *, env: str, limit: int = 200) -> dict:
    import categories_draft
    _category_env(env)
    limit = max(1, min(int(limit), 500))
    nodes = categories_draft.list_nodes(cursor)
    stats = categories_draft.slug_stats(cursor, env)
    by_id = {node["node_id"]: node for node in nodes}
    rows = []
    for node in nodes[:limit]:
        parts, seen, current = [], set(), node
        while current and current["node_id"] not in seen:
            seen.add(current["node_id"])
            parts.append(str(current["name"]))
            current = by_id.get(current["parent_id"])
        rows.append({"node_id": node["node_id"], "slug": node["slug"],
                     "path": " / ".join(reversed(parts))[:2048],
                     **stats.get(node["slug"], {"stores": 0, "products": 0})})
    return {"env": env, "paths": rows, "total": len(nodes), "truncated": len(nodes) > limit}


def cat_mapping_status(cursor, *, env: str, limit: int = 100) -> dict:
    import categories_mapping
    _category_env(env)
    limit = max(1, min(int(limit), 200))
    result = categories_mapping.mapping_status(cursor, env)
    undecided = [row for row in result["slugs"] if not row["action"]]
    empty = [row for row in result["slugs"] if not row["products"]]
    return {"env": env, "summary": result["summary"],
            "undecided": _bounded_category_value(undecided, limit),
            "empty": _bounded_category_value(empty, limit),
            "undecided_count": len(undecided), "empty_count": len(empty),
            "truncated": len(undecided) > limit or len(empty) > limit}


def cat_plan_check(cursor, *, env: str, blog_ids: Optional[list[int]] = None, limit: int = 100) -> dict:
    import categories_planner
    _category_env(env)
    limit = max(1, min(int(limit), 200))
    if blog_ids is not None and len(blog_ids) > 200:
        raise QueryValidationError("At most 200 stores per plan check")
    cursor.execute("SET LOCAL statement_timeout = '120s'")
    result = categories_planner.preview(cursor, env, blog_ids)
    bounded = _bounded_category_value(result, limit)
    bounded["truncated"] = bounded != result
    bounded["store_count"] = len(result["blogs"])
    return bounded


def cat_runs(cursor, *, env: Optional[str] = None, run_id: Optional[int] = None, limit: int = 50) -> dict:
    import categories_runs
    limit = max(1, min(int(limit), 200))
    if env is not None:
        _category_env(env)
    runs = categories_runs.list_runs(cursor, env)
    fields = ("run_id", "env", "status", "created_at", "created_by", "started_at", "finished_at", "worker_stale", "heartbeat_age")
    result = {"runs": [{k: row[k] for k in fields if k in row} for row in runs[:limit]],
              "truncated": len(runs) > limit or len(runs) == 50}
    if run_id is not None:
        run = categories_runs.get_run(cursor, run_id)
        if env is not None and run["env"] != env:
            raise QueryNotFound("Run not found in this environment")
        jobs = run["jobs"]
        result["run"] = {k: run[k] for k in fields if k in run}
        result["run"]["jobs"] = [
            {k: job[k] for k in ("job_id", "blog_id", "blog_path", "status", "stats", "attempt", "started_at", "finished_at", "has_snapshot") if k in job}
            for job in jobs[:limit]]
        result["run"]["job_count"] = len(jobs)
        result["truncated"] = result["truncated"] or len(jobs) > limit
    return _bounded_category_value(result, limit)


def _ops_result(rows, truncated=False, byte_truncated=False, **values):
    return {**values, "rows": rows, "truncated": truncated or byte_truncated,
            "truncation": {"rows": truncated, "bytes": byte_truncated}}


def get_product_state(cursor, *, store, style=None, sku=None, limit=500):
    """Projected parent and sibling variations, including inactive rows."""
    store = _clean(store, "store").upper()
    if (style is None) == (sku is None):
        raise QueryValidationError("Supply exactly one of style or sku")
    limit = max(1, min(int(limit), 500))
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise QueryNotFound("Store not found")
    _bound_statement_timeout(cursor, 15)
    if sku is not None:
        cursor.execute("SELECT style_code FROM woo.store_product_state WHERE fdm4_store=%s AND catalog_id=%s AND sku=%s LIMIT 1",
                       (store, catalog, _clean(sku, "sku")))
        matched = cursor.fetchone()
        if not matched:
            raise QueryNotFound("Product not found")
        style = matched["style_code"]
    style = _clean(style, "style").upper()
    where = "fdm4_store=%s AND catalog_id=%s AND upper(btrim(style_code))=%s"
    cursor.execute(f"""SELECT count(*) AS total,
        count(*) FILTER (WHERE kind='variation') AS variations,
        count(*) FILTER (WHERE kind='variation' AND stock>0) AS in_stock,
        count(*) FILTER (WHERE kind='variation' AND is_active) AS active
        FROM woo.store_product_state WHERE {where}""", (store, catalog, style))
    totals = dict(cursor.fetchone())
    if not totals["total"]:
        raise QueryNotFound("Product not found")
    text_fields = ("sku", "kind", "style_code", "name", "status", "color_code", "color", "size_code", "size",
                   "web_active", "item_status", "brand", "category", "design_id", "mill_code")
    projection = ", ".join(f"left({field}, 1024) AS {field}" for field in text_fields)
    rows, truncated, byte_truncated = _bounded_query(cursor, f"""
        SELECT {projection}, price, base_price, price_levels, stock, is_active, refreshed_at, changed_at
        FROM woo.store_product_state WHERE {where}
        ORDER BY CASE WHEN kind='parent' THEN 0 ELSE 1 END, color, size, sku LIMIT %s
    """, (store, catalog, style, limit + 1), limit)
    rows = _bounded_category_value(rows, 500)
    return _ops_result(rows, truncated, byte_truncated, found=True, store=store, style=style, totals=totals,
                       parent=next((r for r in rows if r["kind"] == "parent"), None),
                       variations=[r for r in rows if r["kind"] == "variation"])


def get_change_history(cursor, *, user_login, category_access=False, store=None, style=None,
                       logo_code=None, rule_id=None, since_days=7, actor=None, limit=100):
    """Audit actors are public to operators; change-set cards remain owner scoped."""
    login = _clean(user_login, "user_login").lower()
    limit = max(1, min(int(limit), 300))
    _bound_statement_timeout(cursor, 20)
    params = {"login": login, "store": _optional(store, "store").upper(),
              "style": _optional(style, "style").upper(), "logo": _optional(logo_code, "logo_code").upper(),
              "rule": str(rule_id) if rule_id is not None else "", "actor": _optional(actor, "actor"),
              "days": max(1, min(int(since_days), 90)), "lim": limit + 1}
    # The payload is used only for exact scope filters; it is never returned.
    sources = """
        SELECT at, 'logo.audit_log' AS source, actor, action, fdm4_store AS store,
               product_style AS style, NULL::text AS change_set_id, NULL::bigint AS batch_id,
               to_jsonb(a) AS payload, id::text AS source_id, action AS label FROM logo.audit_log a
        UNION ALL
        SELECT created_at, 'logo.bulk_batch', created_by, 'bulk_applied',
               fdm4_store, '', NULL, batch_id, to_jsonb(b), batch_id::text || ':applied',
               'Bulk logo applied' || coalesce(' ' || logo_code,'') FROM logo.bulk_batch b
        UNION ALL
        SELECT undone_at, 'logo.bulk_batch', created_by, 'bulk_undone',
               fdm4_store, '', NULL, batch_id, to_jsonb(b), batch_id::text || ':undone',
               'Bulk logo undone' || coalesce(' ' || logo_code,'') FROM logo.bulk_batch b WHERE undone_at IS NOT NULL
        UNION ALL
        SELECT c.updated_at, 'logo.agent_change_set', c.user_login, c.status,
               coalesce(i.arguments->>'store', i.arguments->>'fdm4_store',''),
               coalesce(i.arguments->>'style',i.arguments->>'product_style',i.arguments->>'style_code',''),
               c.id::text, NULL, i.arguments, c.id::text || ':' || coalesce(i.sort_order::text,''), c.status || ' ' || coalesce(replace(i.tool_name,'_',' '),'change set')
          FROM logo.agent_change_set c LEFT JOIN logo.agent_change_set_item i
            ON i.change_set_id=c.id AND i.user_login=c.user_login
         WHERE c.user_login=%(login)s
        UNION ALL
        SELECT updated_at, 'woo.price_rule', updated_by, 'price_rule_updated', '', '',
               NULL, NULL, to_jsonb(p), rule_id::text, 'Price rule updated: ' || name FROM woo.price_rule p
        UNION ALL
        SELECT updated_at, 'woo.sync_exclusion', updated_by, 'sync_block_updated',
               fdm4_store, style_code, NULL, NULL, to_jsonb(e), fdm4_store || ':' || style_code, CASE WHEN active THEN 'Sync freeze set: ' ELSE 'Sync freeze disabled: ' END || scope
          FROM woo.sync_exclusion e
    """
    if category_access:
        sources += """ UNION ALL SELECT at, 'catmgr.audit_log', actor, action,
            coalesce(detail->>'fdm4_store',detail->>'store',''), coalesce(detail->>'style',''),
            NULL, NULL, to_jsonb(a), id::text, action || ' ' || entity || ' ' || entity_key FROM catmgr.audit_log a"""
    def json_match(keys, param):
        return " OR ".join(
            f"jsonb_path_exists(payload, '$.**.{key} ? (@ == $value)', jsonb_build_object('value', %({param})s::text))"
            for key in keys)
    filters = ["at >= now() - %(days)s * interval '1 day'", "(%(actor)s='' OR actor=%(actor)s)",
        "(%(store)s='' OR store=%(store)s OR " + json_match(("store", "fdm4_store", "stores[*]"), "store") + " OR (source='catmgr.audit_log' AND EXISTS (SELECT 1 FROM woo.store_blog_map m WHERE m.fdm4_store=%(store)s AND (payload->'detail'->>'blog_id'=m.blog_id::text OR (payload->>'entity' IN ('snapshot','draft') AND split_part(payload->>'entity_key',':',1) IN ('dev','prod') AND split_part(payload->>'entity_key',':',2)=m.blog_id::text)))))",
        "(%(style)s='' OR style=%(style)s OR " + json_match(("style", "style_code", "product_style", "styles[*]", "style_codes[*]"), "style") + " OR (source='logo.bulk_batch' AND EXISTS (SELECT 1 FROM logo.bulk_batch_row r WHERE r.batch_id=history.batch_id AND r.product_style=%(style)s)))",
        "(%(logo)s='' OR " + json_match(("logo_code", "logo_code.from", "logo_code.to"), "logo") + ")",
        "(%(rule)s='' OR payload->>'rule_id'=%(rule)s OR payload->'detail'->>'rule_id'=%(rule)s)"]
    filters[2] = filters[2][:-1] + """ OR (source='woo.price_rule'
        AND ((coalesce(payload->'stores','null'::jsonb) IN ('null'::jsonb,'[]'::jsonb)
              AND coalesce(payload->'store_tiers','null'::jsonb) IN ('null'::jsonb,'[]'::jsonb))
             OR EXISTS(SELECT 1 FROM woo.store_pricing_tier t WHERE t.fdm4_store=%(store)s AND payload->'store_tiers' ? t.tier_name))
        AND NOT coalesce(payload->'excl_stores' ? %(store)s,false)))"""
    filters[3] = filters[3][:-1] + """ OR (source='woo.price_rule'
        AND coalesce(payload->'styles','null'::jsonb) IN ('null'::jsonb,'[]'::jsonb)
        AND NOT coalesce(payload->'excl_styles' ? %(style)s,false)))"""
    filters.append("(%(store)s='' OR source<>'woo.price_rule' OR NOT coalesce(payload->'excl_stores' ? %(store)s,false))")
    filters.append("(%(style)s='' OR source<>'woo.price_rule' OR NOT coalesce(payload->'excl_styles' ? %(style)s,false))")
    cte = "WITH history AS (" + sources + "), matched AS (SELECT * FROM history WHERE " + " AND ".join(filters) + ") "
    rows, truncated, byte_truncated = _bounded_query(cursor, cte + """
        SELECT at, source, left(coalesce(actor,''),100) AS actor, left(action,100) AS action,
               left(store,100) AS store, left(style,100) AS style, change_set_id, batch_id,
               left(replace(label,'_',' ') || CASE WHEN store<>'' THEN ' on ' || store ELSE '' END
                    || CASE WHEN style<>'' THEN ' / ' || style ELSE '' END, 1024) AS what
          FROM matched ORDER BY at DESC, source, source_id DESC LIMIT %(lim)s
    """, params, limit)
    actors, actor_truncated, actor_bytes = _bounded_query(cursor, cte + """
        SELECT left(coalesce(actor,''),100) AS actor, count(*) AS count
          FROM matched GROUP BY actor ORDER BY count(*) DESC, actor LIMIT 301
    """, params, 300)
    return _ops_result(rows, truncated or actor_truncated, byte_truncated or actor_bytes,
                       actors=actors, since_days=params["days"])


def get_stock(cursor, *, style, color_code=None, size_code=None):
    style = _clean(style, "style").upper()
    _bound_statement_timeout(cursor, 15)
    rows, truncated, byte_truncated = _bounded_query(cursor, """
        SELECT left(i."item-number",100) AS item_number, left(i."upc-code",100) AS sku,
               left(i."style-code",100) AS style, left(i."color-code",100) AS color_code,
               left(i."size-code",100) AS size_code, left(b.warehouse,100) AS warehouse,
               left(b."web-active",100) AS web_active,
               coalesce(nullif(b."inv-bal",'')::numeric,0) AS inv_bal,
               coalesce(nullif(b.committed,'')::numeric,0) AS committed,
               left(b.allocated,100) AS allocated, left(b."on-order",100) AS on_order,
               left(b.backordered,100) AS backordered,
               greatest(0,coalesce(nullif(b."inv-bal",'')::numeric,0)
                         - coalesce(nullif(b.committed,'')::numeric,0)) AS warehouse_available,
               sum(greatest(0,coalesce(nullif(b."inv-bal",'')::numeric,0)
                         - coalesce(nullif(b.committed,'')::numeric,0)))
                   OVER (PARTITION BY i."item-number") AS available
          FROM fdm4.item i JOIN fdm4."inv-balance" b ON b."item-number"=i."item-number"
         WHERE upper(btrim(i."style-code"))=%s AND nullif(i."upc-code",'') IS NOT NULL
           AND (%s='' OR i."color-code"=%s) AND (%s='' OR i."size-code"=%s)
         ORDER BY i."color-code", i."size-code", i."item-number", b.warehouse LIMIT 201
    """, (style, _optional(color_code,"color_code"), _optional(color_code,"color_code"),
           _optional(size_code,"size_code"), _optional(size_code,"size_code")), 200)
    if not rows and not byte_truncated:
        raise QueryNotFound("Stock not found")
    return _ops_result(rows, truncated, byte_truncated, found=True, style=style)


def audit_store_prices(cursor, *, store, limit=50):
    store = _clean(store, "store").upper()
    limit = max(1, min(int(limit),200))
    if _catalog_for_store(cursor,store) is None:
        raise QueryNotFound("Store not found")
    cursor.execute("SET LOCAL statement_timeout = '120s'")
    cte = """WITH cand AS MATERIALIZED (
        SELECT s.sku, s.style_code, s.color, s.size, s.fdm4_store, s.brand, s.category,
               coalesce(s.base_price,s.price) AS before_price, s.price_levels, s.def_cost
          FROM woo.store_product_state s WHERE s.fdm4_store=%s
           AND s.is_active AND s.kind='variation' AND s.price IS NOT NULL
         ORDER BY s.fdm4_store,s.style_code,s.sku LIMIT 50001
        ), hits AS MATERIALIZED (
        SELECT c.*, rp.final_price AS after_price, rp.applied_rule_ids
          FROM cand c CROSS JOIN LATERAL woo.eval_price_rules(
               c.fdm4_store,c.style_code,c.brand,c.category,c.before_price,
               c.price_levels,c.def_cost,current_date,NULL,NULL) rp)
    """
    cursor.execute(cte + """SELECT count(*) AS evaluated,
        count(*) FILTER (WHERE after_price<>before_price) AS changed FROM hits""", (store,))
    summary = dict(cursor.fetchone())
    sample, truncated, byte_truncated = _bounded_query(cursor, cte + """
        SELECT left(sku,100) AS sku, left(style_code,100) AS style_code,
               left(color,100) AS color, left(size,100) AS size, before_price, after_price,
               after_price-before_price AS delta, applied_rule_ids
          FROM hits WHERE after_price<>before_price
         ORDER BY abs(after_price-before_price) DESC,sku LIMIT %s
    """, (store,limit+1),limit)
    per_rule, rule_truncated, rule_bytes = _bounded_query(cursor, cte + """
        SELECT rid AS rule_id,left(p.name,1024) AS name,count(*) AS affected
          FROM hits CROSS JOIN LATERAL unnest(applied_rule_ids) rid
          JOIN woo.price_rule p ON p.rule_id=rid
         GROUP BY rid,p.name ORDER BY rid LIMIT 501
    """,(store,),500)
    cursor.execute("SELECT 1 FROM woo.sync_exclusion WHERE fdm4_store=%s AND style_code='' AND active LIMIT 1",(store,))
    frozen = cursor.fetchone() is not None
    summary["truncated"] = summary["evaluated"] >= 50001
    return _ops_result(sample, truncated or rule_truncated or summary["truncated"], byte_truncated or rule_bytes,
                       store=store, summary=summary, per_rule=per_rule, sample=sample, frozen=frozen,
                       rule_names={str(r["rule_id"]):r["name"] for r in per_rule})


def _wp_blog(cursor, store):
    store = _clean(store,"store").upper()
    cursor.execute("SELECT blog_id FROM woo.store_blog_map WHERE fdm4_store=%s ORDER BY blog_id LIMIT 2",(store,))
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise QueryNotFound("Store must map to exactly one WordPress site")
    return int(rows[0]["blog_id"])


def _wp_mapped_blog(cursor, blog_id):
    """A blog id is accepted only when it belongs to a mapped store, the same
    population a store code can reach; the public site is never one."""
    blog = int(blog_id)
    if blog < 1:
        raise QueryValidationError("Invalid blog_id")
    cursor.execute("SELECT 1 FROM woo.store_blog_map WHERE blog_id=%s LIMIT 1", (blog,))
    if cursor.fetchone() is None:
        raise QueryNotFound("WordPress site is not a mapped store")
    return blog


def _wp_read(callback):
    import wp_bridge
    settings = wp_bridge.get_settings()
    if not (settings.wp_sync_url and settings.wp_sync_user and settings.wp_sync_app_password):
        return {"available": False, "reason": "WordPress bridge is not configured"}
    try:
        result = callback()
        if not isinstance(result, dict):
            raise ValueError("Invalid WordPress response")
        # Bound every nested string/collection before it reaches a caller.
        import json
        if len(json.dumps(result, default=str).encode()) > READ_MAX_RESULT_BYTES:
            raise ValueError("WordPress response exceeds the byte limit")
        bounded = _bounded_category_value(result,200)
        bounded["truncated"] = bool(result.get("truncated")) or bounded != result
        bounded["available"] = result.get("available", True) is True
        return bounded
    except Exception as exc:
        # The reason stays fixed so no URL, credential or order data can leak;
        # the exception type goes to the log so a code defect is not silent.
        _log.warning("WordPress diagnostic read failed: %s", type(exc).__name__)
        return {"available": False, "reason": "WordPress diagnostics could not be read"}


def wp_product_check(cursor, *, store, style=None, sku=None):
    import wp_bridge
    if (style is None) == (sku is None):
        raise QueryValidationError("Supply exactly one of style or sku")
    style = _clean(style,"style") if style is not None else None
    sku = _clean(sku,"sku") if sku is not None else None
    try:
        blog = _wp_blog(cursor,store)
    except QueryNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {**_wp_read(lambda: wp_bridge.wp_diag_product(blog,style=style,sku=sku)), "blog_id": blog}


def wp_store_check(cursor, *, store):
    import wp_bridge
    try:
        blog = _wp_blog(cursor,store)
    except QueryNotFound as exc:
        return {"available": False, "reason": str(exc)}
    result = _wp_read(lambda: wp_bridge.wp_diag_store(blog))
    result["sync_log"] = _wp_read(lambda: wp_bridge.wp_diag_sync_log(blog,limit=20))
    result["blog_id"] = blog
    return result


# Scalars only at leaves: even an allowed key cannot smuggle a nested object.
ORDER_DIAGNOSTIC_KEYS = {
    "found": None, "order_id": None, "blog_id": None, "status": None,
    "created_gmt": None, "modified_gmt": None, "currency": None, "total": None,
    "item_count": None, "truncated": None,
    "items": [{"sku": None, "qty": None, "line_total": None,
               "embellishment": {"logo_codes": [None], "placements": [None]}}],
    "payment": {"method_code": None, "gateway_id": None, "punchout": None},
    "fdm4": {"found": None, "status": None, "last_error": None, "created_at": None,
             "updated_at": None, "in_vn": None, "vn_attempts": None, "vn_so_id": None},
}


def _order_allowlist(value, schema):
    if isinstance(schema, dict):
        value = value if isinstance(value, dict) else {}
        return {key: _order_allowlist(value[key], child) for key, child in schema.items() if key in value}
    if isinstance(schema,list):
        return [_order_allowlist(row,schema[0]) for row in value[:100]] if isinstance(value,list) else []
    if isinstance(value,str):
        return value[:1024]
    return value if value is None or isinstance(value,(bool,int,float)) else None


def get_order_status(cursor, *, order_id, store=None, blog_id=None):
    import wp_bridge
    if (store is None) == (blog_id is None) or int(order_id)<1:
        raise QueryValidationError("Supply an order id and exactly one store or blog_id")
    try:
        blog = _wp_blog(cursor,store) if store is not None else _wp_mapped_blog(cursor, blog_id)
    except QueryNotFound as exc:
        return {"available": False, "reason": str(exc)}
    result = _wp_read(lambda: wp_bridge.wp_diag_order(blog,order_id=int(order_id)))
    if not result.get("available"):
        # Never relay an arbitrary bridge reason that may include order data.
        return {"available": False, "reason": "WordPress order diagnostics are unavailable"}
    return {**_order_allowlist(result,ORDER_DIAGNOSTIC_KEYS), "available": True}


def _ops_section(cursor, callback, *, timeout="3s"):
    """A failed SQL section must not poison the caller's remaining reads."""
    cursor.execute("SAVEPOINT ops_read_section")
    try:
        cursor.execute("SELECT current_setting('statement_timeout') AS timeout")
        previous = cursor.fetchone()["timeout"]
        cursor.execute("SELECT set_config('statement_timeout', %s, true)",(timeout,))
        result = _bounded_category_value(callback(),500)
        cursor.execute("SELECT set_config('statement_timeout', %s, true)",(previous,))
        cursor.execute("RELEASE SAVEPOINT ops_read_section")
        return result
    except Exception as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT ops_read_section")
        cursor.execute("RELEASE SAVEPOINT ops_read_section")
        reason = str(exc)[:200] if isinstance(exc,QueryServiceError) else "This section could not be read"
        return {"available": False, "reason": reason}


def _product_disagreements(state, wordpress):
    findings = []
    if not wordpress.get("available") or state.get("available") is False or not state.get("found"):
        # A missing warehouse side is reported as an unavailable section,
        # never as a disagreement about visibility.
        return findings
    expected = any(r.get("is_active") for r in state.get("rows",[]))
    if expected and not wordpress.get("found"):
        return ["WordPress is missing the product; the warehouse expects it visible."]
    if not wordpress.get("found"):
        return findings
    if expected and wordpress.get("status") != "publish":
        findings.append(f"WordPress has the product as {str(wordpress.get('status'))[:100]}; the warehouse expects it visible.")
    if not expected and wordpress.get("status") == "publish":
        findings.append("WordPress has the product published; the warehouse expects it hidden.")
    by_sku = {r.get("sku"):r for r in wordpress.get("variations",[]) if isinstance(r,dict)}
    if wordpress.get("sku"):
        by_sku.setdefault(wordpress["sku"],wordpress)
    from decimal import Decimal, InvalidOperation
    for row in state.get("variations",[])[:200]:
        live = by_sku.get(row["sku"])
        if live is None:
            if row.get("is_active") and not wordpress.get("truncated"):
                findings.append(f"WordPress is missing active SKU {row['sku']}.")
            continue
        if bool(row.get("is_active")) != (live.get("status")=="publish"):
            findings.append(f"SKU {row['sku']} has a different active status in WordPress.")
        if row.get("stock") is not None and live.get("stock_status") in {"instock", "outofstock"}:
            expected_status = "instock" if Decimal(str(row["stock"])) > 0 else "outofstock"
            if live["stock_status"] != expected_status:
                findings.append(f"SKU {row['sku']} has stock status {live['stock_status']} in WordPress; the warehouse expects {expected_status}.")
        for expected_key,live_key,label in (("price","price","price"),("stock","stock_quantity","stock")):
            if row.get(expected_key) is None or live.get(live_key) in (None,""):
                continue
            try:
                differs = Decimal(str(row[expected_key])) != Decimal(str(live[live_key]))
            except InvalidOperation:
                continue
            if differs:
                findings.append(f"SKU {row['sku']} has {label} {live[live_key]} in WordPress; the warehouse expects {row[expected_key]}.")
    return findings[:200]


def find_issues(cursor, *, store=None, checks=None, limit=50, category_access=False):
    store = _optional(store,"store").upper()
    limit = max(1,min(int(limit),200))
    catalog = ""
    if store:
        catalog = _catalog_for_store(cursor, store)
        if catalog is None:
            raise QueryNotFound("Store not found")
    # Only rows of a store's current catalog count, as get_product_state does.
    current_any = ("AND EXISTS (SELECT 1 FROM woo.store_catalog c WHERE c.fdm4_store=s.fdm4_store"
                   " AND c.catalog_id=s.catalog_id AND c.suggested)")
    current = "AND ((%(catalog)s<>'' AND s.catalog_id=%(catalog)s) OR (%(catalog)s='' " + current_any + "))"
    definitions = {
        "no_logos": ("""SELECT s.fdm4_store AS store,s.style_code AS style FROM woo.store_product_state s
            WHERE s.is_active AND (%(store)s='' OR s.fdm4_store=%(store)s) """ + current + """
            AND NOT EXISTS (SELECT 1 FROM logo.assignment a WHERE a.fdm4_store=s.fdm4_store AND a.product_style=s.style_code AND a.active)
            GROUP BY s.fdm4_store,s.style_code""", "Configure logos for these styles in Logo Configuration."),
        "colors_unclassified": ("""SELECT DISTINCT s.color_code,s.color FROM woo.store_product_state s
            WHERE s.is_active AND s.kind='variation' AND nullif(s.color_code,'') IS NOT NULL
            AND (%(store)s='' OR s.fdm4_store=%(store)s) """ + current + """
            AND NOT EXISTS (SELECT 1 FROM logo.color_class c WHERE c.color_code=s.color_code)""", "Classify the garment colors in Logo Colors."),
        "rules_expiring": ("""SELECT rule_id,name,effective_until FROM woo.price_rule p WHERE active
            AND effective_until<=current_date+7 AND (%(store)s='' OR
            (coalesce(cardinality(stores),0)=0 AND coalesce(cardinality(store_tiers),0)=0)
             OR %(store)s=ANY(stores) OR EXISTS(SELECT 1 FROM woo.store_pricing_tier t WHERE t.fdm4_store=%(store)s AND t.tier_name=ANY(p.store_tiers)))
            AND (%(store)s='' OR NOT coalesce(%(store)s=ANY(excl_stores),false))""", "Review the expiry date or switch off the rule in Price Rules."),
        "stores_frozen": ("""SELECT fdm4_store AS store,scope,note,updated_by,updated_at FROM woo.sync_exclusion
            WHERE active AND style_code='' AND created_at<now()-interval '7 days'
            AND (%(store)s='' OR fdm4_store=%(store)s)""", "Review the note and remove the freeze in Sync Blocks when ready."),
        "stock_overrides_stale": ("""SELECT style_code,mode,note,updated_by FROM woo.stock_override o
            WHERE NOT EXISTS (SELECT 1 FROM woo.store_product_state s WHERE s.style_code=o.style_code AND s.is_active """ + current_any + """)
            AND (%(store)s='' OR EXISTS(SELECT 1 FROM woo.store_product_state s WHERE s.fdm4_store=%(store)s AND s.style_code=o.style_code """ + current + """))""", "Remove obsolete style exceptions in Fake Inventory."),
        "uncategorized_products": ("""SELECT u.env,u.blog_id,u.product_id,u.sku FROM catmgr.wp_uncategorized_product u
            JOIN catmgr.snapshot s ON s.env=u.env AND s.blog_id=u.blog_id AND s.version=u.snapshot_version
            WHERE (%(store)s='' OR EXISTS(SELECT 1 FROM woo.store_blog_map m WHERE m.blog_id=u.blog_id AND m.fdm4_store=%(store)s))""", "Review the latest category snapshot and assign product categories."),
    }
    names = list(dict.fromkeys(checks or [*definitions,"wordpress_mismatch"]))
    if set(names)-set(definitions)-{"wordpress_mismatch"}:
        raise QueryValidationError("Unknown issue check")
    deadline = time.monotonic()+8
    output = []
    for name in names:
        fix = definitions.get(name,(None,"Review the product and sync diagnostics before changing it."))[1]
        if name=="uncategorized_products" and not category_access:
            result = {"available":False,"reason":"Category access is required"}
        elif time.monotonic()>=deadline:
            result = {"available":False,"reason":"Check time budget reached"}
        elif name=="wordpress_mismatch":
            def compare():
                if not store:
                    return {"available":False,"reason":"Select one store for WordPress comparisons"}
                cursor.execute("SELECT DISTINCT style_code FROM woo.store_product_state WHERE fdm4_store=%s AND catalog_id=%s AND is_active ORDER BY style_code LIMIT 26",(store,catalog))
                styles = cursor.fetchall()
                sample, failures, checked = [], [], 0
                for row in styles[:25]:
                    if time.monotonic()>=deadline-2:
                        failures.append({"available":False,"reason":"Comparison time budget reached"})
                        break
                    state = get_product_state(cursor,store=store,style=row["style_code"])
                    live = wp_product_check(cursor,store=store,style=row["style_code"])
                    if not live.get("available"):
                        failures.append({"style":row["style_code"],**live})
                        break
                    checked += 1
                    findings = _product_disagreements(state,live)
                    if findings:
                        sample.append({"style":row["style_code"],"findings":findings[:10]})
                return {"available":not failures,"count":len(sample),"sample":sample[:limit],
                        "checked":checked,"failures":failures,"truncated":checked<len(styles)}
            result = _ops_section(cursor,compare,timeout="1s")
        else:
            sql = definitions[name][0]
            def check(sql=sql):
                rows,truncated,byte_truncated = _bounded_query(cursor,
                    "SELECT q.*,count(*) OVER() AS issue_count FROM ("+sql+") q ORDER BY to_jsonb(q)::text LIMIT %(limit)s",
                    {"store":store,"catalog":catalog,"limit":limit+1},limit)
                count = int(rows[0]["issue_count"]) if rows else 0
                for row in rows:
                    row.pop("issue_count",None)
                return {"available":True,"count":count,"sample":rows,"truncated":truncated or byte_truncated}
            result = _ops_section(cursor,check,timeout="1s")
        output.append({"check":name,"count":None,"sample":[],"how_to_fix":fix,**result})
    return {"store":store or None,"checks":output,"truncated":any(r.get("truncated",False) for r in output)}


def explain_product(cursor, *, store, style):
    store,style = _clean(store,"store").upper(),_clean(style,"style").upper()
    state = _ops_section(cursor,lambda:get_product_state(cursor,store=store,style=style))
    prices = _ops_section(cursor,lambda:check_price_rules(cursor,store=store,style=style))
    stock = _ops_section(cursor,lambda:get_stock_rules(cursor,store=store))
    mills = {r.get("mill_code") for r in state.get("rows",[])}
    if "brand_rules" in stock:
        stock["brand_rules"] = [r for r in stock["brand_rules"] if r["mill_code"] in mills]
        stock["style_exceptions"] = [r for r in stock.get("style_exceptions",[]) if r["style_code"]==style]
    blocks = _ops_section(cursor,lambda:list_sync_blocks(cursor,store=store))
    if "blocks" in blocks:
        blocks["blocks"] = [r for r in blocks["blocks"] if r["active"] and r["style_code"] in ("",style)]
    def mix_section():
        registry = mix_service.registry(cursor,store,required=False)
        if not registry or not registry["active"]:
            return {"mode":"fdm4","in_mix":any(r.get("is_active") for r in state.get("rows",[])),
                    "reason":"Not using a custom product list"}
        return get_style_mix(cursor,store=store,style=style)
    mix = _ops_section(cursor,mix_section)
    wordpress = _ops_section(cursor,lambda:wp_product_check(cursor,store=store,style=style))
    findings = _product_disagreements(state,wordpress)
    for block in blocks.get("blocks",[]):
        findings.append(f"The {'store' if not block['style_code'] else 'style'} has a {block['scope']} sync freeze since {block['updated_at']}; warehouse changes in that scope will not reach the site.")
    if mix.get("mode")=="list" and not mix.get("in_mix"):
        findings.append("The style is excluded from the store's custom product list.")
    sections = {"state":state,"prices":prices,"stock_rules":stock,"blocks":blocks,"mix":mix}
    for name,section in {**sections,"wordpress":wordpress}.items():
        if section.get("available") is False:
            findings.append(f"{name.replace('_',' ').capitalize()}: {section.get('reason','unavailable')}.")
    visible = any(r.get("is_active") for r in state.get("rows",[])) if state.get("found") else None
    modes = [r["mode"] for r in stock.get("style_exceptions",[]) if r.get("active")]
    modes += [r["mode"] for r in stock.get("brand_rules",[]) if r.get("mode")]
    stock_mode = modes[0] if modes else ("automatic" if stock.get("available") is not False else None)
    return {"store":store,"style":style,"intent":{"visible":visible,"stock_mode":stock_mode,**sections},
            "wordpress":wordpress,"findings":findings[:200]}
