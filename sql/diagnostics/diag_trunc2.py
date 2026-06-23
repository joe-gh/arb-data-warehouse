import os, sys
sys.path.insert(0, '/opt/fdm4-extractor')
os.environ['ARB_DBTEST_ENV'] = '/opt/fdm4-extractor/fdm4.env'
from explore import load_env, connect
cfg = load_env()
conn = connect(cfg, 'fdm4', cfg['OPENEDGE_JAR'])
cur = conn.cursor()
W = "WHERE detail_type='storeData' AND LENGTH(detail_value)>100"
tests = {
    'plain':     f'SELECT TOP 1 detail_value FROM PUB."catalog_product_detail" {W}',
    'cast':      f'SELECT TOP 1 CAST(detail_value AS VARCHAR(30742)) FROM PUB."catalog_product_detail" {W}',
    'substring': f'SELECT TOP 1 SUBSTRING(detail_value FROM 1 FOR 30742) FROM PUB."catalog_product_detail" {W}',
}
for name, q in tests.items():
    try:
        cur.execute(q)
        v = cur.fetchone()[0]
        print(f"{name:10}: python len = {len(v) if v is not None else None}")
    except Exception as e:
        print(f"{name:10}: ERROR {' '.join(str(e).split())[:140]}")
cur.close(); conn.close()
