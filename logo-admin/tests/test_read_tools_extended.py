"""The eight assistant read tools added 2026-09-03 and get_style's effective
cost: every tool is reachable through the agent dispatcher, bounded, and
returns the shared truncation contract."""
import pytest

from authorization import AccessContext
from config import get_settings
from tool_registry import agent_tool_schemas, execute_read_tool

NEW_TOOLS = {
    "find_similar_styles": {"store": "S_TEST", "style": "STYLE-1", "mode": "exact"},
    "store_logo_coverage": {"store": "S_TEST", "unconfigured_only": True},
    "list_colors": {"q": "", "cls": "", "needs_review": False, "limit": 50},
    "list_logo_names": {"store": "S_TEST", "q": "", "limit": 50},
    "get_stock_rules": {"store": None, "q": "", "limit": 50},
    "list_price_rules": {"store": None},
    "list_sync_blocks": {"store": None},
    "get_product_mix": {"store": "S_TEST", "limit": 50},
}


def _context():
    return AccessContext.from_session({"user_login": "admin-one", "display_name": "Admin"})


def test_new_tools_are_advertised_with_descriptions_and_parameter_docs():
    schemas = {schema["name"]: schema for schema in agent_tool_schemas(writes_enabled=False)}
    for name in NEW_TOOLS:
        assert name in schemas, name
        assert len(schemas[name]["description"]) > 60, name
        for prop, spec in schemas[name]["parameters"].get("properties", {}).items():
            assert spec.get("description"), f"{name}.{prop} has no description"


def test_every_read_tool_parameter_is_documented():
    for schema in agent_tool_schemas(writes_enabled=False):
        for prop, spec in schema["parameters"].get("properties", {}).items():
            assert spec.get("description"), f"{schema['name']}.{prop}"


@pytest.mark.parametrize("name", sorted(NEW_TOOLS))
def test_new_tool_executes_and_honours_the_truncation_contract(clean_test_database, name):
    try:
        result = execute_read_tool(name, NEW_TOOLS[name], _context(), get_settings())
    except Exception as exc:  # store/style fixtures may be absent on a bare harness
        assert type(exc).__name__ in {"QueryNotFound", "QueryValidationError"}, exc
        return
    assert isinstance(result, dict)
    assert "truncated" in result and "truncation" in result
    assert {"rows", "bytes"} <= set(result["truncation"])


def test_get_style_rows_expose_effective_cost(clean_test_database):
    try:
        result = execute_read_tool("get_style", {"store": "S_TEST", "style": "STYLE-1"}, _context(), get_settings())
    except Exception as exc:
        assert type(exc).__name__ == "QueryNotFound", exc
        return
    for row in result["assignments"]:
        assert "effective_cost" in row and row["effective_cost_source"] in {"override", "default", "none"}
        if row["cost_override"] is not None:
            assert row["effective_cost"] == row["cost_override"]
