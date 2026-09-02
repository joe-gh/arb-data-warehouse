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


def test_design_search_carries_scheme_options(client_as):
    """Bulk Apply builds its logo dropdown from search results alone: each
    design must carry its (color scheme, logo code) pairs or no scheme is
    ever selectable. The pair stays linked because an assignment stores the
    scheme's own logo code (C1 for SCHEME-1), not just any code the design
    owns."""
    client = client_as()

    designs = client.get("/api/designs", params={"q": "DESIGN-1"}).json()["designs"]
    by_id = {d["design_id"]: d for d in designs}
    assert by_id["DESIGN-1"]["schemes"] == [
        {"color_scheme_id": "SCHEME-1", "logo_code": "C1"}
    ]

    # Art linked via design_pool only (newer designs) resolves the same way.
    designs = client.get("/api/designs", params={"q": "DESIGN-2"}).json()["designs"]
    by_id = {d["design_id"]: d for d in designs}
    assert by_id["DESIGN-2"]["schemes"] == [
        {"color_scheme_id": "SCHEME-2", "logo_code": "C2"}
    ]

    # A design with no usable art rows still returns a list, never null.
    designs = client.get("/api/designs", params={"q": "ART-9001"}).json()["designs"]
    by_id = {d["design_id"]: d for d in designs}
    assert by_id["ART-9001"]["schemes"] == []
