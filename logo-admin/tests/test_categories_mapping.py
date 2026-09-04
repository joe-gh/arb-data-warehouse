"""Category editor Phase 3: slug map, rules, assignments, membership.

Requires a provisioned test database (TEST_DATABASE_DSN + TEST_DATABASE_ADMIN_DSN).
"""

import psycopg2
import pytest

import categories_draft
import categories_mapping
import categories_service
from categories_draft import DraftConflict, DraftError
from db import database
from tests.conftest import TEST_ADMIN_DSN


def _write(fn, *args, **kwargs):
    with database.cursor(write=True, actor="tester") as cursor:
        return fn(cursor, *args, **kwargs, actor="tester")


def _read(fn, *args, **kwargs):
    with database.cursor() as cursor:
        return fn(cursor, *args, **kwargs)


def _snapshot(env="prod"):
    terms = [
        {"term_id": 1, "slug": "men-s", "name": "Men's", "parent": 0, "count": 2},
        {"term_id": 2, "slug": "men-s-bottoms", "name": "Men's Pants & Shorts",
         "parent": 1, "count": 1},
        {"term_id": 3, "slug": "footwear-work-boots", "name": "Work Boots",
         "parent": 0, "count": 2},
        {"term_id": 4, "slug": "saws", "name": "Saws", "parent": 0, "count": 0},
    ]
    products = [
        {"term_id": 1, "product_id": 11, "sku": "408045"},
        {"term_id": 2, "product_id": 12, "sku": "112"},
        {"term_id": 3, "product_id": 13, "sku": "BOOT-M"},
        {"term_id": 3, "product_id": 14, "sku": "BOOT-W"},
    ]
    with database.cursor(write=True, actor="seed") as cursor:
        categories_service.import_blog_snapshot(
            cursor, env=env, blog_id=1, blog_path="/",
            terms=terms, products=products, actor="seed",
        )


def _seed_product_state():
    rows = [
        ("S_1", "CAT", "408045", "parent", "408045", "Original Tree Pants",
         "22", "Arborwear", "PANTS"),
        ("S_1", "CAT", "112", "parent", "112", "Men's Utility Shorts",
         "22", "Arborwear", "SHORTS"),
        ("S_1", "CAT", "BOOT-M", "parent", "BOOT-M", "Men's Work Boot",
         "186", "Port Authority", "FOOTWEAR"),
        ("S_1", "CAT", "BOOT-W", "parent", "BOOT-W", "Women's Work Boot",
         "186", "Port Authority", "FOOTWEAR"),
    ]
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            for store, cat, sku, kind, style, name, mill, brand, category in rows:
                cursor.execute(
                    """
                    INSERT INTO woo.store_product_state
                        (fdm4_store, catalog_id, sku, kind, style_code, name,
                         mill_code, brand, category, is_active,
                         payload, content_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true,
                            '{}'::jsonb, '')
                    """,
                    (store, cat, sku, kind, style, name, mill, brand, category),
                )


def _node(name, **kwargs):
    return _write(categories_draft.create_node, parent_id=kwargs.pop("parent_id", None),
                  name=name, **kwargs)


def test_mapping_status_and_forcing_function():
    _snapshot()
    status = _read(categories_mapping.mapping_status, "prod")
    assert status["summary"] == {"total": 4, "mapped": 0, "unmapped": 4,
                                 "by_action": {}}
    clothing = _node("Clothing")
    saved = _write(categories_mapping.set_mapping, old_slug="men-s",
                   action="map", target_node_id=clothing["node_id"])
    assert saved["is_primary"] is True  # first map auto-primary
    merged = _write(categories_mapping.set_mapping, old_slug="men-s-bottoms",
                    action="map", target_node_id=clothing["node_id"])
    assert merged["is_primary"] is False  # merge source
    _write(categories_mapping.set_mapping, old_slug="saws", action="delete")
    status = _read(categories_mapping.mapping_status, "prod")
    assert status["summary"]["mapped"] == 3
    assert status["summary"]["unmapped"] == 1
    assert status["summary"]["by_action"] == {"map": 2, "delete": 1}
    unmapped = [s for s in status["slugs"] if not s["action"]]
    assert [s["old_slug"] for s in unmapped] == ["footwear-work-boots"]

    with pytest.raises(DraftConflict):
        _write(categories_mapping.set_mapping, old_slug="men-s-bottoms",
               action="map", target_node_id=clothing["node_id"],
               is_primary=True)
    with pytest.raises(DraftError):
        _write(categories_mapping.set_mapping, old_slug="ghost-slug",
               action="delete")
    with pytest.raises(DraftError):
        _write(categories_mapping.set_mapping, old_slug="men-s", action="map",
               target_node_id=999999)


def test_mapping_clear_and_node_delete_cascade():
    _snapshot()
    clothing = _node("Clothing")
    _write(categories_mapping.set_mapping, old_slug="men-s", action="map",
           target_node_id=clothing["node_id"])
    _write(categories_mapping.clear_mapping, "men-s")
    with pytest.raises(DraftError):
        _write(categories_mapping.clear_mapping, "men-s")
    _write(categories_mapping.set_mapping, old_slug="men-s", action="map",
           target_node_id=clothing["node_id"])
    _write(categories_draft.delete_node, clothing["node_id"])
    status = _read(categories_mapping.mapping_status, "prod")
    assert status["summary"]["mapped"] == 0  # cascade returned slug to unmapped


