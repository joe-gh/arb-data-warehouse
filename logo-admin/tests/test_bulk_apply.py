"""Integration tests for compute_bulk_preview (read-only bulk-apply dry-run).

Requires a provisioned test database (TEST_DATABASE_DSN + TEST_DATABASE_ADMIN_DSN).
Run via: cd logo-admin && python -m pytest tests/test_bulk_apply.py -v

FDM4 fixture rows for B9H-TEST-DESIGN / WH are in tests/sql/seed.sql (inserted
via the admin DSN, because logo_admin has only SELECT on fdm4 tables).
reset.sql cleans them between tests along with DESIGN-1 / DESIGN-2.
"""

from db import database
import queries


# Stable design_id seeded in tests/sql/seed.sql for B9H / WH resolution.
_B9H_DESIGN_ID = "B9H-TEST-DESIGN"


# ---------------------------------------------------------------------------
# Store / color-class fixture (seeded via logo_admin write cursor)
# ---------------------------------------------------------------------------

def _seed_store(cur):
    """Seed two active variations for store S_BULK, style ST1, colors 0001/0002.

    No woo.store_catalog row is inserted: logo_admin may lack INSERT on
    woo.store_catalog, and compute_bulk_preview queries woo.store_product_state
    directly (there is no blocking FK from store_product_state to store_catalog).
    """
    cur.execute("SET LOCAL logo.actor='t'")
    for code, name in [("0001", "Navy"), ("0002", "White")]:
        cur.execute(
            """
            INSERT INTO woo.store_product_state
                (fdm4_store, catalog_id, sku, kind, style_code, color_code,
                 color, is_active, payload, content_hash)
            VALUES ('S_BULK', 'S_BULK_catalog', %s, 'variation', 'ST1',
                    %s, %s, true, '{}', %s)
            ON CONFLICT DO NOTHING
            """,
            (f"sku-{code}", code, name, f"h-{code}"),
        )
    cur.execute(
        """
        INSERT INTO logo.color_class (color_code, color_name, light_dark, source)
        VALUES ('0001', 'Navy', 'dark', 'manual'),
               ('0002', 'White', 'light', 'manual')
        ON CONFLICT (color_code) DO UPDATE
            SET light_dark = EXCLUDED.light_dark,
                source = EXCLUDED.source
        """
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_preview_light_dark_targets_matching_colors():
    """light_dark='dark' filter returns only Navy (0001), not White (0002)."""
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
    with database.cursor() as cur:
        res = queries.compute_bulk_preview(
            cur,
            fdm4_store="S_BULK",
            logo_code="B9H",
            color_scheme="WH",
            target={"mode": "light_dark", "class": "dark"},
        )
        assert not res.get("unresolved_reason"), res
        rows = res["rows"]
        assert {r["color_code"] for r in rows} == {"0001"}
        assert rows[0]["new"]["logo_code"] == "B9H"
        assert rows[0]["new"]["color_scheme"] == "WH"
        assert rows[0]["new"]["design_id"] == _B9H_DESIGN_ID
        assert rows[0]["was"] is None  # no existing assignment for this color


def test_preview_light_dark_excludes_light_colors():
    """Complement: light_dark='light' returns White (0002), not Navy (0001)."""
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
    with database.cursor() as cur:
        res = queries.compute_bulk_preview(
            cur,
            fdm4_store="S_BULK",
            logo_code="B9H",
            color_scheme="WH",
            target={"mode": "light_dark", "class": "light"},
        )
        assert not res.get("unresolved_reason"), res
        assert {r["color_code"] for r in res["rows"]} == {"0002"}


def test_preview_colors_mode():
    """mode='colors' with explicit color_codes returns only those colors."""
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
    with database.cursor() as cur:
        res = queries.compute_bulk_preview(
            cur,
            fdm4_store="S_BULK",
            logo_code="B9H",
            color_scheme="WH",
            target={"mode": "colors", "color_codes": ["0001"]},
        )
        assert not res.get("unresolved_reason"), res
        assert {r["color_code"] for r in res["rows"]} == {"0001"}


def test_preview_style_codes_filter():
    """style_codes narrows results to matching styles only."""
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
        # Add a second style to make the filter meaningful.
        cur.execute(
            """
            INSERT INTO woo.store_product_state
                (fdm4_store, catalog_id, sku, kind, style_code, color_code,
                 color, is_active, payload, content_hash)
            VALUES ('S_BULK', 'S_BULK_catalog', 'sku-st2-0001', 'variation',
                    'ST2', '0001', 'Navy', true, '{}', 'h-st2-0001')
            ON CONFLICT DO NOTHING
            """
        )
    with database.cursor() as cur:
        res = queries.compute_bulk_preview(
            cur,
            fdm4_store="S_BULK",
            logo_code="B9H",
            color_scheme="WH",
            target={"mode": "light_dark", "class": "dark"},
            style_codes=["ST1"],
        )
        assert not res.get("unresolved_reason"), res
        assert all(r["style_code"] == "ST1" for r in res["rows"])


def test_preview_unresolved_variant():
    """Unknown logo_code returns unresolved_reason, not an exception."""
    with database.cursor() as cur:
        res = queries.compute_bulk_preview(
            cur,
            fdm4_store="S_BULK",
            logo_code="ZZZ_NOPE",
            color_scheme="WH",
            target={"mode": "light_dark", "class": "dark"},
        )
        assert res.get("unresolved_reason")
        assert res["counts"]["total"] == 0
        assert res["rows"] == []


def test_preview_invalid_mode_raises():
    """Invalid target.mode raises ValueError immediately."""
    import pytest

    with database.cursor() as cur:
        with pytest.raises(ValueError, match="target.mode"):
            queries.compute_bulk_preview(
                cur,
                fdm4_store="S_BULK",
                logo_code="B9H",
                color_scheme="WH",
                target={"mode": "bad"},
            )


def test_preview_was_populated_when_assignment_exists():
    """'was' field is non-None when an existing active assignment overlaps."""
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
        # Seed a pre-existing assignment on ST1/0001 at option_row=1, position=1.
        # DESIGN-1 / C1 / SCHEME-1 are in seed.sql so they exist at test time.
        cur.execute(
            """
            INSERT INTO logo.assignment
                (fdm4_store, product_style, garment_color_code, option_row, position,
                 design_id, logo_code, color_scheme_id, location, optional,
                 background, cost_override, sort_order, image_url, active, updated_by)
            VALUES ('S_BULK', 'ST1', '0001', 1, 1,
                    'DESIGN-1', 'C1', 'SCHEME-1', 'Left Chest', false,
                    '', NULL, 0, '', true, 'seed')
            ON CONFLICT DO NOTHING
            """
        )
    with database.cursor() as cur:
        res = queries.compute_bulk_preview(
            cur,
            fdm4_store="S_BULK",
            logo_code="B9H",
            color_scheme="WH",
            target={"mode": "light_dark", "class": "dark"},
        )
        assert not res.get("unresolved_reason"), res
        dark_rows = [r for r in res["rows"] if r["color_code"] == "0001"]
        assert dark_rows, "Expected at least one dark row for 0001"
        assert dark_rows[0]["was"] == {"logo_code": "C1", "color_scheme": "SCHEME-1"}


def test_preview_route(client_as):
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
    r = client_as("admin-one").post("/api/bulk-apply/preview", json={
        "fdm4_store": "S_BULK", "logo_code": "B9H", "color_scheme": "WH",
        "target": {"mode": "light_dark", "class": "dark"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["total"] == 1 and body["rows"][0]["color_code"] == "0001"


def test_execute_writes_and_snapshots(client_as):
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
    r = client_as("admin-one").post("/api/bulk-apply/execute", json={
        "fdm4_store": "S_BULK", "logo_code": "B9H", "color_scheme": "WH", "placement": "Left Chest",
        "rows": [{"style_code": "ST1", "color_code": "0001"}]})
    assert r.status_code == 200, r.text
    batch_id = r.json()["batch_id"]
    with database.cursor() as cur:
        cur.execute("""SELECT logo_code,color_scheme_id FROM logo.assignment
                        WHERE fdm4_store='S_BULK' AND product_style='ST1'
                          AND garment_color_code='0001' AND option_row=1 AND position=1 AND active""")
        row = cur.fetchone(); assert row["logo_code"]=="B9H" and row["color_scheme_id"]=="WH"
        cur.execute("SELECT before_row FROM logo.bulk_batch_row WHERE batch_id=%s", (batch_id,))
        assert cur.fetchone()["before_row"] is None


def test_undo_restores_prior_state(client_as):
    c = client_as("admin-one")
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
    batch = c.post("/api/bulk-apply/execute", json={"fdm4_store":"S_BULK","logo_code":"B9H",
        "color_scheme":"WH","placement":"Left Chest","rows":[{"style_code":"ST1","color_code":"0001"}]}).json()["batch_id"]
    assert c.post("/api/bulk-apply/undo", json={"batch_id": batch}).status_code == 200
    with database.cursor() as cur:
        cur.execute("""SELECT count(*) n FROM logo.assignment WHERE fdm4_store='S_BULK'
                        AND product_style='ST1' AND garment_color_code='0001'
                        AND option_row=1 AND position=1 AND active""")
        assert cur.fetchone()["n"] == 0  # inserted then undone -> gone


def test_undo_already_undone_409(client_as):
    c = client_as("admin-one")
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)
    batch = c.post("/api/bulk-apply/execute", json={"fdm4_store":"S_BULK","logo_code":"B9H",
        "color_scheme":"WH","placement":"Left Chest","rows":[{"style_code":"ST1","color_code":"0001"}]}).json()["batch_id"]
    assert c.post("/api/bulk-apply/undo", json={"batch_id": batch}).status_code == 200
    assert c.post("/api/bulk-apply/undo", json={"batch_id": batch}).status_code == 409


def test_dashboard_has_bulk_panel(client_as):
    html = client_as("admin-one").get("/").text
    assert 'id="bulk-apply-panel"' in html


def test_mcp_has_bulk_and_color_tools():
    import mcp_server
    names = set(mcp_server.tool_names())
    assert {"list_colors", "set_color_class", "bulk_apply_preview",
            "bulk_apply_execute", "bulk_apply_undo"} <= names


def test_end_to_end_two_pass(client_as):
    """Two independent bulk-apply passes (dark garment, then light garment),
    verify both landed, then undo only the second pass and confirm its rows
    are gone while the first pass survives.

    NOTE: the plan's original sketch used two color schemes (WH -> dark, BK ->
    light), but only the B9H / WH variant has FDM4 art seeded (tests/sql/seed.sql
    seeds B9H_WH.png only), so BK would never resolve to a design and execute
    would 400. The Mariani-style two-pass intent - one dark garment color, one
    light garment color, verify + undo one pass - is preserved by keeping both
    passes on the resolvable B9H / WH variant and partitioning by garment color:
    pass 1 targets the dark color 0001 (Navy), pass 2 the light color 0002
    (White). The two passes are distinguished by garment_color_code and by their
    separate batch ids.
    """
    c = client_as("admin-one")
    with database.cursor(write=True, actor="t") as cur:
        _seed_store(cur)  # 0001 Navy=dark, 0002 White=light

    # Pass 1: apply onto the dark garment color (0001).
    dark = c.post("/api/bulk-apply/execute", json={
        "fdm4_store": "S_BULK", "logo_code": "B9H", "color_scheme": "WH",
        "placement": "Left Chest",
        "rows": [{"style_code": "ST1", "color_code": "0001"}]})
    assert dark.status_code == 200, dark.text
    # Pass 2: apply onto the light garment color (0002).
    light = c.post("/api/bulk-apply/execute", json={
        "fdm4_store": "S_BULK", "logo_code": "B9H", "color_scheme": "WH",
        "placement": "Left Chest",
        "rows": [{"style_code": "ST1", "color_code": "0002"}]})
    assert light.status_code == 200, light.text
    light_batch = light.json()["batch_id"]

    # Both assignments landed with the resolved variant.
    with database.cursor() as cur:
        cur.execute(
            """SELECT garment_color_code, logo_code, color_scheme_id
                 FROM logo.assignment
                WHERE fdm4_store='S_BULK' AND active
                ORDER BY garment_color_code""")
        got = {r["garment_color_code"]: (r["logo_code"], r["color_scheme_id"])
               for r in cur.fetchall()}
        assert got == {"0001": ("B9H", "WH"), "0002": ("B9H", "WH")}

    # Undo only the second pass (light garment) and confirm its row is gone
    # while the first pass (dark garment) survives.
    assert c.post("/api/bulk-apply/undo",
                  json={"batch_id": light_batch}).status_code == 200
    with database.cursor() as cur:
        cur.execute(
            """SELECT count(*) n FROM logo.assignment
                WHERE fdm4_store='S_BULK' AND garment_color_code='0002' AND active""")
        assert cur.fetchone()["n"] == 0
        cur.execute(
            """SELECT count(*) n FROM logo.assignment
                WHERE fdm4_store='S_BULK' AND garment_color_code='0001' AND active""")
        assert cur.fetchone()["n"] == 1
