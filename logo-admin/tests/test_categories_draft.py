"""Category editor Phase 2: draft tree, overlays, seeding.

Requires a provisioned test database (TEST_DATABASE_DSN + TEST_DATABASE_ADMIN_DSN).
"""

import pytest

import categories_draft
import categories_service
from categories_draft import DraftConflict, DraftError
from db import database


def _write(fn, *args, **kwargs):
    with database.cursor(write=True, actor="tester") as cursor:
        return fn(cursor, *args, **kwargs, actor="tester")


def _read(fn, *args, **kwargs):
    with database.cursor() as cursor:
        return fn(cursor, *args, **kwargs)


def _seed_snapshot(env="prod", blog_id=1):
    terms = [
        {"term_id": 1, "slug": "men-s", "name": "Men&#039;s", "parent": 0,
         "sort_order": 1, "count": 5},
        {"term_id": 2, "slug": "men-s-bottoms", "name": "Men's Pants &amp; Shorts",
         "parent": 1, "sort_order": 0, "count": 3},
        {"term_id": 3, "slug": "batt01", "name": "Batteries &amp;", "parent": 0,
         "sort_order": 2, "count": 0},
    ]
    with database.cursor(write=True, actor="seed") as cursor:
        categories_service.import_blog_snapshot(
            cursor, env=env, blog_id=blog_id, blog_path="/",
            terms=terms, products=[], actor="seed",
        )


def test_seed_from_snapshot_builds_clean_tree():
    _seed_snapshot()
    result = _write(categories_draft.seed_from_snapshot, env="prod", blog_id=1)
    assert result == {"nodes": 3}
    nodes = _read(categories_draft.list_nodes)
    by_slug = {n["slug"]: n for n in nodes}
    assert by_slug["men-s"]["name"] == "Men's"                    # entity-decoded
    assert by_slug["men-s-bottoms"]["name"] == "Men's Pants & Shorts"
    assert by_slug["batt01"]["name"] == "Batteries"               # trailing & debris stripped
    assert by_slug["men-s-bottoms"]["parent_id"] == by_slug["men-s"]["node_id"]


def test_seed_refuses_nonempty_unless_forced():
    _seed_snapshot()
    _write(categories_draft.seed_from_snapshot, env="prod", blog_id=1)
    with pytest.raises(DraftConflict):
        _write(categories_draft.seed_from_snapshot, env="prod", blog_id=1)
    result = _write(categories_draft.seed_from_snapshot, env="prod", blog_id=1,
                    force=True)
    assert result == {"nodes": 3}


def test_seed_requires_snapshot():
    with pytest.raises(DraftError):
        _write(categories_draft.seed_from_snapshot, env="prod", blog_id=42)


def test_create_update_move_delete_cycle():
    clothing = _write(categories_draft.create_node, parent_id=None,
                      name="Clothing")
    assert clothing["slug"] == "clothing"
    mens = _write(categories_draft.create_node, parent_id=clothing["node_id"],
                  name="Men's")
    assert mens["slug"] == "mens"                                  # apostrophe dropped
    dup = _write(categories_draft.create_node, parent_id=None, name="Men's")
    assert dup["slug"] == "mens-2"                                 # clash suffix

    with pytest.raises(DraftConflict):
        _write(categories_draft.create_node, parent_id=None, name="X",
               slug="clothing")
    with pytest.raises(DraftError):
        _write(categories_draft.create_node, parent_id=999999, name="Orphan")

    renamed = _write(categories_draft.update_node, mens["node_id"],
                     name="Men's Clothing", slug="mens-clothing")
    assert renamed["name"] == "Men's Clothing"
    with pytest.raises(DraftConflict):
        _write(categories_draft.update_node, dup["node_id"], slug="mens-clothing")

    # move dup under clothing at position 0 (before mens-clothing)
    moved = _write(categories_draft.move_node, dup["node_id"],
                   parent_id=clothing["node_id"], position=0)
    assert moved["parent_id"] == clothing["node_id"]
    nodes = _read(categories_draft.list_nodes)
    kids = [n for n in nodes if n["parent_id"] == clothing["node_id"]]
    assert [k["node_id"] for k in kids] == [dup["node_id"], renamed["node_id"]]

    # cycle refusal: clothing under its own child
    with pytest.raises(DraftError):
        _write(categories_draft.move_node, clothing["node_id"],
               parent_id=dup["node_id"], position=None)

    # delete: RESTRICT without cascade, subtree with cascade
    with pytest.raises(DraftConflict):
        _write(categories_draft.delete_node, clothing["node_id"])
    result = _write(categories_draft.delete_node, clothing["node_id"],
                    cascade=True)
    assert set(result["deleted"]) == {
        clothing["node_id"], dup["node_id"], renamed["node_id"],
    }
    assert _read(categories_draft.list_nodes) == []


