"""Strict, transport-neutral mutation command validation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from commands import (
    COMMAND_MODELS,
    HARD_DELETE_TOOLS,
    ApplyToColorsCommand,
    DeleteStorePricingTierCommand,
    HardDeleteAssignmentCommand,
    HardDeleteColorCommand,
    SaveAssignmentCommand,
    SetStorePricingTierCommand,
    command_arguments,
    parse_command,
)


EXPECTED_COMMANDS = {
    # assignments
    "save_assignment",
    "deactivate_assignment",
    "hard_delete_assignment",
    "deactivate_color",
    "hard_delete_color",
    "set_style_active",
    "set_styles_active",
    "apply_to_colors",
    "copy_style",
    "copy_style_to_many",
    "paste_logo_set",
    "reorder_logo_rows",
    "replace_design",
    "bulk_apply",
    # store settings and pricing
    "update_store_settings",
    "set_store_extra_customers",
    "set_store_pricing_tier",
    "delete_store_pricing_tier",
    "set_price_rule_active",
    "delete_price_rule",
    # logo names, costs, colors
    "set_logo_name",
    "clear_logo_name",
    "set_logo_cost",
    "set_logo_default_cost",
    "set_color_class",
    # stock rules and sync blocks
    "set_brand_stock_rule",
    "remove_brand_stock_rule",
    "set_stock_override",
    "remove_stock_override",
    "set_sync_block",
    "remove_sync_block",
    # product mix
    "set_product_mix",
    "disable_product_mix",
    "add_mix_styles",
    "remove_mix_styles",
}


def _assignment_arguments() -> dict:
    return {
        "fdm4_store": "S_TEST",
        "product_style": "STYLE-1",
        "garment_color_code": "BLK",
        "position": 1,
        "option_row": 1,
        "design_id": "D-1",
        "logo_code": "L1",
        "color_scheme_id": "SCHEME",
    }


def test_command_registry_is_exactly_the_approved_write_surface():
    assert set(COMMAND_MODELS) == EXPECTED_COMMANDS
    assert HARD_DELETE_TOOLS == {
        "hard_delete_assignment",
        "hard_delete_color",
    }


@pytest.mark.parametrize("model", COMMAND_MODELS.values())
def test_every_command_forbids_unknown_fields(model):
    required = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        if name in {"fdm4_store", "store"}:
            required[name] = "S_TEST"
        elif name in {"product_style", "style", "source_style", "target_style"}:
            required[name] = "STYLE"
        elif name == "garment_color_code":
            required[name] = "BLK"
        elif name in {"position", "option_row"}:
            required[name] = 1
        elif name in {"design_id", "logo_code", "color_scheme_id", "tier_name"}:
            required[name] = "VALUE"
        elif name in {"active", "enabled", "allows_none"}:
            required[name] = True
    required["unexpected"] = "must fail"
    with pytest.raises(ValidationError):
        model.model_validate(required)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position", 0),
        ("position", 4),
        ("option_row", 0),
        ("option_row", 1000),
        ("fdm4_store", ""),
        ("logo_code", "X" * 101),
        ("location", "X" * 201),
        ("image_url", "X" * 2049),
    ],
)
def test_assignment_bounds_are_enforced(field, value):
    arguments = _assignment_arguments()
    arguments[field] = value
    with pytest.raises(ValidationError):
        SaveAssignmentCommand.model_validate(arguments)


def test_cost_override_is_decimal_and_json_storage_is_stable():
    command = SaveAssignmentCommand.model_validate({
        **_assignment_arguments(),
        "cost_override": "12.34",
    })
    assert command.cost_override == Decimal("12.34")
    assert command_arguments(command)["cost_override"] == "12.34"


def test_malformed_decimal_is_rejected():
    with pytest.raises(ValidationError):
        SaveAssignmentCommand.model_validate({
            **_assignment_arguments(),
            "cost_override": "not-a-number",
        })


def test_hard_delete_is_a_distinct_command_not_a_boolean_switch():
    target = {
        key: value
        for key, value in _assignment_arguments().items()
        if key in {
            "fdm4_store",
            "product_style",
            "garment_color_code",
            "position",
            "option_row",
        }
    }
    command = HardDeleteAssignmentCommand.model_validate(target)
    assert "hard" not in command.model_fields
    with pytest.raises(ValidationError):
        HardDeleteAssignmentCommand.model_validate({**target, "hard": True})
    assert "hard" not in HardDeleteColorCommand.model_fields


def test_parse_command_rejects_unknown_tool_name():
    with pytest.raises(ValueError, match="unknown mutation command"):
        parse_command("sync_to_wordpress", {})


def test_pricing_note_and_apply_to_colors_bounds():
    with pytest.raises(ValidationError):
        SetStorePricingTierCommand(
            fdm4_store="S_TEST",
            tier_name="MSRP",
            note="x" * 501,
        )
    with pytest.raises(ValidationError):
        ApplyToColorsCommand(
            store="S_TEST",
            style="STYLE",
            garment_color_code="BLK",
            position=3,
            option_row=1000,
        )
    assert DeleteStorePricingTierCommand(fdm4_store="S_TEST").fdm4_store == "S_TEST"
