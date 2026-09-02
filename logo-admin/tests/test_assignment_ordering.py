"""Editor ordering: option rows follow sort_order (the storefront's key)."""

import psycopg2

from tests.conftest import TEST_ADMIN_DSN


def _seed_second_row(sort_order: int):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO logo.assignment
                    (fdm4_store, product_style, garment_color_code, option_row,
                     position, design_id, logo_code, color_scheme_id,
                     sort_order, updated_by)
                VALUES ('S_TEST', 'STYLE-1', 'RED', 2, 1, 'DESIGN-1', 'C1',
                        'SCHEME-1', %s, 'seed')
                """,
                (sort_order,),
            )


def test_style_read_orders_option_rows_by_sort_order(client_as):
    _seed_second_row(-5)
    client = client_as()
    payload = client.get("/api/style", params={"store": "S_TEST", "style": "STYLE-1"}).json()
    order = [(row["option_row"], row["position"]) for row in payload["assignments"]]
    assert order == [(2, 1), (1, 1), (1, 2)]


def _sort_orders(store="S_TEST", style="STYLE-1", color="RED"):
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT option_row, position, sort_order FROM logo.assignment "
                "WHERE fdm4_store=%s AND product_style=%s AND garment_color_code=%s "
                "ORDER BY option_row, position",
                (store, style, color),
            )
            return cursor.fetchall()


def test_reorder_renumbers_every_position_and_is_undoable(client_as):
    _seed_second_row(0)
    client = client_as()
    response = client.post("/api/assignments/reorder", json={
        "store": "S_TEST", "style": "STYLE-1", "garment_color_code": "RED",
        "option_rows": [2, 1], "apply_to": "color",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == 3 and body["colors"] == ["RED"]
    assert _sort_orders() == [(1, 1, 20), (1, 2, 20), (2, 1, 10)]

    undo = client.post("/api/bulk-apply/undo", json={"batch_id": body["batch_id"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["restored"] == 3
    assert _sort_orders() == [(1, 1, 0), (1, 2, 1), (2, 1, 0)]


def test_reorder_is_fenced_against_stale_row_sets(client_as):
    client = client_as()
    stale = client.post("/api/assignments/reorder", json={
        "store": "S_TEST", "style": "STYLE-1", "garment_color_code": "RED",
        "option_rows": [2, 1],
    })
    assert stale.status_code == 409
    missing = client.post("/api/assignments/reorder", json={
        "store": "S_TEST", "style": "STYLE-1", "garment_color_code": "GRN",
        "option_rows": [1],
    })
    assert missing.status_code == 404
    dupes = client.post("/api/assignments/reorder", json={
        "store": "S_TEST", "style": "STYLE-1", "garment_color_code": "RED",
        "option_rows": [1, 1],
    })
    assert dupes.status_code == 422


def test_reorder_style_wide_ranks_every_color_by_logo_identity(client_as):
    _seed_second_row(0)
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO logo.assignment
                    (fdm4_store, product_style, garment_color_code, option_row,
                     position, design_id, logo_code, color_scheme_id, updated_by)
                VALUES ('S_TEST','STYLE-1','BLU',1,1,'DESIGN-1','C1','SCHEME-1','seed'),
                       ('S_TEST','STYLE-1','BLU',2,1,'DESIGN-1','C1','SCHEME-1','seed'),
                       ('S_TEST','STYLE-1','GRN',1,1,'DESIGN-1','C1','SCHEME-1','seed')
                """
            )
    client = client_as()
    response = client.post("/api/assignments/reorder", json={
        "store": "S_TEST", "style": "STYLE-1", "garment_color_code": "RED",
        "option_rows": [2, 1], "apply_to": "style",
    })
    assert response.status_code == 200, response.text
    # RED itself follows the dragged order exactly (row 2 -> 10, row 1 -> 20);
    # every other color takes the GLOBAL rank of each row's logo identity,
    # which is the FDM4 design of its position-1 logo (thread colors and
    # placements differ per garment color but are "the same logo"). DESIGN-1
    # ranks first, so every BLU/GRN row gets 10 - equal values are fine, the
    # storefront and the editor both tie-break by option_row.
    assert sorted(response.json()["colors"]) == ["BLU", "GRN", "RED"]
    assert _sort_orders() == [(1, 1, 20), (1, 2, 20), (2, 1, 10)]
    assert _sort_orders(color="BLU") == [(1, 1, 10), (2, 1, 10)]
    assert _sort_orders(color="GRN") == [(1, 1, 10)]


def _clear_color_order():
    # tests/sql/reset.sql does not (yet) truncate logo.style_color_order, so
    # this test owns its own cleanup to stay order-independent.
    with psycopg2.connect(TEST_ADMIN_DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM logo.style_color_order")


def test_color_order_is_editor_only_and_global_per_style(client_as):
    _clear_color_order()
    client = client_as()
    saved = client.put("/api/style-color-order", json={"style": "STYLE-1", "colors": ["BLU", "RED"]})
    assert saved.status_code == 200, saved.text
    payload = client.get("/api/style", params={"store": "S_TEST", "style": "STYLE-1"}).json()
    assert [c["code"] for c in payload["colors"]] == ["BLU", "RED", "GRN"]
    assert [c["editor_order"] for c in payload["colors"]] == [10, 20, None]
    # full replace: dropping a color returns it to alphabetical tail
    client.put("/api/style-color-order", json={"style": "STYLE-1", "colors": ["GRN"]})
    payload = client.get("/api/style", params={"store": "S_TEST", "style": "STYLE-1"}).json()
    assert [c["code"] for c in payload["colors"]] == ["GRN", "BLU", "RED"]
    assert client.put("/api/style-color-order", json={"style": "STYLE-1", "colors": []}).status_code == 200
