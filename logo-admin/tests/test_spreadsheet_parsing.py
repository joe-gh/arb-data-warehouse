"""Defensive CSV/XLSX parsing rejects active and oversized content."""

from io import BytesIO
import zipfile

import openpyxl
import pytest

from domain import InvalidCommand
from spreadsheet import (
    ASSIGNMENT_COLUMNS,
    SpreadsheetLimits,
    known_mapping,
    parse_spreadsheet,
    translate_rows,
)


def _xlsx(rows) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _zip(entries: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_utf8_bom_csv_is_normalized_and_formula_text_remains_data():
    parsed = parse_spreadsheet(
        b"\xef\xbb\xbfStore Code,Tier Name,Note\nS_TEST,MSRP,=1+1\n",
        "pricing.csv",
        SpreadsheetLimits(),
    )
    assert parsed.format_name == "csv"
    assert parsed.headers == ("store_code", "tier_name", "note")
    assert parsed.rows == ({
        "store_code": "S_TEST",
        "tier_name": "MSRP",
        "note": "=1+1",
    },)


@pytest.mark.parametrize(
    ("data", "filename", "message"),
    [
        (b"", "empty.csv", "empty"),
        (b"a,b\n1,2\n", "sheet.xls", "Only .csv and .xlsx"),
        (b"a,b\n1,\x00\n", "sheet.csv", "NUL"),
        (b"\xff\xfe", "sheet.csv", "UTF-8"),
    ],
)
def test_invalid_file_types_encodings_and_nul_are_rejected(data, filename, message):
    with pytest.raises(InvalidCommand, match=message):
        parse_spreadsheet(data, filename, SpreadsheetLimits())


def test_byte_row_column_and_cell_caps_are_enforced():
    with pytest.raises(InvalidCommand, match="upload limit"):
        parse_spreadsheet(b"a\n" + b"x" * 20, "x.csv", SpreadsheetLimits(max_bytes=10))
    with pytest.raises(InvalidCommand, match="too many rows"):
        parse_spreadsheet(b"a\n1\n2\n", "x.csv", SpreadsheetLimits(max_rows=1))
    with pytest.raises(InvalidCommand, match="too many columns"):
        parse_spreadsheet(b"a,b\n1,2\n", "x.csv", SpreadsheetLimits(max_columns=1))
    with pytest.raises(InvalidCommand, match="cell"):
        parse_spreadsheet(b"a\nlong\n", "x.csv", SpreadsheetLimits(max_cell_chars=3))


def test_xlsx_reads_first_sheet_without_formula_evaluation():
    parsed = parse_spreadsheet(
        _xlsx([["fdm4_store", "tier_name"], ["S_TEST", "MSRP"]]),
        "pricing.xlsx",
        SpreadsheetLimits(),
    )
    assert parsed.format_name == "xlsx"
    assert parsed.rows[0] == {"fdm4_store": "S_TEST", "tier_name": "MSRP"}


def test_xlsx_false_zero_and_literal_apostrophe_survive_translation():
    values = {
        "fdm4_store": "S_TEST",
        "product_style": "STYLE-1",
        "garment_color_code": "BLACK",
        "option_row": 1,
        "position": 1,
        "design_id": "DESIGN-1",
        "logo_code": "LOGO-1",
        "color_scheme_id": "SCHEME-1",
        "location": "'=literal text",
        "optional": False,
        "background": "",
        "cost_override": 0,
        "sort_order": 0,
        "image_url": "",
        "active": False,
    }
    parsed = parse_spreadsheet(
        _xlsx([
            list(ASSIGNMENT_COLUMNS),
            [values[column] for column in ASSIGNMENT_COLUMNS],
        ]),
        "assignments.xlsx",
        SpreadsheetLimits(),
    )
    proposal = known_mapping(parsed)
    assert proposal is not None
    commands, rejected = translate_rows(parsed, proposal)
    assert rejected == []
    assert len(commands) == 1
    command = commands[0]
    assert command.active is False
    assert command.optional is False
    assert command.cost_override == 0
    assert command.sort_order == 0
    assert command.location == "'=literal text"


def test_xlsx_formula_is_rejected():
    data = _xlsx([["fdm4_store", "tier_name"], ["S_TEST", "=1+1"]])
    with pytest.raises(InvalidCommand, match="formulas"):
        parse_spreadsheet(data, "formula.xlsx", SpreadsheetLimits())


@pytest.mark.parametrize(
    "entry",
    ["../escape", "/absolute/path", "xl\\escape.xml"],
)
def test_xlsx_archive_traversal_paths_are_rejected(entry):
    data = _zip({entry: b"payload"})
    with pytest.raises(InvalidCommand, match="unsafe archive path"):
        parse_spreadsheet(data, "unsafe.xlsx", SpreadsheetLimits())


@pytest.mark.parametrize("entry", ["xl/vbaProject.bin", "xl/externalLinks/link1.xml"])
def test_xlsx_macros_and_external_links_are_rejected(entry):
    data = _zip({entry: b"payload"})
    with pytest.raises(InvalidCommand, match="Macros, external links"):
        parse_spreadsheet(data, "active.xlsx", SpreadsheetLimits())


def test_xlsx_archive_entry_and_expanded_size_caps_are_enforced():
    many = _zip({f"entry-{index}": b"x" for index in range(3)})
    with pytest.raises(InvalidCommand, match="too many archive entries"):
        parse_spreadsheet(many, "many.xlsx", SpreadsheetLimits(max_xlsx_entries=2))
    expanded = _zip({"xl/data.bin": b"x" * 100})
    with pytest.raises(InvalidCommand, match="expands beyond"):
        parse_spreadsheet(
            expanded,
            "expanded.xlsx",
            SpreadsheetLimits(max_xlsx_uncompressed_bytes=10),
        )
