"""woo.refresh_product_state(): price precedence and delta-version bumps.

Requires a provisioned test database (TEST_DATABASE_DSN +
TEST_DATABASE_ADMIN_DSN) with
sql/migrations/2026-09-06-transform-price-and-reactivation.sql applied.

The transform reads raw fdm4.* tables, which the reset/seed fixtures do not
own, so every row this module inserts is removed again in the fixture teardown.
"""

import json
import os

import psycopg2
import psycopg2.extras
import pytest


STORE = "S_PRICE"
CATALOG = "S_PRICE_Woo"
MILL = "P-MILL"
STYLES = ("PSTYLE-1", "PSTYLE-2", "PSTYLE-3")
ITEMS = ("ITEM-P1", "ITEM-P2", "ITEM-P3")


def _store_data(product_price=None, color_code="RED", color_price=None):
    product = {"color": [{"colorCode": color_code}]}
    if product_price is not None:
        product["customPrice"] = product_price
    if color_price is not None:
        product["color"][0]["customPrice"] = color_price
    return json.dumps({"product": [product]})


@pytest.fixture
def warehouse():
    """Admin connection with a minimal FDM4 catalog for one store."""

    connection = psycopg2.connect(os.environ["TEST_DATABASE_ADMIN_DSN"])
    connection.autocommit = False
    try:
        with connection.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cursor:
            cursor.execute(
                'INSERT INTO fdm4.mill ("mill-code", description)'
                " VALUES (%s, %s)",
                (MILL, "Price Test Mill"),
            )
            for style in STYLES:
                cursor.execute(
                    """
                    INSERT INTO fdm4.style
                        ("style-code", description, "item-status", "mill-code",
                         "product-code", "origin-country", harmonization)
                    VALUES (%s, %s, 'A', %s, 'CAT-P', 'US', 'HS-P')
                    """,
                    (style, f"{style} name", MILL),
                )
            # PSTYLE-1: colour price 0 masking a product price of 80.
            # PSTYLE-2: no catalog price at all (tier + retail decide).
            # PSTYLE-3: a plain product price, used for the version tests.
            for style, detail in (
                ("PSTYLE-1", _store_data("80", "RED", "0")),
                ("PSTYLE-2", _store_data(None, "BLU", None)),
                ("PSTYLE-3", _store_data("50", "GRN", None)),
            ):
                cursor.execute(
                    """
                    INSERT INTO fdm4.catalog_product_detail
                        (site_id, catalog_id, product_id, detail_type,
                         detail_value)
                    VALUES (%s, %s, %s, 'storeData', %s)
                    """,
                    (STORE, CATALOG, style, detail),
                )
            for style, color, item, upc, retail in (
                ("PSTYLE-1", "RED", "ITEM-P1", "UPC-P1", "100"),
                ("PSTYLE-2", "BLU", "ITEM-P2", "UPC-P2", "100"),
                ("PSTYLE-3", "GRN", "ITEM-P3", "UPC-P3", "30"),
            ):
                cursor.execute(
                    """
                    INSERT INTO fdm4."style-color"
                        ("style-code", "color-code", description)
                    VALUES (%s, %s, %s)
                    """,
                    (style, color, f"{color} colour"),
                )
                cursor.execute(
                    """
                    INSERT INTO fdm4."style-size"
                        ("style-code", "size-code", description,
                         "size-group-id")
                    VALUES (%s, 'M', 'Medium', 'SG-P')
                    """,
                    (style,),
                )
                cursor.execute(
                    """
                    INSERT INTO fdm4.item
                        ("item-number", "style-code", "color-code",
                         "size-code", "upc-code", "retail-price",
                         "sale-price", "web-active", active, "mill-code",
                         "item-name", "product-category", "origin-country",
                         harmonization, "item-status", "ean-code",
                         "def-cost", weight)
                    VALUES (%s, %s, %s, 'M', %s, %s,
                            '', 'True', 'True', %s,
                            %s, 'CAT-P', 'US', 'HS-P', 'A', 'EAN-P',
                            '5', '1')
                    """,
                    (item, style, color, upc, retail, MILL, f"{item} name"),
                )
            # A tier whose computed price is 0: it must not mask retail.
            cursor.execute(
                """
                INSERT INTO fdm4."price-list"
                    ("item-number", "base-price", "sale-price")
                VALUES ('ITEM-P2', '100', '0;0;0;0;0;0')
                """
            )
            cursor.execute(
                """
                INSERT INTO woo.pricing_tier
                    (tier_name, price_levels_key, is_msrp, sort_order)
                VALUES ('Corp 1', 'corp1', false, 1)
                ON CONFLICT (tier_name) DO NOTHING
                """
            )
            cursor.execute(
                """
                INSERT INTO woo.store_pricing_tier (fdm4_store, tier_name)
                VALUES (%s, 'Corp 1')
                """,
                (STORE,),
            )
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                'DELETE FROM fdm4.item WHERE "item-number" = ANY(%s)',
                (list(ITEMS),),
            )
            for table in ('fdm4.style', 'fdm4."style-color"',
                          'fdm4."style-size"'):
                cursor.execute(
                    f'DELETE FROM {table} WHERE "style-code" = ANY(%s)',
                    (list(STYLES),),
                )
            cursor.execute(
                "DELETE FROM fdm4.catalog_product_detail WHERE site_id = %s",
                (STORE,),
            )
            cursor.execute(
                'DELETE FROM fdm4."price-list" WHERE "item-number" = ANY(%s)',
                (list(ITEMS),),
            )
            cursor.execute(
                'DELETE FROM fdm4.mill WHERE "mill-code" = %s', (MILL,)
            )
            cursor.execute(
                "DELETE FROM woo.store_product_state WHERE fdm4_store = %s",
                (STORE,),
            )
        connection.commit()
        connection.close()


