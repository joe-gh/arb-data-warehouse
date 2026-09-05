"""Names resolve only on exact matches, and resolutions precede human apply."""

import csv
from io import StringIO

import pytest

from db import database
import queries
import spreadsheet
from spreadsheet_mapping import SpreadsheetNameResolver, NameResolutionError
from snapshots import states_equal
from staging import undo_change_set
from tests.test_rule_agent_tools import _admin, _snapshot, USER
from tests.test_spreadsheet_workflow import _session_and_csv, _settings
from tests.test_warehouse_ops_tools import _apply
from mutations import MutationScope


@pytest.fixture
def names():
    _admin("INSERT INTO woo.store_blog_map (blog_id,fdm4_store,blog_path,blog_name) VALUES (900001,'S_TEST','/test-crew/','Test Crew'), (900002,'S_EMPTY','/empty-crew/','Empty Crew')")
    _admin("INSERT INTO logo.color_class (color_code,color_name,light_dark,source,updated_by) VALUES ('RED','Red','dark','manual','fixture'), ('BLU','Blue','dark','manual','fixture')")
    try:
        yield
    finally:
        _admin("DELETE FROM woo.store_blog_map WHERE blog_id IN (900001,900002)")


@pytest.mark.parametrize("store", ["Test Crew", "/test-crew/", "test-crew", "900001"])
def test_exact_store_aliases_resolve(names, store):
    with database.cursor() as cursor:
        values, report = SpreadsheetNameResolver(cursor).resolve({"fdm4_store": store}, 2)
    assert values["fdm4_store"] == "S_TEST"
    assert report == [{"row": 2, "field": "fdm4_store", "input": store, "code": "S_TEST", "status": "resolved from name"}]


def test_exact_style_color_and_design_names_resolve(names):
    with database.cursor() as cursor:
        values, report = SpreadsheetNameResolver(cursor).resolve({"fdm4_store": "S_TEST", "product_style": "Style One", "garment_color_code": "Red", "design_id": "Test Logo"}, 3)
    assert values == {"fdm4_store": "S_TEST", "product_style": "STYLE-1", "garment_color_code": "RED", "design_id": "DESIGN-1"}
    # Red is also the canonical code RED, so it is recognized as a code first.
    assert {row["field"] for row in report} == {"product_style", "design_id"}


def test_exact_color_name_that_differs_from_code_resolves(names):
    with database.cursor() as cursor:
        values, report = SpreadsheetNameResolver(cursor).resolve({"fdm4_store": "S_TEST", "garment_color_code": "Blue"}, 2)
    assert values["garment_color_code"] == "BLU" and report[0]["status"] == "resolved from name"


@pytest.mark.parametrize("field,value", [("fdm4_store", "Crew"), ("product_style", "Style"), ("garment_color_code", "Bluish"), ("design_id", "Logo")])
def test_partial_and_unmatched_values_are_errors_with_candidates(names, field, value):
    with database.cursor() as cursor:
        with pytest.raises(NameResolutionError) as error:
            SpreadsheetNameResolver(cursor).resolve({"fdm4_store": "S_TEST", field: value}, 2)
    assert error.value.field == field and "Candidates:" in str(error.value)
    assert len(error.value.candidates) <= 10


@pytest.mark.parametrize("field,value", [("fdm4_store", "Test Crew"), ("product_style", "Style One"), ("garment_color_code", "Blue"), ("design_id", "Test Logo")])
def test_duplicate_exact_names_are_never_guessed(names, field, value):
    if field == "fdm4_store":
        _admin("UPDATE woo.store_blog_map SET blog_name='Test Crew' WHERE blog_id=900002")
    elif field == "product_style":
        _admin("UPDATE woo.store_product_state SET name='Style One' WHERE style_code='STYLE-2' AND kind='parent'")
    elif field == "garment_color_code":
        _admin("UPDATE logo.color_class SET color_name='Blue' WHERE color_code='RED'")
    else:
        _admin("UPDATE fdm4.dec_design SET description='Test Logo', web_description='Test Logo' WHERE design_id='DESIGN-2'")
    with database.cursor() as cursor:
        with pytest.raises(NameResolutionError) as error:
            SpreadsheetNameResolver(cursor).resolve({"fdm4_store": "S_TEST", field: value}, 2)
    assert len(error.value.candidates) == 2


def test_truncated_lookup_cannot_claim_a_unique_exact_name(names, monkeypatch):
    real = queries.list_styles
    monkeypatch.setattr(queries, "list_styles", lambda *a, **kw: {**real(*a, **kw), "truncated": True})
    with database.cursor() as cursor:
        with pytest.raises(NameResolutionError, match="incomplete lookup"):
            SpreadsheetNameResolver(cursor).resolve({"fdm4_store": "S_TEST", "product_style": "Style One"}, 2)


@pytest.mark.asyncio
async def test_confirmation_reports_resolutions_and_row_errors_before_apply(names, tmp_path):
    session_id, row, data = _session_and_csv()
    rows = list(csv.DictReader(StringIO(data.decode())))
    rows[0].update({"fdm4_store": "Test Crew", "product_style": "Style One", "garment_color_code": "Blue", "design_id": "Test Logo", "position": "1", "logo_code": "C1", "color_scheme_id": "SCHEME-1"})
    rows.append({**rows[0], "product_style": "Style"})
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=spreadsheet.ASSIGNMENT_COLUMNS)
    writer.writeheader(); writer.writerows(rows)
    scopes = (MutationScope("assignment_store", {"fdm4_store": "S_TEST"}),)
    before = _snapshot(scopes)
    settings = _settings(tmp_path)
    job = await spreadsheet.create_spreadsheet_job(session_id, USER, stream.getvalue().encode(), "names.csv", "text/csv", "", settings)
    result = spreadsheet.confirm_spreadsheet_mapping(job["id"], USER, job["mapping_revision"], job["mapping_hash"], 50, settings)
    assert result["status"] == "staged"
    assert len(result["resolutions"]) == 4
    assert all(item["status"] == "resolved from name" for item in result["resolutions"])
    assert result["rejected_rows"][0]["row"] == 3
    assert len(result["rejected_rows"][0]["candidates"]) == 2
    assert result["mapping"]["_resolutions"] == result["resolutions"]
    assert states_equal(before, _snapshot(scopes))
    retry = spreadsheet.confirm_spreadsheet_mapping(job["id"], USER, job["mapping_revision"], job["mapping_hash"], 50, settings)
    assert retry["resolutions"] == result["resolutions"]
    _apply(result["change_set"])
    assert _admin("SELECT design_id FROM logo.assignment WHERE fdm4_store='S_TEST' AND product_style='STYLE-1' AND garment_color_code='BLU'") == [("DESIGN-1",)]
    undo_change_set(result["change_set"]["id"], USER)
    assert states_equal(before, _snapshot(scopes))


def test_design_scheme_name_resolves_even_when_search_displays_another_name(names):
    _admin("INSERT INTO logo.display_name (design_id,color_scheme_id,name,source,locked,uses,updated_by,fdm4_store) VALUES ('DESIGN-1','ALT','Alternate crew logo','manual',true,0,'fixture','S_TEST')")
    with database.cursor() as cursor:
        values, report = SpreadsheetNameResolver(cursor).resolve({"fdm4_store": "S_TEST", "design_id": "Alternate crew logo"}, 2)
    assert values["design_id"] == "DESIGN-1" and report[0]["status"] == "resolved from name"