def test_auto_suggest():
    _snapshot()
    _node("Saws", slug="hand-saws")     # NAME matches live 'saws', slug differs
    _node("Anything", slug="men-s")     # slug-exact -> IMPLICIT map, no suggestion
    outcome = _read(categories_mapping.auto_suggest, "prod")
    assert outcome["ambiguous"] == []
    by_slug = {s["old_slug"]: s for s in outcome["suggestions"]}
    assert "men-s" not in by_slug       # implicit identity mapping covers it
    assert by_slug["saws"]["reason"] == "name_exact"
    assert by_slug["saws"]["node_slug"] == "hand-saws"
    status = _read(categories_mapping.mapping_status, "prod")
    rows = {r["old_slug"]: r for r in status["slugs"]}
    assert rows["men-s"]["action"] == "map" and rows["men-s"]["implicit"] is True


def test_rules_validation_and_evaluation():
    _snapshot()
    _seed_product_state()
    node = _node("Women's Footwear")

    with pytest.raises(DraftError):
        _read(categories_mapping.evaluate_rule, "prod", {"field": "nope",
                                                         "op": "equals",
                                                         "value": "x"})
    with pytest.raises(DraftError):
        _read(categories_mapping.evaluate_rule, "prod",
              {"field": "name", "op": "regex", "value": "("})

    outcome = _read(categories_mapping.evaluate_rule, "prod",
                    {"from": ["footwear-work-boots"], "field": "name",
                     "op": "regex", "value": "^Women"})
    assert outcome == {"count": 1, "skus": ["BOOT-W"]}

    outcome = _read(categories_mapping.evaluate_rule, "prod",
                    {"field": "brand", "op": "equals", "value": "arborwear"})
    assert outcome["count"] == 2 and set(outcome["skus"]) == {"112", "408045"}

    rule = _write(categories_mapping.set_rule, node_id=node["node_id"],
                  spec={"from": ["footwear-work-boots"], "field": "name",
                        "op": "prefix", "value": "women"})
    assert rule["node_slug"] == "womens-footwear"
    assert _read(categories_mapping.list_rules, node["node_id"])[0]["rule_id"] == rule["rule_id"]
    _write(categories_mapping.delete_rule, rule["rule_id"])
    with pytest.raises(DraftError):
        _write(categories_mapping.delete_rule, rule["rule_id"])


def test_assignments_and_membership():
    _snapshot()
    _seed_product_state()
    clothing = _node("Clothing")
    _write(categories_mapping.set_mapping, old_slug="men-s", action="map",
           target_node_id=clothing["node_id"])
    _write(categories_mapping.set_mapping, old_slug="men-s-bottoms",
           action="map", target_node_id=clothing["node_id"])

    result = _write(categories_mapping.set_assignments,
                    node_id=clothing["node_id"],
                    skus=["extra-1", "EXTRA-1", "boot-w"], mode="add")
    assert result["count"] == 2  # deduped, uppercased
    _write(categories_mapping.set_assignments, node_id=clothing["node_id"],
           skus=["112"], mode="remove")

    membership = _read(categories_mapping.effective_membership, "prod",
                       clothing["node_id"])
    assert membership["carried_count"] == 2          # 408045, 112
    assert membership["added_count"] == 2            # EXTRA-1, BOOT-W
    assert membership["removed_count"] == 1          # 112
    assert membership["final_count"] == 3
    assert set(membership["final_sample"]) == {"408045", "BOOT-W", "EXTRA-1"}

    # add wins over previous remove for the same sku
    _write(categories_mapping.set_assignments, node_id=clothing["node_id"],
           skus=["112"], mode="add")
    membership = _read(categories_mapping.effective_membership, "prod",
                       clothing["node_id"])
    assert membership["removed_count"] == 0
    assert membership["final_count"] == 4


def test_routes_and_csv_round_trip(client_as, monkeypatch):
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    monkeypatch.setenv("CATMGR_PROD_URL", "https://prod.example.test/base")
    monkeypatch.setenv("CATMGR_PROD_USER", "svc")
    monkeypatch.setenv("CATMGR_PROD_APP_PASSWORD", "pw")
    from config import get_settings
    get_settings.cache_clear()
    client = client_as()
    _snapshot()

    node = client.post("/api/categories/nodes", json={"name": "Clothing"}).json()["node"]

    saved = client.put("/api/categories/mapping", json={"rows": [
        {"old_slug": "men-s", "action": "map", "target_node_id": node["node_id"]},
        {"old_slug": "ghost", "action": "delete"},
    ]})
    assert saved.status_code == 200
    results = saved.json()["results"]
    assert results[0]["ok"] is True and results[1]["ok"] is False

    mapping = client.get("/api/categories/mapping", params={"env": "prod"})
    assert mapping.status_code == 200
    assert mapping.json()["summary"]["mapped"] == 1

    put = client.put("/api/categories/assignments", json={
        "node_id": node["node_id"], "skus": ["A1", "B2"], "mode": "add",
    })
    assert put.status_code == 200

    export = client.get("/api/categories/assignments/export")
    assert export.status_code == 200
    assert "sku,node_slugs" in export.text
    assert "A1,clothing" in export.text.replace("\r", "")

    imported = client.post("/api/categories/assignments/import", json={
        "csv": "sku,node_slugs\nC3,clothing\nD4,unknown-slug\n",
    })
    assert imported.status_code == 200
    body = imported.json()
    ok_rows = [r for r in body["results"] if r["ok"]]
    bad = [r for r in body["results"] if not r["ok"]]
    assert ok_rows and ok_rows[0]["count"] == 1
    assert bad and bad[0]["node_slug"] == "unknown-slug"
    get_settings.cache_clear()
