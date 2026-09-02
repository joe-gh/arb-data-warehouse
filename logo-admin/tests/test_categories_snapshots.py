"""Category editor Phase 1: snapshot import + routes.

Requires a provisioned test database (TEST_DATABASE_DSN + TEST_DATABASE_ADMIN_DSN).
"""

import pytest

import categories_service
from config import ConfigurationError, get_settings
from db import database


TERMS_A = [
    {"term_id": 10, "slug": "mens", "name": "Men's", "parent": 0,
     "description": "", "count": 2, "sort_order": 1, "thumbnail_id": 77,
     "name_locked": False},
    {"term_id": 11, "slug": "mens-t-shirts", "name": "Men's T-Shirts",
     "parent": 10, "description": "tees", "count": 2, "sort_order": 0,
     "thumbnail_id": 0, "name_locked": True},
]
PRODUCTS_A = [
    {"term_id": 10, "product_id": 501, "sku": "408045"},
    {"term_id": 11, "product_id": 501, "sku": "408045"},
    {"term_id": 11, "product_id": 502, "sku": "112"},
    # membership referencing an unknown term must be dropped, not inserted
    {"term_id": 999, "product_id": 503, "sku": "GHOST"},
]


@pytest.fixture
def catmgr_enabled(monkeypatch):
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    monkeypatch.setenv("CATMGR_DEV_URL", "https://dev.example.test/wp-json/arb/v1/logo-admin/categories")
    monkeypatch.setenv("CATMGR_DEV_USER", "svc")
    monkeypatch.setenv("CATMGR_DEV_APP_PASSWORD", "pw pw pw")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _import(env="dev", blog_id=1, terms=TERMS_A, products=PRODUCTS_A, actor="tester"):
    with database.cursor(write=True, actor=actor) as cur:
        return categories_service.import_blog_snapshot(
            cur, env=env, blog_id=blog_id, blog_path="/", terms=terms,
            products=products, actor=actor,
        )


def test_import_fresh_blog_snapshot():
    result = _import()
    assert result == {
        "blog_id": 1, "version": 1, "term_count": 2, "membership_count": 3,
    }
    with database.cursor() as cur:
        cur.execute(
            "SELECT slug, name, parent_term_id, name_locked, snapshot_version"
            "  FROM catmgr.wp_term WHERE env='dev' AND blog_id=1 ORDER BY term_id"
        )
        rows = cur.fetchall()
        assert [r["slug"] for r in rows] == ["mens", "mens-t-shirts"]
        assert rows[0]["parent_term_id"] == 0
        assert rows[1]["name_locked"] is True
        assert {r["snapshot_version"] for r in rows} == {1}
        cur.execute(
            "SELECT count(*) AS n FROM catmgr.wp_term_product"
            " WHERE env='dev' AND blog_id=1"
        )
        assert cur.fetchone()["n"] == 3  # ghost membership dropped
        cur.execute(
            "SELECT actor, action, entity_key FROM catmgr.audit_log ORDER BY id DESC LIMIT 1"
        )
        audit = cur.fetchone()
        assert audit["action"] == "snapshot_import"
        assert audit["actor"] == "tester"
        assert audit["entity_key"] == "dev:1"


def test_reimport_full_replace_bumps_version():
    _import()
    new_terms = [
        {"term_id": 10, "slug": "mens", "name": "Men's RENAMED", "parent": 0,
         "description": "", "count": 1, "sort_order": 1, "thumbnail_id": 0,
         "name_locked": False},
        {"term_id": 12, "slug": "womens", "name": "Women's", "parent": 0,
         "description": "", "count": 0, "sort_order": 2, "thumbnail_id": 0,
         "name_locked": False},
    ]
    result = _import(terms=new_terms, products=[
        {"term_id": 10, "product_id": 501, "sku": "408045"},
    ])
    assert result["version"] == 2
    assert result["term_count"] == 2
    assert result["membership_count"] == 1
    with database.cursor() as cur:
        cur.execute(
            "SELECT slug FROM catmgr.wp_term WHERE env='dev' AND blog_id=1 ORDER BY term_id"
        )
        assert [r["slug"] for r in cur.fetchall()] == ["mens", "womens"]
        cur.execute("SELECT version, term_count FROM catmgr.snapshot WHERE env='dev' AND blog_id=1")
        snap = cur.fetchone()
        assert snap["version"] == 2 and snap["term_count"] == 2


