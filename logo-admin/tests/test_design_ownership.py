"""Design ownership honors FDM4's art-level customer links (logo.design_customer).

Seed (tests/sql/seed.sql): S_TEST's customer number is TEST. DESIGN-1/2 are
TEST's own designs. DESIGN-3 is filed under OTHER but its art ART-3 v2 lists
TEST in fdm4.customer_art_cust (the Lewis 3816 shape). DESIGN-4 belongs to
OTHER only.
"""

from db import database
from design_resolver import design_available_to_store, load_design_index


def test_own_design_is_available():
    with database.cursor() as cursor:
        assert design_available_to_store(cursor, "S_TEST", "DESIGN-1") is True


def test_art_linked_design_is_available():
    with database.cursor() as cursor:
        assert design_available_to_store(cursor, "S_TEST", "DESIGN-3") is True


def test_foreign_design_is_refused():
    with database.cursor() as cursor:
        assert design_available_to_store(cursor, "S_TEST", "DESIGN-4") is False


def test_unowned_design_stays_available():
    with database.cursor() as cursor:
        assert design_available_to_store(cursor, "S_TEST", "B9H-TEST-DESIGN") is True


def test_extra_customer_allowlist_still_works():
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            "UPDATE logo.store_settings SET extra_customers = ARRAY['OTHER'] WHERE fdm4_store = 'S_TEST'"
        )
    with database.cursor() as cursor:
        assert design_available_to_store(cursor, "S_TEST", "DESIGN-4") is True


def test_save_accepts_art_linked_design(client_as):
    client = client_as()
    response = client.put("/api/assignments", json={
        "fdm4_store": "S_TEST", "product_style": "STYLE-1", "garment_color_code": "BLU",
        "option_row": 1, "position": 1, "design_id": "DESIGN-3", "logo_code": "C3",
        "color_scheme_id": "SCHEME-3", "location": "Left Chest", "optional": False,
        "background": "", "cost_override": None, "sort_order": 0, "image_url": "",
        "name_override": None, "active": True,
    })
    assert response.status_code == 200, response.text


def test_save_still_refuses_foreign_design(client_as):
    client = client_as()
    response = client.put("/api/assignments", json={
        "fdm4_store": "S_TEST", "product_style": "STYLE-1", "garment_color_code": "BLU",
        "option_row": 1, "position": 1, "design_id": "DESIGN-4", "logo_code": "C4",
        "color_scheme_id": "SCHEME-4", "location": "Left Chest", "optional": False,
        "background": "", "cost_override": None, "sort_order": 0, "image_url": "",
        "name_override": None, "active": True,
    })
    assert response.status_code == 422, response.text
    assert "different FDM4 customer account" in response.text


def test_design_index_offers_art_linked_design_to_store():
    with database.cursor() as cursor:
        index = load_design_index(cursor)
    assert "DESIGN-3" in index.candidates("S_TEST", "C3", "SCHEME-3")
    assert "DESIGN-4" not in index.candidates("S_TEST", "C4", "SCHEME-4")


def test_unowned_design_with_linked_art_stays_wildcard_for_every_store():
    # DESIGN-5 has no design-level owner; its art is linked to OTHER only.
    with database.cursor() as cursor:
        assert design_available_to_store(cursor, "S_EMPTY", "DESIGN-5") is True
        index = load_design_index(cursor)
    assert "DESIGN-5" in index.candidates("S_EMPTY", "C5", "SCHEME-5")
    assert "DESIGN-5" in index.candidates("S_TEST", "C5", "SCHEME-5")


def test_browse_lists_art_linked_design_for_store(client_as):
    client = client_as()
    body = client.get("/api/designs", params={"store": "S_TEST", "q": ""}).json()
    ids = {row["design_id"] for row in body["designs"]}
    assert "DESIGN-3" in ids
    assert "DESIGN-4" not in ids


