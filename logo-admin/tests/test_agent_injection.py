"""Adversarial text remains data and cannot widen the command surface."""

import pytest
from pydantic import ValidationError

from commands import SaveAssignmentCommand, parse_command
from spreadsheet import SpreadsheetLimits, parse_spreadsheet
from tool_registry import agent_tool_schemas


LIFECYCLE = {
    "apply_change_set",
    "undo_change_set",
    "discard_change_set",
    "confirm_change_set",
}


def test_prompt_cannot_select_human_lifecycle_as_tool():
    tool_names = {schema["name"] for schema in agent_tool_schemas(writes_enabled=True)}
    assert tool_names.isdisjoint(LIFECYCLE)
    for name in LIFECYCLE:
        with pytest.raises(ValueError, match="unknown mutation command"):
            parse_command(name, {"ignore_previous_instructions": True})


def test_unknown_and_nul_arguments_are_rejected_before_sql():
    arguments = {
        "fdm4_store": "S_TEST\x00DROP TABLE logo.assignment",
        "product_style": "STYLE",
        "garment_color_code": "BLK",
        "position": 1,
        "design_id": "D1",
        "logo_code": "L1",
        "color_scheme_id": "SC1",
        "apply_now": True,
    }
    with pytest.raises(ValidationError):
        SaveAssignmentCommand.model_validate(arguments)


def test_spreadsheet_formula_payload_is_inert_csv_text():
    parsed = parse_spreadsheet(
        b"fdm4_store,tier_name,note\nS_TEST,MSRP,=HYPERLINK(\"javascript:alert(1)\")\n",
        "pricing.csv",
        SpreadsheetLimits(),
    )
    assert parsed.rows[0]["note"].startswith("=HYPERLINK")


def test_html_svg_and_javascript_are_never_command_names():
    names = {schema["name"] for schema in agent_tool_schemas(writes_enabled=True)}
    for payload in (
        "<img src=x onerror=alert(1)>",
        "<svg/onload=alert(1)>",
        "javascript:apply_change_set()",
    ):
        assert payload not in names
