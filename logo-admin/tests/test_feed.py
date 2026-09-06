"""Machine feed endpoints: bearer auth, keyset paging, tombstones, telemetry.

Fixtures (tests/sql/seed.sql): consumer 'feedtest' authenticates with bearer
token 'feed-test-token'; 'feedoff' exists but is inactive. S_FEEDDEAD carries
one live row and one is_active=false tombstone.
"""

import threading
import time

import psycopg2
import pytest
from fastapi.testclient import TestClient

import main
from db import database
from tests.conftest import TEST_ADMIN_DSN
from tests.test_rule_agent_tools import _admin

TOKEN = "feed-test-token"
CURATED_BLOG = 4242


@pytest.fixture
def feed_client():
    # Production versions come from the warehouse transform, which this
    # fixture does not run. Seed a unique positive feed cursor per row.
    _admin("""WITH versions AS (
                  SELECT ctid, row_number() OVER (ORDER BY fdm4_store, catalog_id, sku) AS version
                    FROM woo.store_product_state
              ) UPDATE woo.store_product_state s SET row_version=v.version
                  FROM versions v WHERE s.ctid=v.ctid""")
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


def test_feed_products_never_exceeds_the_ceiling_it_reports(feed_client):
    first = _get(feed_client, "/feed/products", since_version=0, limit=2).json()
    ceiling = first["version_ceiling"]
    assert first["rows"]
    assert all(row["row_version"] <= ceiling for row in first["rows"])

    # A refresh commits mid-walk and moves every row the consumer has not
    # reached above the ceiling it was given. Pinning that ceiling has to keep
    # the new generation out of this walk.
    _admin(
        "UPDATE woo.store_product_state SET row_version = row_version + 1000"
        " WHERE row_version > %s",
        (first["rows"][-1]["row_version"],),
    )
    pinned = _get(
        feed_client, "/feed/products",
        since_version=first["next_since_version"], ceiling=ceiling, limit=100,
    ).json()
    assert pinned["version_ceiling"] == ceiling
    assert pinned["rows"] == []
    assert pinned["next_since_version"] is None

    # Without the pin the same request sees the newer generation, so it was
    # the ceiling that withheld those rows, not an empty table.
    fresh = _get(
        feed_client, "/feed/products",
        since_version=first["next_since_version"], limit=100,
    ).json()
    assert fresh["rows"]
    assert fresh["version_ceiling"] > ceiling


def test_feed_products_ceiling_cannot_be_raised_above_the_real_maximum(
    feed_client,
):
    current = _get(feed_client, "/feed/version").json()["version"]
    payload = _get(
        feed_client, "/feed/products", since_version=0, ceiling=current + 5000,
    ).json()
    assert payload["version_ceiling"] == current


def test_snapshot_cursor_reads_one_frozen_generation():
    with database.cursor(snapshot=True) as cursor:
        cursor.execute("SHOW transaction_isolation")
        assert cursor.fetchone()["transaction_isolation"] == "repeatable read"
        cursor.execute("SELECT count(*) AS n FROM woo.store_product_state")
        before = int(cursor.fetchone()["n"])
        _admin(
            "INSERT INTO woo.store_product_state (fdm4_store, catalog_id, sku,"
            " kind, style_code, name, status, price, stock, payload,"
            " content_hash, is_active) VALUES ('S_SNAP','S_SNAP_catalog',"
            "'SNAP-1','parent','SNAP-1','Snap','publish',1,1,'{}'::jsonb,"
            "'snap-1',true)"
        )
        cursor.execute("SELECT count(*) AS n FROM woo.store_product_state")
        assert int(cursor.fetchone()["n"]) == before

    # The default cursor keeps the old behaviour for everything else.
    with database.cursor() as cursor:
        cursor.execute("SHOW transaction_isolation")
        assert cursor.fetchone()["transaction_isolation"] == "read committed"
        cursor.execute("SELECT count(*) AS n FROM woo.store_product_state")
        assert int(cursor.fetchone()["n"]) == before + 1


def _import_curated(terms, memberships):
    """One full-replace import of the curated tree, exactly as
    infra/import-curated-categories.sh does it: delete then load, in one
    transaction, so every row of one import shares an imported_at."""

    connection = psycopg2.connect(TEST_ADMIN_DSN)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM curated.category_product WHERE blog_id = %s",
                (CURATED_BLOG,),
            )
            cursor.execute(
                "DELETE FROM curated.category WHERE blog_id = %s",
                (CURATED_BLOG,),
            )
            for term_id, slug in terms:
                cursor.execute(
                    "INSERT INTO curated.category"
                    " (blog_id, term_id, slug, name, path)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (CURATED_BLOG, term_id, slug, slug.title(), "/" + slug),
                )
            for term_id, product_id in memberships:
                cursor.execute(
                    "INSERT INTO curated.category_product"
                    " (blog_id, term_id, sku, product_id)"
                    " VALUES (%s, %s, %s, %s)",
                    (CURATED_BLOG, term_id, f"SKU-{product_id}", product_id),
                )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def curated_blog():
    _import_curated(
        [(10, "jackets"), (20, "gloves")],
        [(10, 101), (10, 102), (20, 201), (20, 202), (20, 203)],
    )
    yield
    _admin(
        "DELETE FROM curated.category_product WHERE blog_id = %s",
        (CURATED_BLOG,),
    )
    _admin("DELETE FROM curated.category WHERE blog_id = %s", (CURATED_BLOG,))