def test_find_issues_reports_design_conflicts(client_as):
    client = client_as()
    # STYLE-2/RED gets DESIGN-3 at Left Chest / C1 / SCHEME-1... but seed row
    # STYLE-1/RED 1/1 is DESIGN-1 / C1 / SCHEME-1 / Left Chest, so give the
    # same key a second design on another style.
    from db import database
    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            """INSERT INTO logo.assignment (fdm4_store, product_style, garment_color_code, option_row, position,
                   design_id, logo_code, color_scheme_id, location, optional, background, cost_override,
                   sort_order, image_url, name_override, active, updated_by)
               VALUES ('S_TEST', 'STYLE-2', 'RED', 1, 1, 'DESIGN-3', 'C1', 'SCHEME-1', 'LEFT CHEST',
                       false, '', NULL, 0, '', NULL, true, 'fixture')"""
        )
    body = client.get("/api/issues", params={"store": "S_TEST", "checks": "design_conflicts"}).json()
    check = [c for c in body["checks"] if c["check"] == "design_conflicts"][0]
    assert check["available"] is True and check["count"] == 1
    sample = check["sample"][0]
    assert sample["logo_code"] == "C1" and sample["scheme"] == "SCHEME-1"
    assert sample["location"] == "left chest" and sample["designs"] == "DESIGN-1, DESIGN-3"


def test_design_conflicts_still_sees_design_level_ownership(client_as):
    """Finding I4: the check's "is this design ours" gate must be the same rule
    WordPress applies - the art-level link table OR the old design-level owner.
    With logo.design_customer empty (a stale or not-yet-refreshed ETL), the
    dec_design branch alone must still report the conflict."""
    from tests.test_rule_agent_tools import _admin

    with database.cursor(write=True, actor="fixture") as cursor:
        cursor.execute(
            """INSERT INTO logo.assignment (fdm4_store, product_style, garment_color_code, option_row, position,
                   design_id, logo_code, color_scheme_id, location, optional, background, cost_override,
                   sort_order, image_url, name_override, active, updated_by)
               VALUES ('S_TEST', 'STYLE-2', 'RED', 1, 1, 'DESIGN-2', 'C1', 'SCHEME-1', 'LEFT CHEST',
                       false, '', NULL, 0, '', NULL, true, 'fixture')"""
        )
    _admin("DELETE FROM logo.design_customer")
    assert _admin("SELECT count(*) AS n FROM logo.design_customer")[0][0] == 0

    body = client_as().get("/api/issues", params={"store": "S_TEST", "checks": "design_conflicts"}).json()
    check = [c for c in body["checks"] if c["check"] == "design_conflicts"][0]
    assert check["available"] is True and check["count"] == 1, check
    sample = check["sample"][0]
    assert sample["logo_code"] == "C1" and sample["scheme"] == "SCHEME-1"
    assert sample["designs"] == "DESIGN-1, DESIGN-2"


def test_the_index_sql_rowset_does_not_grow_with_allowed_customers():
    """Finding I2: art-level customers are expanded in Python, not joined into
    DESIGN_INDEX_SQL. DESIGN-3 has two allowed customers (OTHER + TEST via
    customer_art_cust) but only one cust_art_file row, so the index SQL must
    still return exactly one row for it - otherwise the rowset is art files x
    customers and production (52,644 art rows, 1,882 multi-customer designs)
    blows through MAX_DESIGN_INDEX_ROWS."""
    from design_resolver import DESIGN_INDEX_SQL

    with database.cursor() as cursor:
        cursor.execute(
            "SELECT count(DISTINCT cust_number) AS n FROM logo.design_customer"
            " WHERE design_id = 'DESIGN-3'"
        )
        assert cursor.fetchone()["n"] == 2
        cursor.execute(
            "SELECT count(*) AS n FROM fdm4.cust_art_file caf"
            "  JOIN fdm4.design_pool dp ON btrim(dp.art_id) = btrim(caf.art_id)"
            " WHERE btrim(dp.design_id) = 'DESIGN-3'"
        )
        art_rows = cursor.fetchone()["n"]
        assert art_rows == 1
        cursor.execute(
            f"SELECT count(*) AS n FROM ({DESIGN_INDEX_SQL}) i WHERE i.design_id = 'DESIGN-3'"
        )
        assert cursor.fetchone()["n"] == art_rows
