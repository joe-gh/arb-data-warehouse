"""Editing what /feed/logos serves has to move the logo feed cursor.

logo_name comes from logo.display_name and price from logo.default_cost, but
the delta cursor lives on logo.assignment.row_version. Before
migrations/2026-09-06-logo-feed-dependency-bumps.sql a caught-up consumer kept
the old name or the old cost forever, because nothing re-versioned the
assignments those edits changed.

Seed facts used here (tests/sql/seed.sql): S_TEST/STYLE-1/RED carries two
assignments - position 1 (DESIGN-1 / SCHEME-1 / C1) has a name_override, and
position 2 (DESIGN-2 / SCHEME-2 / C2) has none and no cost_override.
"""

import pytest
from fastapi.testclient import TestClient

import main
from tests.test_rule_agent_tools import _admin

TOKEN = "feed-test-token"
STORE = "S_TEST"


@pytest.fixture
def feed_client():
    client = TestClient(main.app)
    yield client
    client.close()


def _get(client, path, **params):
    return client.get(
        path,
        headers={"Authorization": f"Bearer {TOKEN}"},
        params=params or None,
    )


def _logo_version(client):
    return int(_get(client, "/feed/version").json()["logo_version"])


def _served(client, since_version):
    rows = _get(
        client, "/feed/logos", since_version=since_version, store=STORE,
        limit=1000,
    ).json()["rows"]
    return {row["position"]: row for row in rows}


def _audit_count():
    return int(_admin(
        "SELECT count(*) AS n FROM logo.audit_log"
        " WHERE action = 'assignment_updated'"
    )[0][0])


def test_setting_a_global_display_name_moves_the_cursor(feed_client):
    before = _logo_version(feed_client)
    _admin(
        "INSERT INTO logo.display_name"
        " (design_id, color_scheme_id, name, fdm4_store)"
        " VALUES ('DESIGN-2', 'SCHEME-2', 'Global two', '')"
    )
    assert _logo_version(feed_client) > before
    served = _served(feed_client, before)
    assert served[2]["payload"]["logo_name"] == "Global two"
    # The overridden assignment serves the same name as before, so it stays put.
    assert 1 not in served


def test_setting_a_per_store_display_name_moves_the_cursor(feed_client):
    before = _logo_version(feed_client)
    _admin(
        "INSERT INTO logo.display_name"
        " (design_id, color_scheme_id, name, fdm4_store)"
        " VALUES ('DESIGN-2', 'SCHEME-2', 'Store two', %s)",
        (STORE,),
    )
    assert _logo_version(feed_client) > before
    served = _served(feed_client, before)
    assert served[2]["payload"]["logo_name"] == "Store two"


def test_clearing_a_display_name_moves_the_cursor(feed_client):
    _admin(
        "INSERT INTO logo.display_name"
        " (design_id, color_scheme_id, name, fdm4_store)"
        " VALUES ('DESIGN-2', 'SCHEME-2', 'Global two', '')"
    )
    before = _logo_version(feed_client)
    _admin(
        "DELETE FROM logo.display_name"
        " WHERE design_id = 'DESIGN-2' AND color_scheme_id = 'SCHEME-2'"
        "   AND fdm4_store = ''"
    )
    assert _logo_version(feed_client) > before
    served = _served(feed_client, before)
    # Back to the logo code, which is what the storefront falls back to.
    assert served[2]["payload"]["logo_name"] == "C2"


def test_setting_a_default_cost_moves_the_cursor(feed_client):
    before = _logo_version(feed_client)
    _admin(
        "INSERT INTO logo.default_cost (logo_code, color_scheme_id, cost)"
        " VALUES ('C2', 'SCHEME-2', 7.50)"
    )
    assert _logo_version(feed_client) > before
    served = _served(feed_client, before)
    assert served[2]["payload"]["price"] == "7.50"

    # Changing it again moves the cursor again.
    second = _logo_version(feed_client)
    _admin(
        "UPDATE logo.default_cost SET cost = 10.00"
        " WHERE logo_code = 'C2' AND color_scheme_id = 'SCHEME-2'"
    )
    assert _logo_version(feed_client) > second
    assert _served(feed_client, second)[2]["payload"]["price"] == "10.00"


def test_a_rename_that_changes_nothing_served_does_not_move_the_cursor(
    feed_client,
):
    # Every DESIGN-1 assignment carries a name_override, so renaming that
    # design changes nothing a consumer would receive.
    before = _logo_version(feed_client)
    _admin(
        "UPDATE logo.display_name SET name = 'Renamed global'"
        " WHERE design_id = 'DESIGN-1' AND color_scheme_id = 'SCHEME-1'"
        "   AND fdm4_store = ''"
    )
    assert _logo_version(feed_client) == before
    assert _served(feed_client, before) == {}


def test_a_cost_change_skips_assignments_with_their_own_price(feed_client):
    _admin(
        "UPDATE logo.assignment SET cost_override = 3.25"
        " WHERE fdm4_store = %s AND product_style = 'STYLE-1'"
        "   AND position = 2",
        (STORE,),
    )
    before = _logo_version(feed_client)
    _admin(
        "INSERT INTO logo.default_cost (logo_code, color_scheme_id, cost)"
        " VALUES ('C2', 'SCHEME-2', 7.50)"
    )
    assert _logo_version(feed_client) == before


def test_a_bump_writes_one_audit_row_per_assignment(feed_client):
    before = _audit_count()
    _admin(
        "INSERT INTO logo.display_name"
        " (design_id, color_scheme_id, name, fdm4_store)"
        " VALUES ('DESIGN-2', 'SCHEME-2', 'Global two', '')"
    )
    # Accepted cost of the bump: the assignment audit trigger sees row_version
    # move, so one history line per bumped assignment.
    assert _audit_count() == before + 1
