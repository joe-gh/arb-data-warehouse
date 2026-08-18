from db import database
from tests.fixtures import ADMIN_ONE, ADMIN_TWO, COLORS, STORE
from tests.state import snapshot_business_state


def test_identities_are_distinct():
    assert ADMIN_ONE["user_login"] != ADMIN_TWO["user_login"]


def test_seed_has_expected_assignment_pair():
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT position
              FROM logo.assignment
             WHERE fdm4_store = %s
             ORDER BY position
            """,
            (STORE,),
        )
        assert [row["position"] for row in cursor.fetchall()] == [1, 2]


def test_seed_has_three_warehouse_colors():
    with database.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT color_code
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND kind = 'variation'
               AND style_code = 'STYLE-1'
             ORDER BY color_code
            """,
            (STORE,),
        )
        assert {row["color_code"] for row in cursor.fetchall()} == set(COLORS)


def test_business_snapshot_is_deterministic():
    with database.cursor() as cursor:
        first = snapshot_business_state(cursor)
        second = snapshot_business_state(cursor)
    assert first == second
