#!/usr/bin/env python3
"""
Load a pull_fdm4.py CSV dump into the warehouse Postgres (runs ON the box).

Creates fdm4.<table> with all-TEXT columns (raw layer; cast later in staging),
COPYs each CSV in, and grants SELECT to the reader roles. Run as the postgres
OS user so it authenticates to local Postgres via peer auth (no password):

  sudo -u postgres /opt/fdm4-extractor/venv/bin/python load_dump.py /tmp/dump

Idempotent: each table is dropped + recreated from the CSV header.
"""

import csv
import glob
import os
import sys

import psycopg2

DB = "arb_warehouse"
SCHEMA = "fdm4"
READERS = ["woo_reader", "insights_reader"]


def ident(name):
    # quote a SQL identifier, escaping embedded quotes
    return '"' + name.replace('"', '""') + '"'


def load(conn, table, csv_path):
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
    conn.commit()
    return n


def main():
    dump_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dump"
    conn = psycopg2.connect(host="/var/run/postgresql", dbname=DB, user="postgres")
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {ident(SCHEMA)}")
        for r in READERS:
            cur.execute(f"GRANT USAGE ON SCHEMA {ident(SCHEMA)} TO {ident(r)}")
    conn.commit()

    total = 0
    for path in sorted(glob.glob(os.path.join(dump_dir, "*.csv"))):
        table = os.path.splitext(os.path.basename(path))[0]
        n = load(conn, table, path)
        total += n
        print(f"  loaded {SCHEMA}.{table:<24} {n:>8,} rows")
    print(f"\nLoaded {total:,} rows into {DB}.{SCHEMA}")

    # Rebuild the Woo-facing query layer (woo.store_product_state) from the
    # freshly loaded raw tables, so the full pipeline is one step. No-op if the
    # transform hasn't been applied yet (db-test/sql/woo_transform.sql).
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regprocedure('woo.refresh_product_state()') IS NOT NULL")
            if cur.fetchone()[0]:
                cur.execute("SELECT woo.refresh_product_state()")
                rows = cur.fetchone()[0]
                conn.commit()
                print(f"Rebuilt woo.store_product_state: {rows:,} rows")
            else:
                print("woo.refresh_product_state() not found — apply db-test/sql/woo_transform.sql to enable the transform")
    except Exception as e:
        print(f"transform refresh skipped: {e}")

    conn.close()


if __name__ == "__main__":
    main()
