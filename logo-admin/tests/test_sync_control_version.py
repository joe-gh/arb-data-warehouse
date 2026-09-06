"""The loader's gate version must cover tombstones, not just live rows.

Requires a provisioned test database (TEST_DATABASE_DSN +
TEST_DATABASE_ADMIN_DSN).

A removal-only refresh bumps row_version on the rows it tombstones. When the
loader took its watermark from active rows only, that maximum did not move (it
could even fall back to an older number), so the WordPress reconcile - which is
gated on a newer successful pull - skipped the run that carried the removal.
The test runs the loader's own SQL, taken from the shipped source, against a
state table holding exactly one live row and one newer tombstone.
"""

import importlib.util
import os
from pathlib import Path
import re

import psycopg2
import pytest

# Load by file path: the repo root's `infra/` namespace package is shadowed by
# the regular `logo-admin/infra/` package under pytest's rootdir, so a plain
# `from infra import load_dump` can never resolve to the pipeline module.
pytest.importorskip("psycopg2")  # load_dump imports it at module scope
_LOAD_DUMP_PATH = Path(__file__).resolve().parents[2] / "infra" / "load_dump.py"
_spec = importlib.util.spec_from_file_location("pipeline_load_dump", _LOAD_DUMP_PATH)
load_dump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_dump)


STORE = "S_GATE"
CATALOG = "S_GATE_Woo"


def _gate_query():
    """The exact statement load_dump.main() runs after the transform."""

    source = _LOAD_DUMP_PATH.read_text()
    match = re.search(
        r'"""\s*(SELECT COALESCE\(MAX\(row_version\).*?)"""', source, re.S
    )
    assert match is not None, "load_dump no longer contains the gate query"
    return match.group(1)


@pytest.fixture
def state_rows():
    connection = psycopg2.connect(os.environ["TEST_DATABASE_ADMIN_DSN"])
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM woo.store_product_state WHERE fdm4_store <> %s",
                (STORE,),
            )
            cursor.executemany(
                """
                INSERT INTO woo.store_product_state
                    (fdm4_store, catalog_id, sku, kind, payload, content_hash,
                     is_active, row_version)
                VALUES (%s, %s, %s, 'variation', '{}'::jsonb, %s, %s, %s)
                """,
                [
                    (STORE, CATALOG, "GATE-LIVE", "hash-live", True, 100),
                    (STORE, CATALOG, "GATE-GONE", "hash-gone", False, 200),
                ],
            )
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM woo.store_product_state WHERE fdm4_store = %s",
                (STORE,),
            )
        connection.commit()
        connection.close()


def test_gate_version_follows_the_newest_tombstone(state_rows):
    with state_rows.cursor() as cursor:
        cursor.execute(_gate_query())
        refresh_version, active_rows = cursor.fetchone()
    state_rows.commit()
    assert (int(refresh_version), int(active_rows)) == (200, 1)


def test_gate_query_does_not_filter_the_version_by_is_active():
    query = " ".join(_gate_query().split())
    # The is_active test may only narrow the row count, never the whole scan.
    assert "FILTER (WHERE is_active)" in query
    assert query.replace("FILTER (WHERE is_active)", "").count("is_active") == 0
