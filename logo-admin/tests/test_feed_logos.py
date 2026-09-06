"""/feed/logos publishes a resolved artwork or none at all.

An FDM4 design can carry several decorations, each with its own artwork at its
own placement. The feed used to take the first pool row for the design (so a
sleeve logo could be published with the left-chest artwork) and, when the
design had no pool row, published the DESIGN id in the art_id field. Design ids
and art ids share one number space across customers, so that could hand a
consumer another company's artwork number.

Seed facts (tests/sql/seed.sql): DESIGN-1 has exactly one pool row (art
ART-9001, which is also another customer's design id); DESIGN-2 has none.
"""

import pytest
from fastapi.testclient import TestClient

import main
from tests.test_rule_agent_tools import _admin

TOKEN = "feed-test-token"
STORE = "S_ARTFEED"
MULTI_DESIGN = "DESIGN-MULTI"


@pytest.fixture
def feed_client():
    client = TestClient(main.app)
    yield client
    client.close()


def _get(client, path, **params):
    return client.get(
        path,
        headers={"Authorization": f"Bearer {TOKEN}"},
        params=params or None,
    )


@pytest.fixture
def multi_art_design():
    """One design, two decorations: left chest and bicep left sleeve, each
    with its own artwork. design_pool_num is deliberately out of text order so
    the numeric ordering is exercised."""

    _admin(
        "INSERT INTO fdm4.design_pool"
        " (design_pool_num, design_id, art_id, art_version_id, location_id)"
        " VALUES ('10', %s, 'ART-LC', '1', 'LC'),"
        "        ('2',  %s, 'ART-BLS', '2', 'BLS')",
        (MULTI_DESIGN, MULTI_DESIGN),
    )
    _admin(
        "INSERT INTO logo.assignment"
        " (fdm4_store, product_style, garment_color_code, option_row, position,"
        "  design_id, logo_code, color_scheme_id, location, updated_by)"
        " VALUES (%s, 'ART-STYLE', '', 1, 1, %s, 'CX', 'SCH',"
        "         'Left Chest', 'fixture'),"
        "        (%s, 'ART-STYLE', '', 2, 1, %s, 'CX', 'SCH',"
        "         'Bicep Left Sleeve', 'fixture'),"
        "        (%s, 'ART-STYLE', '', 3, 1, %s, 'CX', 'SCH',"
        "         'Full Back', 'fixture')",
        (STORE, MULTI_DESIGN, STORE, MULTI_DESIGN, STORE, MULTI_DESIGN),
    )
    yield
    # fdm4 rows survive tests/sql/reset.sql, so this fixture cleans up its own.
    _admin(
        "DELETE FROM fdm4.design_pool WHERE btrim(design_id) = %s",
        (MULTI_DESIGN,),
    )


def _by_option_row(client, store):
    rows = _get(client, "/feed/logos", store=store, limit=1000).json()["rows"]
    return {row["option_row"]: row["payload"] for row in rows}


def test_each_placement_gets_its_own_artwork(feed_client, multi_art_design):
    payloads = _by_option_row(feed_client, STORE)
    assert payloads[1]["art_id"] == "ART-LC.1"
    assert payloads[1]["art_unresolved"] is False
    assert payloads[2]["art_id"] == "ART-BLS.2"
    assert payloads[2]["art_unresolved"] is False


def test_an_unlisted_placement_leaves_the_artwork_unresolved(
    feed_client, multi_art_design,
):
    payloads = _by_option_row(feed_client, STORE)
    # The design has no decoration on the full back and the logo code matches
    # neither artwork, so there is no answer - and a guess would be worse.
    assert "art_id" not in payloads[3]
    assert payloads[3]["art_unresolved"] is True


def test_the_logo_code_resolves_a_placement_the_design_does_not_list(
    feed_client, multi_art_design,
):
    _admin(
        "INSERT INTO fdm4.cust_art_file (art_id, source_path)"
        " VALUES ('ART-BLS', 'CX_SCH.eps')"
    )
    try:
        payloads = _by_option_row(feed_client, STORE)
        assert payloads[3]["art_id"] == "ART-BLS.2"
        assert payloads[3]["art_unresolved"] is False
    finally:
        _admin(
            "DELETE FROM fdm4.cust_art_file"
            " WHERE btrim(art_id) = 'ART-BLS' AND btrim(source_path) = 'CX_SCH.eps'"
        )


def test_a_single_artwork_design_resolves_at_any_placement(feed_client):
    # S_TEST position 1 is DESIGN-1, whose pool holds one artwork.
    payloads = {
        row["position"]: row["payload"]
        for row in _get(feed_client, "/feed/logos", store="S_TEST").json()["rows"]
    }
    assert payloads[1]["art_id"] == "ART-9001"
    assert payloads[1]["art_unresolved"] is False


def test_a_design_with_no_pool_row_is_unresolved_not_its_own_id(feed_client):
    # DESIGN-2 has no fdm4.design_pool row at all. The old feed published
    # 'DESIGN-2' as the artwork id.
    payloads = {
        row["position"]: row["payload"]
        for row in _get(feed_client, "/feed/logos", store="S_TEST").json()["rows"]
    }
    assert "art_id" not in payloads[2]
    assert payloads[2]["art_unresolved"] is True


def test_no_payload_ever_publishes_a_design_id_as_an_artwork(
    feed_client, multi_art_design,
):
    rows = _get(feed_client, "/feed/logos", limit=5000).json()["rows"]
    assert rows
    for row in rows:
        payload = row["payload"]
        if "art_id" in payload and "design_id" in payload:
            assert payload["art_id"] != payload["design_id"]
