"""Integration tests for the bulk-apply-logos migration.

Verify that the three new tables (color_class, bulk_batch, bulk_batch_row)
exist in the logo schema and that the light_dark CHECK constraint on
color_class is enforced.

Requires a provisioned test database (TEST_DATABASE_DSN + TEST_DATABASE_ADMIN_DSN).
Run via: cd logo-admin && python -m pytest tests/test_color_class.py -v
"""

import pytest
import psycopg2

from db import database


def test_color_class_tables_exist():
    with database.cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.tables
                        WHERE table_schema='logo'
                          AND table_name IN ('color_class','bulk_batch','bulk_batch_row')""")
        assert {r["table_name"] for r in cur.fetchall()} == {"color_class", "bulk_batch", "bulk_batch_row"}


def test_color_class_check_constraint():
    with database.cursor(write=True, actor="test") as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("""INSERT INTO logo.color_class(color_code,color_name,light_dark)
                           VALUES ('X','X','purple')""")


from color_classify import classify_color

def test_classify_obvious():
    assert classify_color("Black")[0] == "dark"
    assert classify_color("White")[0] == "light"
    assert classify_color("Navy")[0] == "dark"
    assert classify_color("Natural")[0] == "light"

def test_classify_grey_modifiers():
    assert classify_color("Charcoal Grey")[0] == "dark"
    assert classify_color("Heather Grey")[0] == "light"
    assert classify_color("Athletic Gray")[0] == "light"

def test_classify_bright_takes_dark_logo():
    assert classify_color("Safety Yellow")[0] == "light"
    assert classify_color("Blaze Orange")[0] == "light"

def test_classify_low_confidence_flag():
    cls, conf = classify_color("Zephyr Quartz")
    assert conf < 0.7


def test_seed_inserts_ai_and_preserves_manual():
    from infra.seed_color_class import seed_color_class
    with database.cursor(write=True, actor="seed") as cur:
        cur.execute("SET LOCAL logo.actor='seed'")
        cur.execute("""INSERT INTO woo.store_product_state
            (fdm4_store,catalog_id,sku,kind,style_code,color_code,color,is_active,payload,content_hash)
            VALUES ('S_TEST','c','sku1','variation','ST1','0001','Navy',true,'{}','testhash')
            ON CONFLICT DO NOTHING""")
        cur.execute("""INSERT INTO logo.color_class(color_code,color_name,light_dark,source)
            VALUES ('0001','Navy','light','manual')
            ON CONFLICT (color_code) DO UPDATE SET light_dark='light', source='manual'""")
    seed_color_class()
    with database.cursor() as cur:
        cur.execute("SELECT light_dark,source FROM logo.color_class WHERE color_code='0001'")
        row = cur.fetchone()
        assert row["source"] == "manual" and row["light_dark"] == "light"


def test_list_colors_filters_needs_review():
    import queries
    with database.cursor(write=True, actor="t") as cur:
        cur.execute("SET LOCAL logo.actor='t'")
        cur.execute("""INSERT INTO logo.color_class(color_code,color_name,light_dark,source,confidence)
            VALUES ('D1','Navy','dark','ai',0.85),('D2','Mystery','dark','ai',0.4)
            ON CONFLICT (color_code) DO UPDATE SET confidence=EXCLUDED.confidence""")
    with database.cursor() as cur:
        res = queries.list_colors(cur, needs_review=True)
        codes = {r["color_code"] for r in res["colors"]}
        assert "D2" in codes and "D1" not in codes


def test_colors_get_and_put(client_as):
    c = client_as("admin-one")
    with database.cursor(write=True, actor="t") as cur:
        cur.execute("SET LOCAL logo.actor='t'")
        cur.execute("""INSERT INTO logo.color_class(color_code,color_name,light_dark,source,confidence)
            VALUES ('P1','Navy','dark','ai',0.85) ON CONFLICT (color_code) DO NOTHING""")
    assert c.get("/api/colors?q=Navy").json()["colors"][0]["color_code"] == "P1"
    r = c.put("/api/colors", json={"color_code": "P1", "light_dark": "light"})
    assert r.status_code == 200
    with database.cursor() as cur:
        cur.execute("SELECT light_dark,source FROM logo.color_class WHERE color_code='P1'")
        row = cur.fetchone()
        assert row["light_dark"] == "light" and row["source"] == "manual"


def test_dashboard_has_colors_view(client_as):
    html = client_as("admin-one").get("/").text
    assert 'id="view-colors"' in html and 'data-view="colors"' in html
