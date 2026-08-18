"""Canonical store-aware FDM4 design -> art resolution.

Modern FDM4 rows link ``design_pool.design_id`` to ``cust_art_file.art_id``.
Legacy rows may omit that link and use the same value for both identifiers;
that equality is a fallback only.  Callers always receive real design IDs.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple


DESIGN_INDEX_SQL = """
    WITH mapped AS (
        SELECT DISTINCT
               btrim(dp.design_id) AS design_id,
               btrim(design.cust_number) AS customer,
               btrim(caf.color_scheme_id) AS color_scheme_id,
               upper(regexp_replace(
                   regexp_replace(caf.target_filename, '^.*/', ''),
                   '[^A-Za-z0-9].*$', ''
               )) AS logo_prefix,
               upper(btrim(caf.resource_type)) AS resource_type,
               COALESCE(
                   NULLIF(btrim(caf.target_web_path), ''),
                   NULLIF(ltrim(btrim(caf.target_filename), '/'), '')
               ) AS asset_file
          FROM fdm4.design_pool AS dp
          JOIN fdm4.dec_design AS design
            ON btrim(design.design_id) = btrim(dp.design_id)
          JOIN fdm4.cust_art_file AS caf
            ON btrim(caf.art_id) = btrim(dp.art_id)
         WHERE NULLIF(btrim(dp.design_id), '') IS NOT NULL
           AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
           AND NULLIF(btrim(caf.target_filename), '') IS NOT NULL
    ), legacy AS (
        SELECT DISTINCT
               btrim(design.design_id) AS design_id,
               btrim(design.cust_number) AS customer,
               btrim(caf.color_scheme_id) AS color_scheme_id,
               upper(regexp_replace(
                   regexp_replace(caf.target_filename, '^.*/', ''),
                   '[^A-Za-z0-9].*$', ''
               )) AS logo_prefix,
               upper(btrim(caf.resource_type)) AS resource_type,
               COALESCE(
                   NULLIF(btrim(caf.target_web_path), ''),
                   NULLIF(ltrim(btrim(caf.target_filename), '/'), '')
               ) AS asset_file
          FROM fdm4.dec_design AS design
          JOIN fdm4.cust_art_file AS caf
            ON btrim(caf.art_id) = btrim(design.design_id)
         WHERE NULLIF(btrim(design.design_id), '') IS NOT NULL
           AND NULLIF(btrim(caf.target_filename), '') IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
                 FROM fdm4.design_pool AS dp
                WHERE (
                        btrim(dp.design_id) = btrim(design.design_id)
                        OR btrim(dp.art_id) = btrim(caf.art_id)
                      )
                  AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
           )
    )
    SELECT * FROM mapped
    UNION ALL
    SELECT * FROM legacy
"""


@dataclass(frozen=True)
class DesignIndex:
    by_key: Dict[Tuple[str, str, str], Set[str]]
    usable_art: Set[Tuple[str, str]]

    def candidates(self, store: str, logo_prefix: str, scheme: str) -> Set[str]:
        customer = store_customer(store)
        prefix = logo_prefix.strip().upper()
        scheme_key = scheme.strip().upper()
        if customer:
            owned = self.by_key.get((customer, prefix, scheme_key)) or self.by_key.get(
                (customer, prefix, "*")
            )
            if owned:
                return set(owned)
        return set(
            self.by_key.get(("*", prefix, scheme_key))
            or self.by_key.get(("*", prefix, "*"))
            or ()
        )


def store_customer(store: str) -> str:
    value = str(store).strip()
    return value[2:] if value.startswith("S_") else ""


def load_design_index(cursor) -> DesignIndex:
    cursor.execute(DESIGN_INDEX_SQL)
    by_key: Dict[Tuple[str, str, str], Set[str]] = {}
    usable_art: Set[Tuple[str, str]] = set()
    for row in cursor.fetchall():
        prefix = str(row["logo_prefix"] or "").upper()
        scheme = str(row["color_scheme_id"] or "").upper()
        design_id = str(row["design_id"] or "").strip()
        customer = str(row["customer"] or "").strip()
        if not prefix or not design_id:
            continue
        # A customer-owned design must never become a wildcard candidate for
        # another store. Only genuinely unowned legacy rows use the fallback.
        owner = customer or "*"
        by_key.setdefault((owner, prefix, scheme), set()).add(design_id)
        by_key.setdefault((owner, prefix, "*"), set()).add(design_id)
        if str(row["resource_type"] or "").upper() in {"PREVIEW", "THUMB"} and row[
            "asset_file"
        ]:
            usable_art.add((design_id, scheme))
    return DesignIndex(by_key=by_key, usable_art=usable_art)


def validate_design_asset(
    cursor,
    *,
    store: str,
    design_id: str,
    scheme: str,
    logo_code: Optional[str] = None,
) -> bool:
    """Return whether the mapped/fallback art has the scheme and optional code."""

    customer = store_customer(store)
    cursor.execute(
        """
        SELECT 1
          FROM fdm4.dec_design
         WHERE btrim(design_id) = %s
           AND (
               btrim(cust_number) = %s
               OR NULLIF(btrim(cust_number), '') IS NULL
           )
         ORDER BY (btrim(cust_number) = %s) DESC
         LIMIT 1
        """,
        (design_id, customer, customer),
    )
    if cursor.fetchone() is None:
        return False

    cursor.execute(
        """
        WITH mapped AS (
            SELECT caf.target_filename
              FROM fdm4.design_pool dp
              JOIN fdm4.cust_art_file caf
                ON btrim(caf.art_id) = btrim(dp.art_id)
             WHERE btrim(dp.design_id) = %s
               AND upper(btrim(caf.color_scheme_id)) = upper(%s)
        ), candidates AS (
            SELECT target_filename FROM mapped
            UNION ALL
            SELECT caf.target_filename
              FROM fdm4.cust_art_file caf
             WHERE btrim(caf.art_id) = %s
               AND upper(btrim(caf.color_scheme_id)) = upper(%s)
               AND NOT EXISTS (SELECT 1 FROM mapped)
               AND NOT EXISTS (
                   SELECT 1 FROM fdm4.design_pool collision
                    WHERE btrim(collision.art_id) = %s
                      AND NULLIF(btrim(collision.art_id), '') IS NOT NULL
               )
        )
        SELECT 1
          FROM candidates
         WHERE %s IS NULL
            OR upper(regexp_replace(
                   regexp_replace(target_filename, '^.*/', ''),
                   '[^A-Za-z0-9].*$', ''
               )) LIKE upper(%s) || '%%'
         LIMIT 1
        """,
        (
            design_id, scheme, design_id, scheme, design_id,
            logo_code, logo_code or "",
        ),
    )
    return cursor.fetchone() is not None
