"""Machine feed endpoints: bearer auth, keyset paging, tombstones, telemetry.

Fixtures (tests/sql/seed.sql): consumer 'feedtest' authenticates with bearer
token 'feed-test-token'; 'feedoff' exists but is inactive. S_FEEDDEAD carries
one live row and one is_active=false tombstone.
"""

import pytest
from fastapi.testclient import TestClient

import main
from db import database

TOKEN = "feed-test-token"


@pytest.fixture
def feed_client():
    client = TestClient(main.app)
    yield client
    client.close()


def _get(client, path, token=TOKEN, **params):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.get(path, headers=headers, params=params or None)


def test_feed_requires_valid_active_token(feed_client):
    assert feed_client.get("/feed/version").status_code == 401
    assert _get(feed_client, "/feed/version", token="wrong").status_code == 401
    # Inactive consumers are rejected even with their correct token.
    assert _get(
        feed_client, "/feed/version", token="feed-off-token"
    ).status_code == 401
    assert _get(feed_client, "/feed/version").status_code == 200


def test_feed_version_reports_ceiling_and_active_rows(feed_client):
    payload = _get(feed_client, "/feed/version").json()
    assert payload["version"] > 0
    assert payload["active_rows"] > 0
    assert payload["refreshed_at"]
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT max(row_version) AS v,"
            "       count(*) FILTER (WHERE is_active) AS a"
            "  FROM woo.store_product_state"
        )
        row = cursor.fetchone()
    assert payload["version"] == int(row["v"])
    assert payload["active_rows"] == int(row["a"])


def test_feed_products_pages_by_keyset_and_includes_tombstones(feed_client):
    first = _get(feed_client, "/feed/products", since_version=0, limit=5).json()
    assert len(first["rows"]) == 5
    assert first["next_since_version"] == first["rows"][-1]["row_version"]
    versions = [row["row_version"] for row in first["rows"]]
    assert versions == sorted(versions)

    # Walk the whole feed; it must terminate and cover every state row once.
    seen = []
    cursor_version = 0
    for _ in range(100):
        page = _get(
            feed_client, "/feed/products",
            since_version=cursor_version, limit=5,
        ).json()
        seen.extend(page["rows"])
        if page["next_since_version"] is None:
            break
        cursor_version = page["next_since_version"]
    with database.cursor() as db_cursor:
        db_cursor.execute("SELECT count(*) AS n FROM woo.store_product_state")
        total = int(db_cursor.fetchone()["n"])
    assert len(seen) == total
    assert len({row["row_version"] for row in seen}) == total
    by_sku = {(row["fdm4_store"], row["sku"]): row for row in seen}
    assert by_sku[("S_FEEDDEAD", "FEED-1")]["is_active"] is True
    assert by_sku[("S_FEEDDEAD", "FEED-2")]["is_active"] is False
    assert by_sku[("S_FEEDDEAD", "FEED-1")]["payload"] == {"price": "12"}
    assert page["version_ceiling"] == max(r["row_version"] for r in seen)


def test_feed_products_rejects_bad_paging_params(feed_client):
    assert _get(
        feed_client, "/feed/products", since_version=-1
    ).status_code == 422
    assert _get(feed_client, "/feed/products", limit=0).status_code == 422
    assert _get(feed_client, "/feed/products", limit=5001).status_code == 422


def test_feed_pull_stamps_consumer_telemetry(feed_client):
    page = _get(feed_client, "/feed/products", since_version=0, limit=3).json()
    reached = page["rows"][-1]["row_version"]
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT last_pull_at, last_pull_version"
            "  FROM woo.feed_consumer WHERE name = 'feedtest'"
        )
        row = cursor.fetchone()
    assert row["last_pull_at"] is not None
    assert int(row["last_pull_version"]) == int(reached)
    # Telemetry only moves forward.
    _get(feed_client, "/feed/products", since_version=0, limit=1)
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT last_pull_version FROM woo.feed_consumer"
            " WHERE name = 'feedtest'"
        )
        assert int(cursor.fetchone()["last_pull_version"]) == int(reached)


def test_feed_stores_lists_stores_with_blog_info(feed_client):
    payload = _get(feed_client, "/feed/stores").json()
    stores = {row["fdm4_store"]: row for row in payload["stores"]}
    assert "S_FEEDDEAD" in stores
    assert stores["S_FEEDDEAD"]["active_rows"] == 1
    assert "S_TEST" in stores
