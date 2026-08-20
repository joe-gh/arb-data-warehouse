#!/usr/bin/env python3
"""Pull the Sales Layer api2 catalog into pim.api_* landing tables.

Content authority split: FDM4 owns skus, colors, sizes, prices, and stock;
the PIM owns names, descriptions, attribute metadata, and imagery. This
puller lands the COMPLETE raw payloads; the pim.api_product_content view is
the sanctioned content-only read surface.

Field lists for $select are discovered from /catalog/$metadata at runtime so
new PIM fields are picked up automatically. Pulls are incremental by
modification date with a small overlap window; --full ignores watermarks.

Run on the warehouse box as the postgres OS user:

    sudo -u postgres env PIM_API_KEY=... \
        /opt/fdm4-extractor/venv/bin/python /opt/fdm4-extractor/pull_pim.py [--full]

The hourly cron wrapper sources /opt/fdm4-extractor/pim.env for the key.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import psycopg2
import psycopg2.extras

API_BASE = "https://api2.saleslayer.com"
PAGE_SIZE = 100          # the API silently returns zero rows above this
OVERLAP_SECONDS = 600    # re-read window behind the watermark
TIMEOUT = 30
MAX_RETRIES = 3

DB_NAME = "arb_warehouse"
DB_SOCKET = "/var/run/postgresql"


def api_get(path, key):
    url = API_BASE + path
    request = urllib.request.Request(url, headers={"X-API-KEY": key})
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.load(response)
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))


def type_names(spec):
    declared = (spec or {}).get("type")
    if isinstance(declared, list):
        return set(declared)
    return {declared} if declared else set()


def discover_fields(key):
    """Map entity title -> (selectable property names, object-typed names).

    Navigation properties (Products, Variants, AttributeSets) are arrays and
    never selectable. Object-typed properties are the image/video assets; they
    are wanted content, but kept separately so a pull can drop them if the
    API ever refuses them in $select.
    """
    doc = api_get("/catalog/$metadata", key)
    fields = {}
    for entity in doc.get("value", []):
        title = entity.get("title")
        props = entity.get("properties") or {}
        names = [n for n, spec in props.items() if "array" not in type_names(spec)]
        objects = {n for n, spec in props.items() if "object" in type_names(spec)}
        if title and names:
            fields[title] = (names, objects)
    return fields


def probe_fields(key, entity, names, objects):
    """Return the widest field list the API accepts for this entity."""
    for candidate in (names, [n for n in names if n not in objects]):
        try:
            api_get(f"/catalog/{entity}?" + urllib.parse.urlencode(
                {"$select": ",".join(candidate), "$top": "1"},
                quote_via=urllib.parse.quote), key)
            if candidate is not names:
                print(f"pull_pim: {entity}: object fields rejected by $select; "
                      f"pulling without {sorted(objects)}")
            return candidate
        except Exception:
            if candidate is not names:
                raise
    return names


def walk(key, first_path):
    path = first_path
    while path:
        data = api_get(path, key)
        for item in data.get("value", []):
            yield item
        next_link = data.get("@nextLink") or ""
        path = next_link[len(API_BASE):] if next_link.startswith(API_BASE) else next_link or None


def catalog_path(entity, fields, modify_field, since):
    query = {"$select": ",".join(fields), "$top": str(PAGE_SIZE)}
    if since:
        query["$filter"] = f"{modify_field} gt {since}"
    return f"/catalog/{entity}?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def get_watermark(cursor, entity):
    cursor.execute("SELECT watermark FROM pim.api_pull_state WHERE entity = %s", (entity,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_watermark(cursor, entity, watermark, count, note=""):
    cursor.execute(
        """
        INSERT INTO pim.api_pull_state (entity, watermark, last_run, last_count, note)
        VALUES (%s, %s, now(), %s, %s)
        ON CONFLICT (entity) DO UPDATE SET
            watermark = COALESCE(EXCLUDED.watermark, pim.api_pull_state.watermark),
            last_run = now(), last_count = EXCLUDED.last_count, note = EXCLUDED.note
        """,
        (entity, watermark, count, note),
    )


def since_expression(cursor, entity, full):
    if full:
        return None
    watermark = get_watermark(cursor, entity)
    if not watermark:
        return None
    cursor.execute(
        "SELECT to_char((%s::timestamptz - make_interval(secs => %s)) AT TIME ZONE 'UTC', "
        "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')",
        (watermark, OVERLAP_SECONDS),
    )
    return cursor.fetchone()[0]


def pull_products(connection, key, fields, full):
    since = None
    with connection.cursor() as cursor:
        since = since_expression(cursor, "products", full)
    count = 0
    newest = None
    with connection.cursor() as cursor:
        for item in walk(key, catalog_path("products", fields, "prod_modify", since)):
            ref = (item.get("prod_ref") or "").strip()
            if not ref:
                continue
            modify = item.get("prod_modify") or None
            cursor.execute(
                """
                INSERT INTO pim.api_product (prod_ref, style_number, prod_modify, payload, pulled_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (prod_ref) DO UPDATE SET
                    style_number = EXCLUDED.style_number,
                    prod_modify = EXCLUDED.prod_modify,
                    payload = EXCLUDED.payload,
                    pulled_at = now()
                """,
                (
                    ref,
                    (item.get("prod_stylenumber") or "").strip(),
                    modify,
                    psycopg2.extras.Json(item),
                ),
            )
            count += 1
            if modify and (newest is None or modify > newest):
                newest = modify
        set_watermark(cursor, "products", newest, count, "incremental" if since else "full")
    connection.commit()
    return count


def pull_variants(connection, key, fields, full):
    with connection.cursor() as cursor:
        since = since_expression(cursor, "variants", full)
    count = 0
    newest = None
    with connection.cursor() as cursor:
        for item in walk(key, catalog_path("variants", fields, "frmt_modify", since)):
            ref = (item.get("frmt_ref") or "").strip()
            if not ref:
                continue
            modify = item.get("frmt_modify") or None
            cursor.execute(
                """
                INSERT INTO pim.api_variant (frmt_ref, prod_ref, frmt_modify, payload, pulled_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (frmt_ref) DO UPDATE SET
                    prod_ref = EXCLUDED.prod_ref,
                    frmt_modify = EXCLUDED.frmt_modify,
                    payload = EXCLUDED.payload,
                    pulled_at = now()
                """,
                (
                    ref,
                    (item.get("prod_ref") or "").strip(),
                    modify,
                    psycopg2.extras.Json(item),
                ),
            )
            count += 1
            if modify and (newest is None or modify > newest):
                newest = modify
        set_watermark(cursor, "variants", newest, count, "incremental" if since else "full")
    connection.commit()
    return count


def pull_images(connection, key, full):
    """DAM images. $filter support is probed once; without it, incremental
    runs skip the DAM (a --full run always covers it)."""
    with connection.cursor() as cursor:
        since = since_expression(cursor, "images", full)
    first = "/dam/images?" + urllib.parse.urlencode(
        {"$top": str(PAGE_SIZE)}
        | ({"$filter": f"modifiedOn gt {since}"} if since else {}),
        quote_via=urllib.parse.quote,
    )
    try:
        probe = api_get(first, key)
    except Exception:
        if since:
            with connection.cursor() as cursor:
                set_watermark(cursor, "images", None, 0, "dam filter unsupported; skipped")
            connection.commit()
            return 0
        raise
    count = 0
    newest = None
    with connection.cursor() as cursor:
        path = first
        data = probe
        while True:
            for item in data.get("value", []):
                image_id = item.get("id")
                if image_id is None:
                    continue
                modified = item.get("modifiedOn") or None
                cursor.execute(
                    """
                    INSERT INTO pim.api_image (image_id, reference, modified_on, payload, pulled_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (image_id) DO UPDATE SET
                        reference = EXCLUDED.reference,
                        modified_on = EXCLUDED.modified_on,
                        payload = EXCLUDED.payload,
                        pulled_at = now()
                    """,
                    (
                        int(image_id),
                        (item.get("reference") or "").strip(),
                        modified,
                        psycopg2.extras.Json(item),
                    ),
                )
                count += 1
                if modified and (newest is None or modified > newest):
                    newest = modified
            next_link = data.get("@nextLink") or ""
            path = next_link[len(API_BASE):] if next_link.startswith(API_BASE) else next_link or None
            if not path:
                break
            data = api_get(path, key)
        set_watermark(cursor, "images", newest, count, "incremental" if since else "full")
    connection.commit()
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="ignore watermarks and pull everything")
    parser.add_argument("--skip-images", action="store_true", help="catalog entities only")
    args = parser.parse_args(argv)

    key = (os.environ.get("PIM_API_KEY") or "").strip()
    if not key:
        print("pull_pim: PIM_API_KEY is not set", file=sys.stderr)
        return 1

    started = time.time()
    fields = discover_fields(key)
    for required in ("Product", "Variant"):
        if required not in fields:
            print(f"pull_pim: entity {required} missing from /catalog/$metadata", file=sys.stderr)
            return 1
    product_fields = probe_fields(key, "products", *fields["Product"])
    variant_fields = probe_fields(key, "variants", *fields["Variant"])

    connection = psycopg2.connect(dbname=DB_NAME, host=DB_SOCKET)
    try:
        products = pull_products(connection, key, product_fields, args.full)
        variants = pull_variants(connection, key, variant_fields, args.full)
        images = 0 if args.skip_images else pull_images(connection, key, args.full)
    finally:
        connection.close()

    mode = "full" if args.full else "incremental"
    print(
        f"pull_pim: {mode} products={products} variants={variants} images={images} "
        f"elapsed={time.time() - started:.0f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