def test_feed_categories_pages_memberships_by_key(feed_client, curated_blog):
    first = _get(
        feed_client, "/feed/categories", blog_id=CURATED_BLOG, limit=2,
    ).json()
    assert {c["term_id"] for c in first["categories"]} == {10, 20}
    assert first["imported_at"]
    assert first["next_after_term_id"] == 10
    assert first["next_after_product_id"] == 102

    seen = list(first["memberships"])
    page = first
    for _ in range(10):
        if page["next_after_term_id"] is None:
            break
        page = _get(
            feed_client, "/feed/categories", blog_id=CURATED_BLOG, limit=2,
            imported_at=first["imported_at"],
            after_term_id=page["next_after_term_id"],
            after_product_id=page["next_after_product_id"],
        ).json()
        # The tree belongs to the first page only.
        assert page["categories"] == []
        assert page["imported_at"] == first["imported_at"]
        seen.extend(page["memberships"])
    assert page["next_after_term_id"] is None
    keys = [(m["term_id"], m["product_id"]) for m in seen]
    assert keys == sorted(keys)
    assert keys == [(10, 101), (10, 102), (20, 201), (20, 202), (20, 203)]


def test_feed_categories_refuses_a_walk_that_would_span_two_imports(
    feed_client, curated_blog,
):
    first = _get(
        feed_client, "/feed/categories", blog_id=CURATED_BLOG, limit=2,
    ).json()
    _import_curated([(30, "hats")], [(30, 301), (30, 302)])
    stale = _get(
        feed_client, "/feed/categories", blog_id=CURATED_BLOG, limit=2,
        imported_at=first["imported_at"],
        after_term_id=first["next_after_term_id"],
        after_product_id=first["next_after_product_id"],
    )
    assert stale.status_code == 409

    # Starting over gets the new generation whole.
    restarted = _get(
        feed_client, "/feed/categories", blog_id=CURATED_BLOG, limit=100,
    ).json()
    assert [c["term_id"] for c in restarted["categories"]] == [30]
    assert restarted["imported_at"] != first["imported_at"]


def test_feed_categories_rejects_half_a_membership_cursor(
    feed_client, curated_blog,
):
    assert _get(
        feed_client, "/feed/categories", blog_id=CURATED_BLOG, after_term_id=10,
    ).status_code == 400


def test_feed_categories_tree_and_memberships_come_from_one_import(
    feed_client, curated_blog,
):
    """An import that commits while the request is running must not split it.

    The importer takes ACCESS EXCLUSIVE on curated.category_product, so the
    handler's membership read blocks until the import commits. Under READ
    COMMITTED that read would return the new import's memberships next to the
    old tree; under the snapshot the whole request stays on one generation."""

    ready = threading.Event()

    def importer():
        connection = psycopg2.connect(TEST_ADMIN_DSN)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "LOCK TABLE curated.category_product"
                    " IN ACCESS EXCLUSIVE MODE"
                )
                ready.set()
                # Give the request time to get past the tree read and block on
                # the membership read.
                time.sleep(1.0)
                cursor.execute(
                    "DELETE FROM curated.category_product WHERE blog_id = %s",
                    (CURATED_BLOG,),
                )
                cursor.execute(
                    "DELETE FROM curated.category WHERE blog_id = %s",
                    (CURATED_BLOG,),
                )
                cursor.execute(
                    "INSERT INTO curated.category"
                    " (blog_id, term_id, slug, name, path)"
                    " VALUES (%s, 30, 'hats', 'Hats', '/hats')",
                    (CURATED_BLOG,),
                )
                cursor.execute(
                    "INSERT INTO curated.category_product"
                    " (blog_id, term_id, sku, product_id)"
                    " VALUES (%s, 30, 'SKU-301', 301)",
                    (CURATED_BLOG,),
                )
            connection.commit()
        finally:
            connection.close()

    thread = threading.Thread(target=importer)
    thread.start()
    try:
        assert ready.wait(10)
        payload = _get(
            feed_client, "/feed/categories", blog_id=CURATED_BLOG, limit=100,
        ).json()
    finally:
        thread.join(20)

    tree_terms = {category["term_id"] for category in payload["categories"]}
    assert tree_terms == {10, 20}
    assert payload["memberships"]
    assert all(
        membership["term_id"] in tree_terms
        for membership in payload["memberships"]
    )
    stamps = {category["imported_at"] for category in payload["categories"]}
    assert stamps == {payload["imported_at"]}
