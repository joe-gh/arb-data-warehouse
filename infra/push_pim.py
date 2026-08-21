#!/usr/bin/env python3
"""PIM (Sales Layer api2) push service: warehouse -> PIM.

Fixes the PIM where the warehouse knows better and fills what it is missing,
per the approved August 2026 review sheets:

  color_fill      lane a  variant has no color code; FDM4 knows it
  color_fix       lane a/b  variant color code differs from FDM4
                  (a = pure zero padding, b = real divergence)
  variant_remove  lane b  ONLY refs from an explicit --remove-skus file
                  (the human-approved removal sheet); the diff never
                  auto-proposes deletions
  variant_create  lane b  FDM4 variant missing under an existing PIM product
  product_create  lane b  Arborwear style absent from the PIM; created hidden
                  (prod_stat D) with content backfilled from the Woo mirror

Stages:
  --diff                 compute a new change set (read-only against the PIM)
  --summary [--set N]    print a change set summary with samples
  --approve --set N [--lane X] [--action Y]   mark rows approved
  --apply --set N [--limit N] [--allow-removals]
                         execute approved rows; requires PIM_PUSH_ENABLED=1,
                         otherwise prints what it would do

Safety: the apply engine re-reads every target first and skips rows whose
precondition no longer holds; it never deletes products; variant deletes
additionally require --allow-removals. Every row records its outcome.

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
# the hourly pull cron also shares. Pace requests well under that.
MIN_REQUEST_INTERVAL = 0.25
REMOVAL_CAP = 200
DB_NAME = "arb_warehouse"
DB_SOCKET = "/var/run/postgresql"

ACTIONS = ("color_fill", "color_fix", "variant_remove", "variant_create", "product_create")


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
    "variants": "frmt_id,frmt_ref,frmt_colorcode,frmt_colorname",
    "products": "prod_id,prod_ref,prod_stat",
}


def api_get_one(entity, ref_field, ref, key):
    query = urllib.parse.urlencode(
        {"$select": GET_SELECT[entity],
         "$filter": f"{ref_field} eq '{ref}'", "$top": "1"},
        quote_via=urllib.parse.quote)
    status, payload = api_call("GET", f"/catalog/{entity}?{query}", key)
    if status != 200:
        raise RuntimeError(f"GET {entity} {ref}: {status} {payload}")
    rows = payload.get("value") or []
    return rows[0] if rows else None


def connect():
    connection = psycopg2.connect(dbname=DB_NAME, host=DB_SOCKET)
    connection.autocommit = False
    return connection


# --------------------------------------------------------------------------
# Diff engine

WAREHOUSE_VARIANTS_SQL = """
    SELECT upper(btrim(sku)) AS sku,
           max(upper(btrim(style_code))) AS style_code,
           max(NULLIF(btrim(color_code), '')) AS color_code,
           max(NULLIF(btrim(color), ''))      AS color_name,
           max(NULLIF(btrim(size_code), ''))  AS size_code,
           max(NULLIF(btrim(size), ''))       AS size_name,
           bool_or(is_active)                 AS is_active,
           max(NULLIF(btrim(mill_code), ''))  AS mill_code,
           max(NULLIF(btrim(brand), ''))      AS brand,
           max(NULLIF(btrim(category), ''))   AS category,
           max(NULLIF(btrim(name), ''))       AS product_name
      FROM woo.store_product_state
     WHERE kind = 'variation' AND NULLIF(btrim(sku), '') IS NOT NULL
     GROUP BY 1
