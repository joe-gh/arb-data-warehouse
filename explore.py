#!/usr/bin/env python3
"""
FDM4 / OpenEdge exploration harness (read-only).

Connects over JDBC (DataDirect OpenEdge driver) via JayDeBeApi using the
credentials in ~/.arb-dbtest.env. The driver jar path comes from the
OPENEDGE_JAR key in that file (or --jar).

Read-only by design: only SELECT statements are allowed through, and the
connection is set read-only at the JDBC level as well.

Usage:
  python3 db-test/explore.py smoke                      # connect + version + table count (both DBs)
  python3 db-test/explore.py schemas [--db fdm4|irms]
  python3 db-test/explore.py tables  [--db fdm4] [--schema PUB]
  python3 db-test/explore.py columns --table PUB.customer [--db fdm4]
  python3 db-test/explore.py sample  --table PUB.customer [-n 5] [--db fdm4]
  python3 db-test/explore.py sql "SELECT ..." [--db fdm4]
  python3 db-test/explore.py inventory [--db fdm4] [--counts] [-o inventory_fdm4.csv]

Phase-0 deliverable: `inventory` dumps every table/view + column to CSV.
"""

import argparse
import csv
import os
import sys

ENV_FILE = os.environ.get("ARB_DBTEST_ENV") or os.path.expanduser("~/.arb-dbtest.env")
DRIVER_CLASS = "com.ddtek.jdbc.openedge.OpenEdgeDriver"
LOGIN_TIMEOUT_S = 8
QUERY_TIMEOUT_S = 30


def load_env():
    cfg = {}
    if not os.path.isfile(ENV_FILE):
        sys.exit(f"Cannot read {ENV_FILE}")
    with open(ENV_FILE) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip("\"'")
    return cfg


def connect(cfg, db, jar):
    import jaydebeapi

    host = cfg["DB_HOST"]
    port = cfg["DB1_PORT"] if db == cfg.get("DB1_NAME") else cfg["DB2_PORT"]
    url = f"jdbc:datadirect:openedge://{host}:{port};databaseName={db};LoginTimeout={LOGIN_TIMEOUT_S}"

    conn = jaydebeapi.connect(DRIVER_CLASS, url, [cfg["DB_USER"], cfg["DB_PASS"]], jars=jar)
    try:
        conn.jconn.setReadOnly(True)
    except Exception:
        pass  # belt and braces; the SELECT-only gate below is the hard rule
    return conn


def run_select(conn, sql, max_rows=200):
    stripped = sql.lstrip().lstrip("(").lstrip()
    if not stripped.lower().startswith("select"):
        sys.exit("Refusing: only SELECT statements are allowed.")
    cur = conn.cursor()
    try:
        cur._connection.jconn.setReadOnly(True)
    except Exception:
        pass
    cur.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(max_rows)
    cur.close()
    return cols, rows


