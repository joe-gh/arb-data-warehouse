"""Style discovery reads: styles sharing a logo set, and store logo coverage."""

import psycopg2
from fastapi.encoders import jsonable_encoder

from db import database
import queries
from tests.conftest import TEST_ADMIN_DSN


def _seed_discovery_rows():
    """Extra live styles for S_TEST on top of the seed (STYLE-1 RED/BLU/GRN
    with two active rows on RED; STYLE-2 RED with none):
      STYLE-2  gets exactly STYLE-1's logo set (on its only color, RED)
      STYLE-3  (BLU) shares one tuple; scheme is lower-case on purpose
      STYLE-4  (RED) shares one tuple; DESIGN-1 sits at another location
      STYLE-5  (RED, BLU) carries only an INACTIVE row -> unconfigured
    logo.assignment inserts omit row_version (trigger-stamped) and catalog_id."""
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO woo.store_product_state
                    (fdm4_store, catalog_id, sku, kind, style_code, parent_sku,
                     name, status, color_code, color, size_code, size, price,
                     stock, payload, content_hash, is_active)
                VALUES
                    ('S_TEST','S_TEST_catalog','STYLE-3','parent','STYLE-3',NULL,
                     'Style Three','publish',NULL,NULL,NULL,NULL,10,1,'{}'::jsonb,'parent-3',true),
                    ('S_TEST','S_TEST_catalog','STYLE-3-BLU','variation','STYLE-3','STYLE-3',
                     'Style Three Blue','publish','BLU','Blue','M','Medium',10,1,'{}'::jsonb,'variation-5',true),
                    ('S_TEST','S_TEST_catalog','STYLE-4','parent','STYLE-4',NULL,
                     'Style Four','publish',NULL,NULL,NULL,NULL,10,1,'{}'::jsonb,'parent-4',true),
                    ('S_TEST','S_TEST_catalog','STYLE-4-RED','variation','STYLE-4','STYLE-4',
                     'Style Four Red','publish','RED','Red','M','Medium',10,1,'{}'::jsonb,'variation-6',true),
                    ('S_TEST','S_TEST_catalog','STYLE-5','parent','STYLE-5',NULL,
                     'Style Five','publish',NULL,NULL,NULL,NULL,10,1,'{}'::jsonb,'parent-5',true),
                    ('S_TEST','S_TEST_catalog','STYLE-5-RED','variation','STYLE-5','STYLE-5',
                     'Style Five Red','publish','RED','Red','M','Medium',10,1,'{}'::jsonb,'variation-7',true),
                    ('S_TEST','S_TEST_catalog','STYLE-5-BLU','variation','STYLE-5','STYLE-5',
                     'Style Five Blue','publish','BLU','Blue','M','Medium',10,1,'{}'::jsonb,'variation-8',true)
                """
            )
            cursor.execute(
                """
                INSERT INTO logo.assignment
                    (fdm4_store, product_style, garment_color_code, option_row,
                     position, design_id, logo_code, color_scheme_id, location,
                     sort_order, active, updated_by)
                VALUES
                    ('S_TEST','STYLE-2','RED',1,1,'DESIGN-1','C1','SCHEME-1','Left Chest',0,true,'seed'),
                    ('S_TEST','STYLE-2','RED',1,2,'DESIGN-2','C2','SCHEME-2','Right Chest',1,true,'seed'),
                    ('S_TEST','STYLE-3','BLU',1,1,'DESIGN-1','C1','scheme-1','Left Chest',0,true,'seed'),
                    ('S_TEST','STYLE-4','RED',1,1,'DESIGN-1','C1','SCHEME-1','Right Chest',0,true,'seed'),
                    ('S_TEST','STYLE-4','RED',1,2,'DESIGN-2','C2','SCHEME-2','Right Chest',1,true,'seed'),
                    ('S_TEST','STYLE-5','RED',1,1,'DESIGN-1','C1','SCHEME-1','Left Chest',0,false,'seed')
                """
            )


SOURCE_LOGO_SET = [
    {"design_id": "DESIGN-1", "color_scheme_id": "SCHEME-1", "position": 1, "location": "Left Chest"},
    {"design_id": "DESIGN-2", "color_scheme_id": "SCHEME-2", "position": 2, "location": "Right Chest"},
]


def test_exact_mode_returns_only_styles_with_the_identical_logo_set(client_as):
    _seed_discovery_rows()
    client = client_as()
    response = client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-1", "mode": "exact"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == {"style": "STYLE-1", "warehouse_active": True, "logo_set": SOURCE_LOGO_SET}
    assert body["styles"] == [{
        "style": "STYLE-2", "name": "Style Two", "shared": 2,
        "only_in_source": 0, "only_in_target": 0, "warehouse_active": True,
    }]
    assert body["truncated"] is False


def test_overlap_mode_ranks_by_shared_tuples_and_ignores_colors_and_scheme_case(client_as):
    _seed_discovery_rows()
    client = client_as()
    body = client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-1", "mode": "overlap"}).json()
    assert [(r["style"], r["shared"], r["only_in_source"], r["only_in_target"]) for r in body["styles"]] == [
        ("STYLE-2", 2, 0, 0),
        ("STYLE-3", 1, 1, 0),
        ("STYLE-4", 1, 1, 1),
    ]


def test_similar_defaults_validates_and_404s(client_as):
    client = client_as()
    assert client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-1"}).json()["mode"] == "exact"
    assert client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-1", "mode": "fuzzy"}).status_code == 422
    assert client.get("/api/styles/similar", params={"store": "S_NOPE", "style": "STYLE-1"}).status_code == 404
    assert client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-9"}).status_code == 404
    # A live style with no active logos has an empty set and matches nothing.
    empty = client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-2", "mode": "overlap"}).json()
    assert empty["source"]["logo_set"] == [] and empty["styles"] == []


def test_similar_results_are_bounded(client_as, monkeypatch):
    _seed_discovery_rows()
    monkeypatch.setattr(queries, "SIMILAR_STYLE_RESULT_LIMIT", 2)
    client = client_as()
    body = client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-1", "mode": "overlap"}).json()
    assert [r["style"] for r in body["styles"]] == ["STYLE-2", "STYLE-3"]
    assert body["truncated"] is True and body["truncation"]["rows"] is True


def test_coverage_lists_live_colors_without_active_logos(client_as):
    _seed_discovery_rows()
    client = client_as()
    response = client.get("/api/styles/coverage", params={"store": "S_TEST", "unconfigured_only": "true"})
    assert response.status_code == 200, response.text
    assert response.json()["styles"] == [
        {"style": "STYLE-1", "name": "Style One", "colors_total": 3, "colors_configured": 1, "unconfigured": ["BLU", "GRN"]},
        {"style": "STYLE-5", "name": "Style Five", "colors_total": 2, "colors_configured": 0, "unconfigured": ["BLU", "RED"]},
    ]
    everything = client.get("/api/styles/coverage", params={"store": "S_TEST", "unconfigured_only": "false"}).json()
    assert [r["style"] for r in everything["styles"]] == ["STYLE-1", "STYLE-2", "STYLE-3", "STYLE-4", "STYLE-5"]
    assert {r["style"]: r["unconfigured"] for r in everything["styles"]}["STYLE-2"] == []
    assert client.get("/api/styles/coverage", params={"store": "S_NOPE"}).status_code == 404


def test_coverage_default_is_unconfigured_only_and_bounded(client_as, monkeypatch):
    _seed_discovery_rows()
    monkeypatch.setattr(queries, "COVERAGE_STYLE_RESULT_LIMIT", 1)
    client = client_as()
    body = client.get("/api/styles/coverage", params={"store": "S_TEST"}).json()
    assert body["unconfigured_only"] is True
    assert [r["style"] for r in body["styles"]] == ["STYLE-1"]
    assert body["truncated"] is True


def _service(function, **kwargs):
    with database.cursor() as cursor:
        return jsonable_encoder(function(cursor, **kwargs))


def test_discovery_routes_are_thin_wrappers_over_the_query_services(client_as):
    _seed_discovery_rows()
    client = client_as()
    assert client.get("/api/styles/similar", params={"store": "S_TEST", "style": "STYLE-1", "mode": "overlap"}).json() == _service(
        queries.find_similar_styles, fdm4_store="S_TEST", product_style="STYLE-1", mode="overlap"
    )
    assert client.get("/api/styles/coverage", params={"store": "S_TEST"}).json() == _service(
        queries.store_logo_coverage, fdm4_store="S_TEST", unconfigured_only=True
    )
