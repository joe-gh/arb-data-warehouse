"""Logo feed versions are allocated in commit order.

logo.assignment.row_version comes from a sequence stamped by a BEFORE trigger.
Without a lock two concurrent editor writes can allocate 101 and 102 and commit
102 first, so a consumer that pages while 101 is uncommitted advances past it
and never sees that edit again. The advisory lock in
migrations/2026-09-06-logo-feed-commit-order.sql makes the writer hold the
sequence until it commits, so a lower version can never appear after a higher
one.
"""

import threading

import psycopg2
import pytest
from fastapi.testclient import TestClient

import main
from tests.conftest import TEST_ADMIN_DSN

TOKEN = "feed-test-token"
STORE_A = "S_VERA"
STORE_B = "S_VERB"

INSERT_ASSIGNMENT = """
    INSERT INTO logo.assignment
        (fdm4_store, product_style, garment_color_code, option_row, position,
         design_id, logo_code, color_scheme_id, location, updated_by)
    VALUES (%s, 'VER-STYLE', '', 1, 1, 'DESIGN-1', 'C1', 'SCHEME-1',
            'Left Chest', 'fixture')
    RETURNING row_version
"""


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


def test_a_late_commit_cannot_be_skipped_by_a_feed_cursor(feed_client):
    connection_a = psycopg2.connect(TEST_ADMIN_DSN)
    connection_b = psycopg2.connect(TEST_ADMIN_DSN)
    outcome: dict = {}
    finished = threading.Event()

    def write_b():
        try:
            with connection_b.cursor() as cursor:
                # Watchdog: if the lock is never released this fails the test
                # instead of hanging the suite.
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute(INSERT_ASSIGNMENT, (STORE_B,))
                outcome["version"] = int(cursor.fetchone()[0])
            connection_b.commit()
        except Exception as error:  # surfaced by the assertions below
            outcome["error"] = error
        finally:
            finished.set()

    thread = threading.Thread(target=write_b)
    try:
        with connection_a.cursor() as cursor:
            cursor.execute(INSERT_ASSIGNMENT, (STORE_A,))
            version_a = int(cursor.fetchone()[0])

        thread.start()
        # B has to wait for A: allocation is serialized until A ends.
        assert not finished.wait(1.0)
        assert "version" not in outcome

        # A consumer paging now cannot see either write, so its cursor stays
        # below A's version.
        page = _get(feed_client, "/feed/logos", since_version=0, limit=1000)
        assert page.status_code == 200
        payload = page.json()
        stored_cursor = payload["version_ceiling"]
        assert stored_cursor < version_a
        assert not [
            row for row in payload["rows"]
            if row["fdm4_store"] in (STORE_A, STORE_B)
        ]

        connection_a.commit()
        assert finished.wait(10)
        assert "error" not in outcome, outcome.get("error")
        assert outcome["version"] > version_a
    finally:
        thread.join(15)
        connection_a.rollback()
        connection_a.close()
        connection_b.rollback()
        connection_b.close()

    # The cursor the consumer stored before A committed still delivers A.
    rows = _get(
        feed_client, "/feed/logos", since_version=stored_cursor, limit=1000,
    ).json()["rows"]
    stores = {row["fdm4_store"] for row in rows}
    assert STORE_A in stores
    assert STORE_B in stores


def test_deleting_an_assignment_also_allocates_in_commit_order(feed_client):
    """The tombstone trigger takes the same lock, so a hard delete cannot
    publish a version below a cursor that has already moved on."""

    connection_a = psycopg2.connect(TEST_ADMIN_DSN)
    connection_b = psycopg2.connect(TEST_ADMIN_DSN)
    outcome: dict = {}
    finished = threading.Event()

    def write_b():
        try:
            with connection_b.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute(INSERT_ASSIGNMENT, (STORE_B,))
                outcome["version"] = int(cursor.fetchone()[0])
            connection_b.commit()
        except Exception as error:
            outcome["error"] = error
        finally:
            finished.set()

    thread = threading.Thread(target=write_b)
    try:
        with connection_a.cursor() as cursor:
            cursor.execute(
                "DELETE FROM logo.assignment"
                " WHERE fdm4_store = 'S_TEST' AND product_style = 'STYLE-1'"
                "   AND position = 2"
            )
            cursor.execute(
                "SELECT row_version FROM logo.assignment_tombstone"
                " WHERE fdm4_store = 'S_TEST' AND product_style = 'STYLE-1'"
                "   AND position = 2"
            )
            version_a = int(cursor.fetchone()[0])

        thread.start()
        assert not finished.wait(1.0)
        connection_a.commit()
        assert finished.wait(10)
        assert "error" not in outcome, outcome.get("error")
        assert outcome["version"] > version_a
    finally:
        thread.join(15)
        connection_a.rollback()
        connection_a.close()
        connection_b.rollback()
        connection_b.close()
