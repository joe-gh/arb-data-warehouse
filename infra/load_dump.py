#!/usr/bin/env python3
"""
Load a pull_fdm4.py CSV dump into the warehouse Postgres (runs ON the box).

Creates fdm4.<table> with all-TEXT columns (raw layer; cast later in staging),
COPYs each CSV in, and grants SELECT to the reader roles. Run as the postgres
OS user so it authenticates to local Postgres via peer auth (no password):

  sudo -u postgres /opt/fdm4-extractor/venv/bin/python load_dump.py /tmp/dump

The directory must contain a successful manifest.json, or be a dump root whose
current.json points at one. All raw table DDL/COPY work commits as one atomic
transaction (readers see either the previous complete raw snapshot or the new
one); the Woo transform then commits separately so its ~12-minute runtime never
holds the raw tables' AccessExclusive locks against application reads.
"""

import csv
import json
import os
import re
import sys

import psycopg2

DB = "arb_warehouse"
SCHEMA = "fdm4"
# Roles re-granted SELECT on every recreated fdm4.* table. logo_admin (the Logo
# Admin app) reads fdm4 design/art data; without an explicit re-grant it would
# depend solely on default privileges surviving, which silently breaks if this
# loader ever runs as a role other than the one that set them.
READERS = ["woo_reader", "insights_reader", "logo_admin"]
SAFE_TABLE = re.compile(r"^[A-Za-z0-9_-]+$")
REQUIRED_TABLES = frozenset({
    "style", "style-color", "style-size", "item", "price-list",
    "item-balance", "inv-balance", "catalog_product",
    "catalog_product_detail", "mill", "vendor", "dec_design",
    "design_pool", "cust_art_file", "price-categ",
    "customer_art", "customer_art_cust",
})


def existing_roles(conn, names):
    """Filter to roles that exist so a missing optional role never fails the load."""
    with conn.cursor() as cur:
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (names,))
        found = {r[0] for r in cur.fetchall()}
    missing = [n for n in names if n not in found]
    if missing:
        print(f"note: skipping grants for missing role(s): {', '.join(missing)}")
    return [n for n in names if n in found]


def ident(name):
    # quote a SQL identifier, escaping embedded quotes
    return '"' + name.replace('"', '""') + '"'


LOAD_FLAG = "fdm4_load"


def set_load_flag(enabled, note=""):
    """Tell the Warehouse Operations app whether the raw fdm4.* swap is in
    progress: the minutes in which every read of fdm4.* waits on the
    drop/create locks (logo sync included). Uses its own autocommit
    connection so the state is visible outside the load transaction.
    Advisory only: a failure here never fails the load."""
    try:
        flag_conn = psycopg2.connect(host="/var/run/postgresql", dbname=DB, user="postgres")
        flag_conn.autocommit = True
        with flag_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO woo.app_flag (name, enabled, note, updated_by, updated_at)
                VALUES (%s, %s, %s, 'load_dump', now())
                ON CONFLICT (name) DO UPDATE
                   SET enabled = EXCLUDED.enabled, note = EXCLUDED.note,
                       updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                (LOAD_FLAG, bool(enabled), note[:200]),
            )
        flag_conn.close()
    except Exception as exc:  # noqa: BLE001 - advisory flag
        print(f"warning: could not update woo.app_flag {LOAD_FLAG}: {exc}", file=sys.stderr)


def load(conn, table, csv_path, expected_rows):
    with open(csv_path, newline="") as fh:
        header = next(csv.reader(fh))
    cols_ddl = ", ".join(f"{ident(c)} text" for c in header)
    fq = f"{ident(SCHEMA)}.{ident(table)}"

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {fq}")
        cur.execute(f"CREATE TABLE {fq} ({cols_ddl})")
        with open(csv_path) as fh:
            cur.copy_expert(f"COPY {fq} FROM STDIN WITH (FORMAT csv, HEADER true)", fh)
        cur.execute(f"SELECT count(*) FROM {fq}")
        n = cur.fetchone()[0]
        for r in READERS:
            cur.execute(f"GRANT SELECT ON {fq} TO {ident(r)}")
    if n != expected_rows:
        raise RuntimeError(
            f"manifest row count mismatch for {table}: expected {expected_rows}, loaded {n}"
        )
    return n


def resolve_run_dir(dump_path):
    direct_manifest = os.path.join(dump_path, "manifest.json")
    if os.path.isfile(direct_manifest):
        return os.path.realpath(dump_path), None
    pointer_path = os.path.join(dump_path, "current.json")
    with open(pointer_path) as fh:
        pointer = json.load(fh)
    run_name = str(pointer.get("run_dir", ""))
    if not re.fullmatch(r"run-[A-Za-z0-9_.-]+", run_name):
        raise RuntimeError("current.json contains an invalid run directory")
    root = os.path.realpath(dump_path)
    run_dir = os.path.realpath(os.path.join(root, run_name))
    if os.path.commonpath((root, run_dir)) != root:
        raise RuntimeError("current run directory escapes the dump root")
    if not os.path.isfile(os.path.join(run_dir, "manifest.json")):
        raise RuntimeError("current run has no manifest.json")
    pointer_run_id = pointer.get("run_id")
    if not isinstance(pointer_run_id, str) or not pointer_run_id:
        raise RuntimeError("current.json contains an invalid run id")
    return run_dir, pointer_run_id


