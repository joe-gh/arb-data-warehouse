#!/usr/bin/env python3
"""
FDM4 -> CSV extractor (the "pull", run wherever FDM4 is reachable).

For the PoC this runs on the laptop (on the Arborwear VPN). Later the IDENTICAL
code runs on the warehouse box once FDM4 whitelists its EIP — only the network
location changes, not the logic.

Streams each PUB table to db-test/dump/<table>.csv (all columns as text =
faithful raw layer; type-casting happens later in the warehouse/staging) and
writes a manifest.json with row counts.

Usage:
  JAVA_HOME=/opt/homebrew/opt/openjdk python3 db-test/pull_fdm4.py
  ... db-test/pull_fdm4.py item style price-list      # explicit table list
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from explore import load_env, connect  # noqa: E402

# Product domain we've been analyzing — representative + useful for Woo/Insights.
DEFAULT_TABLES = [
    "style", "style-color", "style-size",
    "item", "price-list", "item-balance",
    "catalog_product", "catalog_product_detail",
]

BATCH = 5000
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dump")


def table_columns(conn, table):
    """Column names in ordinal order via JDBC metadata (PUB schema)."""
    rs = conn.jconn.getMetaData().getColumns(None, "PUB", table, "%")
    cols = []
    while rs.next():
        cols.append(rs.getString(4))  # COLUMN_NAME
    rs.close()
    return cols


def dump_table(conn, table, out_dir):
    # Use an EXPLICIT column list, never "SELECT *": the DataDirect OpenEdge driver
    # returns character columns truncated to their display-format width on
    # "SELECT *" (e.g. catalog_product_detail.detail_value JSON came back as 40
    # chars), but the full SQL width when the columns are named explicitly.
    names = table_columns(conn, table)
    select_list = ", ".join('"' + c.replace('"', '""') + '"' for c in names) if names else "*"

    cur = conn.cursor()
    try:
        cur._connection.jconn.setReadOnly(True)
    except Exception:
        pass
    cur.execute(f'SELECT {select_list} FROM PUB."{table}"')
    cols = [d[0] for d in cur.description]

    path = os.path.join(out_dir, f"{table}.csv")
    rows = 0
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        while True:
            batch = cur.fetchmany(BATCH)
            if not batch:
                break
            for r in batch:
                w.writerow(["" if v is None else str(v) for v in r])
            rows += len(batch)
    cur.close()
    return rows, len(cols), path


def main():
    tables = sys.argv[1:] or DEFAULT_TABLES
    cfg = load_env()
    jar = cfg.get("OPENEDGE_JAR")
    if not jar or not os.path.isfile(jar):
        sys.exit("OPENEDGE_JAR not set / jar missing in ~/.arb-dbtest.env")

    os.makedirs(OUT_DIR, exist_ok=True)
    conn = connect(cfg, cfg.get("DB1_NAME", "fdm4"), jar)

    manifest = {"source": "FDM4 OpenEdge PUB", "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tables": {}}
    for t in tables:
        try:
            rows, ncols, path = dump_table(conn, t, OUT_DIR)
            manifest["tables"][t] = {"rows": rows, "columns": ncols}
            print(f"  {t:<24} {rows:>8,} rows x {ncols} cols -> {os.path.basename(path)}")
        except Exception as e:
            manifest["tables"][t] = {"error": str(e)[:200]}
            print(f"  {t:<24} ERROR: {str(e)[:120]}")
    conn.close()

    with open(os.path.join(OUT_DIR, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    total = sum(v.get("rows", 0) for v in manifest["tables"].values())
    print(f"\nDumped {total:,} rows across {len(tables)} tables -> {OUT_DIR}/")


if __name__ == "__main__":
    main()
