#!/usr/bin/env python3
"""Backfill frmt_variantname on PIM variants created by push_pim.py.

The 2026-08-25 all-brands push (change set 6) created ~33.5k variants without
frmt_variantname, so they show no name in the PIM while legacy Woo-import
variants do. This composes "<product title> <color name> <size label>" from
the api2 mirror (pim.api_variant + pim.api_product) and PATCHes each variant
by its frmt_id, reusing push_pim's throttled api2 client.

Scope: only variants recorded as applied variant_create rows in
pim.push_change_row whose mirrored frmt_variantname is still empty, so the
run is idempotent — re-running after the hourly pull_pim refresh picks up
only what is still missing. Pre-existing blank-name variants from the
original Woo import are deliberately NOT touched. Every PATCH is preceded by a
live read of the variant, so a name someone typed after the worklist was taken
is skipped rather than overwritten.

Dry-run by default; --apply plus PIM_PUSH_ENABLED=1 writes.
"""

import argparse
import os
import sys
import time

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_pim import DB_NAME, DB_SOCKET, api_call, api_get_one, api_key  # noqa: E402

WORKLIST_SQL = """
    WITH pushed AS (
        SELECT DISTINCT upper(btrim(frmt_ref)) AS ref
          FROM pim.push_change_row
         WHERE action = 'variant_create' AND status = 'applied'
    )
    SELECT v.payload ->> 'frmt_id' AS frmt_id,
           v.frmt_ref,
           btrim(concat_ws(' ',
               COALESCE(NULLIF(btrim(p.payload ->> 'prod_title'), ''),
                        NULLIF(btrim(p.payload ->> 'name'), '')),
               NULLIF(btrim(v.payload ->> 'frmt_colorname'), ''),
               NULLIF(btrim(v.payload ->> 'frmt_sizelabel'), ''))) AS new_name,
           COALESCE(NULLIF(btrim(p.payload ->> 'prod_title'), ''),
                    NULLIF(btrim(p.payload ->> 'name'), '')) AS title
      FROM pim.api_variant v
      JOIN pushed ON upper(btrim(v.frmt_ref)) = pushed.ref
      LEFT JOIN pim.api_product p ON btrim(p.prod_ref) = btrim(v.prod_ref)
     WHERE COALESCE(btrim(v.payload ->> 'frmt_variantname'), '') = ''
       AND NULLIF(btrim(v.payload ->> 'frmt_id'), '') IS NOT NULL
       AND v.retired_at IS NULL
     ORDER BY v.frmt_ref
"""


def skip_reason(live, row):
    """Why this variant must not be PATCHed, or None to go ahead.

    The worklist comes from the mirror, which lags the PIM by up to an hour,
    and a full run takes hours more. Someone can type a name in that window;
    a blanket PATCH would wipe it. So the live object decides.
    """
    if live is None:
        return "gone from the PIM since the worklist was read"
    if str(live.get("frmt_variantname") or "").strip():
        return "already named in the PIM"
    if str(live.get("frmt_id") or "").strip() != str(row["frmt_id"] or "").strip():
        return "frmt_id changed since the worklist was read"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write to the PIM (also requires PIM_PUSH_ENABLED=1)")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap the number of variants processed this run")
    args = parser.parse_args(argv)

    connection = psycopg2.connect(dbname=DB_NAME, host=DB_SOCKET)
    cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cursor.execute(WORKLIST_SQL)
    rows = cursor.fetchall()
    no_title = [r for r in rows if not r["title"]]
    rows = [r for r in rows if r["title"] and r["new_name"]]
    if args.limit > 0:
        rows = rows[: args.limit]
    print(f"{len(rows)} variant(s) to name"
          + (f" ({len(no_title)} skipped: parent product has no title in the mirror)"
             if no_title else ""))

    enabled = os.environ.get("PIM_PUSH_ENABLED") == "1"
    if not args.apply or not enabled:
        for row in rows[:10]:
            print(f"  would set {row['frmt_ref']} ({row['frmt_id']}): {row['new_name']}")
        print("DRY RUN — pass --apply with PIM_PUSH_ENABLED=1 to write.")
        return 0

    key = api_key()
    done = failed = skipped = 0
    started = time.time()
    for index, row in enumerate(rows, 1):
        reason = None
        status, payload = 0, ""
        try:
            # Read the live variant first: the mirror the worklist came from
            # can be an hour stale, and this run takes hours more.
            reason = skip_reason(api_get_one("variants", "frmt_ref", row["frmt_ref"], key), row)
            if reason is None:
                status, payload = api_call(
                    "PATCH", f"/catalog/variants({row['frmt_id']})", key,
                    {"frmt_variantname": row["new_name"]})
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
            reason, status, payload = None, 0, f"exception: {exc}"
        if reason is not None:
            skipped += 1
            print(f"SKIP {row['frmt_ref']} ({row['frmt_id']}): {reason}", flush=True)
        elif 200 <= status < 300:
            done += 1
        else:
            failed += 1
            print(f"FAIL {row['frmt_ref']} ({row['frmt_id']}) -> {status} {str(payload)[:200]}",
                  flush=True)
        if index % 250 == 0 or index == len(rows):
            elapsed = int(time.time() - started)
            print(f"{index}/{len(rows)} ok={done} skipped={skipped} failed={failed} elapsed={elapsed}s",
                  flush=True)
    print(f"finished: ok={done} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
