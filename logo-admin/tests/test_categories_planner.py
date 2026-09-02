"""Category editor Phase 4: the diff engine (golden scenario).

Requires a provisioned test database (TEST_DATABASE_DSN + TEST_DATABASE_ADMIN_DSN).

Scenario (env=prod):
  Blog 1 live: men-s (kept+rename+reslug), men-s-bottoms (merge into mens),
    saws (delete, empty), old-boots (delete WITH product rescued by rule into
    footwear), lonely (delete WITH product NOT rescued -> zero-category),
    field-uniform (store_custom).
  Blog 7 live: men-s, saws, kendall-x (store_custom, blog-7 only).
  Draft: Clothing[clothing] > Men's[mens]; Footwear[footwear]; PPE[ppe].
  Overlays: blog 7 excludes PPE subtree; blog 7 extra node "Crew Shop"
    [crew-shop] under Clothing; blog 7 renames Men's -> "Team Men's".
  Rules: footwear gets products of old-boots whose name starts "Boot".
  Assignments: add EXTRA-9 to ppe; remove RESCUE-1 from ppe (no-op there).
"""

import psycopg2
import pytest

import categories_draft
import categories_mapping
import categories_planner
import categories_service
from categories_draft import DraftError
from db import database
from tests.conftest import TEST_ADMIN_DSN


def _write(fn, *args, **kwargs):
    with database.cursor(write=True, actor="tester") as cursor:
        return fn(cursor, *args, **kwargs, actor="tester")


def _read(fn, *args, **kwargs):
    with database.cursor() as cursor:
        return fn(cursor, *args, **kwargs)


def _build_scenario():
    blog1_terms = [
        {"term_id": 10, "slug": "men-s", "name": "Men&#039;s", "parent": 0,
         "sort_order": 1, "count": 2},
        {"term_id": 11, "slug": "men-s-bottoms", "name": "Men's Pants &amp; Shorts",
         "parent": 10, "count": 1},
        {"term_id": 12, "slug": "saws", "name": "Saws", "parent": 0, "count": 0},
        {"term_id": 13, "slug": "old-boots", "name": "Old Boots", "parent": 0,
         "count": 2},
        {"term_id": 14, "slug": "lonely", "name": "Lonely", "parent": 0,
         "count": 1},
        {"term_id": 15, "slug": "field-uniform", "name": "Field Uniform",
         "parent": 0, "count": 1},
    ]
    blog1_products = [
        {"term_id": 10, "product_id": 100, "sku": "PANT-1"},
        {"term_id": 11, "product_id": 100, "sku": "PANT-1"},
        {"term_id": 11, "product_id": 101, "sku": "SHORT-1"},
        {"term_id": 13, "product_id": 102, "sku": "RESCUE-1"},
        {"term_id": 13, "product_id": 103, "sku": "RESCUE-2"},
        {"term_id": 14, "product_id": 104, "sku": "ORPHAN-1"},
        {"term_id": 15, "product_id": 105, "sku": "UNIFORM-1"},
    ]
    blog7_terms = [
        {"term_id": 20, "slug": "men-s", "name": "Men's", "parent": 0},
        {"term_id": 21, "slug": "saws", "name": "Saws", "parent": 0},
        {"term_id": 22, "slug": "kendall-x", "name": "Kendall X", "parent": 0},
    ]
    blog7_products = [
        {"term_id": 20, "product_id": 200, "sku": "PANT-1"},
        {"term_id": 22, "product_id": 201, "sku": "KEND-1"},
    ]
    with database.cursor(write=True, actor="seed") as cursor:
        categories_service.import_blog_snapshot(
            cursor, env="prod", blog_id=1, blog_path="/",
            terms=blog1_terms, products=blog1_products, actor="seed",
        )
        categories_service.import_blog_snapshot(
            cursor, env="prod", blog_id=7, blog_path="/isa/",
            terms=blog7_terms, products=blog7_products, actor="seed",
        )

    # warehouse product data for the rescue rule
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            for sku, name in (("RESCUE-1", "Boot Alpha"),
                              ("RESCUE-2", "Sandal Beta")):
                cursor.execute(
                    """
                    INSERT INTO woo.store_product_state
                        (fdm4_store, catalog_id, sku, kind, style_code, name,
                         is_active, payload, content_hash)
                    VALUES ('S_1', 'CAT', %s, 'parent', %s, %s, true,
                            '{}'::jsonb, '')
                    """,
                    (sku, sku, name),
                )

    clothing = _write(categories_draft.create_node, parent_id=None,
                      name="Clothing")
    mens = _write(categories_draft.create_node,
                  parent_id=clothing["node_id"], name="Men's")
    footwear = _write(categories_draft.create_node, parent_id=None,
                      name="Footwear")
    ppe = _write(categories_draft.create_node, parent_id=None, name="PPE")

    _write(categories_draft.set_override, blog_id=7, kind="exclude",
           node_id=ppe["node_id"])
    _write(categories_draft.set_override, blog_id=7, kind="extra_node",
           name="Crew Shop", parent_node_id=clothing["node_id"])
    _write(categories_draft.set_override, blog_id=7, kind="rename",
           node_id=mens["node_id"], name="Team Men's")

    _write(categories_mapping.set_mapping, old_slug="men-s", action="map",
           target_node_id=mens["node_id"])                      # primary
    _write(categories_mapping.set_mapping, old_slug="men-s-bottoms",
           action="map", target_node_id=mens["node_id"])        # merge source
    _write(categories_mapping.set_mapping, old_slug="saws", action="delete")
    _write(categories_mapping.set_mapping, old_slug="old-boots",
           action="delete")
    _write(categories_mapping.set_mapping, old_slug="lonely", action="delete")
    _write(categories_mapping.set_mapping, old_slug="field-uniform",
           action="store_custom")
    _write(categories_mapping.set_mapping, old_slug="kendall-x",
           action="store_custom")

    _write(categories_mapping.set_rule, node_id=footwear["node_id"],
           spec={"from": ["old-boots"], "field": "name", "op": "prefix",
                 "value": "Boot"})
    _write(categories_mapping.set_assignments, node_id=ppe["node_id"],
           skus=["EXTRA-9"], mode="add")
    return {"clothing": clothing, "mens": mens, "footwear": footwear,
            "ppe": ppe}


