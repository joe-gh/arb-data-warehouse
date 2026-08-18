#!/usr/bin/env python3
"""Seed ``logo.assignment`` from the current Woo logo-sheet export.

The input is newline-delimited JSON produced by ``logo-export-current``. Run
this once, on the warehouse host, as the postgres OS user so peer
authentication applies::

    sudo -u postgres /opt/fdm4-extractor/venv/bin/python seed_logo.py export.ndjson

The import is atomic. Any malformed input or database failure rolls back both
assignments and its import-report rows.
"""

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import psycopg2


DB_NAME = "arb_warehouse"
DB_SOCKET = "/var/run/postgresql"

COLOR_LOOKUP_SQL = """
    SELECT btrim("style-code") AS style,
           lower(btrim(description)) AS color_name,
           btrim("color-code") AS color_code
      FROM fdm4."style-color"
     WHERE NULLIF(btrim("style-code"), '') IS NOT NULL
       AND NULLIF(btrim(description), '') IS NOT NULL
       AND NULLIF(btrim("color-code"), '') IS NOT NULL
"""

DESIGN_LOOKUP_SQL = """
    WITH mapped AS (
    SELECT DISTINCT
           btrim(dp.design_id) AS design_id,
           btrim(design.cust_number) AS customer,
           btrim(caf.color_scheme_id) AS color_scheme_id,
           upper(
               regexp_replace(
                   regexp_replace(caf.target_filename, '^.*/', ''),
                   '[^A-Za-z0-9].*$',
                   ''
               )
           ) AS logo_prefix,
           upper(btrim(caf.resource_type)) AS resource_type,
           COALESCE(
               NULLIF(btrim(caf.target_web_path), ''),
               NULLIF(ltrim(btrim(caf.target_filename), '/'), '')
           ) AS asset_file
      FROM fdm4.cust_art_file AS caf
      JOIN fdm4.design_pool AS dp
        ON btrim(dp.art_id) = btrim(caf.art_id)
      JOIN fdm4.dec_design AS design
        ON btrim(design.design_id) = btrim(dp.design_id)
     WHERE NULLIF(btrim(dp.design_id), '') IS NOT NULL
       AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
       AND NULLIF(btrim(caf.target_filename), '') IS NOT NULL
    ), legacy AS (
    SELECT DISTINCT
           btrim(design.design_id) AS design_id,
           btrim(design.cust_number) AS customer,
           btrim(caf.color_scheme_id) AS color_scheme_id,
           upper(
               regexp_replace(
                   regexp_replace(caf.target_filename, '^.*/', ''),
                   '[^A-Za-z0-9].*$',
                   ''
               )
           ) AS logo_prefix,
           upper(btrim(caf.resource_type)) AS resource_type,
           COALESCE(
               NULLIF(btrim(caf.target_web_path), ''),
               NULLIF(ltrim(btrim(caf.target_filename), '/'), '')
           ) AS asset_file
      FROM fdm4.cust_art_file AS caf
      JOIN fdm4.dec_design AS design
        ON btrim(design.design_id) = btrim(caf.art_id)
     WHERE NULLIF(btrim(design.design_id), '') IS NOT NULL
       AND NULLIF(btrim(caf.target_filename), '') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM fdm4.design_pool AS dp
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

UPSERT_ASSIGNMENT_SQL = """
    INSERT INTO logo.assignment (
        fdm4_store,
        product_style,
        garment_color_code,
        option_row,
        position,
        design_id,
        logo_code,
        color_scheme_id,
        location,
        optional,
        background,
        cost_override,
        sort_order,
        image_url,
        active,
        updated_by
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, true, 'seed'
    )
    ON CONFLICT (fdm4_store, product_style, garment_color_code, option_row, position)
    DO UPDATE SET
        design_id       = EXCLUDED.design_id,
        logo_code       = EXCLUDED.logo_code,
        color_scheme_id = EXCLUDED.color_scheme_id,
        location        = EXCLUDED.location,
        optional        = EXCLUDED.optional,
        background      = EXCLUDED.background,
        cost_override   = EXCLUDED.cost_override,
        sort_order      = EXCLUDED.sort_order,
        image_url       = EXCLUDED.image_url,
        active          = EXCLUDED.active,
        updated_by      = EXCLUDED.updated_by,
        updated_at      = now()
"""

INSERT_REPORT_SQL = """
    INSERT INTO logo.import_report (
        fdm4_store,
        product_style,
        product_color,
        logo_code,
        reason,
        detail
    ) VALUES (%s, %s, %s, %s, %s, %s)
