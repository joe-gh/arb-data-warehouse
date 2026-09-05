"""Warehouse tools share route behavior, reviewed impact, and exact undo."""

from types import SimpleNamespace
from decimal import Decimal

import pytest
from pydantic import ValidationError
from fastapi.encoders import jsonable_encoder

import categories_draft
import categories_service
import mutations
import queries
import staging
from authorization import AccessContext
from commands import SavePriceRuleCommand
from db import database
from domain import Conflict, InvalidCommand, NotFound, PreviewDrift
from mutations import MutationScope
from snapshots import snapshot_scopes, states_equal
from tests.test_rule_agent_tools import _admin, _session, _snapshot, _round_trip, USER
from tool_registry import execute_read_tool, execute_agent_tool, UnknownTool

CONTEXT = AccessContext(USER, USER)
SETTINGS = SimpleNamespace(catmgr_enabled=True, catmgr_view_users=frozenset({USER}), agent_writes_enabled=True)


def _rule():
    return _admin("SELECT rule_id FROM woo.price_rule WHERE name='Fixture inactive rule'")[0][0]


def _stage(tool, args):
    change_set = staging.new_change_set(_session(), USER)
    return staging.stage_write(change_set["id"], tool, args, "ops-call", USER, max_items=50)


def _apply(staged):
    return staging.apply_change_set(staged["id"], USER, revision=staged["revision"], confirmed_hash=staged["preview_hash"], acknowledge_hard_delete=False)


def _read(name, args):
    return execute_read_tool(name, args, CONTEXT, SETTINGS)


@pytest.mark.parametrize("active", [False, True])
def test_new_price_rule_stages_without_leaking_and_undo_deletes(active):
    args = {"name": "New price", "effect_type": "percent", "effect_value": "10", "stores": ["s_test"], "active": active}
    staged = _stage("save_price_rule", args)
    rid = staged["preview_results"][0]["rule_id"]
    scopes = (MutationScope("price_rule_row", {"rule_id": rid}),)
    before = _snapshot(scopes)
    assert _admin("SELECT count(*) FROM woo.price_rule WHERE rule_id=%s", (rid,)) == [(0,)]
    retried = staging.stage_write(staged["id"], "save_price_rule", args, "ops-call", USER, max_items=50)
    assert retried["idempotent"] is True
    refreshed = staging.refresh_change_set(staged["id"], USER)
    assert refreshed["affected_scopes"] == staged["affected_scopes"]
    _apply(refreshed)
    assert _admin("SELECT active, stores, last_previewed_at IS NOT NULL FROM woo.price_rule WHERE rule_id=%s", (rid,)) == [(active, ["S_TEST"], active)]
    if active:
        assert staged["preview_diff"]["price_rule_impacts"][0]["summary"]["affected"] == 4
    staging.undo_change_set(staged["id"], USER)
    assert states_equal(_snapshot(scopes), before)


@pytest.mark.parametrize("active", [False, True])
def test_edit_price_rule_restores_exact_prior_row(active):
    rid = _rule()
    _admin("UPDATE woo.price_rule SET active=true, last_previewed_at=now(), excl_styles=ARRAY['ZZZ'], effective_from='2025-01-01' WHERE rule_id=%s", (rid,))
    scope = (MutationScope("price_rule_row", {"rule_id": rid}),)
    def during():
        assert _admin("SELECT effect_value, active, last_previewed_at IS NOT NULL FROM woo.price_rule WHERE rule_id=%s", (rid,)) == [(Decimal("8"), active, active)]
    _round_trip("save_price_rule", {"rule_id": rid, "name": "Edited", "effect_type": "flat", "effect_value": "8", "active": active}, scope, "save-edit", during=during)


def test_rule_impact_drift_refuses_activation_and_rolls_back():
    rid = _rule()
    staged = _stage("set_price_rule_active", {"rule_id": rid, "active": True})
    _admin("UPDATE woo.store_product_state SET base_price=30 WHERE fdm4_store='S_TEST' AND kind='variation'")
    with pytest.raises(PreviewDrift):
        _apply(staged)
    assert _admin("SELECT active, last_previewed_at FROM woo.price_rule WHERE rule_id=%s", (rid,)) == [(False, None)]