def test_overrides_and_effective_tree():
    ppe = _write(categories_draft.create_node, parent_id=None, name="PPE")
    chainsaw = _write(categories_draft.create_node, parent_id=ppe["node_id"],
                      name="Chainsaw Protection")
    stickers = _write(categories_draft.create_node, parent_id=None,
                      name="Stickers")

    rename = _write(categories_draft.set_override, blog_id=7, kind="rename",
                    node_id=stickers["node_id"], name="Stickers & Patches")
    assert rename["kind"] == "rename"
    _write(categories_draft.set_override, blog_id=7, kind="exclude",
           node_id=ppe["node_id"])
    extra = _write(categories_draft.set_override, blog_id=61, kind="extra_node",
                   name="Kendall Approved HVSA",
                   parent_node_id=ppe["node_id"])
    assert extra["slug"] == "kendall-approved-hvsa"

    # blog 7: PPE subtree gone, stickers renamed
    tree7 = _read(categories_draft.effective_tree, 7)
    names7 = {n["name"] for n in tree7}
    assert "PPE" not in names7 and "Chainsaw Protection" not in names7
    assert "Stickers & Patches" in names7

    # blog 61: extra node grafted under PPE
    tree61 = _read(categories_draft.effective_tree, 61)
    extras = [n for n in tree61 if n.get("extra")]
    assert len(extras) == 1
    assert extras[0]["parent_id"] == ppe["node_id"]
    assert extras[0]["override_id"] == extra["override_id"]

    # exclude with include_descendants=False keeps children
    _write(categories_draft.set_override, blog_id=9, kind="exclude",
           node_id=ppe["node_id"], include_descendants=False)
    tree9 = _read(categories_draft.effective_tree, 9)
    names9 = {n["name"] for n in tree9}
    assert "PPE" not in names9 and "Chainsaw Protection" in names9

    # validation shapes
    with pytest.raises(DraftError):
        _write(categories_draft.set_override, blog_id=7, kind="rename",
               node_id=None, name="X")
    with pytest.raises(DraftError):
        _write(categories_draft.set_override, blog_id=7, kind="extra_node",
               name="")
    _write(categories_draft.delete_override, rename["override_id"])
    with pytest.raises(DraftError):
        _write(categories_draft.delete_override, rename["override_id"])


def test_node_delete_cascades_overrides():
    ppe = _write(categories_draft.create_node, parent_id=None, name="PPE")
    _write(categories_draft.set_override, blog_id=7, kind="exclude",
           node_id=ppe["node_id"])
    _write(categories_draft.delete_node, ppe["node_id"])
    assert _read(categories_draft.list_overrides, 7) == []


def test_draft_routes(client_as, monkeypatch):
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    monkeypatch.setenv("CATMGR_DEV_URL", "https://dev.example.test/base")
    monkeypatch.setenv("CATMGR_DEV_USER", "svc")
    monkeypatch.setenv("CATMGR_DEV_APP_PASSWORD", "pw")
    from config import get_settings
    get_settings.cache_clear()
    client = client_as()

    created = client.post("/api/categories/nodes", json={"name": "Clothing"})
    assert created.status_code == 200
    node = created.json()["node"]

    child = client.post("/api/categories/nodes",
                        json={"name": "Men's", "parent_id": node["node_id"]})
    assert child.status_code == 200

    tree = client.get("/api/categories/tree").json()["nodes"]
    assert len(tree) == 2

    conflict = client.post("/api/categories/nodes",
                           json={"name": "Zed", "slug": "clothing"})
    assert conflict.status_code == 409

    move = client.post(
        f"/api/categories/nodes/{node['node_id']}/move",
        json={"parent_id": child.json()["node"]["node_id"], "position": 0},
    )
    assert move.status_code == 422  # cycle

    override = client.put("/api/categories/overrides", json={
        "blog_id": 7, "kind": "rename", "node_id": node["node_id"],
        "name": "Apparel",
    })
    assert override.status_code == 200

    effective = client.get("/api/categories/tree/effective",
                           params={"blog_id": 7})
    assert effective.status_code == 200
    assert effective.json()["nodes"][0]["name"] == "Apparel"

    delete = client.delete(f"/api/categories/nodes/{node['node_id']}")
    assert delete.status_code == 409  # has child
    delete = client.delete(
        f"/api/categories/nodes/{node['node_id']}", params={"cascade": "true"}
    )
    assert delete.status_code == 200
    get_settings.cache_clear()


def test_view_allowlist_hides_the_editor_from_other_logins(client_as, monkeypatch):
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    monkeypatch.setenv("CATMGR_DEV_URL", "https://dev.example.test/base")
    monkeypatch.setenv("CATMGR_DEV_USER", "svc")
    monkeypatch.setenv("CATMGR_DEV_APP_PASSWORD", "pw")
    from config import get_settings
    monkeypatch.setenv("CATMGR_VIEW_USERS", "SOMEONE-ELSE")
    get_settings.cache_clear()
    hidden = client_as("admin-one")
    assert hidden.get("/api/categories/tree").status_code == 404
    assert 'data-view="categories"' not in hidden.get("/").text
    monkeypatch.setenv("CATMGR_VIEW_USERS", "ADMIN-ONE")
    get_settings.cache_clear()
    shown = client_as("admin-one")
    assert shown.get("/api/categories/tree").status_code == 200
    assert 'data-view="categories"' in shown.get("/").text
    monkeypatch.delenv("CATMGR_VIEW_USERS")
    get_settings.cache_clear()
    assert client_as("admin-one").get("/api/categories/tree").status_code == 200
    get_settings.cache_clear()
