"""HTTP routes (MCP's transport) and the assistant run the same handlers:
the routes below execute mutations.* commands through _execute_mutation and
the MCP server exposes a tool for every one of them."""

import psycopg2
import pytest

from tests.conftest import TEST_ADMIN_DSN
import wp_bridge


def _admin(sql, params=()):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else None


def test_logo_name_route_writes_store_specific_locked_row(client_as):
    client = client_as()
    response = client.put("/api/logo-names", json={"design_id": "DESIGN-1", "color_scheme_id": "scheme-1", "name": "Route name", "fdm4_store": "S_TEST"})
    assert response.status_code == 200, response.text
    assert response.json()["name"]["store"] == "S_TEST"
    assert _admin("SELECT name, source, locked FROM logo.display_name WHERE design_id='DESIGN-1' AND fdm4_store='S_TEST'") == [("Route name", "manual", True)]
    assert client.put("/api/logo-names", json={"design_id": "NOPE", "color_scheme_id": "X", "name": "n"}).status_code == 404


def test_stock_override_and_brand_rule_routes(client_as):
    client = client_as()
    _admin("DELETE FROM woo.stock_override WHERE style_code='STYLE-1'")
    body = client.put("/api/stock-overrides", json={"style_code": "style-1", "mode": "fake", "note": "n"}).json()
    assert body["style_code"] == "STYLE-1" and body["variants"] == 3
    assert client.put("/api/stock-overrides", json={"style_code": "NO-SUCH", "mode": "fake"}).status_code == 404
    assert client.delete("/api/stock-overrides", params={"style": "STYLE-1"}).json() == {"ok": True}
    assert client.delete("/api/stock-overrides", params={"style": "STYLE-1"}).status_code == 404
    _admin("DELETE FROM woo.brand_stock_rule WHERE mill_code='T-MILL'")
    _admin('DELETE FROM fdm4.mill WHERE "mill-code" = %s', ("T-MILL",))
    _admin('INSERT INTO fdm4.mill ("mill-code", description) VALUES (%s, %s)', ("T-MILL", "Test Mill"))
    body = client.put("/api/stock-overrides/brands", json={"mill_code": "T-MILL", "mode": "real"}).json()
    assert body["brand_name"] == "Test Mill" and body["mode"] == "real"
    assert client.delete("/api/stock-overrides/brands", params={"mill": "T-MILL"}).json() == {"ok": True}
    assert client.put("/api/stock-overrides/brands", json={"mill_code": "999", "mode": "fake"}).status_code == 404


def test_sync_block_routes_keep_their_validation_and_shape(client_as):
    client = client_as()
    _admin("DELETE FROM woo.sync_exclusion WHERE fdm4_store='S_TEST'")
    assert client.put("/api/sync-blocks", json={"fdm4_store": "S_TEST", "whole_store": True, "styles": ["STYLE-1"]}).status_code == 400
    body = client.put("/api/sync-blocks", json={"fdm4_store": "S_TEST", "whole_store": False, "styles": ["style-1", "STYLE-2"], "note": "hold"}).json()
    assert body["saved"] == 2 and body["per_style"] == [{"style": "STYLE-1", "products": 3}, {"style": "STYLE-2", "products": 1}]
    assert client.put("/api/sync-blocks", json={"fdm4_store": "S_NOPE", "whole_store": True, "scope": "pricing"}).status_code == 404
    assert client.delete("/api/sync-blocks", params={"store": "S_TEST", "style": "STYLE-1"}).json() == {"ok": True}
    assert _admin("SELECT style_code FROM woo.sync_exclusion WHERE fdm4_store='S_TEST'") == [("STYLE-2",)]
    _admin("DELETE FROM woo.sync_exclusion WHERE fdm4_store='S_TEST'")


def test_price_rule_toggle_keeps_preview_required_contract_and_delete(client_as):
    client = client_as()
    rule_id = _admin("SELECT rule_id FROM woo.price_rule WHERE name='Fixture inactive rule'")[0][0]
    blocked = client.put("/api/price-rules/toggle", json={"rule_id": rule_id, "active": True})
    assert blocked.status_code == 409 and blocked.json()["error"] == "preview_required"
    _admin("UPDATE woo.price_rule SET last_previewed_at = now() WHERE rule_id = %s", (rule_id,))
    assert client.put("/api/price-rules/toggle", json={"rule_id": rule_id, "active": True}).json() == {"ok": True, "rule_id": rule_id, "active": True}
    assert client.delete("/api/price-rules", params={"rule_id": rule_id}).json() == {"ok": True}
    assert client.delete("/api/price-rules", params={"rule_id": rule_id}).status_code == 404


def test_new_shared_routes(client_as, monkeypatch):
    client = client_as()
    cost = client.post("/api/assignments/logo-cost", json={"store": "S_TEST", "design_id": "DESIGN-1", "cost_override": "4.25", "styles": ["STYLE-1"]}).json()
    assert cost["updated"] == 1
    assert client.put("/api/settings/S_TEST/extra-customers", json={"customers": ["OTHER"]}).json()["settings"]["extra_customers"] == ["OTHER"]
    assert client.put("/api/settings/S_TEST/extra-customers", json={"customers": ["NOBODY"]}).status_code == 404
    assert client.put("/api/default-costs", json={"logo_code": "C1", "color_scheme_id": "SCHEME-1", "cost": "2.50"}).json()["cost"] == "2.50"
    monkeypatch.setattr(wp_bridge, "logo_ownership", lambda: {"stores": [{"fdm4_store": "S_TEST", "blog_id": 3, "owned": False}], "owned_blogs": []})
    status = client.get("/api/sync-status", params={"store": "S_TEST"}).json()
    assert status["logo_sync_ownership"]["owned"] is False and "pipeline" in status
    usage = client.get("/api/design-usage", params={"store": "S_TEST", "design_id": "DESIGN-2"}).json()
    assert usage["style_codes"] == ["STYLE-1"]


def test_mcp_exposes_a_tool_for_every_shared_write_and_read():
    import mcp_server
    names = set(mcp_server.tool_names())
    for tool in ("get_sync_status", "list_design_usage", "set_logo_cost", "set_store_extra_customers",
                 "set_logo_default_cost", "set_price_rule_active", "delete_price_rule", "list_price_rules",
                 "set_stock_override", "remove_stock_override", "set_brand_stock_rule", "remove_brand_stock_rule",
                 "set_sync_block", "remove_sync_block", "set_product_mix", "disable_product_mix",
                 "add_mix_styles", "remove_mix_styles", "set_logo_name", "bulk_apply_execute", "get_product_link"):
        assert tool in names, tool