def test_price_preview_is_read_only_and_routes_keep_stamp_requirement(client_as):
    client = client_as()
    rid = _rule()
    scopes = (MutationScope("price_rule_row", {"rule_id": rid}),)
    before = _snapshot(scopes)
    result = _read("preview_price_rule", {"rule_id": rid, "sample_limit": 1})
    assert len(result["sample"]) == 1
    assert result["summary"]["affected"] >= 4 and result["store_count"] >= 1
    assert states_equal(_snapshot(scopes), before)
    assert client.put("/api/price-rules/toggle", json={"rule_id": rid, "active": True}).status_code == 409
    response = client.post("/api/price-rules/preview", json={"rule_id": rid, "sample_limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert body.pop("preview_recorded") is True
    assert body == result
    assert client.put("/api/price-rules/toggle", json={"rule_id": rid, "active": True}).status_code == 200
    label = client.put("/api/price-rules", json={"rule_id": rid, "name": "New label", "effect_type": "percent", "effect_value": 5, "active": True})
    assert label.status_code == 200 and label.json()["active"] is True
    edited = client.put("/api/price-rules", json={"rule_id": rid, "name": "New label", "effect_type": "percent", "effect_value": 6, "active": True})
    assert edited.status_code == 200 and edited.json()["deactivated"] is True
    assert _admin("SELECT active, last_previewed_at FROM woo.price_rule WHERE rule_id=%s", (rid,)) == [(False, None)]


@pytest.mark.parametrize("extra", [
    {"effect_type": "nope"}, {"effect_value": "-100"}, {"rounding": "bad"},
    {"basis": "bad"}, {"effective_from": "tomorrow"},
    {"effective_from": "2026-02-01", "effective_until": "2026-01-01"},
    {"floor_price": "20", "ceiling_price": "10"},
    {"stores": ["x" * 101]}, {"name": "   "},
])
def test_price_rule_validation_is_shared_with_http(extra, client_as):
    args = {"name": "Invalid", "effect_type": "percent", "effect_value": "5", **extra}
    with pytest.raises((InvalidCommand, ValidationError)):
        _stage("save_price_rule", args)
    response = client_as().put("/api/price-rules", json=args)
    assert response.status_code in (400, 422)


def test_price_check_and_dimensions_share_route_results(client_as):
    rid = _rule()
    _apply(_stage("set_price_rule_active", {"rule_id": rid, "active": True}))
    args = {"store": "S_TEST", "style": "STYLE-1"}
    result = _read("check_price_rules", args)
    assert len(result["rows"]) == 3
    assert all(row["applied_rule_ids"] == [rid] for row in result["rows"])
    assert all(Decimal(str(row["final_price"])) == Decimal("10.5") for row in result["rows"])
    assert {**result, "rule_names": {str(k): v for k, v in result["rule_names"].items()}} == client_as().get("/api/price-rules/check", params=args).json()
    dimensions = _read("list_price_rule_dimensions", {})
    assert "Corporate" in dimensions["tiers"]
    assert dimensions == client_as().get("/api/price-rules/dimensions").json()


@pytest.mark.parametrize("editor_first", [False, True])
def test_fill_missing_colors_exact_undo_and_editor_interlock(editor_first, client_as):
    scopes = (MutationScope("assignment_store", {"fdm4_store": "S_TEST"}),)
    before = _snapshot(scopes)
    plan = _read("preview_fill_missing_colors", {"store": "S_TEST", "styles": ["STYLE-1"]})
    assert plan["copyable"][0]["auto_source"] == "RED"
    staged = _stage("fill_missing_colors", {"store": "S_TEST", "entries": [{"style": "STYLE-1", "source_color": "RED"}]})
    assert staged["preview_results"][0]["created"] == 4
    assert states_equal(before, _snapshot(scopes))
    assert _admin("SELECT count(*) FROM logo.bulk_batch WHERE target->>'kind'='fill_gaps'") == [(0,)]
    _apply(staged)
    batch_id = _admin("SELECT batch_id FROM logo.bulk_batch WHERE target->>'kind'='fill_gaps'")[0][0]
    client = client_as()
    if editor_first:
        result = client.post("/api/bulk-apply/undo", json={"batch_id": batch_id})
        assert result.status_code == 200 and result.json()["restored"] == 4
        with pytest.raises(Conflict):
            staging.undo_change_set(staged["id"], USER)
    else:
        staging.undo_change_set(staged["id"], USER)
        result = client.post("/api/bulk-apply/undo", json={"batch_id": batch_id})
        assert result.status_code == 200 and result.json()["restored"] == 0 and result.json()["skipped"] == 4
    assert states_equal(before, _snapshot(scopes))


def test_fill_bounds_unknown_styles_and_duplicate_sources():
    with pytest.raises(ValidationError):
        _read("preview_fill_missing_colors", {"store": "S_TEST", "styles": ["X"] * 51})
    with pytest.raises(ValidationError):
        _stage("fill_missing_colors", {"store": "S_TEST", "entries": [{"style": "X", "source_color": "RED"}] * 51})
    with pytest.raises(InvalidCommand):
        _stage("fill_missing_colors", {"store": "S_TEST", "entries": [{"style": "STYLE-1", "source_color": "RED"}] * 2})
    with pytest.raises(NotFound):
        _stage("fill_missing_colors", {"store": "S_TEST", "entries": [{"style": "NOPE", "source_color": "RED"}]})


def test_style_mix_reads_store_detail_and_cross_store_source(client_as):
    result = _read("get_style_mix", {"style": "MIX-1", "store": "S_MIXED"})
    assert result["source"] == "import" and result["in_mix"] is True
    assert result == client_as().get("/api/product-mix/style", params={"store": "S_MIXED", "style": "MIX-1"}).json()
    stores = _read("get_style_mix", {"style": "MIX-1", "limit": 1})["stores"]
    assert len(stores) == 1 and stores[0]["supplied_by"] == "import"
    assert _read("get_style_mix", {"style": "STYLE-1"})["stores"][0]["supplied_by"] == "fdm4"


def test_health_overview_is_bounded_and_shared(client_as):
    result = _read("get_health_overview", {})
    response = client_as().get("/api/health/overview")
    assert response.status_code == 200
    body = response.json()
    result.pop("generated_at"); body.pop("generated_at")
    assert result == body
    assert len(result["pipeline"]["runs"]) <= 12
    assert len(result["features"]["mix_stores"]) <= 50
    assert len(result["feeds"]["consumers"]) <= 100


def _categories(monkeypatch):
    monkeypatch.setattr(categories_service, "get_target", lambda env: object())
    with database.cursor(write=True, actor="fixture") as cursor:
        root = categories_draft.create_node(cursor, parent_id=None, name="Clothing", actor="fixture")
        child = categories_draft.create_node(cursor, parent_id=root["node_id"], name="Shirts", actor="fixture")
        categories_service.import_blog_snapshot(cursor, env="dev", blog_id=1, blog_path="/", actor="fixture",
            terms=[{"term_id": 1, "name": "Shirts", "slug": child["slug"], "parent": 0},
                   {"term_id": 2, "name": "Undecided", "slug": "undecided", "parent": 0}],
            products=[{"term_id": 1, "product_id": 10, "sku": "SHIRT"}])
    return root, child


def test_category_tree_mapping_and_plan_are_bounded_reads(monkeypatch):
    _categories(monkeypatch)
    tree = _read("cat_tree", {"env": "dev"})
    child = next(row for row in tree["paths"] if row["slug"] == "shirts")
    assert child["path"] == "Clothing / Shirts" and child["stores"] == 1 and child["products"] == 1
    assert _read("cat_tree", {"env": "dev", "limit": 1})["truncated"] is True
    mapping = _read("cat_mapping_status", {"env": "dev", "limit": 1})
    assert mapping["summary"]["unmapped"] == 1
    assert mapping["undecided"][0]["old_slug"] == "undecided"
    assert mapping["empty"][0]["old_slug"] == "undecided"
    before = _admin("SELECT count(*) FROM catmgr.run")
    plan = _read("cat_plan_check", {"env": "dev", "limit": 1})
    assert plan["ok"] is False and plan["blockers"][0]["kind"] == "unmapped_slugs"
    assert "totals" in plan and "warnings" in plan and "blogs" in plan
    assert _admin("SELECT count(*) FROM catmgr.run") == before


def test_category_run_list_and_job_summary_are_capped(monkeypatch):
    _categories(monkeypatch)
    rid = _admin("INSERT INTO catmgr.run (env,target_blogs,status,plan_totals,snapshot_versions,stop_on_failure,created_by) VALUES ('dev',ARRAY[1,2],'queued','{}','{}',true,'fixture') RETURNING run_id")[0][0]
    _admin("INSERT INTO catmgr.run_job (run_id,blog_id,blog_path,seq,payload) VALUES (%s,1,'/',1,'{}'),(%s,2,'/two/',2,'{}')", (rid,rid))
    result = _read("cat_runs", {"env": "dev", "run_id": rid, "limit": 1})
    assert result["runs"][0]["run_id"] == rid
    assert result["run"]["job_count"] == 2 and len(result["run"]["jobs"]) == 1
    assert result["truncated"] is True and result["run"]["status"] == "queued"


@pytest.mark.parametrize("name", ["cat_tree", "cat_mapping_status", "cat_plan_check", "cat_runs"])
@pytest.mark.parametrize("dispatcher", [execute_read_tool, execute_agent_tool])
def test_category_allowlist_is_enforced_before_reading(name, dispatcher, monkeypatch):
    monkeypatch.setattr(database, "cursor", lambda **kw: pytest.fail("Unauthorized tool reached database"))
    denied = SimpleNamespace(catmgr_enabled=True, catmgr_view_users={"someone-else"}, agent_writes_enabled=True)
    with pytest.raises(UnknownTool):
        dispatcher(name, {"env": "dev"}, CONTEXT, denied)