"""


class SeedInputError(ValueError):
    """Raised when an NDJSON row cannot be safely imported."""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Seed logo.assignment from logo-export-current NDJSON."
    )
    parser.add_argument("ndjson", help="Path to the exported NDJSON file")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.ndjson):
        parser.error(f"input is not a regular file: {args.ndjson}")
    if not os.access(args.ndjson, os.R_OK):
        parser.error(f"input is not readable: {args.ndjson}")
    return args


def required_text(row, key, line_number, maximum=100):
    value = row.get(key)
    if value is None or isinstance(value, (dict, list)):
        raise SeedInputError(f"line {line_number}: {key} must be a non-empty scalar")
    value = str(value).strip()
    if not value:
        raise SeedInputError(f"line {line_number}: {key} must be non-empty")
    if "\x00" in value or len(value) > maximum:
        raise SeedInputError(f"line {line_number}: {key} is invalid")
    return value


def optional_text(row, key, line_number, maximum=200):
    value = row.get(key)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raise SeedInputError(f"line {line_number}: {key} must be a scalar")
    value = str(value).strip()
    if "\x00" in value or len(value) > maximum:
        raise SeedInputError(f"line {line_number}: {key} is invalid")
    return value


def parse_integer(value, field, line_number, default=0):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise SeedInputError(f"line {line_number}: {field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SeedInputError(
            f"line {line_number}: {field} must be an integer"
        ) from exc
    if isinstance(value, float) and not value.is_integer():
        raise SeedInputError(f"line {line_number}: {field} must be an integer")
    if isinstance(value, str) and value.strip() != str(parsed):
        raise SeedInputError(f"line {line_number}: {field} must be an integer")
    return parsed


def parse_position(value, line_number):
    position = parse_integer(value, "position", line_number, default=1)
    if position < 1 or position > 3:
        raise SeedInputError(f"line {line_number}: position must be between 1 and 3")
    return position


def parse_option_row(value, line_number):
    option_row = parse_integer(value, "option_row", line_number, default=1)
    if option_row < 1 or option_row > 999:
        raise SeedInputError(
            f"line {line_number}: option_row must be between 1 and 999"
        )
    return option_row


def parse_boolean(value, field, line_number, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    raise SeedInputError(f"line {line_number}: {field} must be a boolean")


def parse_cost(value, line_number):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SeedInputError(f"line {line_number}: cost must be numeric or null")
    try:
        cost = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise SeedInputError(
            f"line {line_number}: cost must be numeric or null"
        ) from exc
    if (
        not cost.is_finite()
        or cost < Decimal("-9999999999.99")
        or cost > Decimal("9999999999.99")
        or cost.as_tuple().exponent < -2
    ):
        raise SeedInputError(
            f"line {line_number}: cost must fit numeric(12,2) and use at most two decimal places"
        )
    return cost


def validate_image_url(value, line_number):
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise SeedInputError(
            f"line {line_number}: image must be an absolute HTTP(S) URL"
        )
    try:
        parsed.port
    except ValueError:
        raise SeedInputError(f"line {line_number}: image URL has an invalid port") from None
    return value


def load_color_lookup(cursor):
    cursor.execute(COLOR_LOOKUP_SQL)
    lookup = {}
    for style, color_name, color_code in cursor.fetchall():
        key = (style, color_name)
        lookup.setdefault(key, set()).add(color_code)
    return lookup


def load_design_lookup(cursor):
    cursor.execute(DESIGN_LOOKUP_SQL)
    by_prefix_scheme = {}
    usable_art = set()

    for (
        design_id, customer, color_scheme, logo_prefix, resource_type, asset_file
    ) in cursor.fetchall():
        scheme_key = (color_scheme or "").upper()
        prefix_key = (logo_prefix or "").upper()
        if not prefix_key:
            continue

        # Customer-owned designs never enter the wildcard bucket; doing so
        # would reintroduce cross-store candidates when a store has no match.
        owner = (customer or "").strip() or "*"
        by_prefix_scheme.setdefault(
            (owner, prefix_key, scheme_key), set()
        ).add(design_id)
        by_prefix_scheme.setdefault(
            (owner, prefix_key, "*"), set()
        ).add(design_id)

        if resource_type in ("PREVIEW", "THUMB") and asset_file:
            usable_art.add((design_id, scheme_key))

    return by_prefix_scheme, usable_art


def report(cursor, row, logo_code, reason, detail):
    cursor.execute(
        INSERT_REPORT_SQL,
        (
            row.get("fdm4_store"),
            row.get("product_style"),
            row.get("product_color"),
            logo_code,
            reason,
            detail,
        ),
    )


def import_row(cursor, row, line_number, color_lookup, design_lookup, usable_art, seen):
    if not isinstance(row, dict):
        raise SeedInputError(f"line {line_number}: each NDJSON value must be an object")

    store = required_text(row, "fdm4_store", line_number)
    style = required_text(row, "product_style", line_number)
    product_color = optional_text(row, "product_color", line_number, 100)
    logo_code = optional_text(row, "logo_code", line_number, 100).upper()
    color_scheme = optional_text(row, "color_scheme", line_number, 100)
    scheme_key = color_scheme.upper()

    if not logo_code:
        report(cursor, row, logo_code, "no_design", "empty logo_code")
        return False

    color_codes = color_lookup.get((style, product_color.lower()))
    if not color_codes:
        report(
            cursor,
            row,
            logo_code,
            "no_color_code",
            f"normalized color name: {product_color.lower()}",
        )
        return False
    if len(color_codes) > 1:
        report(
            cursor,
            row,
            logo_code,
            "ambiguous_color",
            ",".join(sorted(color_codes)),
        )
        return False
    garment_color_code = next(iter(color_codes))

    customer = store[2:] if store.startswith("S_") else ""
    design_ids = (
        design_lookup.get((customer, logo_code, scheme_key))
        or design_lookup.get((customer, logo_code, "*"))
        if customer
        else None
    )
    resolution = "prefix+scheme"
    if not design_ids:
        design_ids = design_lookup.get(("*", logo_code, scheme_key))
        resolution = "global prefix+scheme fallback"
    if not design_ids:
        design_ids = design_lookup.get(("*", logo_code, "*"))
        resolution = "global prefix-only fallback"

    if not design_ids:
        report(
            cursor,
            row,
            logo_code,
            "no_design",
            f"color_scheme={color_scheme}",
        )
        return False
    if len(design_ids) > 1:
        report(
            cursor,
            row,
            logo_code,
            "ambiguous_design",
            f"{resolution}; design_ids={','.join(sorted(design_ids))}",
        )
        return False
    design_id = next(iter(design_ids))

    # The storefront image is warehouse-owned (seeded from the sheet's media URL;
    # Phase B manages imports/uploads). Only reject on missing FDM4 art when the
    # row also carries no image of its own.
    image_url = validate_image_url(
        optional_text(row, "image", line_number, 2048), line_number
    )
    if not image_url and (design_id, scheme_key) not in usable_art:
        report(
            cursor,
            row,
            logo_code,
            "no_art",
            f"design_id={design_id}; color_scheme={color_scheme}; "
            "no sheet image and no PREVIEW/THUMB art",
        )
        return False

    position = parse_position(row.get("position"), line_number)
    option_row = parse_option_row(row.get("option_row"), line_number)
    sort_order = parse_integer(
        row.get("sort_order"), "sort_order", line_number, default=0
    )
    if sort_order < -2147483648 or sort_order > 2147483647:
        raise SeedInputError(f"line {line_number}: sort_order is outside integer range")

    key = (store, style, garment_color_code, option_row, position)
    if key in seen:
        report(cursor, row, logo_code, "duplicate_row", "duplicate assignment key in NDJSON")
        return False
    if position > 1:
        cursor.execute(
            """
            SELECT 1 FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s
               AND position = 1 AND active = true
             LIMIT 1
            """,
            key[:4],
        )
        if cursor.fetchone() is None:
            report(
                cursor,
                row,
                logo_code,
                "orphaned_companion",
                "position 2/3 requires an active position-1 assignment in the same option row",
            )
            return False

    # Per-row placement differences within a style/position are valid legacy
    # data (pocket vs chest logo rows); the projection collapses the
    # placements[] label to the first non-empty location, so no conflict gate.
    location = optional_text(row, "location", line_number, 200)

    cursor.execute(
        UPSERT_ASSIGNMENT_SQL,
        (
            store,
            style,
            garment_color_code,
            option_row,
            position,
            design_id,
            logo_code,
            color_scheme,
            location,
            parse_boolean(row.get("optional"), "optional", line_number),
            optional_text(row, "background", line_number, 200),
            parse_cost(row.get("cost"), line_number),
            sort_order,
            image_url,
        ),
    )
    seen.add(key)
    return True


def seed(path):
    connection = psycopg2.connect(
        host=DB_SOCKET,
        dbname=DB_NAME,
        user="postgres",
    )
    connection.autocommit = False

    imported = 0
    misses = 0
    try:
        with connection.cursor() as cursor:
            # Attribute this run's rows in logo.audit_log (read by the audit
            # triggers via the transaction-local logo.actor setting).
            cursor.execute("SELECT set_config('logo.actor', 'seed', true)")
            color_lookup = load_color_lookup(cursor)
            design_lookup, usable_art = load_design_lookup(cursor)
            seen = set()

            with open(path, encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise SeedInputError(
                            f"line {line_number}: invalid JSON: {exc.msg}"
                        ) from exc

                    if import_row(
                        cursor,
                        row,
                        line_number,
                        color_lookup,
                        design_lookup,
                        usable_art,
                        seen,
                    ):
                        imported += 1
                    else:
                        misses += 1

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return imported, misses


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        imported, misses = seed(args.ndjson)
    except Exception as exc:
        print(f"seed_logo: failed; transaction rolled back: {exc}", file=sys.stderr)
        return 1

    print(f"seed_logo: inserted/updated={imported} misses={misses}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
