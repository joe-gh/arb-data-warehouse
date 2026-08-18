"""Seed logo.color_class from distinct live catalog colors. Idempotent:
inserts new colors as source='ai'; NEVER modifies source='manual' rows."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import database
from color_classify import classify_color

def seed_color_class():
    with database.cursor(write=True, actor="seed-color-class") as cur:
        cur.execute("SET LOCAL logo.actor='seed-color-class'")
        cur.execute("""
            SELECT color_code, max(color) AS color
              FROM woo.store_product_state
             WHERE is_active AND kind='variation'
               AND NULLIF(btrim(color_code),'') IS NOT NULL
               AND NULLIF(btrim(color),'') IS NOT NULL
             GROUP BY color_code
        """)
        rows = cur.fetchall()
        written = 0
        for r in rows:
            cls, conf = classify_color(r["color"])
            cur.execute("""
                INSERT INTO logo.color_class(color_code,color_name,light_dark,source,confidence,updated_by)
                VALUES (%s,%s,%s,'ai',%s,'seed')
                ON CONFLICT (color_code) DO UPDATE
                   SET color_name = EXCLUDED.color_name,
                       light_dark = 'both',
                       confidence = NULL,
                       updated_at = now(),
                       updated_by = 'seed-color-class:review'
                 WHERE logo.color_class.source = 'ai'
                   AND logo.color_class.color_name IS DISTINCT FROM EXCLUDED.color_name
            """, (r["color_code"], r["color"], cls, conf))
            written += cur.rowcount
        return {"seen": len(rows), "written": written}

if __name__ == "__main__":
    print(seed_color_class())
