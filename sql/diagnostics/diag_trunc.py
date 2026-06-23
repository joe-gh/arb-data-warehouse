import csv, os, sys
sys.path.insert(0, '/opt/fdm4-extractor')

# 1) What's actually in the dumped CSV?
path = '/opt/fdm4-extractor/dump/catalog_product_detail.csv'
m = 0; ex = ''
with open(path) as f:
    r = csv.reader(f); h = next(r)
    di = h.index('detail_value'); ti = h.index('detail_type')
    for row in r:
        if len(row) > max(di, ti) and row[ti] == 'storeData':
            L = len(row[di])
            if L > m:
                m = L; ex = row[di][:80]
print(f"CSV   : max storeData detail_value len = {m}; head={ex!r}")

# 2) What does the JDBC driver actually hand Python for a long value right now?
os.environ['ARB_DBTEST_ENV'] = '/opt/fdm4-extractor/fdm4.env'
from explore import load_env, connect
cfg = load_env()
conn = connect(cfg, 'fdm4', cfg['OPENEDGE_JAR'])
cur = conn.cursor()
cur.execute("SELECT detail_value FROM PUB.\"catalog_product_detail\" "
            "WHERE detail_type='storeData' AND LENGTH(detail_value)>100 "
            "ORDER BY LENGTH(detail_value) DESC LIMIT 1")
v = cur.fetchone()[0]
print(f"DRIVER: python len(detail_value) = {len(v) if v is not None else None}; head={(v or '')[:80]!r}")
cur.close(); conn.close()