"""


def run_diff(note, remove_skus=None):
    connection = connect()
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute("CREATE TEMP TABLE wh AS " + WAREHOUSE_VARIANTS_SQL)
    cursor.execute("CREATE INDEX ON wh (sku)")
    cursor.execute("CREATE INDEX ON wh (style_code)")

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
               CASE WHEN pim_code IS NULL OR pim_code = '' THEN 'a'
                    WHEN lpad(pim_code, 4, '0') = wh_code THEN 'a'
                    ELSE 'b' END,
               CASE WHEN pim_code IS NULL OR pim_code = '' THEN 'color_fill' ELSE 'color_fix' END,
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
         WHERE pim_code IS DISTINCT FROM wh_code
        """,
        {"set_id": set_id})

    # variant_remove (lane b): removals are NEVER auto-proposed. Only refs
    # from an explicitly supplied, human-approved sheet are staged, and each
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
               AND NOT EXISTS (SELECT 1 FROM fdm4.item i
                                WHERE upper(btrim(i."upc-code")) = upper(btrim(v.frmt_ref)))
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
                   'frmt_colorcode', w.color_code,
                   'frmt_colorname', w.color_name,
                   'frmt_sizecode', w.size_code,
                   'frmt_sizelabel', w.size_name)
          FROM wh w
          JOIN pim.api_product p ON upper(btrim(p.style_number)) = w.style_code
         WHERE w.is_active
           AND w.sku NOT IN (SELECT upper(btrim(frmt_ref)) FROM pim.api_variant)
        """,
        {"set_id": set_id})

    # product_create (lane b): active Arborwear styles absent from the PIM,
    # with content backfilled from the Woo mirror (lowest blog with content).
    cursor.execute(
        """
        WITH missing AS (
            SELECT w.style_code,
                   max(w.brand) AS brand,
                   max(w.category) AS category,
                   max(w.product_name) AS wh_name,
                   count(*) AS variants
              FROM wh w
             WHERE w.mill_code = '22' AND w.is_active
               AND w.style_code NOT IN
                   (SELECT upper(btrim(style_number)) FROM pim.api_product WHERE btrim(style_number) <> '')
               AND w.sku NOT IN (SELECT upper(btrim(frmt_ref)) FROM pim.api_variant)
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
                   'prod_stat', 'D',
                   'prod_brand', COALESCE(m.brand, 'Arborwear'),
                   'prod_title', COALESCE(c.name, m.wh_name, m.style_code),
                   'prod_description', c.description,
                   'prod_shortdescription', c.short_description,
                   'content_source', CASE WHEN c.name IS NOT NULL THEN 'woo_mirror' ELSE 'fdm4_basics' END,
                   'variants', m.variants)
          FROM missing m
          LEFT JOIN content c ON c.style_code = m.style_code
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
                   'frmt_colorcode', w.color_code,
                   'frmt_colorname', w.color_name,
                   'frmt_sizecode', w.size_code,
                   'frmt_sizelabel', w.size_name)
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

def apply_set(set_id, limit, allow_removals):
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
        return
    if not enabled:
        print(f"DRY RUN (PIM_PUSH_ENABLED not set): would apply {len(rows)} rows")
    done = failed = skipped = 0
    for row in rows:
        # One bad row must not abort a multi-hour run.
        try:
            outcome, detail = apply_row(row, key, enabled, allow_removals)
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


def apply_row(row, key, enabled, allow_removals):
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
        if not enabled:
            return "skipped", "dry run"
        status, payload = api_call("DELETE", f"/catalog/variants({current['frmt_id']})", key)
        return ("applied", "") if 200 <= status < 300 else ("failed", f"{status} {payload}")
    if action == "variant_create":
        current = api_get_one("variants", "frmt_ref", row["frmt_ref"], key)
        if current is not None:
            return "skipped", "variant already exists"
        # POST /catalog/variants links to the parent by numeric prod_id.
        parent = api_get_one("products", "prod_ref", row["prod_ref"], key)
        if parent is None:
            return "skipped", "parent product not in the PIM"
        if not enabled:
            return "skipped", "dry run"
        body = {k: v for k, v in after.items()
                if k.startswith("frmt_") and v is not None}
        body["prod_id"] = parent["prod_id"]
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
    return "failed", f"unknown action {action}"


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)

    if args.diff:
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
        set_id, counts = run_diff(args.note or "diff run", remove_skus)
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
