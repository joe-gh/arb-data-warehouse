"""Strict command models for transaction-safe logo/pricing mutations."""

from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field


class Command(BaseModel):
    """Base class shared by every mutation surface."""

    model_config = ConfigDict(extra="forbid")


class AssignmentTarget(Command):
    fdm4_store: str = Field(min_length=1, max_length=100)
    product_style: str = Field(min_length=1, max_length=100)
    garment_color_code: str = Field(min_length=1, max_length=100)
    position: int = Field(ge=1, le=3)
    option_row: int = Field(default=1, ge=1, le=999)


class SaveAssignmentCommand(AssignmentTarget):
    design_id: str = Field(min_length=1, max_length=100)
    logo_code: str = Field(min_length=1, max_length=100)
    color_scheme_id: str = Field(min_length=1, max_length=100)
    location: str = Field(default="", max_length=200)
    optional: bool = False
    background: str = Field(default="", max_length=200)
    cost_override: Optional[Decimal] = None
    sort_order: int = Field(default=0, ge=-2147483648, le=2147483647)
    image_url: str = Field(default="", max_length=2048)
    # None means an older/cached client omitted the field: preserve the stored
    # value.  An explicit empty string clears it.
    name_override: Optional[str] = Field(default=None, max_length=200)
    expected_updated_at: Optional[datetime] = None
    active: bool = True


class DeactivateAssignmentCommand(AssignmentTarget):
    pass


class HardDeleteAssignmentCommand(AssignmentTarget):
    pass


class ColorTarget(Command):
    fdm4_store: str = Field(min_length=1, max_length=100)
    product_style: str = Field(min_length=1, max_length=100)
    garment_color_code: str = Field(min_length=1, max_length=100)


class DeactivateColorCommand(ColorTarget):
    pass


class HardDeleteColorCommand(ColorTarget):
    pass


class SetStyleActiveCommand(Command):
    store: str = Field(min_length=1, max_length=100)
    style: str = Field(min_length=1, max_length=100)
    active: bool


class ApplyToColorsCommand(Command):
    store: str = Field(min_length=1, max_length=100)
    style: str = Field(min_length=1, max_length=100)
    garment_color_code: str = Field(min_length=1, max_length=100)
    position: int = Field(ge=1, le=3)
    option_row: int = Field(default=1, ge=1, le=999)
    overwrite: bool = False


class CopyStyleCommand(Command):
    store: str = Field(min_length=1, max_length=100)
    source_style: str = Field(min_length=1, max_length=100)
    target_style: str = Field(min_length=1, max_length=100)
    overwrite: bool = False


class UpdateStoreSettingsCommand(Command):
    store: str = Field(min_length=1, max_length=100)
    enabled: bool
    allows_none: bool


class SetStorePricingTierCommand(Command):
    fdm4_store: str = Field(min_length=1, max_length=100)
    tier_name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class DeleteStorePricingTierCommand(Command):
    fdm4_store: str = Field(min_length=1, max_length=100)


MutationCommand = Union[
    SaveAssignmentCommand,
    DeactivateAssignmentCommand,
    HardDeleteAssignmentCommand,
    DeactivateColorCommand,
    HardDeleteColorCommand,
    SetStyleActiveCommand,
    ApplyToColorsCommand,
    CopyStyleCommand,
    UpdateStoreSettingsCommand,
    SetStorePricingTierCommand,
    DeleteStorePricingTierCommand,
]


COMMAND_MODELS: Dict[str, Type[Command]] = {
    "save_assignment": SaveAssignmentCommand,
    "deactivate_assignment": DeactivateAssignmentCommand,
    "hard_delete_assignment": HardDeleteAssignmentCommand,
    "deactivate_color": DeactivateColorCommand,
    "hard_delete_color": HardDeleteColorCommand,
    "set_style_active": SetStyleActiveCommand,
    "apply_to_colors": ApplyToColorsCommand,
    "copy_style": CopyStyleCommand,
    "update_store_settings": UpdateStoreSettingsCommand,
    "set_store_pricing_tier": SetStorePricingTierCommand,
    "delete_store_pricing_tier": DeleteStorePricingTierCommand,
}

HARD_DELETE_TOOLS = frozenset({
    "hard_delete_assignment",
    "hard_delete_color",
})


def parse_command(tool_name: str, arguments: dict) -> MutationCommand:
    """Validate stored/model arguments using the command's canonical model."""

    model = COMMAND_MODELS.get(tool_name)
    if model is None:
        raise ValueError(f"unknown mutation command: {tool_name}")
    return model.model_validate(arguments)  # type: ignore[return-value]


def command_arguments(command: MutationCommand) -> dict:
    """Return stable JSON-compatible arguments for storage and hashing."""

    return command.model_dump(mode="json")
