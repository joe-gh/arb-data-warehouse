"""woo.eval_price_rules(): an exclusion only excludes what it names.

Requires a provisioned test database (TEST_DATABASE_DSN +
TEST_DATABASE_ADMIN_DSN) with
sql/migrations/2026-09-06-price-rule-null-exclusions.sql applied.

`NOT (p_brand = ANY(excl_brands))` used to evaluate to NULL for a row with an
unknown brand, which dropped the rule for that row entirely - so items whose
brand or category the transform could not resolve quietly escaped every rule
that carried any exclusion list.
"""

import os

import psycopg2
import psycopg2.extras
import pytest


@pytest.fixture
def excluding_rule():
    """One active -10% rule that excludes brand X and category CAT-X."""

    connection = psycopg2.connect(os.environ["TEST_DATABASE_ADMIN_DSN"])
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO woo.price_rule
                    (name, active, priority, effect_type, effect_value,
                     excl_brands, excl_categories)
                VALUES ('null-exclusion probe', true, 10, 'percent', -10,
                        ARRAY['X'], ARRAY['CAT-X'])
                RETURNING rule_id
                """
            )
            rule_id = cursor.fetchone()[0]
        connection.commit()
        yield connection, rule_id
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM woo.price_rule WHERE rule_id = %s", (rule_id,)
            )
        connection.commit()
        connection.close()


def _evaluate(connection, brand, category="CAT-OK"):
    with connection.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cursor:
        cursor.execute(
            """
            SELECT final_price, applied_rule_ids
              FROM woo.eval_price_rules(
                       'S_TEST', 'STYLE-1', %s, %s, 100, NULL, NULL)
            """,
            (brand, category),
        )
        row = dict(cursor.fetchone())
    connection.commit()
    return row


def test_excluded_brand_is_skipped(excluding_rule):
    connection, rule_id = excluding_rule
    result = _evaluate(connection, "X")
    assert result["final_price"] is None
    assert result["applied_rule_ids"] == []


def test_other_brand_still_gets_the_rule(excluding_rule):
    connection, rule_id = excluding_rule
    result = _evaluate(connection, "Y")
    assert result["final_price"] == 90
    assert result["applied_rule_ids"] == [rule_id]


def test_unknown_brand_is_not_treated_as_excluded(excluding_rule):
    connection, rule_id = excluding_rule
    result = _evaluate(connection, None)
    assert result["final_price"] == 90
    assert result["applied_rule_ids"] == [rule_id]


def test_unknown_category_is_not_treated_as_excluded(excluding_rule):
    connection, rule_id = excluding_rule
    result = _evaluate(connection, "Y", category=None)
    assert result["final_price"] == 90
    assert result["applied_rule_ids"] == [rule_id]


def test_excluded_category_is_still_skipped(excluding_rule):
    connection, rule_id = excluding_rule
    result = _evaluate(connection, "Y", category="CAT-X")
    assert result["final_price"] is None
    assert result["applied_rule_ids"] == []
