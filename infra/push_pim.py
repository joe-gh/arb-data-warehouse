#!/usr/bin/env python3
"""PIM (Sales Layer api2) push service: warehouse -> PIM.

Fixes the PIM where the warehouse knows better and fills what it is missing,
per the approved August 2026 review sheets:

  color_fill      lane a  variant has no color code; FDM4 knows it
  color_fix       lane a/b  variant color code differs from FDM4
                  (a = pure zero padding, b = real divergence)
  variant_remove  lane b  ONLY refs from an explicit --remove-skus file
                  (the approved removal sheet); the diff never
                  auto-proposes deletions
  variant_create  lane b  FDM4 variant missing under an existing PIM product
  product_create  lane b  style live in Woo but absent from the PIM; created
                  visible with content backfilled from the Woo mirror
  product_publish lane b  draft (prod_stat D) product that is live in Woo;
                  switched to visible
  variant_publish lane b  draft (frmt_stat D) variant under a live product;
                  switched to visible
  product_remove  lane b  product that is live nowhere in Woo, OR that FDM4
                  does not know at all; deleted from the PIM together with
                  its variants (the reason is recorded on the row)
  style_fill      lane b  product with no style number whose reference or a
                  variant UPC FDM4 knows; gets FDM4's style code
  variant_orphan_remove
                  lane b  variant with no product reference. Sales Layer's
                  product DELETE does not delete variants: it detaches them
                  (prod_ref cleared) and drops them to draft, so every product
                  removal used to leave its variants behind as orphans.
                  product_remove now deletes a product's variants first; this
                  action sweeps up any orphan that still appears.
  (product_draft  retired 2026-09-14: removal replaced it; old rows remain)

Rule (2026-09-14, extended 2026-09-15): FDM4 is the single source of truth.
The PIM carries exactly the FDM4 products the Woo stores carry, and all of it
visible; a product none of whose identifiers (style number, reference, variant
UPCs) FDM4 knows leaves the PIM whatever Woo says, and a product FDM4 knows
but no store carries stays out. A product is "live" when a published, catalog-visible product
on at least one Woo store that is NOT an all-products store carries its style
number, its PIM reference, or one of its variant SKUs. The all-products stores
(PIM_ALL_PRODUCTS_BLOGS, default 2,36,59) carry the whole FDM4 range and say
nothing about merchandising, so they do not count. Presence comes from
pim.woo_presence, replaced hourly by WordPress (wp arb pim-presence-push).
When that set is missing or older than PIM_PRESENCE_MAX_AGE_HOURS every
presence-driven rule (create, publish, remove) is skipped for the run; only
colour fixes and variants under existing products go out, the variants
taking their parent's status. Removals additionally need the set to hold at
least PIM_PRESENCE_MIN_ROWS rows, so a truncated refresh cannot empty the PIM.

Automatic mode (the scheduled hourly run):
  --auto                 diff, then apply every row except variant removals,
                         in one go. Nothing here waits for a person. Older
                         change sets still holding proposed rows are closed
                         as superseded first (a fresh diff re-proposes
                         whatever still applies). Requires PIM_PUSH_ENABLED=1
                         like --apply. Two anomaly brakes: PIM_AUTO_MAX_FLIPS
                         (default 1000) caps product+variant publishes and
                         PIM_AUTO_MAX_REMOVALS (default 250) caps product
                         removals one run will send; above a cap those rows
                         are left proposed and reported, everything else
                         still applies, and the next run tries again. A
                         bigger backlog is applied by hand:
                         --approve --set N --action product_remove, then
                         --apply --set N --allow-removals [--limit N].

Manual stages (kept for removals and for inspection):
  --diff                 compute a new change set (read-only against the PIM)
  --summary [--set N]    print a change set summary with samples
  --approve --set N [--lane X] [--action Y]   mark rows approved
  --apply --set N [--limit N] [--allow-removals]
                         execute approved rows; requires PIM_PUSH_ENABLED=1,
                         otherwise prints what it would do

Safety: the apply engine re-reads every target first and skips rows whose
precondition no longer holds. A product delete re-checks the live presence set
at apply time and is skipped when the product went live since the diff or the
set is missing, stale or truncated; in manual mode it also requires
--allow-removals. Variant deletes require --allow-removals and are never
auto-proposed. A variant delete rechecks the premise
it was staged on - the sku must still be absent from FDM4 and the live colour
and size must still match the reviewed sheet - and a set carrying more than
REMOVAL_CAP removals is refused whole. Every row records its outcome.

Run on the warehouse box as postgres with the key in the environment:
  sudo -u postgres env $(sudo grep PIM_API_KEY /opt/fdm4-extractor/pim.env) \
      python3 /opt/fdm4-extractor/push_pim.py --diff
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

import psycopg2
import psycopg2.extras

API_BASE = "https://api2.saleslayer.com"
TIMEOUT = 30
MAX_RETRIES = 8
# Sales Layer documents 50 requests per 10 seconds for the whole key, which
# the hourly pull cron also shares. Pace requests well under that. Raising
# PIM_PUSH_MIN_INTERVAL also slows Sales Layer's downstream push fan-out to
# the Woo stores, which lands on prod with real weight during big batches.
MIN_REQUEST_INTERVAL = float(os.environ.get("PIM_PUSH_MIN_INTERVAL", "0.25"))
REMOVAL_CAP = 200
DB_NAME = "arb_warehouse"
DB_SOCKET = "/var/run/postgresql"

ACTIONS = ("color_fill", "color_fix", "variant_remove", "variant_create", "product_create",
           "product_publish", "product_draft", "variant_publish", "product_remove", "style_fill",
           "variant_orphan_remove")

# Product visibility in the PIM follows Woo presence (pim.woo_presence, pushed
# hourly by WordPress). Only stores outside PIM_ALL_PRODUCTS_BLOGS count as
# merchandising a product; those blogs carry the whole FDM4 range.
PRESENCE_ENV = os.environ.get("PIM_PRESENCE_ENV", "production")
PRESENCE_MAX_AGE_HOURS = float(os.environ.get("PIM_PRESENCE_MAX_AGE_HOURS", "3"))
ALL_PRODUCTS_BLOGS = tuple(
    int(b) for b in os.environ.get("PIM_ALL_PRODUCTS_BLOGS", "2,36,59").split(",") if b.strip())
# Anomaly brake for the automatic run: more publish/draft flips than this in
# one hour means something upstream is wrong (a broken presence set slips past
# the freshness check, a mass store change), so they wait for the next run.
AUTO_MAX_FLIPS = int(os.environ.get("PIM_AUTO_MAX_FLIPS", "1000"))
# Same idea for deletions: a presence set that lost a store (or a Woo-side
# sweep) must not empty the PIM unattended.
AUTO_MAX_REMOVALS = int(os.environ.get("PIM_AUTO_MAX_REMOVALS", "250"))
# A presence set smaller than this is treated as truncated: removals are
# skipped for the run (the full set is ~33k rows across ~100 stores).
PRESENCE_MIN_ROWS = int(os.environ.get("PIM_PRESENCE_MIN_ROWS", "20000"))
# Same guard for the FDM4 item master (rebuilt hourly by the extractor): a
# truncated read must not read as "FDM4 knows nothing". ~5.8k styles / ~57k UPCs.
FDM4_MIN_STYLES = int(os.environ.get("PIM_FDM4_MIN_STYLES", "1000"))
FDM4_MIN_UPCS = int(os.environ.get("PIM_FDM4_MIN_UPCS", "10000"))
# Orphan variants only ever come from product deletions (ours, or the PIM
# team's), so a big number in one hour is a backlog to clear by hand, not a
# signal to act on unattended.
AUTO_MAX_ORPHANS = int(os.environ.get("PIM_AUTO_MAX_ORPHANS", "2000"))
AUTO_ACTIONS = ("color_fill", "color_fix", "variant_create", "product_create",
                "product_publish", "variant_publish", "product_remove", "style_fill",
                "variant_orphan_remove")
FLIP_ACTIONS = ("product_publish", "variant_publish")


def api_key():
    key = (os.environ.get("PIM_API_KEY") or "").strip()
    if not key:
        print("push_pim: PIM_API_KEY is not set", file=sys.stderr)
        sys.exit(2)
    return key


_last_request_at = [0.0]


def _throttle():
    wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at[0])
    if wait > 0:
        time.sleep(wait)
    _last_request_at[0] = time.monotonic()


def api_call(method, path, key, body=None):
    url = API_BASE + path
    data = None
    headers = {"X-API-KEY": key}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    last = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            # 429 is transient (rolling-window rate limit shared with the
            # hourly pull cron): exponential backoff with jitter, honoring
            # Retry-After when the server sends one.
            if exc.code == 429:
                last = f"HTTP 429: {detail}"
                delay = min(60, 2 ** (attempt + 1)) + random.uniform(0, 1)
                retry_after = exc.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, min(int(retry_after), 120))
                time.sleep(delay)
                continue
            # Other 4xx are semantic, not transient - surface immediately.
            if 400 <= exc.code < 500:
                return exc.code, {"error": detail}
            last = f"HTTP {exc.code}: {detail}"
        except Exception as exc:  # noqa: BLE001 - network layer
            last = str(exc)
        time.sleep(2 * (attempt + 1))
    return 0, {"error": last or "request failed"}


# The API rejects collection GETs without $select; ask only for the fields
# the precondition checks read. The numeric *_id is required because item
# routes are addressed OData-style by internal id: /catalog/variants({id}).
GET_SELECT = {
    # frmt_sizelabel and frmt_variantname are read so a removal can compare the
    # live variant against the one the reviewer saw on the sheet.
    "variants": "frmt_id,frmt_ref,frmt_stat,frmt_colorcode,frmt_colorname,frmt_sizelabel,frmt_variantname",
    "products": "prod_id,prod_ref,prod_stat,prod_stylenumber",
}


def api_get_one(entity, ref_field, ref, key):
    query = urllib.parse.urlencode(
        {"$select": GET_SELECT[entity],
         "$filter": f"{ref_field} eq '{ref}'", "$top": "1"},
        quote_via=urllib.parse.quote)
    status, payload = api_call("GET", f"/catalog/{entity}?{query}", key)
    # This API answers a zero-match collection filter with 404, not an
    # empty list - that is the normal "does not exist yet" signal.
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"GET {entity} {ref}: {status} {payload}")
    rows = payload.get("value") or []
    return rows[0] if rows else None


def connect():
    connection = psycopg2.connect(dbname=DB_NAME, host=DB_SOCKET)
    connection.autocommit = False
    return connection


def fdm4_has_ref(cursor, ref):
    """True when FDM4 still carries this upc.

    A removal is staged because FDM4 no longer has the item; between the diff
    and the approved apply the item can come back, and then the premise of the
    approval is gone. Uses the apply loop's own cursor - a removal batch is
    capped at REMOVAL_CAP rows, so this is a handful of point lookups.
    """
    cursor.execute(
        'SELECT 1 FROM fdm4.item WHERE upper(btrim("upc-code")) = %s LIMIT 1',
        ((ref or "").strip().upper(),),
    )
    return cursor.fetchone() is not None


def product_live_now(cursor, prod_ref, style_code):
    """Is this PIM product live on a counting store right now?

    Reads pim.woo_presence directly (fresh, full set only) for the style
    number, the PIM reference, or any active variant SKU. Returns None when
    the set is missing, stale or truncated, so a removal refuses rather than
    guesses.
    """
    cursor.execute(
        """
        SELECT count(*) AS n,
               coalesce(max(refreshed_at) < now() - %(age)s * interval '1 hour', true) AS stale
          FROM pim.woo_presence WHERE env = %(env)s
        """,
        {"age": PRESENCE_MAX_AGE_HOURS, "env": PRESENCE_ENV})
    state = cursor.fetchone()
    if not state["n"] or state["stale"] or state["n"] < PRESENCE_MIN_ROWS:
        return None
    cursor.execute(
        """
        SELECT 1 FROM pim.woo_presence w
         WHERE w.env = %(env)s AND w.visible AND NOT (w.blog_id = ANY(%(all_blogs)s))
           AND (w.parent_sku = %(style)s OR w.parent_sku = %(ref)s
                OR w.upcs && ARRAY(SELECT upper(btrim(v.frmt_ref)) FROM pim.api_variant v
                                    WHERE v.prod_ref = %(ref)s AND v.retired_at IS NULL))
         LIMIT 1
        """,
        {"env": PRESENCE_ENV, "all_blogs": list(ALL_PRODUCTS_BLOGS),
         "style": (style_code or "").strip().upper(), "ref": (prod_ref or "").strip().upper()})
    return cursor.fetchone() is not None


_fdm4_master_ok = None


def product_known_to_fdm4(cursor, prod_ref, style_code):
    """Does FDM4 know this product by style number, reference, or any active
    variant UPC? Returns None when the item master looks truncated (checked
    once per process), so a removal refuses rather than guesses."""
    global _fdm4_master_ok
    if _fdm4_master_ok is None:
        cursor.execute(
            """
            SELECT (SELECT count(DISTINCT upper(btrim("style-code"))) FROM fdm4.item) AS styles,
                   (SELECT count(*) FROM fdm4.item WHERE btrim("upc-code") <> '') AS upcs
            """)
        fk = cursor.fetchone()
        _fdm4_master_ok = fk["styles"] >= FDM4_MIN_STYLES and fk["upcs"] >= FDM4_MIN_UPCS
    if not _fdm4_master_ok:
        return None
    params = {"style": (style_code or "").strip().upper(), "ref": (prod_ref or "").strip().upper()}
    cursor.execute(
        """
        SELECT 1 FROM fdm4.item i
         WHERE upper(btrim(i."style-code")) IN (%(style)s, %(ref)s)
            OR upper(btrim(i."upc-code")) = %(ref)s
            OR upper(btrim(i."upc-code")) IN (SELECT upper(btrim(v.frmt_ref)) FROM pim.api_variant v
                                               WHERE v.prod_ref = %(ref)s AND v.retired_at IS NULL)
         LIMIT 1
        """, params)
    if cursor.fetchone() is not None:
        return True
    cursor.execute(
        'SELECT 1 FROM fdm4.style WHERE upper(btrim("style-code")) IN (%(style)s, %(ref)s) LIMIT 1', params)
    return cursor.fetchone() is not None


def delete_product_variants(prod_ref, key):
    """Delete every live variant attached to a product, paging the live API.

    Sales Layer's product DELETE detaches variants instead of deleting them,
    so a product removal has to clear its variants first or it leaves
    orphans. Returns (deleted, failure_detail); failure_detail is None when
    every variant went.
    """
    deleted = 0
    while True:
        query = urllib.parse.urlencode(
            {"$select": "frmt_id,frmt_ref", "$filter": f"prod_ref eq '{prod_ref}'", "$top": "100"},
            quote_via=urllib.parse.quote)
        status, payload = api_call("GET", f"/catalog/variants?{query}", key)
        if status == 404:
            return deleted, None                      # no (more) variants
        if status != 200:
            return deleted, f"listing variants: {status} {payload}"
        rows = payload.get("value") or []
        if not rows:
            return deleted, None
        for row in rows:
            status, payload = api_call("DELETE", f"/catalog/variants({row['frmt_id']})", key)
            if not 200 <= status < 300 and status != 404:
                return deleted, f"variant {row.get('frmt_ref')}: {status} {payload}"
            deleted += 1
        if len(rows) < 100:
            return deleted, None


# --------------------------------------------------------------------------
# Diff engine

# Sellable universe straight from FDM4 (web-active items with a positive
# retail price and a UPC) - the same criteria the virtual catalog uses. The
# earlier source, woo.store_product_state, only covered products carried by
# Woo-synced stores and missed items added to non-synced FDM4 stores.
WAREHOUSE_VARIANTS_SQL = """
    SELECT upper(btrim(i."upc-code")) AS sku,
           max(upper(btrim(i."style-code")))            AS style_code,
           max(NULLIF(btrim(i."color-code"), ''))       AS color_code,
           max(NULLIF(btrim(sc.description), ''))       AS color_name,
           max(NULLIF(btrim(i."size-code"), ''))        AS size_code,
           max(NULLIF(btrim(ss.description), ''))       AS size_name,
           true                                         AS is_active,
           max(NULLIF(btrim(i."mill-code"), ''))        AS mill_code,
           max(NULLIF(btrim(m.description), ''))        AS brand,
           max(NULLIF(btrim(i."product-category"), '')) AS category,
           max(NULLIF(btrim(st.description), ''))       AS product_name
      FROM fdm4.item i
      LEFT JOIN fdm4."style-color" sc ON sc."style-code" = i."style-code" AND sc."color-code" = i."color-code"
      LEFT JOIN fdm4."style-size"  ss ON ss."style-code" = i."style-code" AND ss."size-code"  = i."size-code"
      LEFT JOIN fdm4.style st ON st."style-code" = i."style-code"
      LEFT JOIN fdm4.mill  m  ON btrim(m."mill-code") = btrim(i."mill-code")
     WHERE i."upc-code" IS NOT NULL AND btrim(i."upc-code") <> ''
       AND btrim(i."web-active") = 'True'
       AND btrim(i."retail-price") ~ '^[0-9]+(\\.[0-9]+)?$' AND i."retail-price"::numeric > 0
     GROUP BY 1