def print_table(cols, rows):
    if not cols:
        print("(no result set)")
        return
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c)) for i, c in enumerate(cols)]
    line = " | ".join(str(c).ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print(" | ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    print(f"({len(rows)} row(s) shown)")


def meta(conn):
    return conn.jconn.getMetaData()


def list_schemas(conn):
    rs = meta(conn).getSchemas()
    out = []
    while rs.next():
        out.append(rs.getString(1))
    rs.close()
    return out


def list_tables(conn, schema=None):
    rs = meta(conn).getTables(None, schema, "%", ["TABLE", "VIEW", "SYSTEM TABLE"])
    out = []
    while rs.next():
        out.append((rs.getString(2), rs.getString(3), rs.getString(4)))  # schema, name, type
    rs.close()
    return out


def list_columns(conn, schema, table):
    rs = meta(conn).getColumns(None, schema, table, "%")
    out = []
    while rs.next():
        out.append((rs.getString(4), rs.getString(6), rs.getInt(7), rs.getString(18)))  # name, type, size, nullable
    rs.close()
    return out


def split_table_arg(table_arg):
    if "." in table_arg:
        schema, table = table_arg.split(".", 1)
        return schema, table
    return None, table_arg


def cmd_smoke(cfg, jar, _args):
    for db in [cfg.get("DB1_NAME"), cfg.get("DB2_NAME")]:
        if not db:
            continue
        print(f"=== {db} ===")
        try:
            conn = connect(cfg, db, jar)
            md = meta(conn)
            print(f"  connected: {md.getDatabaseProductName()} {md.getDatabaseProductVersion()}")
            print(f"  driver:    {md.getDriverName()} {md.getDriverVersion()}")
            tables = list_tables(conn)
            schemas = sorted({t[0] for t in tables if t[0]})
            print(f"  tables/views visible: {len(tables)} across schemas: {', '.join(schemas)}")
            conn.close()
        except Exception as e:
            print(f"  FAILED: {e}")


def cmd_schemas(cfg, jar, args):
    conn = connect(cfg, args.db, jar)
    for s in list_schemas(conn):
        print(s)
    conn.close()


def cmd_tables(cfg, jar, args):
    conn = connect(cfg, args.db, jar)
    rows = list_tables(conn, args.schema)
    print_table(["schema", "table", "type"], rows)
    conn.close()


def cmd_columns(cfg, jar, args):
    schema, table = split_table_arg(args.table)
    conn = connect(cfg, args.db, jar)
    rows = list_columns(conn, schema, table)
    print_table(["column", "type", "size", "nullable"], rows)
    conn.close()


def cmd_sample(cfg, jar, args):
    conn = connect(cfg, args.db, jar)
    cols, rows = run_select(conn, f'SELECT * FROM {args.table}', max_rows=args.n)
    print_table(cols, rows)
    conn.close()


def cmd_sql(cfg, jar, args):
    conn = connect(cfg, args.db, jar)
    cols, rows = run_select(conn, args.query, max_rows=args.n)
    print_table(cols, rows)
    conn.close()


def cmd_inventory(cfg, jar, args):
    conn = connect(cfg, args.db, jar)
    out_path = args.output or f"db-test/inventory_{args.db}.csv"
    tables = list_tables(conn)
    written = 0
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["schema", "table", "table_type", "column", "data_type", "size", "nullable", "row_count"])
        for schema, table, ttype in tables:
            count = ""
            if args.counts and ttype in ("TABLE", "VIEW") and schema not in ("SYSPROGRESS",):
                try:
                    _, rows = run_select(conn, f'SELECT COUNT(*) FROM "{schema}"."{table}"', max_rows=1)
                    count = rows[0][0] if rows else ""
                except Exception as e:
                    count = f"err: {e}"[:60]
            for col, dtype, size, nullable in list_columns(conn, schema, table):
                w.writerow([schema, table, ttype, col, dtype, size, nullable, count])
                written += 1
            print(f"  {schema}.{table} ({ttype}) {'rows=' + str(count) if count != '' else ''}")
    print(f"\nWrote {written} column rows for {len(tables)} tables -> {out_path}")
    conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["smoke", "schemas", "tables", "columns", "sample", "sql", "inventory"])
    p.add_argument("query", nargs="?", help="SQL for the 'sql' command")
    p.add_argument("--db", default=None, help="database name (default: DB1 from env)")
    p.add_argument("--schema", default=None)
    p.add_argument("--table", default=None)
    p.add_argument("--jar", default=None, help="path to OpenEdge JDBC jar (default: OPENEDGE_JAR from env)")
    p.add_argument("-n", type=int, default=10, help="max rows to show")
    p.add_argument("--counts", action="store_true", help="inventory: include row counts (slower)")
    p.add_argument("-o", "--output", default=None)
    args = p.parse_args()

    cfg = load_env()
    args.db = args.db or cfg.get("DB1_NAME")
    jar = args.jar or cfg.get("OPENEDGE_JAR")
    if not jar or not os.path.isfile(jar):
        sys.exit("OpenEdge JDBC jar not found. Set OPENEDGE_JAR=/path/to/openedge.jar in ~/.arb-dbtest.env or pass --jar.")

    if args.command in ("columns",) and not args.table:
        sys.exit("--table is required")
    if args.command == "sample" and not args.table:
        sys.exit("--table is required")
    if args.command == "sql" and not args.query:
        sys.exit("provide a SELECT statement")

    {
        "smoke": cmd_smoke,
        "schemas": cmd_schemas,
        "tables": cmd_tables,
        "columns": cmd_columns,
        "sample": cmd_sample,
        "sql": cmd_sql,
        "inventory": cmd_inventory,
    }[args.command](cfg, jar, args)


if __name__ == "__main__":
    main()