def _refresh(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT woo.refresh_product_state()")
    connection.commit()


def _row(connection, sku):
    with connection.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cursor:
        cursor.execute(
            """
            SELECT price, base_price, is_active, row_version, content_hash,
                   structural_hash, stockprice_hash, feed_hash, brand
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND catalog_id = %s AND sku = %s
            """,
            (STORE, CATALOG, sku),
        )
        row = cursor.fetchone()
    connection.commit()
    return dict(row) if row else None


def test_zero_colour_price_falls_through_to_the_product_price(warehouse):
    _refresh(warehouse)
    assert _row(warehouse, "UPC-P1")["price"] == 80


def test_zero_tier_price_falls_through_to_retail(warehouse):
    _refresh(warehouse)
    assert _row(warehouse, "UPC-P2")["price"] == 100


def test_unchanged_rows_keep_their_hash_and_version(warehouse):
    _refresh(warehouse)
    first = _row(warehouse, "UPC-P3")
    _refresh(warehouse)
    second = _row(warehouse, "UPC-P3")
    assert second["content_hash"] == first["content_hash"]
    assert second["row_version"] == first["row_version"]


def test_reactivation_advances_the_version_even_when_identical(warehouse):
    _refresh(warehouse)
    active = _row(warehouse, "UPC-P3")
    assert active["is_active"] is True

    with warehouse.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM fdm4.catalog_product_detail
             WHERE site_id = %s AND product_id = 'PSTYLE-3'
            """,
            (STORE,),
        )
    warehouse.commit()
    _refresh(warehouse)
    tombstoned = _row(warehouse, "UPC-P3")
    assert tombstoned["is_active"] is False
    assert tombstoned["row_version"] > active["row_version"]

    with warehouse.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fdm4.catalog_product_detail
                (site_id, catalog_id, product_id, detail_type, detail_value)
            VALUES (%s, %s, 'PSTYLE-3', 'storeData', %s)
            """,
            (STORE, CATALOG, _store_data("50", "GRN", None)),
        )
    warehouse.commit()
    _refresh(warehouse)
    restored = _row(warehouse, "UPC-P3")
    assert restored["is_active"] is True
    # Byte-identical content, but the row moved from tombstone to live: a
    # consumer sitting on the tombstone's version must be able to see it.
    assert restored["content_hash"] == active["content_hash"]
    assert restored["row_version"] > tombstoned["row_version"]


def test_feed_only_column_change_bumps_the_version_but_no_routing_hash(
    warehouse,
):
    _refresh(warehouse)
    before = _row(warehouse, "UPC-P3")
    with warehouse.cursor() as cursor:
        cursor.execute(
            'UPDATE fdm4.mill SET description = %s WHERE "mill-code" = %s',
            ("Price Test Mill renamed", MILL),
        )
    warehouse.commit()
    _refresh(warehouse)
    after = _row(warehouse, "UPC-P3")

    assert after["brand"] == "Price Test Mill renamed"
    assert after["row_version"] > before["row_version"]
    assert after["feed_hash"] != before["feed_hash"]
    # brand is outside every payload, so the Woo engine's routing hashes and
    # the content hash must not move.
    assert after["content_hash"] == before["content_hash"]
    assert after["structural_hash"] == before["structural_hash"]
    assert after["stockprice_hash"] == before["stockprice_hash"]