"""


def run_diff(note, remove_skus=None, arborwear_only=False):
    connection = connect()
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("CREATE TEMP TABLE wh AS " + WAREHOUSE_VARIANTS_SQL)
    cursor.execute("CREATE INDEX ON wh (sku)")
    cursor.execute("CREATE INDEX ON wh (style_code)")

    # Woo presence: the stores that count for visibility (see the module
    # docstring). A missing or stale set leaves live_blog empty, which skips
    # the publish/draft rules and creates new products as drafts.
    cursor.execute(
        """
        SELECT count(*) AS n, max(refreshed_at) AS fresh,
               coalesce(max(refreshed_at) < now() - %s * interval '1 hour', true) AS stale
          FROM pim.woo_presence WHERE env = %s
        """,
        (PRESENCE_MAX_AGE_HOURS, PRESENCE_ENV))
    presence = cursor.fetchone()
    presence_ok = bool(presence["n"]) and not presence["stale"]
    # Deletions need more than freshness: a set that lost most of its rows
    # (a partial write, a broken store loop) must not read as "nothing is live".
    presence_full = presence_ok and presence["n"] >= PRESENCE_MIN_ROWS
    cursor.execute(
        """
        CREATE TEMP TABLE live_blog AS
        SELECT blog_id, parent_sku, upcs
          FROM pim.woo_presence
         WHERE env = %(env)s AND visible AND NOT (blog_id = ANY(%(all_blogs)s)) AND %(ok)s
        """,
        {"env": PRESENCE_ENV, "all_blogs": list(ALL_PRODUCTS_BLOGS), "ok": presence_ok})
    cursor.execute("CREATE INDEX ON live_blog (parent_sku)")
    cursor.execute("CREATE INDEX ON live_blog USING gin (upcs)")
    # Styles a counting store carries, computed ONCE: by parent SKU, or by
    # any of the style's UPCs appearing among a store's published variations.
    # (Evaluating this per wh row inside the product_create query took 40
    # minutes and, inside the diff transaction, held fdm4.* read locks that
    # blocked the hourly FDM4 load - 2026-09-16.)
    cursor.execute(
        """
        CREATE TEMP TABLE live_style AS
        SELECT DISTINCT w.style_code FROM wh w
         WHERE EXISTS (SELECT 1 FROM live_blog l WHERE l.parent_sku = w.style_code)
        UNION
        SELECT DISTINCT w.style_code FROM live_blog l
          CROSS JOIN LATERAL unnest(l.upcs) AS u(sku)
          JOIN wh w ON w.sku = upper(u.sku)
        """)
    cursor.execute("CREATE INDEX ON live_style (style_code)")
    cursor.execute(
        """
        CREATE TEMP TABLE live_product AS
        SELECT p.prod_ref FROM pim.api_product p
         WHERE p.retired_at IS NULL
           AND EXISTS (SELECT 1 FROM live_blog l WHERE l.parent_sku = upper(btrim(p.style_number)))
        UNION
        SELECT p.prod_ref FROM pim.api_product p
         WHERE p.retired_at IS NULL
           AND EXISTS (SELECT 1 FROM live_blog l WHERE l.parent_sku = upper(btrim(p.prod_ref)))
        UNION
        SELECT v.prod_ref FROM pim.api_variant v
          JOIN pim.api_product p ON p.prod_ref = v.prod_ref AND p.retired_at IS NULL
         WHERE v.retired_at IS NULL
           AND EXISTS (SELECT 1 FROM live_blog l WHERE l.upcs @> ARRAY[upper(btrim(v.frmt_ref))])
        """)
    cursor.execute("SELECT count(*) AS n FROM live_product")
    live_n = cursor.fetchone()["n"]

    # FDM4 universe: every style code and UPC the item master knows, any
    # status. known_product = PIM products FDM4 knows by style number,
    # reference, or an active variant UPC. Guarded like presence: a truncated
    # item master (mid-refresh, failed pull) skips the FDM4-driven rules.
    cursor.execute(
        """
        CREATE TEMP TABLE f_style AS
        SELECT DISTINCT upper(btrim("style-code")) AS s FROM fdm4.item WHERE btrim("style-code") <> ''
        UNION SELECT DISTINCT upper(btrim("style-code")) FROM fdm4.style WHERE btrim("style-code") <> ''
        """)
    cursor.execute("CREATE INDEX ON f_style (s)")
    cursor.execute(
        """
        CREATE TEMP TABLE f_upc AS
        SELECT DISTINCT upper(btrim("upc-code")) AS u FROM fdm4.item WHERE btrim("upc-code") <> ''
        """)
    cursor.execute("CREATE INDEX ON f_upc (u)")
    cursor.execute(
        """
        CREATE TEMP TABLE f_upc_style AS
        SELECT DISTINCT upper(btrim("upc-code")) AS u, upper(btrim("style-code")) AS s FROM fdm4.item
         WHERE btrim("upc-code") <> '' AND btrim("style-code") <> ''
        """)
    cursor.execute("CREATE INDEX ON f_upc_style (u)")
    cursor.execute("SELECT (SELECT count(*) FROM f_style) AS styles, (SELECT count(*) FROM f_upc) AS upcs")
    fk = cursor.fetchone()
    fdm4_ok = fk["styles"] >= FDM4_MIN_STYLES and fk["upcs"] >= FDM4_MIN_UPCS
    cursor.execute(
        """
        CREATE TEMP TABLE known_product AS
        SELECT p.prod_ref FROM pim.api_product p
         WHERE p.retired_at IS NULL
           AND (upper(btrim(p.style_number)) IN (SELECT s FROM f_style)
                OR upper(btrim(p.prod_ref)) IN (SELECT s FROM f_style)
                OR upper(btrim(p.prod_ref)) IN (SELECT u FROM f_upc)
                OR EXISTS (SELECT 1 FROM pim.api_variant v JOIN f_upc f ON f.u = upper(btrim(v.frmt_ref))
                            WHERE v.prod_ref = p.prod_ref AND v.retired_at IS NULL))
        """)
    print(f"fdm4: {fk['styles']} styles, {fk['upcs']} UPCs"
          + ("" if fdm4_ok else " - looks truncated, not-in-FDM4 removals and style fills skipped this run"))
    if presence_ok:
        print(f"presence[{PRESENCE_ENV}]: {presence['n']} rows refreshed {presence['fresh']:%Y-%m-%d %H:%M}Z;"
              f" {live_n} PIM products live outside blogs {list(ALL_PRODUCTS_BLOGS)}"
              + ("" if presence_full else
                 f"; below PIM_PRESENCE_MIN_ROWS={PRESENCE_MIN_ROWS}, removals skipped this run"))
    else:
        print(f"presence[{PRESENCE_ENV}]: {'missing' if not presence['n'] else 'stale (' + str(presence['fresh']) + ')'}"
              " - create/publish/remove rules skipped this run")

    # Every fdm4.* read is done (the temp tables above survive the commit);
    # release those read locks now so the hourly FDM4 load can DROP/CREATE
    # its tables even if the diff below runs long.
    connection.commit()

    cursor.execute(
        "INSERT INTO pim.push_change_set (created_by, note) VALUES (%s, %s) RETURNING set_id",
        ("push_pim", note))
    set_id = cursor.fetchone()["set_id"]
    counts = {}

    # color_fill (lane a) and color_fix (lane a for zero padding, b otherwise)
    cursor.execute(
        """
        INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, frmt_ref, style_code, before, after)
        SELECT %(set_id)s,
               CASE WHEN pim_code IS NULL THEN 'a' ELSE 'b' END,
               CASE WHEN pim_code IS NULL THEN 'color_fill' ELSE 'color_fix' END,
               prod_ref, frmt_ref, style_code,
               jsonb_build_object('frmt_colorcode', pim_code, 'frmt_colorname', pim_name),
               jsonb_build_object('frmt_colorcode', wh_code, 'frmt_colorname', wh_name)
          FROM (
            SELECT v.frmt_ref, v.prod_ref,
                   w.style_code,
                   NULLIF(btrim(v.payload ->> 'frmt_colorcode'), '') AS pim_code,
                   NULLIF(btrim(v.payload ->> 'frmt_colorname'), '') AS pim_name,
                   w.color_code AS wh_code, w.color_name AS wh_name
              FROM pim.api_variant v
              JOIN wh w ON w.sku = upper(btrim(v.frmt_ref))
             WHERE w.color_code IS NOT NULL
          ) d
         -- The PIM stores codes numerically (leading zeros cannot survive),
         -- so compare pad-insensitively or every pushed code re-proposes
         -- forever. Name-only differences are deliberately NOT staged: the
         -- PIM team owns display names once a code is correct.
         WHERE pim_code IS NULL
            OR lpad(pim_code, 4, '0') IS DISTINCT FROM lpad(wh_code, 4, '0')
        """,
        {"set_id": set_id})

    # variant_remove (lane b): removals are NEVER auto-proposed. Only refs
    # from an explicitly supplied, approved sheet are staged, and each
    # is verified to be genuinely absent from FDM4 before it is included.
    # (A full auto-scan found ~7k FDM4-gone variants in the PIM - that
    # backlog belongs to a separate PIM-team review, not this push.)
    if remove_skus:
        cursor.execute(
            """
            INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, frmt_ref, before, after)
            SELECT %(set_id)s, 'b', 'variant_remove', v.prod_ref, v.frmt_ref,
                   jsonb_build_object(
                       'frmt_colorname', NULLIF(btrim(v.payload ->> 'frmt_colorname'), ''),
                       'frmt_sizelabel', NULLIF(btrim(v.payload ->> 'frmt_sizelabel'), '')),
                   NULL
              FROM pim.api_variant v
             WHERE upper(btrim(v.frmt_ref)) = ANY(%(skus)s)
               AND upper(btrim(v.frmt_ref)) NOT IN (SELECT sku FROM wh)
               AND upper(btrim(v.frmt_ref)) NOT IN (SELECT u FROM f_upc)
            """,
            {"set_id": set_id, "skus": remove_skus})

    # variant_create (lane b): FDM4 variants absent under an existing product.
    cursor.execute(
        """
        INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, frmt_ref, style_code, before, after)
        SELECT %(set_id)s, 'b', 'variant_create', p.prod_ref, w.sku, w.style_code, NULL,
               jsonb_build_object(
                   'prod_ref', p.prod_ref,
                   'frmt_ref', w.sku,
                   -- A variant takes its parent's visibility (input enum is
                   -- lowercase): visible under a visible or live product,
                   -- draft under a draft that presence cannot vouch for.
                   'frmt_stat', CASE WHEN p.payload ->> 'prod_stat' = 'V'
                                       OR p.prod_ref IN (SELECT prod_ref FROM live_product)
                                     THEN 'v' ELSE 'd' END,
                   'frmt_colorcode', w.color_code,
                   'frmt_colorname', w.color_name,
                   'frmt_sizecode', w.size_code,
                   'frmt_sizelabel', w.size_name,
                   -- Display name: "<product title> <color name> <size label>",
                   -- matching the legacy Woo-import variants. Prefer the PIM's
                   -- own product title so variants read consistently with the
                   -- product they hang under.
                   'frmt_variantname', NULLIF(btrim(concat_ws(' ',
                       COALESCE(NULLIF(btrim(p.payload ->> 'prod_title'), ''),
                                NULLIF(btrim(p.payload ->> 'name'), ''),
                                w.product_name, w.style_code),
                       w.color_name, w.size_name)), ''))
          FROM wh w
          JOIN pim.api_product p ON upper(btrim(p.style_number)) = w.style_code
         WHERE w.is_active
           -- Retired mirror rows are records of what the PIM no longer has;
           -- counting them here would suppress a legitimate re-create.
           AND w.sku NOT IN (SELECT upper(btrim(frmt_ref)) FROM pim.api_variant
                              WHERE retired_at IS NULL)
           -- With a fresh presence set, products live nowhere are on their
           -- way out of the PIM (product_remove below); do not grow them.
           AND (NOT %(ok)s OR p.prod_ref IN (SELECT prod_ref FROM live_product))
        """,
        {"set_id": set_id, "ok": presence_ok})

    # product_create (lane b): active store-carried styles absent from the
    # PIM, with content backfilled from the Woo mirror (lowest blog with
    # content). The PIM manages every brand; --arborwear-only restores the
    # original mill-22-only backfill scope.
    mill_clause = "w.mill_code = '22' AND " if arborwear_only else ""
    cursor.execute(
        f"""
        WITH missing AS (
            SELECT w.style_code,
                   max(w.brand) AS brand,
                   max(w.category) AS category,
                   max(w.product_name) AS wh_name,
                   count(*) AS variants
              FROM wh w
             WHERE {mill_clause} w.is_active
               AND w.style_code NOT IN
                   (SELECT upper(btrim(style_number)) FROM pim.api_product
                     WHERE btrim(style_number) <> '' AND retired_at IS NULL)
               AND w.sku NOT IN (SELECT upper(btrim(frmt_ref)) FROM pim.api_variant
                                  WHERE retired_at IS NULL)
               -- Only styles a counting store already carries (live_style:
               -- by style number or by one of the style's SKUs). It is empty
               -- when the presence set is missing or stale, so nothing is
               -- created then.
               AND w.style_code IN (SELECT style_code FROM live_style)
             GROUP BY 1
        ), content AS (
            SELECT DISTINCT ON (upper(btrim(sku_parent)))
                   upper(btrim(sku_parent)) AS style_code,
                   NULLIF(btrim(name), '') AS name,
                   NULLIF(btrim(description), '') AS description,
                   NULLIF(btrim(short_description), '') AS short_description
              FROM pim.product_state
             WHERE NULLIF(btrim(name), '') IS NOT NULL
             -- Prefer the row that actually carries a description, then the
             -- lowest blog, matching the dedupe convention.
             ORDER BY upper(btrim(sku_parent)),
                      (NULLIF(btrim(description), '') IS NULL), blog_id
        )
        INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, style_code, before, after)
        SELECT %(set_id)s, 'b', 'product_create', m.style_code, m.style_code, NULL,
               jsonb_build_object(
                   'prod_ref', m.style_code,
                   'prod_stylenumber', m.style_code,
                   -- input enum is lowercase v/i/d/r; every created product
                   -- is live by construction (see the WHERE above)
                   'prod_stat', 'v',
                   'prod_brand', m.brand,
                   'prod_title', COALESCE(c.name, m.wh_name, m.style_code),
                   'prod_description', c.description,
                   'prod_shortdescription', c.short_description,
                   'content_source', CASE WHEN c.name IS NOT NULL THEN 'woo_mirror' ELSE 'fdm4_basics' END,
                   'variants', m.variants)
          FROM missing m
          LEFT JOIN content c ON c.style_code = m.style_code
        """,
        {"set_id": set_id})

    # Visibility (lane b): drafts that are live in Woo become visible, and so
    # do draft variants under live products. The mirror status is the premise;
    # the apply step re-reads the live status and skips rows that no longer
    # hold. Skipped whole when the presence set is missing or stale.
    if presence_ok:
        cursor.execute(
            """
            INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, style_code, before, after)
            SELECT %(set_id)s, 'b', 'product_publish', p.prod_ref, upper(btrim(p.style_number)),
                   jsonb_build_object('prod_stat', 'D'),
                   jsonb_build_object('prod_stat', 'v')
              FROM pim.api_product p
             WHERE p.retired_at IS NULL
               AND p.payload ->> 'prod_stat' = 'D'
               AND p.prod_ref IN (SELECT prod_ref FROM live_product)
            """,
            {"set_id": set_id})
        # Only drafts flip: a variant the PIM team set invisible ('I') is a
        # deliberate choice and is left alone.
        cursor.execute(
            """
            INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, frmt_ref, style_code, before, after)
            SELECT %(set_id)s, 'b', 'variant_publish', v.prod_ref, v.frmt_ref, upper(btrim(p.style_number)),
                   jsonb_build_object('frmt_stat', 'D'),
                   jsonb_build_object('frmt_stat', 'v')
              FROM pim.api_variant v
              JOIN pim.api_product p ON p.prod_ref = v.prod_ref AND p.retired_at IS NULL
             WHERE v.retired_at IS NULL
               AND v.payload ->> 'frmt_stat' = 'D'
               AND v.prod_ref IN (SELECT prod_ref FROM live_product)
            """,
            {"set_id": set_id})

    # Removal (lane b): a product leaves the PIM, variants included (the API
    # deletes them with the product), when it is live on no counting store
    # (needs a fresh AND full presence set) or when FDM4 does not know it at
    # all (needs a full item master). The reason rides on the row; the apply
    # step re-checks that same premise right before the delete.
    if presence_full or fdm4_ok:
        cursor.execute(
            """
            INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, style_code, before, after)
            SELECT %(set_id)s, 'b', 'product_remove', p.prod_ref, upper(btrim(p.style_number)),
                   jsonb_build_object(
                       'prod_id', (p.payload ->> 'prod_id')::bigint,
                       'prod_stat', p.payload ->> 'prod_stat',
                       'prod_title', NULLIF(btrim(p.payload ->> 'prod_title'), ''),
                       'variants', (SELECT count(*) FROM pim.api_variant v
                                     WHERE v.prod_ref = p.prod_ref AND v.retired_at IS NULL),
                       'reason', CASE WHEN %(fdm4_ok)s AND p.prod_ref NOT IN (SELECT prod_ref FROM known_product)
                                      THEN 'not in FDM4' ELSE 'not live in Woo' END),
                   NULL
              FROM pim.api_product p
             WHERE p.retired_at IS NULL
               AND ((%(presence_full)s AND p.prod_ref NOT IN (SELECT prod_ref FROM live_product))
                    OR (%(fdm4_ok)s AND p.prod_ref NOT IN (SELECT prod_ref FROM known_product)))
            """,
            {"set_id": set_id, "presence_full": presence_full, "fdm4_ok": fdm4_ok})

    # style_fill (lane b): a product with no style number gets FDM4's style
    # code when its reference is one, or when its reference / an active
    # variant UPC maps to exactly one FDM4 style. Products on their way out
    # (live nowhere) are not worth filling.
    if fdm4_ok:
        cursor.execute(
            """
            WITH bare AS (
              SELECT p.prod_ref FROM pim.api_product p
               WHERE p.retired_at IS NULL AND coalesce(btrim(p.style_number), '') = ''
                 AND (NOT %(presence_full)s OR p.prod_ref IN (SELECT prod_ref FROM live_product))
            ), cand AS (
              SELECT b.prod_ref, upper(btrim(b.prod_ref)) AS style FROM bare b
               WHERE upper(btrim(b.prod_ref)) IN (SELECT s FROM f_style)
              UNION
              SELECT b.prod_ref, f.s FROM bare b
                JOIN f_upc_style f ON f.u = upper(btrim(b.prod_ref))
              UNION
              SELECT b.prod_ref, f.s FROM bare b
                JOIN pim.api_variant v ON v.prod_ref = b.prod_ref AND v.retired_at IS NULL
                JOIN f_upc_style f ON f.u = upper(btrim(v.frmt_ref))
            ), one AS (
              SELECT prod_ref, min(style) AS style FROM cand
               WHERE coalesce(style, '') <> '' GROUP BY prod_ref HAVING count(DISTINCT style) = 1
            )
            INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, style_code, before, after)
            SELECT %(set_id)s, 'b', 'style_fill', o.prod_ref, o.style,
                   jsonb_build_object('prod_stylenumber', NULL),
                   jsonb_build_object('prod_stylenumber', o.style)
              FROM one o
            """,
            {"set_id": set_id, "presence_full": presence_full})

    # variant_orphan_remove (lane b): variants the PIM holds with no product
    # reference. They are what a product deletion leaves behind (see the
    # module docstring); nothing can sell or enrich them, so they go.
    cursor.execute(
        """
        INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, frmt_ref, style_code, before, after)
        SELECT %(set_id)s, 'b', 'variant_orphan_remove', '', v.frmt_ref, '',
               jsonb_build_object(
                   'frmt_id', (v.payload ->> 'frmt_id')::bigint,
                   'frmt_stat', v.payload ->> 'frmt_stat',
                   'frmt_variantname', NULLIF(btrim(v.payload ->> 'frmt_variantname'), '')),
               NULL
          FROM pim.api_variant v
         WHERE v.retired_at IS NULL
           AND coalesce(btrim(v.prod_ref), '') = ''
        """,
        {"set_id": set_id})

    # variants for the created products ride as variant_create rows keyed to
    # the future prod_ref (the style number).
    cursor.execute(
        """
        INSERT INTO pim.push_change_row (set_id, lane, action, prod_ref, frmt_ref, style_code, before, after)
        SELECT %(set_id)s, 'b', 'variant_create', r.prod_ref, w.sku, w.style_code, NULL,
               jsonb_build_object(
                   'prod_ref', r.prod_ref,
                   'frmt_ref', w.sku,
                   'frmt_stat', 'v',  -- the product is created visible
                   'frmt_colorcode', w.color_code,
                   'frmt_colorname', w.color_name,
                   'frmt_sizecode', w.size_code,
                   'frmt_sizelabel', w.size_name,
                   -- Display name (see the note on the existing-product
                   -- variant_create above); here the product is being created
                   -- in the same set, so its title comes from that change row.
                   'frmt_variantname', NULLIF(btrim(concat_ws(' ',
                       COALESCE(NULLIF(btrim(r.after ->> 'prod_title'), ''),
                                w.product_name, w.style_code),
                       w.color_name, w.size_name)), ''))
          FROM pim.push_change_row r
          JOIN wh w ON w.style_code = r.style_code AND w.is_active
         WHERE r.set_id = %(set_id)s AND r.action = 'product_create'
        """,
        {"set_id": set_id})

    cursor.execute(
        "SELECT action, lane, count(*) AS n FROM pim.push_change_row WHERE set_id = %s GROUP BY 1, 2 ORDER BY 1, 2",
        (set_id,))
    for row in cursor.fetchall():
        counts[f"{row['action']}/{row['lane']}"] = row["n"]
    connection.commit()
    return set_id, counts


# --------------------------------------------------------------------------
# Reporting / approval

def latest_set(cursor):
    cursor.execute("SELECT set_id FROM pim.push_change_set ORDER BY set_id DESC LIMIT 1")
    row = cursor.fetchone()
    return row["set_id"] if row else None


def print_summary(set_id):
    connection = connect()
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if set_id is None:
        set_id = latest_set(cursor)
    if set_id is None:
        print("no change sets")
        return
    cursor.execute("SELECT * FROM pim.push_change_set WHERE set_id = %s", (set_id,))
    header = cursor.fetchone()
    print(f"change set {set_id}  status={header['status']}  created={header['created_at']}  {header['note']}")
    print(f"  rule: live on a store outside blogs {list(ALL_PRODUCTS_BLOGS)} -> in the PIM and visible, else removed"
          f" (presence env {PRESENCE_ENV}, max age {PRESENCE_MAX_AGE_HOURS:g}h)")
    cursor.execute(
        """
        SELECT action, lane, status, count(*) AS n
          FROM pim.push_change_row WHERE set_id = %s
         GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
        """,
        (set_id,))
    for row in cursor.fetchall():
        print(f"  {row['action']:<15} lane {row['lane']}  {row['status']:<9} {row['n']}")
    for action in ACTIONS:
        cursor.execute(
            "SELECT prod_ref, frmt_ref, before, after FROM pim.push_change_row"
            " WHERE set_id = %s AND action = %s ORDER BY row_id LIMIT 3",
            (set_id, action))
        rows = cursor.fetchall()
        if rows:
            print(f"  -- {action} samples:")
            for row in rows:
                print(f"     {row['prod_ref']} {row['frmt_ref']} "
                      f"{json.dumps(row['before'])} -> {json.dumps(row['after'])}")


def approve(set_id, lane, action):
    connection = connect()
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    clauses = ["set_id = %s", "status = 'proposed'"]
    params = [set_id]
    if lane:
        clauses.append("lane = %s")
        params.append(lane)
    if action:
        clauses.append("action = %s")
        params.append(action)
    cursor.execute(
        f"UPDATE pim.push_change_row SET status = 'approved' WHERE {' AND '.join(clauses)}",
        params)
    n = cursor.rowcount
    cursor.execute("UPDATE pim.push_change_set SET status = 'approved' WHERE set_id = %s", (set_id,))
    connection.commit()
    print(f"approved {n} rows in set {set_id}")


# --------------------------------------------------------------------------
# Apply engine

def apply_set(set_id, limit, allow_removals, auto=False):
    enabled = os.environ.get("PIM_PUSH_ENABLED") == "1"
    key = api_key()
    connection = connect()
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Products first so their variants can attach.
    cursor.execute(
        """
        SELECT * FROM pim.push_change_row
         WHERE set_id = %s AND status = 'approved'
         ORDER BY CASE action WHEN 'product_create' THEN 0 ELSE 1 END, row_id
         LIMIT %s
        """,
        (set_id, limit))
    rows = cursor.fetchall()
    if not rows:
        print("nothing approved to apply")
        return 0, 0, 0
    # REMOVAL_CAP is the largest removal batch that can reasonably be reviewed.
    # A set that carries more of them is refused whole rather than half-applied.
    removals = sum(1 for row in rows if row["action"] == "variant_remove")
    if allow_removals and removals > REMOVAL_CAP:
        print(f"refusing set {set_id}: {removals} variant removals exceed REMOVAL_CAP"
              f" ({REMOVAL_CAP}); apply it in smaller batches with --limit")
        return
    if not enabled:
        print(f"DRY RUN (PIM_PUSH_ENABLED not set): would apply {len(rows)} rows")
    done = failed = skipped = 0
    for row in rows:
        # One bad row must not abort a multi-hour run.
        try:
            outcome, detail = apply_row(row, key, enabled, allow_removals, cursor, auto)
        except Exception as exc:  # noqa: BLE001 - keep the batch moving
            outcome, detail = "failed", f"exception: {exc}"
        if enabled:
            cursor.execute(
                "UPDATE pim.push_change_row SET status = %s, result = %s,"
                " applied_at = CASE WHEN %s = 'applied' THEN now() END WHERE row_id = %s",
                (outcome, detail[:500], outcome, row["row_id"]))
            connection.commit()
        if outcome == "applied":
            done += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1
        print(f"  [{outcome}] {row['action']} {row['prod_ref']} {row['frmt_ref']} {detail}")
    print(f"apply finished: {done} applied, {skipped} skipped, {failed} failed"
          + ("" if enabled else " (dry run, nothing sent)"))
    return done, skipped, failed


_parent_id_cache = {}


def apply_row(row, key, enabled, allow_removals, cursor, auto=False):
    action = row["action"]
    after = row["after"] or {}
    if action in ("color_fill", "color_fix"):
        current = api_get_one("variants", "frmt_ref", row["frmt_ref"], key)
        if current is None:
            return "skipped", "variant no longer in the PIM"
        # The PIM stores frmt_colorcode as a number, so live values come back
        # as ints, 0 is a legitimate placeholder code, and zero-padded strings
        # we send are stored stripped.
        live_raw = current.get("frmt_colorcode")
        before_raw = (row["before"] or {}).get("frmt_colorcode")
        live = "" if live_raw is None else str(live_raw).strip()
        expect = "" if before_raw is None else str(before_raw).strip()
        if live != expect:
            return "skipped", f"color code changed since diff ({live!r})"
        if not enabled:
            return "skipped", "dry run"
        body = {"frmt_colorcode": after.get("frmt_colorcode"),
                "frmt_colorname": after.get("frmt_colorname")}
        status, payload = api_call("PATCH", f"/catalog/variants({current['frmt_id']})", key, body)
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "variant_remove":
        if not allow_removals:
            return "skipped", "removals require --allow-removals"
        current = api_get_one("variants", "frmt_ref", row["frmt_ref"], key)
        if current is None:
            return "skipped", "already gone"
        # The staged premise was "FDM4 no longer has this sku"; recheck it,
        # and recheck that the live variant is still the one on the sheet.
        if fdm4_has_ref(cursor, row["frmt_ref"]):
            return "skipped", "back in FDM4 since diff"
        before = row["before"] or {}
        for field in ("frmt_colorname", "frmt_sizelabel"):
            staged = str(before.get(field) or "").strip()
            live = str(current.get(field) or "").strip()
            if staged != live:
                return "skipped", f"{field} changed since diff ({live!r})"
        if not enabled:
            return "skipped", "dry run"
        status, payload = api_call("DELETE", f"/catalog/variants({current['frmt_id']})", key)
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "variant_create":
        current = api_get_one("variants", "frmt_ref", row["frmt_ref"], key)
        if current is not None:
            return "skipped", "variant already exists"
        # POST /catalog/variants links to the parent by numeric prod_id.
        # Products apply before variants, so parents exist by now; cache the
        # id per ref - thousands of variants share a few hundred parents.
        prod_id = _parent_id_cache.get(row["prod_ref"])
        if prod_id is None:
            parent = api_get_one("products", "prod_ref", row["prod_ref"], key)
            if parent is None:
                return "skipped", "parent product not in the PIM"
            prod_id = parent["prod_id"]
            _parent_id_cache[row["prod_ref"]] = prod_id
        if not enabled:
            return "skipped", "dry run"
        body = {k: v for k, v in after.items()
                if k.startswith("frmt_") and v is not None}
        body["prod_id"] = prod_id
        status, payload = api_call("POST", "/catalog/variants", key, body)
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "product_create":
        current = api_get_one("products", "prod_ref", row["prod_ref"], key)
        if current is not None:
            return "skipped", "product already exists"
        if not enabled:
            return "skipped", "dry run"
        body = {k: v for k, v in after.items()
                if k.startswith("prod_") and v is not None}
        status, payload = api_call("POST", "/catalog/products", key, body)
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "product_publish":
        current = api_get_one("products", "prod_ref", row["prod_ref"], key)
        if current is None:
            return "skipped", "product no longer in the PIM"
        live = str(current.get("prod_stat") or "").strip().upper()
        if live != "D":
            return "skipped", f"status is {live or 'unset'}, not a draft"
        if not enabled:
            return "skipped", "dry run"
        status, payload = api_call("PATCH", f"/catalog/products({current['prod_id']})", key,
                                   {"prod_stat": "v"})
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "variant_publish":
        current = api_get_one("variants", "frmt_ref", row["frmt_ref"], key)
        if current is None:
            return "skipped", "variant no longer in the PIM"
        live = str(current.get("frmt_stat") or "").strip().upper()
        if live != "D":
            return "skipped", f"status is {live or 'unset'}, not a draft"
        if not enabled:
            return "skipped", "dry run"
        status, payload = api_call("PATCH", f"/catalog/variants({current['frmt_id']})", key,
                                   {"frmt_stat": "v"})
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "style_fill":
        current = api_get_one("products", "prod_ref", row["prod_ref"], key)
        if current is None:
            return "skipped", "product no longer in the PIM"
        have = str(current.get("prod_stylenumber") or "").strip()
        if have:
            return "skipped", f"style number already set ({have})"
        if not enabled:
            return "skipped", "dry run"
        status, payload = api_call("PATCH", f"/catalog/products({current['prod_id']})", key,
                                   {"prod_stylenumber": after.get("prod_stylenumber")})
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "variant_orphan_remove":
        if not (auto or allow_removals):
            return "skipped", "variant removals require --allow-removals"
        # One live read (GET_SELECT for variants does not carry prod_ref): a
        # variant someone re-attached to a product since the diff is left alone.
        query = urllib.parse.urlencode(
            {"$select": "frmt_id,frmt_ref,prod_ref", "$filter": f"frmt_ref eq '{row['frmt_ref']}'", "$top": "1"},
            quote_via=urllib.parse.quote)
        status, payload = api_call("GET", f"/catalog/variants?{query}", key)
        if status == 404:
            return "skipped", "already gone"
        if status != 200:
            return "failed", f"GET {status} {payload}"
        rows_ = payload.get("value") or []
        if not rows_:
            return "skipped", "already gone"
        live = rows_[0]
        if str(live.get("prod_ref") or "").strip():
            return "skipped", f"re-attached to product {live['prod_ref']} since diff"
        if not enabled:
            return "skipped", "dry run"
        status, payload = api_call("DELETE", f"/catalog/variants({live['frmt_id']})", key)
        if not 200 <= status < 300:
            return "failed", f"{status} {payload}"
        cursor.execute(
            "UPDATE pim.api_variant SET retired_at = now() WHERE frmt_ref = %s AND retired_at IS NULL",
            (row["frmt_ref"],))
        return "applied", ""
    if action == "product_remove":
        # The hourly run deletes on its own (within PIM_AUTO_MAX_REMOVALS);
        # a hand-applied set has to say so explicitly.
        if not (auto or allow_removals):
            return "skipped", "product removals require --allow-removals"
        current = api_get_one("products", "prod_ref", row["prod_ref"], key)
        if current is None:
            return "skipped", "already gone"
        # Recheck the staged premise as things stand now, and refuse to act
        # on a missing, stale or truncated source. "not in FDM4" is rechecked
        # against the item master; "not live in Woo" against the presence set
        # (a product that went live since the diff stays).
        reason = (row["before"] or {}).get("reason") or "not live in Woo"
        if reason == "not in FDM4":
            known = product_known_to_fdm4(cursor, row["prod_ref"], row["style_code"])
            if known is None:
                return "skipped", "FDM4 item master looks truncated at apply time"
            if known:
                return "skipped", "back in FDM4 since diff"
        else:
            live = product_live_now(cursor, row["prod_ref"], row["style_code"])
            if live is None:
                return "skipped", "presence set missing, stale or truncated at apply time"
            if live:
                return "skipped", "went live in Woo since diff"
        if not enabled:
            return "skipped", "dry run"
        # Variants first: the API detaches them on product delete instead of
        # removing them. If any variant refuses to go, the product stays so
        # nothing is left half-removed.
        gone, problem = delete_product_variants(row["prod_ref"], key)
        if problem:
            return "failed", f"after deleting {gone} variant(s): {problem}"
        status, payload = api_call("DELETE", f"/catalog/products({current['prod_id']})", key)
        if not 200 <= status < 300:
            return "failed", f"{status} {payload} ({gone} variant(s) already deleted)"
        # Retire the mirror rows now: the incremental pull never sees a
        # deletion (only the weekly --full reconciles), and until then the
        # diff would keep re-proposing this product every hour.
        cursor.execute(
            "UPDATE pim.api_product SET retired_at = now() WHERE prod_ref = %s AND retired_at IS NULL",
            (row["prod_ref"],))
        cursor.execute(
            "UPDATE pim.api_variant SET retired_at = now() WHERE prod_ref = %s AND retired_at IS NULL",
            (row["prod_ref"],))
        return "applied", ""
    return "failed", f"unknown action {action}"


# --------------------------------------------------------------------------
# Automatic run

def run_auto():
    """Diff, then apply every row except variant removals. See the module docstring."""
    enabled = os.environ.get("PIM_PUSH_ENABLED") == "1"
    connection = connect()
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Close out proposals left behind by earlier sets: the fresh diff below
    # re-proposes whatever still applies, so nothing stays "pending" by accident.
    # Approved-but-unsent rows count too (a dry run leaves them behind), except
    # removals, which are only ever approved by hand and applied right after.
    cursor.execute(
        "UPDATE pim.push_change_row SET status = 'rejected', result = 'superseded by automatic run'"
        " WHERE status = 'proposed'"
        "    OR (status = 'approved' AND action NOT IN ('variant_remove', 'product_remove', 'variant_orphan_remove'))")
    superseded = cursor.rowcount
    cursor.execute(
        """
        UPDATE pim.push_change_set s
           SET status = CASE WHEN EXISTS (SELECT 1 FROM pim.push_change_row r
                                           WHERE r.set_id = s.set_id AND r.status = 'applied')
                             THEN 'done' ELSE 'cancelled' END
         WHERE s.status NOT IN ('done', 'cancelled')
           AND NOT EXISTS (SELECT 1 FROM pim.push_change_row r
                            WHERE r.set_id = s.set_id AND r.status IN ('proposed', 'approved'))
        """)
    connection.commit()

    set_id, counts = run_diff("automatic run")
    cursor.execute(
        "SELECT count(*) FILTER (WHERE action = ANY(%s)) AS flips,"
        "       count(*) FILTER (WHERE action = 'product_remove') AS removals,"
        "       count(*) FILTER (WHERE action = 'variant_orphan_remove') AS orphans"
        "  FROM pim.push_change_row WHERE set_id = %s",
        (list(FLIP_ACTIONS), set_id))
    brakes = cursor.fetchone()
    actions = list(AUTO_ACTIONS)
    if brakes["orphans"] > AUTO_MAX_ORPHANS:
        print(f"auto: {brakes['orphans']} orphan variants exceed PIM_AUTO_MAX_ORPHANS={AUTO_MAX_ORPHANS};"
              " leaving them proposed this run (apply by hand with --allow-removals)")
        actions = [a for a in actions if a != "variant_orphan_remove"]
    if brakes["flips"] > AUTO_MAX_FLIPS:
        print(f"auto: {brakes['flips']} visibility flips exceed PIM_AUTO_MAX_FLIPS={AUTO_MAX_FLIPS};"
              " leaving them proposed this run")
        actions = [a for a in actions if a not in FLIP_ACTIONS]
    if brakes["removals"] > AUTO_MAX_REMOVALS:
        print(f"auto: {brakes['removals']} product removals exceed PIM_AUTO_MAX_REMOVALS={AUTO_MAX_REMOVALS};"
              " leaving them proposed this run (apply by hand with --allow-removals)")
        actions = [a for a in actions if a != "product_remove"]
    cursor.execute(
        "UPDATE pim.push_change_row SET status = 'approved'"
        " WHERE set_id = %s AND status = 'proposed' AND action = ANY(%s)",
        (set_id, actions))
    approved = cursor.rowcount
    cursor.execute("UPDATE pim.push_change_set SET status = %s WHERE set_id = %s",
                   ("approved" if approved else "done", set_id))
    connection.commit()
    if not approved:
        print(f"auto: set {set_id}: nothing to send ({superseded} stale proposal(s) closed)")
        return

    result = apply_set(set_id, 1000000, False, auto=True)
    done, skipped, failed = result if result else (0, 0, 0)
    cursor.execute("SELECT count(*) AS n FROM pim.push_change_row WHERE set_id = %s AND status IN ('approved', 'proposed')",
                   (set_id,))
    left = cursor.fetchone()["n"]
    cursor.execute("UPDATE pim.push_change_set SET status = %s WHERE set_id = %s",
                   ("done" if left == 0 else "approved", set_id))
    connection.commit()
    print(f"auto: set {set_id}: {approved} approved, {done} applied, {skipped} skipped, {failed} failed,"
          f" {left} left for next run, {superseded} stale proposal(s) closed"
          + ("" if enabled else " (DRY RUN, nothing sent)"))


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auto", action="store_true",
                        help="scheduled run: diff, then apply everything except removals")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--set", type=int, default=None)
    parser.add_argument("--lane", choices=["a", "b"], default=None)
    parser.add_argument("--action", choices=list(ACTIONS), default=None)
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--allow-removals", action="store_true")
    parser.add_argument("--note", default="")
    parser.add_argument("--remove-skus", default=None,
                        help="path to a text/CSV file whose rows contain approved variant refs to remove")
    parser.add_argument("--arborwear-only", action="store_true",
                        help="limit product creation to Arborwear (mill 22) styles; default creates every brand")
    args = parser.parse_args(argv)

    if args.auto:
        run_auto()
    elif args.diff:
        remove_skus = None
        if args.remove_skus:
            with open(args.remove_skus) as fh:
                remove_skus = sorted({
                    token.strip().upper()
                    for line in fh
                    for token in line.replace(",", " ").split()
                    if token.strip() and any(ch.isdigit() for ch in token)
                    and len(token.strip()) >= 10
                })
            print(f"removal sheet: {len(remove_skus)} refs")
        set_id, counts = run_diff(args.note or "diff run", remove_skus, args.arborwear_only)
        print(f"change set {set_id} written:")
        for k, v in sorted(counts.items()):
            print(f"  {k}: {v}")
        print_summary(set_id)
    elif args.summary:
        print_summary(args.set)
    elif args.approve:
        if not args.set:
            parser.error("--approve requires --set")
        approve(args.set, args.lane, args.action)
    elif args.apply:
        if not args.set:
            parser.error("--apply requires --set")
        apply_set(args.set, args.limit, args.allow_removals)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
