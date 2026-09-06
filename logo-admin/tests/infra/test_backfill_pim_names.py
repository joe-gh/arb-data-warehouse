"""The name backfill must only ever fill a name that is still blank.

Its worklist comes from the hourly mirror and the run takes hours, so a name
someone typed in between must survive.
"""

import types

import pytest

from tests.infra.fakes import load_script

backfill = load_script("backfill_pim_variant_names.py", "infra_backfill_pim_variant_names")

WORKLIST_ROW = {
    "frmt_id": "9001",
    "frmt_ref": "00700000001",
    "new_name": "Field Shirt Black Large",
    "title": "Field Shirt",
}


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return None

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, **kwargs):
        return FakeCursor(self._rows)


@pytest.fixture
def harness(monkeypatch):
    state = types.SimpleNamespace(rows=[dict(WORKLIST_ROW)], live={}, patches=[])

    monkeypatch.setattr(backfill, "psycopg2", types.SimpleNamespace(
        connect=lambda **kwargs: FakeConnection(state.rows),
        extras=types.SimpleNamespace(RealDictCursor=object),
    ))
    monkeypatch.setattr(backfill, "api_key", lambda: "test-key")
    monkeypatch.setattr(backfill, "api_get_one", lambda entity, field, ref, key: state.live)

    def api_call(method, path, key, body=None):
        state.patches.append((method, path, body))
        return 200, {}

    monkeypatch.setattr(backfill, "api_call", api_call)
    monkeypatch.setenv("PIM_PUSH_ENABLED", "1")
    return state


def test_a_blank_name_is_filled_exactly_once(harness):
    harness.live = {"frmt_id": 9001, "frmt_variantname": ""}

    assert backfill.main(["--apply"]) == 0

    assert harness.patches == [
        ("PATCH", "/catalog/variants(9001)", {"frmt_variantname": "Field Shirt Black Large"})
    ]


def test_a_name_typed_since_the_worklist_is_left_alone(harness, capsys):
    harness.live = {"frmt_id": 9001, "frmt_variantname": "Ranger Shirt, Black, L"}

    assert backfill.main(["--apply"]) == 0

    assert harness.patches == []
    output = capsys.readouterr().out
    assert "already named in the PIM" in output
    assert "skipped=1" in output


def test_a_variant_that_vanished_is_skipped(harness, capsys):
    harness.live = None

    assert backfill.main(["--apply"]) == 0

    assert harness.patches == []
    assert "gone from the PIM" in capsys.readouterr().out


def test_a_variant_whose_id_moved_is_skipped(harness, capsys):
    harness.live = {"frmt_id": 9999, "frmt_variantname": ""}

    assert backfill.main(["--apply"]) == 0

    assert harness.patches == []
    assert "frmt_id changed" in capsys.readouterr().out


def test_skip_reason_reads_the_live_object_only():
    row = dict(WORKLIST_ROW)
    assert backfill.skip_reason({"frmt_id": 9001, "frmt_variantname": ""}, row) is None
    assert backfill.skip_reason({"frmt_id": 9001, "frmt_variantname": "   "}, row) is None
    assert backfill.skip_reason({"frmt_id": 9001, "frmt_variantname": None}, row) is None
    assert backfill.skip_reason({"frmt_id": 9001, "frmt_variantname": "Taken"}, row) is not None
    assert backfill.skip_reason(None, row) is not None


def test_the_worklist_ignores_retired_mirror_rows():
    assert "v.retired_at IS NULL" in backfill.WORKLIST_SQL
