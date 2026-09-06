"""Defensive CSV/XLSX parsing rejects active and oversized content."""

from io import BytesIO
import re
import time
import zipfile

import openpyxl
import pytest

from domain import InvalidCommand
import spreadsheet
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


def _xlsx_with_dimension(rows, dimension: str) -> bytes:
    """A real workbook whose <dimension> element is replaced by hand. A writer
    may omit it, understate it or overstate it; none of that is data."""
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(_xlsx(rows))) as source:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for entry in source.infolist():
                payload = source.read(entry)
                if entry.filename == "xl/worksheets/sheet1.xml":
                    text = payload.decode("utf-8")
                    replaced = re.sub(r"<dimension[^>]*/>", dimension, text, count=1)
                    assert replaced != text or dimension in text
                    payload = replaced.encode("utf-8")
                target.writestr(entry.filename, payload)
    return output.getvalue()


@pytest.mark.parametrize(
    ("dimension", "why"),
    [
        ("", "absent dimension"),
        ('<dimension ref="A1:A2"/>', "understated dimension"),
        ('<dimension ref="A1:A99999"/>', "overstated dimension"),
    ],
)
def test_xlsx_dimension_metadata_never_decides_what_is_read(dimension, why):
    data = _xlsx_with_dimension(
        [["fdm4_store", "tier_name"], ["S_TEST", "MSRP"], ["S_OTHER", "MAP"]],
        dimension,
    )
    parsed = parse_spreadsheet(data, "pricing.xlsx", SpreadsheetLimits())
    assert parsed.headers == ("fdm4_store", "tier_name"), why
    assert parsed.rows == (
        {"fdm4_store": "S_TEST", "tier_name": "MSRP"},
        {"fdm4_store": "S_OTHER", "tier_name": "MAP"},
    ), why


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0), ("-5", -5), ("1e12", 10 ** 12), (7, 7), ("  3  ", 3)],
)
def test_sheet_integer_cells_accept_ordinary_magnitudes(value, expected):
    assert spreadsheet._integer(value, "position") == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("1e13", "position is out of range"),
        ("1e1000000", "position is out of range"),
        ("1e-1000000", "position must be an integer"),
        ("1.5", "position must be an integer"),
        ("nan", "position must be an integer"),
    ],
)
def test_sheet_integer_cells_are_bounded_before_conversion(value, message):
    started = time.monotonic()
    with pytest.raises(ValueError, match=re.escape(message)):
        spreadsheet._integer(value, "position")
    assert time.monotonic() - started < 1


def _assignment_csv(rows) -> bytes:
    lines = [",".join(ASSIGNMENT_COLUMNS)]
    lines.extend(rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


GOOD_ASSIGNMENT = ("S_TEST,STYLE-1,RED,1,1,DESIGN-1,C1,SCHEME-1,Left Chest,"
                   "false,,,0,,true")


def test_huge_exponent_cell_is_refused_with_its_physical_row():
    parsed = parse_spreadsheet(
        _assignment_csv([
            GOOD_ASSIGNMENT,
            "",
            GOOD_ASSIGNMENT.replace(",RED,1,1,", ",BLU,1,1e1000000,"),
        ]),
        "assignments.csv",
        SpreadsheetLimits(),
    )
    assert parsed.row_numbers == (2, 4)
    proposal = known_mapping(parsed)
    started = time.monotonic()
    commands, rejected = translate_rows(parsed, proposal)
    assert time.monotonic() - started < 1
    assert len(commands) == 1
    assert rejected == [{"row": 4, "detail": "position is out of range"}]
