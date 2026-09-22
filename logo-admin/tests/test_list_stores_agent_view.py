"""The assistant's list_stores is compact and can filter by name or code."""

from tests.test_rule_agent_tools import _admin
from tests.test_warehouse_ops_tools import _read

COMPACT_KEYS = {
    "fdm4_store", "display_name", "blog_id", "enabled", "allows_none",
    "assigned_styles", "assignment_count", "products",
}


def test_agent_store_rows_are_compact():
    result = _read("list_stores", {})
    assert result["stores"], "the harness seeds at least one store"
    for row in result["stores"]:
        assert set(row) == COMPACT_KEYS


def test_agent_store_list_filters_by_name_or_code():
    _admin("DELETE FROM woo.store_blog_map WHERE fdm4_store='S_TEST'")
    _admin("INSERT INTO woo.store_blog_map (blog_id,fdm4_store,blog_path,blog_name) VALUES (900102,'S_TEST','/ops-test/','Ops Test Store')")
    try:
        by_name = _read("list_stores", {"q": "ops test"})
        assert [row["fdm4_store"] for row in by_name["stores"]] == ["S_TEST"]
        assert by_name["stores"][0]["display_name"] == "Ops Test Store"
        by_code = _read("list_stores", {"q": "s_test"})
        assert {row["fdm4_store"] for row in by_code["stores"]} == {"S_TEST"}
        assert _read("list_stores", {"q": "no such store anywhere"})["stores"] == []
        assert len(_read("list_stores", {"q": None})["stores"]) >= 1
    finally:
        _admin("DELETE FROM woo.store_blog_map WHERE blog_id=900102")