def validated_manifest(run_dir, pointer_run_id=None):
    with open(os.path.join(run_dir, "manifest.json")) as fh:
        manifest = json.load(fh)
    required = manifest.get("required_tables")
    table_entries = manifest.get("tables")
    if pointer_run_id is not None and manifest.get("run_id") != pointer_run_id:
        raise RuntimeError("current.json and manifest run ids differ")
    if (
        manifest.get("complete") is not True
        or manifest.get("publishable") is not True
        or not isinstance(required, list)
    ):
        raise RuntimeError("dump manifest is not complete")
    if not required or len(required) != len(set(required)) or not isinstance(table_entries, dict):
        raise RuntimeError("dump manifest table inventory is invalid")
    if set(required) != set(table_entries):
        raise RuntimeError("dump manifest required/table inventories differ")
    if set(required) != REQUIRED_TABLES:
        missing = sorted(REQUIRED_TABLES - set(required))
        extra = sorted(set(required) - REQUIRED_TABLES)
        raise RuntimeError(
            "dump manifest is not the canonical production inventory; "
            f"missing={missing}, extra={extra}"
        )
    validated = []
    for table in required:
        if not isinstance(table, str) or not SAFE_TABLE.fullmatch(table):
            raise RuntimeError("dump manifest contains an invalid table name")
        entry = table_entries.get(table)
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            raise RuntimeError(f"required table {table} did not complete")
        filename = entry.get("filename")
        if filename != f"{table}.csv":
            raise RuntimeError(f"required table {table} has an invalid filename")
        rows = entry.get("rows")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise RuntimeError(f"required table {table} has an invalid row count")
        csv_path = os.path.join(run_dir, filename)
        if not os.path.isfile(csv_path):
            raise RuntimeError(f"required table {table} CSV is absent")
        validated.append((table, csv_path, rows))
    return validated


def main():
    global READERS
    dump_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dump"
    run_dir, pointer_run_id = resolve_run_dir(dump_path)
    tables = validated_manifest(run_dir, pointer_run_id)
    conn = psycopg2.connect(host="/var/run/postgresql", dbname=DB, user="postgres")
    try:
        READERS = existing_roles(conn, READERS)
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ident(SCHEMA)}")
            for reader in READERS:
                cur.execute(
                    f"GRANT USAGE ON SCHEMA {ident(SCHEMA)} TO {ident(reader)}"
                )

        total = 0
        set_load_flag(True, f"raw fdm4 swap from {os.path.basename(run_dir)}")
        for table, csv_path, expected_rows in tables:
            loaded = load(conn, table, csv_path, expected_rows)
            total += loaded
            print(f"  loaded {SCHEMA}.{table:<24} {loaded:>8,} rows")
        print(f"\nLoaded {total:,} rows into {DB}.{SCHEMA}")
        # Commit the raw layer as its own atomic swap. Holding the drop/create
        # AccessExclusive locks through the ~12-minute transform would block
        # every app read of fdm4.* for the whole window. A transform failure
        # after this commit leaves new-raw/old-woo with the run marked failed
        # and the WP version gate holding - the same state as a failed run.
        conn.commit()
        set_load_flag(False, "raw fdm4 swap committed")

        with conn.cursor() as cur:
            # Design ownership by art customer (logo.design_customer) is
            # materialized from the freshly swapped fdm4.* tables. Optional
            # until sql/migrations/2026-09-22-design-customer.sql is applied.
            cur.execute("SELECT to_regprocedure('logo.refresh_design_customer()') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute("SELECT logo.refresh_design_customer()")
                print(f"  refreshed logo.design_customer: {int(cur.fetchone()[0]):,} rows")
            else:
                print("note: logo.refresh_design_customer() not installed; design ownership not refreshed")

            cur.execute("SELECT to_regprocedure('woo.refresh_product_state()') IS NOT NULL")
            if not cur.fetchone()[0]:
                raise RuntimeError(
                    "woo.refresh_product_state() not found; apply sql/woo_transform.sql"
                )
            cur.execute("SELECT woo.refresh_product_state()")
            rows_loaded = cur.fetchone()[0]
            # The gate version must cover tombstones too: a removal-only
            # refresh bumps only inactive rows, so a max over active rows
            # alone would not move (or could even go backwards) and the
            # gated WordPress reconcile would skip the removal. The active
            # count stays filtered so the assertion below still compares
            # like with like.
            cur.execute(
                """
                SELECT COALESCE(MAX(row_version), 0),
                       count(*) FILTER (WHERE is_active)
                  FROM woo.store_product_state
                """
            )
            refresh_version, active_rows = cur.fetchone()
            if int(rows_loaded) != int(active_rows):
                raise RuntimeError("transform row count did not match active projection")
        conn.commit()
    except Exception:
        conn.rollback()
        set_load_flag(False, "load failed; raw swap rolled back or transform failed")
        raise
    finally:
        conn.close()

    print(f"Rebuilt woo.store_product_state: {rows_loaded:,} rows")
    print(
        f"SYNC_RESULT refresh_version={int(refresh_version)} "
        f"rows_loaded={int(active_rows)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"load failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
