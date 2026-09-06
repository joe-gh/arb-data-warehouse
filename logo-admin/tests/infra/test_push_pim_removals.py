"""An approved PIM deletion must recheck the premise it was approved on.

A removal is staged because FDM4 no longer carries the sku and because the
variant on the review sheet looked a certain way. Both can change between the
diff and the apply, so both are rechecked, and a batch bigger than a person
can review is refused whole.
"""

import types

import pytest

from tests.infra.fakes import load_script

push = load_script("push_pim.py", "infra_push_pim")

STAGED = {"frmt_colorname": "Black", "frmt_sizelabel": "L"}
LIVE = {"frmt_id": 9001, "frmt_ref": "00700000001",
        "frmt_colorname": "Black", "frmt_sizelabel": "L"}


class FakeCursor:
    """The apply loop's own cursor: answers the FDM4 presence lookup."""

    def __init__(self, fdm4_refs=()):
        self.fdm4 = {ref.upper() for ref in fdm4_refs}
        self.executed = []
        self._last = ("", None)

    def execute(self, sql, params=None):
        self._last = (sql, params)
        self.executed.append((sql, params))

    def fetchone(self):
        sql, params = self._last
        if "fdm4.item" in sql:
            return (1,) if params[0] in self.fdm4 else None
        return None

    def fetchall(self):
        return []


def removal_row(before=None):
    return {
        "row_id": 1,
        "action": "variant_remove",
        "prod_ref": "P1",
        "frmt_ref": "00700000001",
        "before": STAGED if before is None else before,
        "after": None,
    }


@pytest.fixture
def api(monkeypatch):
    """Records every call; api_get_one's answer is set per test."""
    recorder = types.SimpleNamespace(live=dict(LIVE), calls=[])

    def api_get_one(entity, ref_field, ref, key):
        recorder.calls.append(("GET", entity, ref))
        return recorder.live

    def api_call(method, path, key, body=None):
        recorder.calls.append((method, path, body))
        return 200, {}

    monkeypatch.setattr(push, "api_get_one", api_get_one)
    monkeypatch.setattr(push, "api_call", api_call)
    monkeypatch.setattr(push, "api_key", lambda: "test-key")
    return recorder


def test_a_sku_that_came_back_to_fdm4_is_not_deleted(api):
    cursor = FakeCursor(fdm4_refs=["00700000001"])

    outcome, detail = push.apply_row(removal_row(), "key", True, True, cursor)

    assert (outcome, detail) == ("skipped", "back in FDM4 since diff")
    assert [call for call in api.calls if call[0] == "DELETE"] == []


def test_a_variant_that_changed_since_the_sheet_is_not_deleted(api):
    api.live = dict(LIVE, frmt_colorname="Forest Green")
    cursor = FakeCursor()

    outcome, detail = push.apply_row(removal_row(), "key", True, True, cursor)

    assert outcome == "skipped"
    assert detail.startswith("frmt_colorname changed since diff")
    assert [call for call in api.calls if call[0] == "DELETE"] == []


def test_a_resized_variant_is_not_deleted(api):
    api.live = dict(LIVE, frmt_sizelabel="XL")
    cursor = FakeCursor()

    outcome, detail = push.apply_row(removal_row(), "key", True, True, cursor)

    assert outcome == "skipped"
    assert detail.startswith("frmt_sizelabel changed since diff")


def test_a_variant_already_gone_from_the_pim_is_skipped(api):
    api.live = None
    cursor = FakeCursor()

    assert push.apply_row(removal_row(), "key", True, True, cursor) == ("skipped", "already gone")


def test_removals_still_need_the_flag(api):
    cursor = FakeCursor()

    outcome, detail = push.apply_row(removal_row(), "key", True, False, cursor)

    assert (outcome, detail) == ("skipped", "removals require --allow-removals")
    assert api.calls == []


def test_a_still_valid_removal_is_applied(api):
    cursor = FakeCursor()

    outcome, detail = push.apply_row(removal_row(), "key", True, True, cursor)

    assert (outcome, detail) == ("applied", "")
    assert ("DELETE", "/catalog/variants(9001)", None) in api.calls


def test_a_dry_run_still_sends_nothing(api):
    cursor = FakeCursor()

    outcome, detail = push.apply_row(removal_row(), "key", False, True, cursor)

    assert (outcome, detail) == ("skipped", "dry run")
    assert [call for call in api.calls if call[0] == "DELETE"] == []


def test_the_precondition_read_covers_the_fields_the_sheet_showed():
    selected = push.GET_SELECT["variants"].split(",")
    assert "frmt_sizelabel" in selected
    assert "frmt_variantname" in selected


class FakeApplyCursor(FakeCursor):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, **kwargs):
        return FakeApplyCursor(self._rows)

    def commit(self):
        return None


def test_a_set_over_the_removal_cap_is_refused_whole(api, monkeypatch, capsys):
    rows = [dict(removal_row(), row_id=index) for index in range(push.REMOVAL_CAP + 1)]
    monkeypatch.setattr(push, "connect", lambda: FakeConnection(rows))

    push.apply_set(7, 100000, allow_removals=True)

    assert "refusing set 7" in capsys.readouterr().out
    assert api.calls == []


def test_a_set_at_the_cap_still_runs(api, monkeypatch, capsys):
    api.live = None  # every row skips as "already gone"; nothing is deleted
    rows = [dict(removal_row(), row_id=index) for index in range(push.REMOVAL_CAP)]
    monkeypatch.setattr(push, "connect", lambda: FakeConnection(rows))

    push.apply_set(8, 100000, allow_removals=True)

    output = capsys.readouterr().out
    assert "refusing set" not in output
    assert f"{push.REMOVAL_CAP} skipped" in output