def test_environments_are_isolated():
    _import(env="dev")
    _import(env="prod", terms=[TERMS_A[0]], products=[])
    with database.cursor() as cur:
        cur.execute("SELECT env, count(*) AS n FROM catmgr.wp_term GROUP BY env ORDER BY env")
        assert {r["env"]: r["n"] for r in cur.fetchall()} == {"dev": 2, "prod": 1}


def test_snapshot_status_shape():
    _import(blog_id=2)
    _import(blog_id=1)
    with database.cursor() as cur:
        rows = categories_service.snapshot_status(cur, "dev")
    assert [r["blog_id"] for r in rows] == [1, 2]
    assert set(rows[0]) >= {
        "blog_id", "blog_path", "version", "imported_at", "term_count",
        "membership_count",
    }


def test_import_rejects_bad_payload():
    with pytest.raises(ValueError):
        _import(terms=[{"slug": "no-term-id"}])
    with pytest.raises(ValueError):
        _import(env="staging")


# ---------------------------------------------------------------- config


def test_catmgr_disabled_by_default():
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.catmgr_enabled is False
    assert dict(settings.catmgr_targets) == {}


def test_catmgr_enabled_requires_a_target(monkeypatch):
    monkeypatch.setenv("CATMGR_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError):
        get_settings()
    get_settings.cache_clear()


def test_catmgr_partial_target_rejected(monkeypatch):
    monkeypatch.setenv("CATMGR_DEV_URL", "https://dev.example.test/base")
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError):
        get_settings()
    get_settings.cache_clear()


def test_catmgr_target_parsing(catmgr_enabled):
    target = catmgr_enabled.catmgr_targets["dev"]
    assert target.host == "dev.example.test"
    assert target.base_url.endswith("/categories")
    assert "prod" not in catmgr_enabled.catmgr_targets


# ---------------------------------------------------------------- routes


def test_routes_hidden_when_disabled(client_as):
    client = client_as()
    assert client.get("/api/categories/targets").status_code == 404
    assert client.post(
        "/api/categories/snapshots/import", json={"env": "dev", "blog_ids": [1]}
    ).status_code == 404


def test_targets_and_import_routes(client_as, catmgr_enabled, monkeypatch):
    client = client_as()

    response = client.get("/api/categories/targets")
    assert response.status_code == 200
    assert response.json()["targets"] == [{"env": "dev", "host": "dev.example.test"}]

    response = client.get("/api/categories/snapshots", params={"env": "dev"})
    assert response.status_code == 200
    assert response.json() == {"env": "dev", "blogs": []}

    def fake_export(env, blog_id):
        assert env == "dev"
        return {
            "blog_id": blog_id, "blog_path": f"/blog{blog_id}/",
            "terms": TERMS_A, "products": PRODUCTS_A,
        }

    monkeypatch.setattr(categories_service, "fetch_export", fake_export)
    response = client.post(
        "/api/categories/snapshots/import", json={"env": "dev", "blog_ids": [1, 2]}
    )
    assert response.status_code == 200
    body = response.json()
    assert [r["blog_id"] for r in body["results"]] == [1, 2]
    assert all(r["ok"] and r["version"] == 1 for r in body["results"])

    response = client.get("/api/categories/snapshots", params={"env": "dev"})
    blogs = response.json()["blogs"]
    assert [b["blog_id"] for b in blogs] == [1, 2]
    assert blogs[0]["term_count"] == 2

    # unknown environment -> 404 (not configured)
    assert client.get(
        "/api/categories/snapshots", params={"env": "prod"}
    ).status_code == 404


def test_import_reports_per_blog_failures(client_as, catmgr_enabled, monkeypatch):
    client = client_as()

    def flaky_export(env, blog_id):
        if blog_id == 2:
            raise categories_service.BrokerError("boom", 502)
        return {"blog_id": blog_id, "blog_path": "/", "terms": TERMS_A,
                "products": []}

    monkeypatch.setattr(categories_service, "fetch_export", flaky_export)
    response = client.post(
        "/api/categories/snapshots/import", json={"env": "dev", "blog_ids": [1, 2]}
    )
    assert response.status_code == 200
    results = {r["blog_id"]: r for r in response.json()["results"]}
    assert results[1]["ok"] is True
    assert results[2]["ok"] is False and "boom" in results[2]["error"]


def test_blogs_proxy(client_as, catmgr_enabled, monkeypatch):
    client = client_as()
    monkeypatch.setattr(
        categories_service, "fetch_blogs",
        lambda env: [{"blog_id": 1, "path": "/", "name": "Arborwear"}],
    )
    response = client.get("/api/categories/blogs", params={"env": "dev"})
    assert response.status_code == 200
    assert response.json()["blogs"][0]["blog_id"] == 1
