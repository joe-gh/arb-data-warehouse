"""Hard cardinality limits protect previews, journals, and bulk mutations."""

from uuid import uuid4

import pytest

from domain import InvalidCommand
import mutations
from mutations import MutationScope
import snapshots
import staging


class CountCursor:
    def __init__(self, count):
        self.count = count
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return {"row_count": self.count}


def test_assignment_count_rejects_cap_plus_one_before_mutation():
    cursor = CountCursor(mutations.MAX_ASSIGNMENT_MUTATION_ROWS + 1)
    with pytest.raises(InvalidCommand, match="row mutation limit"):
        mutations._bounded_assignment_count(
            cursor,
            "fdm4_store = %s AND product_style = %s",
            ("S_TEST", "STYLE-1"),
            label="Style operation",
        )
    query, params = cursor.executed[0]
    assert "LIMIT %s" in query
    assert params[-1] == mutations.MAX_ASSIGNMENT_MUTATION_ROWS + 1


class SnapshotCursor:
    def __init__(self, stats):
        self.stats = iter(stats)
        self.current = None
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))
        self.current = next(self.stats)

    def fetchall(self):
        row_count, max_bytes, scope_bytes = self.current
        return [{
            "row": None,
            "row_count": row_count,
            "max_row_bytes": max_bytes,
            "scope_bytes": scope_bytes,
        }]


def test_one_scope_snapshot_rejects_cap_plus_one():
    cursor = SnapshotCursor([(
        snapshots.MAX_SNAPSHOT_ROWS_PER_SCOPE + 1,
        1,
        snapshots.MAX_SNAPSHOT_ROWS_PER_SCOPE + 1,
    )])
    scope = MutationScope(
        "assignment_style",
        {"fdm4_store": "S_TEST", "product_style": "STYLE-1"},
    )
    with pytest.raises(InvalidCommand, match="snapshot row limit"):
        snapshots.snapshot_scopes(cursor, [scope])
    query, params = cursor.executed[0]
    assert "LIMIT %s" in query
    assert snapshots.MAX_SNAPSHOT_ROWS_PER_SCOPE + 1 in params[:-6]


def test_multi_scope_snapshot_enforces_total_row_limit(monkeypatch):
    per_scope = snapshots.MAX_SNAPSHOT_ROWS_PER_SCOPE
    cursor = object()
    scopes = [
        MutationScope(
            "assignment_style",
            {"fdm4_store": "S_TEST", "product_style": f"STYLE-{index}"},
        )
        for index in range(3)
    ]
    monkeypatch.setattr(
        snapshots,
        "_snapshot_one",
        lambda *_args, **_kwargs: {
            "scope": snapshots.scope_dict(scopes[0]),
            "table": "logo.assignment",
            "rows": [{}] * per_scope,
            "_bytes": 1,
        },
    )
    with pytest.raises(InvalidCommand, match="total exact-snapshot row"):
        snapshots.snapshot_scopes(cursor, scopes)


def test_snapshot_rejects_single_oversized_row_before_transfer():
    cursor = SnapshotCursor([(
        1,
        snapshots.MAX_SNAPSHOT_ROW_BYTES + 1,
        snapshots.MAX_SNAPSHOT_ROW_BYTES + 1,
    )])
    with pytest.raises(InvalidCommand, match="row exceeds.*byte"):
        snapshots._bounded_snapshot_rows(
            cursor,
            "SELECT 1 AS ordinal, '{}'::jsonb AS row",
            (),
        )
    assert cursor.fetchall()[0]["row"] is None


def test_restore_rejects_oversized_or_tampered_journal_before_delete():
    class NoSqlCursor:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("restore touched data before validating size")

    oversized = [{
        "scope": {
            "kind": "assignment_style",
            "key": {"fdm4_store": "S_TEST", "product_style": "STYLE-1"},
        },
        "table": "logo.assignment",
        "rows": [{}] * (snapshots.MAX_SNAPSHOT_ROWS_PER_SCOPE + 1),
    }]
    with pytest.raises(InvalidCommand, match="snapshot rows"):
        snapshots.restore_state(NoSqlCursor(), oversized)


def _settings_state(store="S_TEST"):
    return [{
        "scope": {
            "kind": "store_settings_row",
            "key": {"fdm4_store": "S_TEST"},
        },
        "table": "logo.store_settings",
        "rows": [{
            "fdm4_store": store,
            "enabled": True,
            "allows_none": False,
            "updated_by": "fixture",
            "updated_at": "2026-07-17T00:00:00+00:00",
            "extra_customers": [],
        }],
    }]


@pytest.mark.parametrize("mutate,match", [
    (
        lambda state: state[0].update({"table": "woo.store_pricing_tier"}),
        "table does not match",
    ),
    (
        lambda state: state[0]["rows"][0].update({"fdm4_store": "S_OTHER"}),
        "outside its declared scope",
    ),
    (
        lambda state: state[0]["rows"][0].update({"unexpected": "value"}),
        "columns do not match",
    ),
])
def test_restore_rejects_scope_table_key_or_column_tampering_before_dml(
    mutate,
    match,
):
    class NoSqlCursor:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("tampered restore reached DML")

    state = _settings_state()
    mutate(state)
    with pytest.raises(InvalidCommand, match=match):
        snapshots.restore_state(NoSqlCursor(), state)


def test_undo_rejects_oversized_stored_state_before_opening_write_cursor(
    monkeypatch,
):
    class ValidationCursor:
        def __init__(self):
            self.rows = iter([
                {
                    "id": uuid4(),
                    "status": "applied",
                    "affected_scopes": [_settings_state()[0]["scope"]],
                    "affected_scopes_oversized": False,
                },
                {
                    "id": uuid4(),
                    "preview_hash": "a" * 64,
                    "before_state": None,
                    "after_state": None,
                    "before_oversized": True,
                    "after_oversized": False,
                },
            ])

        def execute(self, _query, _params=None):
            pass

        def fetchone(self):
            return next(self.rows)

    class CursorContext:
        def __init__(self, cursor):
            self.cursor = cursor

        def __enter__(self):
            return self.cursor

        def __exit__(self, *_args):
            return False

    class ValidationOnlyDatabase:
        def __init__(self):
            self.calls = []
            self.validation_cursor = ValidationCursor()

        def cursor(self, *, write=False, **_kwargs):
            self.calls.append(write)
            if write:
                raise AssertionError("undo opened a write cursor before validation")
            return CursorContext(self.validation_cursor)

    fake_database = ValidationOnlyDatabase()
    monkeypatch.setattr(staging, "database", fake_database)
    with pytest.raises(InvalidCommand, match="undo state exceeds"):
        staging.undo_change_set(uuid4(), "admin-one")
    assert fake_database.calls == [False]