def test_blog1_plan_golden():
    nodes = _build_scenario()
    plan = _read(categories_planner.build_blog_plan, "prod", 1)

    assert plan["blog_path"] == "/"
    assert plan["snapshot_version"] == 1

    updates = {u["expected_slug"]: u for u in plan["terms"]["update"]}
    assert set(updates) == {"men-s"}
    assert updates["men-s"]["term_id"] == 10
    assert updates["men-s"]["set"]["slug"] == "mens"
    assert updates["men-s"]["set"]["name"] == "Men's"
    assert updates["men-s"]["set"]["parent_slug"] == "clothing"
    # live name is entity-encoded ("Men&#039;s"); WordPress re-encodes on
    # save, so the planner compares decoded names and does NOT flag a rename
    # (otherwise every drift audit would re-rename forever). The re-slug, the
    # move from root to under Clothing, and the sort-order normalization are
    # the real changes.
    assert updates["men-s"]["changed"] == {"slug": True, "parent": True,
                                           "sort_order": True}

    creates = [c["slug"] for c in plan["terms"]["create"]]
    assert creates == ["clothing", "footwear", "ppe"]  # parents-first, sorted

    deletes = {d["expected_slug"]: d["reason"] for d in plan["terms"]["delete"]}
    assert deletes == {"men-s-bottoms": "merge", "saws": "delete",
                       "old-boots": "delete", "lonely": "delete"}

    memberships = {m["product_id"]: m for m in plan["memberships"]}
    # 100 (PANT-1): men-s kept as mens + bottoms merged into mens -> final {mens};
    #   after term ops alone it already has mens -> NOT listed.
    assert 100 not in memberships
    # 101 (SHORT-1): only bottoms (merged) -> needs adding to mens.
    assert memberships[101]["final_slugs"] == ["mens"]
    # 102 (RESCUE-1): old-boots deleted, rescued by rule into footwear.
    assert memberships[102]["final_slugs"] == ["footwear"]
    # 103 (RESCUE-2, "Sandal Beta"): not rescued -> zero category, not listed
    #   as membership (nothing to write; term delete detaches it).
    assert 103 not in memberships
    # 104 (ORPHAN-1): zero category as well.
    assert {z["sku"] for z in plan["zero_category"]} == {"RESCUE-2", "ORPHAN-1"}
    # 105 (UNIFORM-1): store_custom keeps its term untouched -> unchanged.
    assert 105 not in memberships

    redirects = {r["old_path"]: r["new_path"] for r in plan["redirects"]}
    assert redirects == {
        "/product-category/men-s/": "/product-category/mens/",
        "/product-category/men-s-bottoms/": "/product-category/mens/",
        "/product-category/saws/": "/store/",
        "/product-category/old-boots/": "/store/",
        "/product-category/lonely/": "/store/",
    }

    stats = plan["stats"]
    assert stats["updates"] == 1 and stats["reslugs"] == 1
    assert stats["renames"] == 0  # decoded-name compare: "Men&#039;s" == "Men's"
    assert stats["creates"] == 3 and stats["deletes"] == 4
    assert stats["membership_changes"] == 2
    assert stats["zero_category"] == 2
    del nodes


