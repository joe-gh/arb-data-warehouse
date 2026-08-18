#!/usr/bin/env python3
"""
FDM4 -> CSV extractor (the "pull", run wherever FDM4 is reachable).

For the PoC this runs on the laptop (on the Arborwear VPN). Later the IDENTICAL
code runs on the warehouse box once FDM4 whitelists its EIP - only the network
location changes, not the logic.

Streams each PUB table to a private run directory under db-test/dump (all
columns as text = faithful raw layer; type-casting happens later in the
warehouse/staging), writes a manifest.json with per-table status and row
counts, and promotes current.json only after every requested table succeeds.

Usage:
  JAVA_HOME=/opt/homebrew/opt/openjdk python3 db-test/pull_fdm4.py
  ... db-test/pull_fdm4.py item style price-list      # explicit table list
"""

import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from explore import load_env, connect  # noqa: E402

# Product domain we've been analyzing - representative + useful for Woo/Insights.
DEFAULT_TABLES = [
    "style", "style-color", "style-size",
    "item", "price-list", "item-balance",
    "inv-balance",    # LIVE per-(item,warehouse) balance: intraday on-hand/committed.
                      # item-balance above is only the NIGHTLY snapshot of this table,
                      # so FDM4-side sales lagged Woo stock by up to a day.
    "catalog_product", "catalog_product_detail",
    "mill",           # brand master: mill-code -> description (brand name). Enriches store_product_state.
    "vendor",         # supplier master: vend-number -> vend-name (distinct from brand/mill).
    "dec_design",     # decoration/logo master: design_id -> description (design name).
    "design_pool",    # per-decoration detail: design_id -> method/location/art_id/color/stitch.
    "cust_art_file",  # art assets: art_id -> resource_type + target_filename (preview PNG / .dst).
    "price-categ",    # price-level labels: price-categ 1 = Corp1;Corp2;Corp3;Wholesale;Employee;MSRP.
]

BATCH = 5000
OUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dump")
KEEP_RUNS = max(1, int(os.environ.get("FDM4_DUMP_KEEP_RUNS", "5")))
SAFE_TABLE = re.compile(r"^[A-Za-z0-9_-]+$")


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
    part_path = path + ".part"
    rows = 0
    try:
        with open(part_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            while True:
                batch = cur.fetchmany(BATCH)
                if not batch:
                    break
                for r in batch:
                    w.writerow(["" if v is None else str(v) for v in r])
                rows += len(batch)
        os.replace(part_path, path)
    except Exception:
        try:
            os.unlink(part_path)
        except FileNotFoundError:
            pass
        raise
    finally:
        cur.close()
    return rows, len(cols), path


def write_json_atomic(path, payload):
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(temporary, path)


def cleanup_runs(out_root, keep):
    current_name = ""
    current_path = os.path.join(out_root, "current.json")
    try:
        with open(current_path) as fh:
            current_name = str(json.load(fh).get("run_dir", ""))
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        pass
    runs = sorted(
        (
            entry
            for entry in os.scandir(out_root)
            if entry.is_dir(follow_symlinks=False) and entry.name.startswith("run-")
        ),
        key=lambda entry: entry.stat(follow_symlinks=False).st_mtime,
        reverse=True,
    )
    retained = {entry.name for entry in runs[:keep]}
    if current_name:
        retained.add(current_name)
    for entry in runs:
        if entry.name not in retained:
            shutil.rmtree(entry.path)


def main():
    tables = list(dict.fromkeys(sys.argv[1:] or DEFAULT_TABLES))
    invalid = [table for table in tables if not SAFE_TABLE.fullmatch(table)]
    if invalid:
        sys.exit(f"invalid table name(s): {', '.join(invalid)}")
    cfg = load_env()
    jar = cfg.get("OPENEDGE_JAR")
    if not jar or not os.path.isfile(jar):
        sys.exit("OPENEDGE_JAR not set / jar missing in ~/.arb-dbtest.env")

    os.makedirs(OUT_ROOT, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_name = f"run-{run_id}-{os.getpid()}"
    run_dir = os.path.join(OUT_ROOT, run_name)
    os.makedirs(run_dir, mode=0o750)
    conn = connect(cfg, cfg.get("DB1_NAME", "fdm4"), jar)

    manifest = {
        "source": "FDM4 OpenEdge PUB",
        "run_id": run_id,
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "required_tables": tables,
        "complete": False,
        "tables": {},
    }
    try:
        for table in tables:
            try:
                rows, ncols, csv_path = dump_table(conn, table, run_dir)
                manifest["tables"][table] = {
                    "status": "ok",
                    "rows": rows,
                    "columns": ncols,
                    "filename": os.path.basename(csv_path),
                }
                print(
                    f"  {table:<24} {rows:>8,} rows x {ncols} cols "
                    f"-> {os.path.basename(csv_path)}"
                )
            except Exception as exc:
                manifest["tables"][table] = {
                    "status": "error",
                    "error": str(exc)[:200],
                }
                print(f"  {table:<24} ERROR: {str(exc)[:120]}")
    finally:
        conn.close()

    failed = [
        table
        for table in tables
        if manifest["tables"].get(table, {}).get("status") != "ok"
    ]
    manifest["complete"] = not failed
    manifest["publishable"] = (
        not failed
        and len(tables) == len(DEFAULT_TABLES)
        and set(tables) == set(DEFAULT_TABLES)
    )
    write_json_atomic(os.path.join(run_dir, "manifest.json"), manifest)
    if manifest["publishable"]:
        write_json_atomic(
            os.path.join(OUT_ROOT, "current.json"),
            {"run_dir": run_name, "run_id": run_id},
        )
    elif not failed:
        print("Partial diagnostic pull completed; current.json was not promoted.")
    cleanup_runs(OUT_ROOT, KEEP_RUNS)
    total = sum(v.get("rows", 0) for v in manifest["tables"].values())
    print(f"\nDumped {total:,} rows across {len(tables)} tables -> {run_dir}/")
    if failed:
        print(f"Required table pull failed: {', '.join(failed)}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
