from fastapi.encoders import jsonable_encoder

from db import database
import queries


def _service(function, **kwargs):
    with database.cursor() as cursor:
        return jsonable_encoder(function(cursor, **kwargs))


def test_store_contract(client_as):
    client = client_as()
    assert client.get("/api/stores").json() == _service(queries.list_stores)


def test_style_contracts(client_as):
    client = client_as()
    assert client.get("/api/styles", params={"store": "S_TEST"}).json() == _service(
        queries.list_styles, store="S_TEST"
    )
    assert client.get("/api/style", params={"store": "S_TEST", "style": "STYLE-1"}).json() == _service(
        queries.get_style, store="S_TEST", style="STYLE-1"
    )


def test_design_contracts(client_as):
    client = client_as()
    assert client.get("/api/designs", params={"q": "DESIGN-1"}).json() == _service(
        queries.search_designs, q="DESIGN-1", store=None
    )
    assert client.get("/api/designs/DESIGN-1").json() == _service(
        queries.get_design,
        design_id="DESIGN-1",
        fdm4_art_base="https://media.example.test/fdm4/",
    )
    assert client.get("/api/vocab").json() == _service(
        queries.get_assignment_vocab
    )


def test_settings_and_report_contracts(client_as):
    client = client_as()
    assert client.get("/api/settings/S_TEST").json() == _service(
        queries.get_store_settings, store="S_TEST"
    )
    assert client.get("/api/import-report").json() == _service(
        queries.get_import_report
    )
    assert client.get("/api/audit-log").json() == _service(
        queries.get_audit_log
    )


def test_pricing_contracts(client_as):
    client = client_as()
    assert client.get("/api/pricing/tiers").json() == _service(
        queries.list_pricing_tiers
    )
    assert client.get("/api/pricing/store-tiers").json() == _service(
        queries.list_store_pricing_tiers
    )