def test_blog7_plan_honors_overlays():
    _build_scenario()
    plan = _read(categories_planner.build_blog_plan, "prod", 7)

    updates = {u["expected_slug"]: u for u in plan["terms"]["update"]}
    assert updates["men-s"]["set"]["name"] == "Team Men's"      # rename overlay
    assert updates["men-s"]["set"]["slug"] == "mens"

    creates = {c["slug"]: c for c in plan["terms"]["create"]}
    assert set(creates) == {"clothing", "footwear", "crew-shop"}  # no ppe (excluded)
    assert creates["crew-shop"]["parent_slug"] == "clothing"

    deletes = {d["expected_slug"]: d["reason"] for d in plan["terms"]["delete"]}
    assert deletes == {"saws": "delete"}
    # kendall-x is store_custom: untouched, no ops at all
    assert "kendall-x" not in deletes and "kendall-x" not in updates

    # blog 7 gets no redirects (blog 1 only)
    assert plan["redirects"] == []

    # PANT-1 product keeps mens via in-place update -> no membership write
    assert plan["memberships"] == []


def test_preview_blockers_and_acks():
    nodes = _build_scenario()
    preview = _read(categories_planner.preview, "prod")
    kinds = {b["kind"]: b for b in preview["blockers"]}
    assert not preview["ok"]
    assert set(kinds) == {"zero_category_skus"}
    assert {z["sku"] for z in kinds["zero_category_skus"]["sample"]} == {
        "RESCUE-2", "ORPHAN-1",
    }
    assert len(preview["blogs"]) == 2
    assert preview["totals"]["deletes"] == 5

    # acknowledge both -> preview unblocks
    _write(categories_planner.set_acks, skus=["rescue-2", "ORPHAN-1"],
           note="deliberate")
    preview = _read(categories_planner.preview, "prod")
    assert preview["ok"] is True
    assert preview["blockers"] == []
    warning_kinds = {w["kind"] for w in preview["warnings"]}
    assert {"code_item", "blog1_slug_changes", "redirects"} <= warning_kinds

    _write(categories_planner.delete_ack, "RESCUE-2")
    preview = _read(categories_planner.preview, "prod")
    assert preview["ok"] is False
    del nodes


def test_preview_unmapped_blocks_everything():
    _build_scenario()
    _write(categories_mapping.clear_mapping, "saws")
    preview = _read(categories_planner.preview, "prod")
    kinds = {b["kind"] for b in preview["blockers"]}
    assert "unmapped_slugs" in kinds
    assert preview["blogs"] == []  # nothing planned while unmapped
    with pytest.raises(DraftError):
        _read(categories_planner.build_blog_plan, "prod", 1)


def test_slug_collision_blocker():
    _build_scenario()
    # A blog-7 extra node colliding with a global slug: rename the extra to
    # "Footwear" -> slug footwear collides with the created global footwear.
    overrides = _read(categories_draft.list_overrides, 7)
    extra = next(o for o in overrides if o["kind"] == "extra_node")
    _write(categories_draft.set_override, blog_id=7, kind="extra_node",
           override_id=extra["override_id"], name="Footwear", slug="footwear",
           parent_node_id=None)
    preview = _read(categories_planner.preview, "prod", [7])
    kinds = {b["kind"]: b for b in preview["blockers"]}
    assert "slug_collisions" in kinds
    assert kinds["slug_collisions"]["blogs"][0]["blog_id"] == 7
    assert "footwear" in kinds["slug_collisions"]["blogs"][0]["slugs"]
