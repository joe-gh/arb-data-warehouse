"""Transaction-only mutation kernel shared by HTTP and the in-app agent.

Every public function in this module accepts a caller-owned PostgreSQL cursor.
It never opens or commits a transaction and never performs non-database I/O.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Callable, Dict, Literal, Mapping, Optional
from urllib.parse import urlsplit

import mix_service
from commands import (
    COMMAND_MODELS,
    SavePriceRuleCommand,
    FillMissingColorsCommand,
    AddMixStylesCommand,
    DeletePriceRuleCommand,
    DisableProductMixCommand,
    RemoveMixStylesCommand,
    SetLogoDefaultCostCommand,
    SetPriceRuleActiveCommand,
    SetProductMixCommand,
    ApplyToColorsCommand,
    AssignmentTarget,
    BulkApplyCommand,
    ClearLogoNameCommand,
    ColorTarget,
    CopyStyleCommand,
    CopyStyleToManyCommand,
    DeactivateAssignmentCommand,
    DeactivateColorCommand,
    DeleteStorePricingTierCommand,
    HardDeleteAssignmentCommand,
    HardDeleteColorCommand,
    MutationCommand,
    PasteLogoSetCommand,
    RemoveBrandStockRuleCommand,
    RemoveStockOverrideCommand,
    RemoveSyncBlockCommand,
    ReorderLogoRowsCommand,
    ReplaceDesignCommand,
    SaveAssignmentCommand,
    SetBrandStockRuleCommand,
    SetColorClassCommand,
    SetLogoCostCommand,
    SetLogoNameCommand,
    SetStockOverrideCommand,
    SetStoreExtraCustomersCommand,
    SetStorePricingTierCommand,
    SetStyleActiveCommand,
    SetStylesActiveCommand,
    SetSyncBlockCommand,
    UpdateStoreSettingsCommand,
)
from design_resolver import (
    design_available_to_store,
    load_design_index,
    validate_design_asset,
)
from domain import Conflict, InvalidCommand, NotFound


ScopeKind = Literal[
    "assignment_option_row",
    "assignment_color",
    "assignment_style",
    "assignment_store",
    "store_settings_row",
    "store_pricing_tier_row",
    "display_name_row",
    "color_class_row",
    "stock_override_row",
    "brand_stock_rule_row",
    "sync_exclusion_row",
    "default_cost_row",
    "price_rule_row",
    "store_mix_store_row",
    "store_mix_items",
]

# Exact preview/undo materializes every affected row. These hard service caps
# keep direct HTTP/MCP mutations and agent staging within a reviewable,
# journalable boundary even if warehouse data is unexpectedly dirty.
MAX_ASSIGNMENT_MUTATION_ROWS = 2_000
MAX_STYLE_COLOR_ROWS = 500
# Styles one agent bulk command may touch; larger jobs are split into calls
# so every change set stays reviewable in one card.
MAX_BULK_STYLES = 50


# Closed declaration of every scope a canonical command can affect.  Startup
# validation cross-checks this map against the command/handler registries and
# the snapshot and restore implementations before exposing agent writes.
COMMAND_SCOPE_KINDS: Dict[str, frozenset[ScopeKind]] = {
    "save_assignment": frozenset({"assignment_option_row"}),
    "deactivate_assignment": frozenset({"assignment_option_row"}),
    "hard_delete_assignment": frozenset({"assignment_option_row"}),
    "deactivate_color": frozenset({"assignment_color"}),
    "hard_delete_color": frozenset({"assignment_color"}),
    "set_style_active": frozenset({"assignment_style"}),
    "apply_to_colors": frozenset({"assignment_style"}),
    "copy_style": frozenset({"assignment_style"}),
    "update_store_settings": frozenset({"store_settings_row"}),
    "set_store_pricing_tier": frozenset({"store_pricing_tier_row"}),
    "delete_store_pricing_tier": frozenset({"store_pricing_tier_row"}),
    "copy_style_to_many": frozenset({"assignment_style"}),
    "paste_logo_set": frozenset({"assignment_style"}),
    "replace_design": frozenset({"assignment_style"}),
    "reorder_logo_rows": frozenset({"assignment_style"}),
    "set_styles_active": frozenset({"assignment_style"}),
    "set_logo_name": frozenset({"display_name_row"}),
    "clear_logo_name": frozenset({"display_name_row"}),
    "set_color_class": frozenset({"color_class_row"}),
    "set_stock_override": frozenset({"stock_override_row"}),
    "remove_stock_override": frozenset({"stock_override_row"}),
    "set_brand_stock_rule": frozenset({"brand_stock_rule_row"}),
    "remove_brand_stock_rule": frozenset({"brand_stock_rule_row"}),
    "set_sync_block": frozenset({"sync_exclusion_row"}),
    "remove_sync_block": frozenset({"sync_exclusion_row"}),
    "set_logo_cost": frozenset({"assignment_style"}),
    "set_store_extra_customers": frozenset({"store_settings_row"}),
    "bulk_apply": frozenset({"assignment_store"}),
    "fill_missing_colors": frozenset({"assignment_store"}),
    "save_price_rule": frozenset({"price_rule_row"}),
    "set_logo_default_cost": frozenset({"default_cost_row"}),
    "set_price_rule_active": frozenset({"price_rule_row"}),
    "delete_price_rule": frozenset({"price_rule_row"}),
    "set_product_mix": frozenset({"store_mix_store_row", "store_mix_items"}),
    "disable_product_mix": frozenset({"store_mix_store_row"}),
    "add_mix_styles": frozenset({"store_mix_items"}),
    "remove_mix_styles": frozenset({"store_mix_items"}),
}


@dataclass(frozen=True)
class MutationScope:
    kind: ScopeKind
    key: Mapping[str, str | int]


@dataclass(frozen=True)
class MutationResult:
    value: dict
    scopes: tuple[MutationScope, ...]


def _clean(value: Any, field: str, maximum: int = 100) -> str:
    cleaned = str(value).strip()
    if not cleaned or len(cleaned) > maximum or "\x00" in cleaned:
        raise InvalidCommand(f"{field} is invalid")
    return cleaned


def _optional_text(value: Any, field: str, maximum: int) -> str:
    if value is None:
        return ""
    cleaned = str(value).strip()
    if len(cleaned) > maximum or "\x00" in cleaned:
        raise InvalidCommand(f"{field} is invalid")
    return cleaned


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise InvalidCommand("cost_override must be numeric") from None
    if (
        not parsed.is_finite()
        or parsed < Decimal("-9999999999.99")
        or parsed > Decimal("9999999999.99")
    ):
        raise InvalidCommand("cost_override is outside the supported range")
    if parsed.as_tuple().exponent < -2:
        raise InvalidCommand(
            "cost_override may have at most two decimal places"
        )
    return parsed


def _image_url(value: Any) -> str:
    url = _optional_text(value, "image_url", 2048)
    if not url:
        return ""
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise InvalidCommand("image_url must be an absolute HTTP(S) URL")
    try:
        parsed.port
    except ValueError:
        raise InvalidCommand("image_url has an invalid port") from None
    return url


def _catalog_for_store(cursor, store: str) -> Optional[str]:
    cursor.execute(
        """
        SELECT catalog_id
          FROM woo.store_catalog
         WHERE fdm4_store = %s AND suggested = true
         ORDER BY suggested DESC, products DESC, catalog_id
         LIMIT 1
        """,
        (store,),
    )
    row = cursor.fetchone()
    return str(row["catalog_id"]) if row else None


def _style_exists(cursor, store: str, catalog: str, style: str) -> bool:
    cursor.execute(
        """
        SELECT 1 FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s
           AND style_code = %s AND is_active = true
         LIMIT 1
        """,
        (store, catalog, style),
    )
    return cursor.fetchone() is not None


def _bounded_assignment_count(
    cursor,
    where: str,
    params: tuple[Any, ...],
    *,
    label: str,
) -> int:
    cursor.execute(
        f"""
        SELECT count(*)::integer AS row_count
          FROM (
              SELECT 1 FROM logo.assignment
               WHERE {where}
               LIMIT %s
          ) AS bounded
        """,
        params + (MAX_ASSIGNMENT_MUTATION_ROWS + 1,),
    )
    count = int(cursor.fetchone()["row_count"])
    if count > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            f"{label} exceeds the {MAX_ASSIGNMENT_MUTATION_ROWS}-row mutation limit"
        )
    return count


def _assert_changed_rows_bounded(changed: int, *, label: str) -> None:
    if int(changed) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            f"{label} exceeds the {MAX_ASSIGNMENT_MUTATION_ROWS}-row mutation limit"
        )


def _color_exists(
    cursor,
    store: str,
    catalog: str,
    style: str,
    color: str,
) -> bool:
    cursor.execute(
        """
        SELECT 1 FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s
           AND style_code = %s AND kind = 'variation' AND is_active = true
           AND color_code = %s
         LIMIT 1
        """,
        (store, catalog, style, color),
    )
    return cursor.fetchone() is not None


def _active_primary_exists(cursor, values: Mapping[str, Any]) -> bool:
    cursor.execute(
        """
        SELECT 1 FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
           AND garment_color_code = %s AND option_row = %s
           AND position = 1 AND active = true
         LIMIT 1
        """,
        (
            values["fdm4_store"],
            values["product_style"],
            values["garment_color_code"],
            values["option_row"],
        ),
    )
    return cursor.fetchone() is not None


def _validate_primary_anchor(cursor, values: Mapping[str, Any]) -> None:
    if int(values["position"]) > 1 and bool(values.get("active", True)):
        if not _active_primary_exists(cursor, values):
            raise InvalidCommand(
                "position 2/3 requires an active position-1 assignment "
                "in the same option row"
            )


def _validate_warehouse_keys(cursor, values: Mapping[str, Any]) -> None:
    store = str(values["fdm4_store"])
    style = str(values["product_style"])
    color = str(values["garment_color_code"])
    design_id = str(values["design_id"])
    scheme = str(values["color_scheme_id"])
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise InvalidCommand(f"unknown FDM4 store {store}")

    cursor.execute(
        """
        SELECT 1 FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
           AND garment_color_code = %s AND option_row = %s AND position = %s
         LIMIT 1
        """,
        (store, style, color, values["option_row"], values["position"]),
    )
    existing = cursor.fetchone() is not None
    if not _style_exists(cursor, store, catalog, style) and not existing:
        raise InvalidCommand(f"style {style} is not active in store {store}")
    if not _color_exists(cursor, store, catalog, style, color) and not existing:
        raise InvalidCommand(f"color {color} is not active for store/style")

    cursor.execute(
        "SELECT 1 FROM fdm4.dec_design WHERE btrim(design_id) = %s LIMIT 1",
        (design_id,),
    )
    if cursor.fetchone() is None:
        raise InvalidCommand(f"unknown design_id {design_id}")
    _validate_primary_anchor(cursor, values)

    # Existing image-only legacy assignments deliberately bypass missing FDM4
    # art/scheme checks, matching the established HTTP behavior.
    if values.get("image_url"):
        return
    if not validate_design_asset(
        cursor,
        store=store,
        design_id=design_id,
        scheme=scheme,
    ):
        if not design_available_to_store(cursor, store, design_id):
            raise InvalidCommand(
                f"design {design_id} belongs to a different FDM4 customer"
                " account and is not available to this store"
            )
        raise InvalidCommand(
            f"design {design_id} has no color scheme {scheme}"
        )
    if not validate_design_asset(
        cursor,
        store=store,
        design_id=design_id,
        scheme=scheme,
        logo_code=str(values["logo_code"]),
    ):
        raise InvalidCommand(
            f"logo_code {values['logo_code']} does not match design "
            f"{design_id} / scheme {scheme}"
        )


def _assignment_values(command: SaveAssignmentCommand) -> Dict[str, Any]:
    return {
        "fdm4_store": _clean(command.fdm4_store, "fdm4_store"),
        "product_style": _clean(command.product_style, "product_style"),
        "garment_color_code": _clean(
            command.garment_color_code,
            "garment_color_code",
        ),
        "position": command.position,
        "option_row": command.option_row,
        "design_id": _clean(command.design_id, "design_id"),
        "logo_code": _clean(command.logo_code, "logo_code").upper(),
        "color_scheme_id": _clean(
            command.color_scheme_id,
            "color_scheme_id",
        ).upper(),
        "location": _optional_text(command.location, "location", 200),
        "optional": command.optional,
        "background": _optional_text(command.background, "background", 200),
        "cost_override": _decimal(command.cost_override),
        "sort_order": command.sort_order,
        "image_url": _image_url(command.image_url),
        "name_override": (
            None
            if command.name_override is None
            else _optional_text(command.name_override, "name_override", 200)
        ),
        "active": command.active,
    }


def _upsert_assignment(
    cursor,
    values: Mapping[str, Any],
    actor: str,
    *,
    overwrite: bool = True,
) -> bool:
    conflict = (
        """
        DO UPDATE SET
            design_id = EXCLUDED.design_id,
            logo_code = EXCLUDED.logo_code,
            color_scheme_id = EXCLUDED.color_scheme_id,
            location = EXCLUDED.location,
            optional = EXCLUDED.optional,
            background = EXCLUDED.background,
            cost_override = EXCLUDED.cost_override,
            sort_order = EXCLUDED.sort_order,
            image_url = EXCLUDED.image_url,
            name_override = COALESCE(
                EXCLUDED.name_override,
                logo.assignment.name_override
            ),
            active = EXCLUDED.active,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        """
        if overwrite
        else "DO NOTHING"
    )
    cursor.execute(
        f"""
        INSERT INTO logo.assignment (
            fdm4_store, product_style, garment_color_code, option_row, position,
            design_id, logo_code, color_scheme_id, location, optional,
            background, cost_override, sort_order, image_url, name_override,
            active, updated_by
        ) VALUES (
            %(fdm4_store)s, %(product_style)s, %(garment_color_code)s,
            %(option_row)s, %(position)s, %(design_id)s, %(logo_code)s,
            %(color_scheme_id)s, %(location)s, %(optional)s, %(background)s,
            %(cost_override)s, %(sort_order)s, %(image_url)s, %(name_override)s,
            %(active)s, %(updated_by)s
        )
        ON CONFLICT (
            fdm4_store, product_style, garment_color_code, option_row, position
        ) {conflict}
        """,
        {**dict(values), "updated_by": actor},
    )
    changed = cursor.rowcount > 0
    if changed and values["position"] == 1 and not values.get("active", True):
        cursor.execute(
            """
            UPDATE logo.assignment
               SET active = false, updated_by = %s, updated_at = now()
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s
               AND position > 1 AND active = true
            """,
            (
                actor,
                values["fdm4_store"],
                values["product_style"],
                values["garment_color_code"],
                values["option_row"],
            ),
        )
    return changed


def assignment_option_scope(command: AssignmentTarget) -> MutationScope:
    return MutationScope(
        "assignment_option_row",
        {
            "fdm4_store": command.fdm4_store.strip(),
            "product_style": command.product_style.strip(),
            "garment_color_code": command.garment_color_code.strip(),
            "option_row": command.option_row,
        },
    )


def assignment_color_scope(command: ColorTarget) -> MutationScope:
    return MutationScope(
        "assignment_color",
        {
            "fdm4_store": command.fdm4_store.strip(),
            "product_style": command.product_style.strip(),
            "garment_color_code": command.garment_color_code.strip(),
        },
    )


def assignment_style_scope(store: str, style: str) -> MutationScope:
    return MutationScope(
        "assignment_style",
        {"fdm4_store": store.strip(), "product_style": style.strip()},
    )


def _clean_styles(values, field: str) -> list:
    """Distinct, cleaned style codes in the given order; bounded per call."""
    cleaned: list = []
    for value in values or ():
        style = _clean(value, field)
        if style not in cleaned:
            cleaned.append(style)
    if not cleaned:
        raise InvalidCommand(f"{field} must name at least one style")
    if len(cleaned) > MAX_BULK_STYLES:
        raise InvalidCommand(f"{field} may name at most {MAX_BULK_STYLES} styles per call")
    return cleaned


def assignment_store_scope(store: str) -> MutationScope:
    return MutationScope("assignment_store", {"fdm4_store": store.strip()})


def _style_scopes(store: str, styles) -> tuple:
    return tuple(
        assignment_style_scope(store, style)
        for style in sorted(_clean_styles(styles, "styles"))
    )


def _upper(value: Any, field: str, maximum: int = 100) -> str:
    return " ".join(_clean(value, field, maximum).split()).upper()


def _shared_or_store(value: Any) -> str:
    """'' = the shared default row every store falls back to."""
    return "" if value in (None, "") else _upper(value, "store")


def _sync_block_keys(store: str, styles) -> list:
    codes = [_upper(style, "styles") for style in (styles or ())]
    codes = list(dict.fromkeys(codes))
    if len(codes) > MAX_BULK_STYLES:
        raise InvalidCommand(f"styles may name at most {MAX_BULK_STYLES} styles per call")
    return codes or [""]


def _sync_block_scopes(command) -> tuple:
    store = _upper(command.store, "store")
    return tuple(
        MutationScope("sync_exclusion_row", {"fdm4_store": store, "style_code": style})
        for style in sorted(_sync_block_keys(store, command.styles))
    )


def affected_scopes(command: MutationCommand) -> tuple[MutationScope, ...]:
    if isinstance(command, AssignmentTarget):
        scopes = (assignment_option_scope(command),)
    elif isinstance(command, ColorTarget):
        scopes = (assignment_color_scope(command),)
    elif isinstance(command, SetStyleActiveCommand):
        scopes = (assignment_style_scope(command.store, command.style),)
    elif isinstance(command, ApplyToColorsCommand):
        scopes = (assignment_style_scope(command.store, command.style),)
    elif isinstance(command, CopyStyleCommand):
        scopes = (assignment_style_scope(command.store, command.target_style),)
    elif isinstance(command, CopyStyleToManyCommand):
        scopes = _style_scopes(command.store, command.target_styles)
    elif isinstance(command, (PasteLogoSetCommand, ReplaceDesignCommand, SetStylesActiveCommand)):
        scopes = _style_scopes(command.store, command.styles)
    elif isinstance(command, ReorderLogoRowsCommand):
        scopes = (assignment_style_scope(command.store, command.style),)
    elif isinstance(command, (SetLogoNameCommand, ClearLogoNameCommand)):
        scopes = (MutationScope("display_name_row", {
            "design_id": _clean(command.design_id, "design_id"),
            "color_scheme_id": _upper(command.color_scheme_id, "color_scheme_id"),
            "fdm4_store": _shared_or_store(command.store),
        }),)
    elif isinstance(command, SetColorClassCommand):
        scopes = (MutationScope("color_class_row", {"color_code": _clean(command.color_code, "color_code")}),)
    elif isinstance(command, (SetStockOverrideCommand, RemoveStockOverrideCommand)):
        scopes = (MutationScope("stock_override_row", {"style_code": _upper(command.style_code, "style_code")}),)
    elif isinstance(command, (SetBrandStockRuleCommand, RemoveBrandStockRuleCommand)):
        scopes = (MutationScope("brand_stock_rule_row", {"mill_code": _clean(command.mill_code, "mill_code", 32)}),)
    elif isinstance(command, (SetSyncBlockCommand, RemoveSyncBlockCommand)):
        scopes = _sync_block_scopes(command)
    elif isinstance(command, SetLogoCostCommand):
        scopes = _style_scopes(command.store, command.styles)
    elif isinstance(command, SetStoreExtraCustomersCommand):
        scopes = (MutationScope("store_settings_row", {"fdm4_store": command.store.strip()}),)
    elif isinstance(command, (BulkApplyCommand, FillMissingColorsCommand)):
        scopes = (assignment_store_scope(command.store),)
    elif isinstance(command, SetLogoDefaultCostCommand):
        scopes = (MutationScope("default_cost_row", {
            "logo_code": _upper(command.logo_code, "logo_code"),
            "color_scheme_id": _upper(command.color_scheme_id, "color_scheme_id"),
        }),)
    elif isinstance(command, SavePriceRuleCommand):
        rule_id = command.rule_id or command._reserved_rule_id
        if rule_id is None:
            raise InvalidCommand("A new price rule needs a reserved id before staging")
        scopes = (MutationScope("price_rule_row", {"rule_id": int(rule_id)}),)
    elif isinstance(command, (SetPriceRuleActiveCommand, DeletePriceRuleCommand)):
        scopes = (MutationScope("price_rule_row", {"rule_id": int(command.rule_id)}),)
    elif isinstance(command, SetProductMixCommand):
        store = mix_service.norm(command.store)
        scopes = (
            MutationScope("store_mix_store_row", {"fdm4_store": store}),
            MutationScope("store_mix_items", {"fdm4_store": store}),
        )
    elif isinstance(command, DisableProductMixCommand):
        scopes = (MutationScope("store_mix_store_row", {"fdm4_store": mix_service.norm(command.store)}),)
    elif isinstance(command, (AddMixStylesCommand, RemoveMixStylesCommand)):
        scopes = (MutationScope("store_mix_items", {"fdm4_store": mix_service.norm(command.store)}),)
    elif isinstance(command, UpdateStoreSettingsCommand):
        scopes = (
            MutationScope(
                "store_settings_row",
                {"fdm4_store": command.store.strip()},
            ),
        )
    elif isinstance(
        command,
        (SetStorePricingTierCommand, DeleteStorePricingTierCommand),
    ):
        scopes = (
            MutationScope(
                "store_pricing_tier_row",
                {"fdm4_store": command.fdm4_store.strip()},
            ),
        )
    else:
        raise InvalidCommand("unsupported mutation command")

    command_name = next(
        (
            name
            for name, model in COMMAND_MODELS.items()
            if type(command) is model
        ),
        None,
    )
    declared = COMMAND_SCOPE_KINDS.get(command_name or "")
    actual = frozenset(scope.kind for scope in scopes)
    if declared is None or not actual or actual != declared:
        raise InvalidCommand("mutation scope contract mismatch")
    return scopes


def save_assignment(cursor, actor: str, command: SaveAssignmentCommand) -> MutationResult:
    values = _assignment_values(command)
    if command.expected_updated_at is not None:
        cursor.execute(
            """
            SELECT updated_at FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s AND position = %s
             FOR UPDATE
            """,
            (
                values["fdm4_store"],
                values["product_style"],
                values["garment_color_code"],
                values["option_row"],
                values["position"],
            ),
        )
        current = cursor.fetchone()
        if current is None or current["updated_at"] != command.expected_updated_at:
            raise Conflict(
                "Assignment changed after it was loaded; reload before saving"
            )
    _validate_warehouse_keys(cursor, values)
    _upsert_assignment(cursor, values, actor)
    cursor.execute(
        """
        SELECT * FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
           AND garment_color_code = %s AND option_row = %s AND position = %s
        """,
        (
            values["fdm4_store"],
            values["product_style"],
            values["garment_color_code"],
            values["option_row"],
            values["position"],
        ),
    )
    return MutationResult(
        {"ok": True, "assignment": dict(cursor.fetchone())},
        affected_scopes(command),
    )


def _remove_assignment(
    cursor,
    actor: str,
    command: AssignmentTarget,
    *,
    hard: bool,
) -> MutationResult:
    store = _clean(command.fdm4_store, "fdm4_store")
    style = _clean(command.product_style, "product_style")
    color = _clean(command.garment_color_code, "garment_color_code")
    position_clause = "" if command.position == 1 else " AND position = %s"
    params: tuple[Any, ...] = (store, style, color, command.option_row)
    if command.position != 1:
        params += (command.position,)
    _bounded_assignment_count(
        cursor,
        (
            "fdm4_store = %s AND product_style = %s "
            "AND garment_color_code = %s AND option_row = %s"
            + position_clause
        ),
        params,
        label="Assignment operation",
    )
    if hard:
        cursor.execute(
            f"""
            DELETE FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s{position_clause}
            """,
            params,
        )
    else:
        cursor.execute(
            f"""
            UPDATE logo.assignment
               SET active = false, updated_by = %s, updated_at = now()
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s{position_clause}
            """,
            (actor,) + params,
        )
    if cursor.rowcount == 0:
        raise NotFound("Assignment not found")
    _assert_changed_rows_bounded(cursor.rowcount, label="Assignment operation")
    return MutationResult(
        {"ok": True, "hard": hard},
        affected_scopes(command),
    )


def deactivate_assignment(
    cursor,
    actor: str,
    command: DeactivateAssignmentCommand,
) -> MutationResult:
    return _remove_assignment(cursor, actor, command, hard=False)


def hard_delete_assignment(
    cursor,
    actor: str,
    command: HardDeleteAssignmentCommand,
) -> MutationResult:
    return _remove_assignment(cursor, actor, command, hard=True)


def _remove_color(
    cursor,
    actor: str,
    command: ColorTarget,
    *,
    hard: bool,
) -> MutationResult:
    store = _clean(command.fdm4_store, "fdm4_store")
    style = _clean(command.product_style, "product_style")
    color = _clean(command.garment_color_code, "garment_color_code")
    _bounded_assignment_count(
        cursor,
        (
            "fdm4_store = %s AND product_style = %s "
            "AND garment_color_code = %s"
        ),
        (store, style, color),
        label="Color operation",
    )
    if hard:
        cursor.execute(
            """
            DELETE FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s
            """,
            (store, style, color),
        )
    else:
        cursor.execute(
            """
            UPDATE logo.assignment
               SET active = false, updated_by = %s, updated_at = now()
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND active = true
            """,
            (actor, store, style, color),
        )
    removed = cursor.rowcount
    if removed == 0:
        raise NotFound("No assignments found for this color")
    _assert_changed_rows_bounded(removed, label="Color operation")
    return MutationResult(
        {"ok": True, "removed": removed, "hard": hard},
        affected_scopes(command),
    )


def deactivate_color(
    cursor,
    actor: str,
    command: DeactivateColorCommand,
) -> MutationResult:
    return _remove_color(cursor, actor, command, hard=False)


def hard_delete_color(
    cursor,
    actor: str,
    command: HardDeleteColorCommand,
) -> MutationResult:
    return _remove_color(cursor, actor, command, hard=True)


def set_style_active(
    cursor,
    actor: str,
    command: SetStyleActiveCommand,
) -> MutationResult:
    store = _clean(command.store, "store")
    style = _clean(command.style, "style")
    _bounded_assignment_count(
        cursor,
        "fdm4_store = %s AND product_style = %s",
        (store, style),
        label="Style operation",
    )
    cursor.execute(
        """
        UPDATE logo.assignment AS assignment
           SET active = %s, updated_by = %s, updated_at = now()
         WHERE fdm4_store = %s AND product_style = %s
           AND (
                %s = false OR position = 1 OR EXISTS (
                    SELECT 1 FROM logo.assignment AS primary_assignment
                     WHERE primary_assignment.fdm4_store = assignment.fdm4_store
                       AND primary_assignment.product_style = assignment.product_style
                       AND primary_assignment.garment_color_code = assignment.garment_color_code
                       AND primary_assignment.option_row = assignment.option_row
                       AND primary_assignment.position = 1
                )
           )
        """,
        (command.active, actor, store, style, command.active),
    )
    changed = cursor.rowcount
    if changed == 0:
        raise NotFound("Style has no assignments")
    _assert_changed_rows_bounded(changed, label="Style operation")
    return MutationResult(
        {"ok": True, "updated": changed, "active": command.active},
        affected_scopes(command),
    )


def apply_to_colors(
    cursor,
    actor: str,
    command: ApplyToColorsCommand,
) -> MutationResult:
    store = _clean(command.store, "store")
    style = _clean(command.style, "style")
    source_color = _clean(command.garment_color_code, "garment_color_code")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise NotFound("Store not found")
    cursor.execute(
        """
        SELECT fdm4_store, product_style, garment_color_code, option_row,
               position, design_id, logo_code, color_scheme_id, location,
               optional, background, cost_override, sort_order, image_url,
               name_override, active
          FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
           AND garment_color_code = %s AND option_row = %s AND position = %s
        """,
        (store, style, source_color, command.option_row, command.position),
    )
    source = cursor.fetchone()
    if source is None:
        raise NotFound("Source assignment not found")
    source_values = dict(source)
    _validate_primary_anchor(cursor, source_values)
    cursor.execute(
        """
        SELECT DISTINCT color_code FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s AND style_code = %s
           AND kind = 'variation' AND is_active = true
           AND NULLIF(btrim(color_code), '') IS NOT NULL
         ORDER BY color_code
         LIMIT %s
        """,
        (store, catalog, style, MAX_STYLE_COLOR_ROWS + 1),
    )
    colors = [str(row["color_code"]) for row in cursor.fetchall()]
    if len(colors) > MAX_STYLE_COLOR_ROWS:
        raise InvalidCommand(
            f"Style exceeds the {MAX_STYLE_COLOR_ROWS}-color mutation limit"
        )
    if not colors:
        raise InvalidCommand("Style has no active colors")
    existing = _bounded_assignment_count(
        cursor,
        "fdm4_store = %s AND product_style = %s",
        (store, style),
        label="Style operation",
    )
    if existing + len(colors) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            "Apply-to-colors could exceed the assignment mutation limit"
        )
    copied = 0
    skipped_without_primary = 0
    for color in colors:
        values = dict(source_values)
        values["garment_color_code"] = color
        if values["position"] > 1 and not _active_primary_exists(cursor, values):
            skipped_without_primary += 1
            continue
        if _upsert_assignment(cursor, values, actor, overwrite=command.overwrite):
            copied += 1
    return MutationResult(
        {
            "ok": True,
            "copied": copied,
            "colors": len(colors),
            "skipped_without_primary": skipped_without_primary,
        },
        affected_scopes(command),
    )


def copy_style(cursor, actor: str, command: CopyStyleCommand) -> MutationResult:
    store = _clean(command.store, "store")
    source_style = _clean(command.source_style, "source_style")
    target_style = _clean(command.target_style, "target_style")
    if source_style == target_style:
        raise InvalidCommand("Source and target styles must differ")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise NotFound("Store not found")
    if not _style_exists(cursor, store, catalog, target_style):
        raise NotFound("Target style not found")
    cursor.execute(
        """
        SELECT fdm4_store, product_style, garment_color_code, option_row,
               position, design_id, logo_code, color_scheme_id, location,
               optional, background, cost_override, sort_order, image_url,
               name_override, active
          FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
         ORDER BY garment_color_code, option_row, position
         LIMIT %s
        """,
        (store, source_style, MAX_ASSIGNMENT_MUTATION_ROWS + 1),
    )
    source_rows = [dict(row) for row in cursor.fetchall()]
    if len(source_rows) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            f"Source style exceeds the {MAX_ASSIGNMENT_MUTATION_ROWS}-row mutation limit"
        )
    if not source_rows:
        raise NotFound("Source style has no assignments")
    cursor.execute(
        """
        SELECT DISTINCT color_code FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s AND style_code = %s
           AND kind = 'variation' AND is_active = true
           AND NULLIF(btrim(color_code), '') IS NOT NULL
         LIMIT %s
        """,
        (store, catalog, target_style, MAX_STYLE_COLOR_ROWS + 1),
    )
    target_color_rows = list(cursor.fetchall())
    if len(target_color_rows) > MAX_STYLE_COLOR_ROWS:
        raise InvalidCommand(
            f"Target style exceeds the {MAX_STYLE_COLOR_ROWS}-color mutation limit"
        )
    target_colors = {str(row["color_code"]) for row in target_color_rows}
    target_existing = _bounded_assignment_count(
        cursor,
        "fdm4_store = %s AND product_style = %s",
        (store, target_style),
        label="Target style",
    )
    if target_existing + len(source_rows) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand("Copy-style could exceed the assignment mutation limit")
    copied = 0
    skipped_colors = 0
    skipped_without_primary = 0
    for source in source_rows:
        if str(source["garment_color_code"]) not in target_colors:
            skipped_colors += 1
            continue
        source["product_style"] = target_style
        try:
            _validate_primary_anchor(cursor, source)
        except InvalidCommand:
            skipped_without_primary += 1
            continue
        if _upsert_assignment(cursor, source, actor, overwrite=command.overwrite):
            copied += 1
    return MutationResult(
        {
            "ok": True,
            "copied": copied,
            "source_rows": len(source_rows),
            "skipped_missing_color": skipped_colors,
            "skipped_without_primary": skipped_without_primary,
        },
        affected_scopes(command),
    )


def update_store_settings(
    cursor,
    actor: str,
    command: UpdateStoreSettingsCommand,
) -> MutationResult:
    store = _clean(command.store, "store")
    if _catalog_for_store(cursor, store) is None:
        raise NotFound("Store not found")
    cursor.execute(
        """
        INSERT INTO logo.store_settings (
            fdm4_store, enabled, allows_none, updated_by, updated_at
        ) VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (fdm4_store) DO UPDATE SET
            enabled = EXCLUDED.enabled,
            allows_none = EXCLUDED.allows_none,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        RETURNING fdm4_store, enabled, allows_none, updated_by, updated_at
        """,
        (store, command.enabled, command.allows_none, actor),
    )
    result = dict(cursor.fetchone())
    return MutationResult(
        {"ok": True, "settings": result, "updated_by": actor},
        affected_scopes(command),
    )


def set_store_pricing_tier(
    cursor,
    actor: str,
    command: SetStorePricingTierCommand,
) -> MutationResult:
    del actor  # woo.store_pricing_tier has no updated_by column.
    store = _clean(command.fdm4_store, "fdm4_store")
    tier = _clean(command.tier_name, "tier_name")
    note = _optional_text(command.note, "note", 500)
    if _catalog_for_store(cursor, store) is None:
        raise NotFound("Store not found")
    cursor.execute("SELECT 1 FROM woo.pricing_tier WHERE tier_name = %s", (tier,))
    if cursor.fetchone() is None:
        raise InvalidCommand("Unknown pricing tier")
    cursor.execute(
        """
        INSERT INTO woo.store_pricing_tier (
            fdm4_store, tier_name, note, updated_at
        ) VALUES (%s, %s, %s, now())
        ON CONFLICT (fdm4_store) DO UPDATE SET
            tier_name = EXCLUDED.tier_name,
            note = EXCLUDED.note,
            updated_at = now()
        RETURNING fdm4_store, tier_name, note
        """,
        (store, tier, note),
    )
    return MutationResult(
        {"ok": True, "assignment": dict(cursor.fetchone())},
        affected_scopes(command),
    )


def delete_store_pricing_tier(
    cursor,
    actor: str,
    command: DeleteStorePricingTierCommand,
) -> MutationResult:
    del actor
    store = _clean(command.fdm4_store, "fdm4_store")
    cursor.execute(
        "DELETE FROM woo.store_pricing_tier WHERE fdm4_store = %s",
        (store,),
    )
    if cursor.rowcount == 0:
        raise NotFound("No tier assignment for that store")
    return MutationResult({"ok": True}, affected_scopes(command))


def copy_style_to_many(cursor, actor: str, command: CopyStyleToManyCommand) -> MutationResult:
    """Agent wrapper over copy_style_batch: one style's logo setup fanned out
    to up to MAX_BULK_STYLES styles, staged and undone through the exact
    per-style snapshot. Replace mode (channel wipe) is deliberately not
    offered to the agent; merge/overwrite never remove rows."""
    store = _clean(command.store, "store")
    source = _clean(command.source_style, "source_style")
    targets = _clean_styles(command.target_styles, "target_styles")
    if source in targets:
        raise InvalidCommand("target_styles must not include the source style")
    outcome = copy_style_batch(
        cursor, fdm4_store=store, source_style=source, target_styles=targets,
        color_match=command.color_match, mode=command.mode, actor=actor,
    )
    return MutationResult(
        {"ok": True, "source_style": source, "targets": len(targets),
         "totals": outcome["totals"], "results": outcome["results"]},
        affected_scopes(command),
    )


def paste_logo_set(cursor, actor: str, command: PasteLogoSetCommand) -> MutationResult:
    """Agent wrapper over paste_batch: the same logo rows placed on every
    targeted color of up to MAX_BULK_STYLES styles."""
    store = _clean(command.store, "store")
    styles = _clean_styles(command.styles, "styles")
    match_color = (
        _clean(command.match_color, "match_color")
        if command.match_color not in (None, "") else None
    )
    if command.color_scope == "match" and match_color is None:
        raise InvalidCommand("match_color is required when color_scope is match")
    rows = [row.model_dump() for row in command.rows]
    outcome = paste_batch(
        cursor, fdm4_store=store, styles=styles, color_scope=command.color_scope,
        match_color=match_color, rows=rows, overwrite=command.overwrite,
        as_new_rows=command.as_new_rows, actor=actor,
    )
    return MutationResult(
        {"ok": True, "styles": len(styles), "rows_per_color": len(rows),
         "totals": outcome["totals"], "results": outcome["results"]},
        affected_scopes(command),
    )


def replace_design(cursor, actor: str, command: ReplaceDesignCommand) -> MutationResult:
    """Agent wrapper over design_swap on an explicit style list (the exact
    snapshot needs the styles up front; list_design_usage finds them)."""
    store = _clean(command.store, "store")
    styles = _clean_styles(command.styles, "styles")
    outcome = design_swap(
        cursor, fdm4_store=store, from_design_id=command.from_design_id,
        from_color_scheme_id=command.from_color_scheme_id,
        to_design_id=command.to_design_id, to_color_scheme_id=command.to_color_scheme_id,
        to_logo_code=command.to_logo_code, styles=styles, actor=actor,
    )
    return MutationResult(
        {"ok": True, "applied": outcome["applied"], "unchanged": outcome["unchanged"],
         "skipped_invalid": outcome["skipped_invalid"], "styles": outcome["styles"],
         "target": outcome["target"], "problems": outcome["problems"]},
        affected_scopes(command),
    )


def reorder_logo_rows(cursor, actor: str, command: ReorderLogoRowsCommand) -> MutationResult:
    """Agent wrapper over reorder_option_rows (sort_order renumbering)."""
    store = _clean(command.store, "store")
    style = _clean(command.style, "style")
    color = _clean(command.garment_color_code, "garment_color_code")
    outcome = reorder_option_rows(
        cursor, fdm4_store=store, product_style=style, garment_color_code=color,
        option_rows=list(command.option_rows), apply_to=command.apply_to, actor=actor,
    )
    return MutationResult(
        {"ok": True, "updated": outcome["updated"], "colors": outcome["colors"],
         "order": list(command.option_rows), "apply_to": command.apply_to},
        affected_scopes(command),
    )


def set_styles_active(cursor, actor: str, command: SetStylesActiveCommand) -> MutationResult:
    """set_style_active over up to MAX_BULK_STYLES styles; styles without
    rows are reported, not fatal, unless none had rows."""
    store = _clean(command.store, "store")
    styles = _clean_styles(command.styles, "styles")
    results = []
    updated = 0
    for style in styles:
        try:
            single = set_style_active(
                cursor, actor,
                SetStyleActiveCommand(store=store, style=style, active=command.active),
            )
        except NotFound as exc:
            results.append({"style": style, "updated": 0, "error": str(exc)})
            continue
        results.append({"style": style, "updated": single.value["updated"]})
        updated += int(single.value["updated"])
    if updated == 0:
        raise NotFound("None of the styles have logo rows")
    return MutationResult(
        {"ok": True, "updated": updated, "active": command.active, "results": results},
        affected_scopes(command),
    )


def _store_exists(cursor, store: str) -> bool:
    cursor.execute("SELECT 1 FROM woo.store_catalog WHERE fdm4_store = %s LIMIT 1", (store,))
    return cursor.fetchone() is not None


def set_logo_name(cursor, actor: str, command: SetLogoNameCommand) -> MutationResult:
    """Shopper-facing name of one logo (design + scheme): the shared default
    (store null) or one store's own name. Hand-set names are locked so the
    FDM4 re-pull never overwrites them."""
    design = _clean(command.design_id, "design_id")
    scheme = _upper(command.color_scheme_id, "color_scheme_id")
    name = " ".join(_clean(command.name, "name", 200).split())
    store = _shared_or_store(command.store)
    cursor.execute(
        """
        SELECT 1 FROM logo.display_name WHERE design_id = %s AND color_scheme_id = %s
        UNION ALL
        SELECT 1 FROM logo.assignment
         WHERE btrim(design_id) = %s AND upper(btrim(color_scheme_id)) = %s
        LIMIT 1
        """,
        (design, scheme, design, scheme),
    )
    if cursor.fetchone() is None:
        raise NotFound("No logo with that design and color scheme is on file")
    if store and not _store_exists(cursor, store):
        raise NotFound("Store not found")
    cursor.execute(
        """
        INSERT INTO logo.display_name
            (design_id, color_scheme_id, fdm4_store, name, source, locked,
             uses, updated_at, updated_by)
        VALUES (%s, %s, %s, %s, 'manual', true, 0, now(), %s)
        ON CONFLICT (design_id, color_scheme_id, fdm4_store) DO UPDATE SET
            name = EXCLUDED.name, source = 'manual', locked = true,
            updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        (design, scheme, store, name, actor),
    )
    return MutationResult(
        {"ok": True, "design_id": design, "color_scheme_id": scheme,
         "store": store or None, "name": name},
        affected_scopes(command),
    )


def clear_logo_name(cursor, actor: str, command: ClearLogoNameCommand) -> MutationResult:
    del actor
    design = _clean(command.design_id, "design_id")
    scheme = _upper(command.color_scheme_id, "color_scheme_id")
    store = _shared_or_store(command.store)
    if not store:
        raise InvalidCommand("store is required; the shared default name cannot be removed")
    cursor.execute(
        "DELETE FROM logo.display_name WHERE design_id = %s AND color_scheme_id = %s AND fdm4_store = %s",
        (design, scheme, store),
    )
    if cursor.rowcount == 0:
        raise NotFound("That store has no name of its own for this logo")
    return MutationResult(
        {"ok": True, "design_id": design, "color_scheme_id": scheme, "store": store, "removed": 1},
        affected_scopes(command),
    )


def set_garment_color_class(cursor, actor: str, command: SetColorClassCommand) -> MutationResult:
    code = _clean(command.color_code, "color_code")
    try:
        outcome = set_color_class(cursor, color_code=code, light_dark=command.light_dark, actor=actor)
    except LookupError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise InvalidCommand(str(exc)) from exc
    return MutationResult({"ok": True, **outcome}, affected_scopes(command))


def set_stock_override(cursor, actor: str, command: SetStockOverrideCommand) -> MutationResult:
    style = _upper(command.style_code, "style_code")
    note = " ".join(_optional_text(command.note, "note", 1000).split())
    cursor.execute(
        """
        SELECT max(brand) AS brand, max(name) AS product_name,
               count(*) FILTER (WHERE kind = 'variation' AND is_active) AS variants
          FROM woo.store_product_state
         WHERE upper(btrim(style_code)) = %s
        """,
        (style,),
    )
    info = cursor.fetchone()
    if not info or not info["variants"]:
        raise NotFound(f"Style {style} has no active variations in the warehouse")
    cursor.execute(
        """
        INSERT INTO woo.stock_override (style_code, mode, note, active, updated_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (style_code) DO UPDATE SET
            mode = EXCLUDED.mode, note = EXCLUDED.note, active = EXCLUDED.active,
            updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        (style, command.mode, note, command.active, actor),
    )
    return MutationResult(
        {"ok": True, "style_code": style, "mode": command.mode, "active": command.active,
         "brand": info["brand"] or "", "product_name": info["product_name"] or "",
         "variants": int(info["variants"])},
        affected_scopes(command),
    )


def remove_stock_override(cursor, actor: str, command: RemoveStockOverrideCommand) -> MutationResult:
    del actor
    style = _upper(command.style_code, "style_code")
    cursor.execute("DELETE FROM woo.stock_override WHERE style_code = %s", (style,))
    if cursor.rowcount == 0:
        raise NotFound("That style has no stock exception")
    return MutationResult({"ok": True, "style_code": style, "removed": 1}, affected_scopes(command))


def set_brand_stock_rule(cursor, actor: str, command: SetBrandStockRuleCommand) -> MutationResult:
    mill = _clean(command.mill_code, "mill_code", 32)
    cursor.execute(
        'SELECT btrim(COALESCE(description, \'\')) AS name FROM fdm4.mill WHERE btrim("mill-code") = %s LIMIT 1',
        (mill,),
    )
    row = cursor.fetchone()
    if not row:
        raise NotFound(f"No FDM4 brand with mill code {mill}")
    cursor.execute(
        """
        INSERT INTO woo.brand_stock_rule (mill_code, brand_name, mode, active, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (mill_code) DO UPDATE SET
            mode = EXCLUDED.mode, brand_name = EXCLUDED.brand_name,
            active = EXCLUDED.active, updated_by = EXCLUDED.updated_by, updated_at = now()
        """,
        (mill, row["name"], command.mode, command.active, actor),
    )
    cursor.execute(
        'SELECT count(DISTINCT btrim("style-code")) AS n FROM fdm4.style WHERE btrim("mill-code") = %s',
        (mill,),
    )
    styles = int(cursor.fetchone()["n"])
    return MutationResult(
        {"ok": True, "mill_code": mill, "brand_name": row["name"], "mode": command.mode,
         "active": command.active, "styles": styles},
        affected_scopes(command),
    )


def remove_brand_stock_rule(cursor, actor: str, command: RemoveBrandStockRuleCommand) -> MutationResult:
    del actor
    mill = _clean(command.mill_code, "mill_code", 32)
    cursor.execute("DELETE FROM woo.brand_stock_rule WHERE mill_code = %s", (mill,))
    if cursor.rowcount == 0:
        raise NotFound("That brand has no rule to remove")
    return MutationResult({"ok": True, "mill_code": mill, "removed": 1}, affected_scopes(command))


def set_sync_block(cursor, actor: str, command: SetSyncBlockCommand) -> MutationResult:
    store = _upper(command.store, "store")
    if not _store_exists(cursor, store):
        raise NotFound(f"Unknown store code: {store}")
    keys = _sync_block_keys(store, command.styles)
    whole_store = keys == [""]
    scope = command.scope if whole_store else "full"
    note = " ".join(_optional_text(command.note, "note", 1000).split())
    for style in keys:
        cursor.execute(
            """
            INSERT INTO woo.sync_exclusion (fdm4_store, style_code, note, active, scope, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fdm4_store, style_code) DO UPDATE SET
                note = EXCLUDED.note, active = EXCLUDED.active, scope = EXCLUDED.scope,
                updated_at = now(), updated_by = EXCLUDED.updated_by
            """,
            (store, style, note, command.active, scope, actor),
        )
    per_style = []
    if not whole_store:
        cursor.execute(
            """
            SELECT upper(btrim(style_code)) AS style, count(*) AS products
              FROM woo.store_product_state
             WHERE fdm4_store = %s AND is_active AND kind = 'variation'
               AND upper(btrim(style_code)) = ANY(%s)
             GROUP BY 1
            """,
            (store, keys),
        )
        counts = {r["style"]: int(r["products"]) for r in cursor.fetchall()}
        per_style = [{"style": s, "products": counts.get(s, 0)} for s in keys]
    return MutationResult(
        {"ok": True, "store": store, "whole_store": whole_store, "scope": scope,
         "active": command.active, "saved": len(keys), "per_style": per_style},
        affected_scopes(command),
    )


def remove_sync_block(cursor, actor: str, command: RemoveSyncBlockCommand) -> MutationResult:
    del actor
    store = _upper(command.store, "store")
    keys = _sync_block_keys(store, command.styles)
    removed = 0
    for style in keys:
        cursor.execute(
            "DELETE FROM woo.sync_exclusion WHERE fdm4_store = %s AND style_code = %s",
            (store, style),
        )
        removed += cursor.rowcount
    if removed == 0:
        raise NotFound("No matching freeze to remove")
    return MutationResult(
        {"ok": True, "store": store, "whole_store": keys == [""], "removed": removed},
        affected_scopes(command),
    )


MAX_EXTRA_CUSTOMERS = 20


def set_logo_cost(cursor, actor: str, command: SetLogoCostCommand) -> MutationResult:
    """One shopper charge for every row of a logo (design, optionally one
    scheme) on the named styles of a store; null clears the override so the
    logo's default cost applies. Rows are updated in place - nothing else on
    them changes."""
    store = _clean(command.store, "store")
    design = _clean(command.design_id, "design_id")
    scheme = (
        _upper(command.color_scheme_id, "color_scheme_id")
        if command.color_scheme_id not in (None, "") else None
    )
    styles = _clean_styles(command.styles, "styles")
    cost = _decimal(command.cost_override)
    if cost is not None and cost < 0:
        raise InvalidCommand("cost_override must be zero or positive")
    _bounded_assignment_count(
        cursor,
        "fdm4_store = %s AND product_style = ANY(%s)",
        (store, styles),
        label="Logo cost change",
    )
    cursor.execute(
        """
        UPDATE logo.assignment
           SET cost_override = %s, updated_by = %s, updated_at = now()
         WHERE fdm4_store = %s AND product_style = ANY(%s)
           AND btrim(design_id) = %s
           AND (%s::text IS NULL OR upper(btrim(color_scheme_id)) = %s)
           AND cost_override IS DISTINCT FROM %s
        """,
        (cost, actor, store, styles, design, scheme, scheme, cost),
    )
    changed = cursor.rowcount
    cursor.execute(
        """
        SELECT count(*) AS n FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = ANY(%s) AND btrim(design_id) = %s
           AND (%s::text IS NULL OR upper(btrim(color_scheme_id)) = %s)
        """,
        (store, styles, design, scheme, scheme),
    )
    matching = int(cursor.fetchone()["n"])
    if matching == 0:
        raise NotFound("None of those styles carry that logo in this store")
    return MutationResult(
        {"ok": True, "store": store, "design_id": design, "color_scheme_id": scheme,
         "cost_override": None if cost is None else str(cost),
         "matching_rows": matching, "updated": changed, "already_set": matching - changed},
        affected_scopes(command),
    )


def set_store_extra_customers(cursor, actor: str, command: SetStoreExtraCustomersCommand) -> MutationResult:
    """Which other FDM4 customers' designs a store may use (logo.store_settings
    .extra_customers). Replaces the list; the row is created with default
    switches when the store has none yet."""
    store = _clean(command.store, "store")
    if _catalog_for_store(cursor, store) is None:
        raise NotFound("Store not found")
    customers: list = []
    for value in command.customers or ():
        code = _clean(value, "customers", 32)
        if code not in customers:
            customers.append(code)
    if len(customers) > MAX_EXTRA_CUSTOMERS:
        raise InvalidCommand(f"at most {MAX_EXTRA_CUSTOMERS} extra customers")
    for code in customers:
        cursor.execute(
            "SELECT 1 FROM fdm4.dec_design WHERE btrim(cust_number) = %s LIMIT 1", (code,),
        )
        if cursor.fetchone() is None:
            raise NotFound(f"No FDM4 designs are on file for customer {code}")
    cursor.execute(
        """
        INSERT INTO logo.store_settings (fdm4_store, enabled, allows_none, extra_customers, updated_by, updated_at)
        VALUES (%s, true, false, %s, %s, now())
        ON CONFLICT (fdm4_store) DO UPDATE SET
            extra_customers = EXCLUDED.extra_customers,
            updated_by = EXCLUDED.updated_by, updated_at = now()
        RETURNING fdm4_store, enabled, allows_none, extra_customers
        """,
        (store, customers, actor),
    )
    result = dict(cursor.fetchone())
    return MutationResult({"ok": True, "settings": result}, affected_scopes(command))


def bulk_apply(cursor, actor: str, command: BulkApplyCommand) -> MutationResult:
    """Agent wrapper over the Bulk Apply page: one logo variant onto every
    garment color of a class (or the listed colors) across a store, or the
    named styles. Same preview rows as the page (queries.compute_bulk_preview),
    same executor and editor journal (bulk_apply_execute). The whole store's
    rows are the exact-undo scope, so it is bounded by the snapshot row cap."""
    from queries import compute_bulk_preview  # local: queries imports this module's peers

    store = _clean(command.store, "store")
    if _catalog_for_store(cursor, store) is None:
        raise NotFound("Store not found")
    logo_code = _upper(command.logo_code, "logo_code")
    scheme = _upper(command.color_scheme_id, "color_scheme_id")
    placement = _clean(command.location, "location", 200)
    # Use the vocabulary's spelling so "LEFT CHEST" lands as "Left Chest".
    cursor.execute(
        "SELECT name FROM logo.placement_vocab WHERE lower(btrim(name)) = lower(%s) ORDER BY active DESC LIMIT 1",
        (placement,),
    )
    known = cursor.fetchone()
    if known:
        placement = str(known["name"])
    if command.target == "light_dark":
        if command.color_class not in ("light", "dark"):
            raise InvalidCommand("color_class (light or dark) is required when target is light_dark")
        target = {"mode": "light_dark", "class": command.color_class}
    else:
        codes = list(dict.fromkeys(_clean(c, "color_codes") for c in (command.color_codes or ())))
        if not codes:
            raise InvalidCommand("color_codes is required when target is colors")
        target = {"mode": "colors", "color_codes": codes}
    styles = _clean_styles(command.styles, "styles") if command.styles else None
    try:
        preview = compute_bulk_preview(
            cursor, fdm4_store=store, logo_code=logo_code, color_scheme=scheme,
            target=target, style_codes=styles, option_row=command.option_row,
            design_id=command.design_id,
        )
    except ValueError as exc:
        raise InvalidCommand(str(exc)) from exc
    if preview.get("unresolved_reason"):
        raise InvalidCommand(f"Cannot resolve the logo variant: {preview['unresolved_reason']}")
    candidates = preview["rows"]
    if not candidates:
        raise NotFound("No products in this store match that target")
    rows = [r for r in candidates if command.overwrite or r.get("was") is None]
    skipped = len(candidates) - len(rows)
    if not rows:
        raise InvalidCommand("Every matching color already has a logo in that slot; set overwrite to replace them")
    if len(rows) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(f"Bulk apply would touch more than the {MAX_ASSIGNMENT_MUTATION_ROWS}-row mutation limit; narrow it with styles")
    try:
        outcome = bulk_apply_execute(
            cursor, fdm4_store=store, logo_code=logo_code, color_scheme=scheme,
            placement=placement,
            rows=[{"style_code": r["style_code"], "color_code": r["color_code"]} for r in rows],
            actor=actor, option_row=command.option_row,
            cost_override=_decimal(command.cost_override), image_url=None,
            design_id=command.design_id,
        )
    except ValueError as exc:
        raise InvalidCommand(str(exc)) from exc
    return MutationResult(
        {"ok": True, "applied": outcome["applied"], "skipped_existing": skipped,
         "image_url_missing": outcome["image_url_missing"],
         "styles": sorted({r["style_code"] for r in rows}),
         "unclassified_colors": int(preview["counts"].get("unclassified", 0) or 0),
         "design_id": preview["design_id"], "target": target},
        affected_scopes(command),
    )


def set_logo_default_cost(cursor, actor: str, command: SetLogoDefaultCostCommand) -> MutationResult:
    """The logo-level default shopper charge (logo.default_cost, per logo code
    + scheme, every store without a row override). Hand-set values are locked
    against cost re-imports."""
    code = _upper(command.logo_code, "logo_code")
    scheme = _upper(command.color_scheme_id, "color_scheme_id")
    cost = _decimal(command.cost)
    if cost is None or cost < 0:
        raise InvalidCommand("cost must be zero or positive")
    cursor.execute(
        """
        SELECT 1 FROM logo.default_cost WHERE logo_code = %s AND color_scheme_id = %s
        UNION ALL
        SELECT 1 FROM logo.assignment
         WHERE upper(btrim(logo_code)) = %s AND upper(btrim(color_scheme_id)) = %s
        LIMIT 1
        """,
        (code, scheme, code, scheme),
    )
    if cursor.fetchone() is None:
        raise NotFound("No logo with that code and color scheme is on file")
    cursor.execute(
        """
        SELECT count(DISTINCT fdm4_store) AS stores FROM logo.assignment
         WHERE upper(btrim(logo_code)) = %s AND upper(btrim(color_scheme_id)) = %s
           AND cost_override IS NULL AND active
        """,
        (code, scheme),
    )
    affected_stores = int(cursor.fetchone()["stores"])
    cursor.execute(
        """
        INSERT INTO logo.default_cost (logo_code, color_scheme_id, cost, source, locked, updated_by, updated_at)
        VALUES (%s, %s, %s, 'manual', %s, %s, now())
        ON CONFLICT (logo_code, color_scheme_id) DO UPDATE SET
            cost = EXCLUDED.cost, source = 'manual', locked = EXCLUDED.locked,
            updated_by = EXCLUDED.updated_by, updated_at = now()
        """,
        (code, scheme, cost, command.locked, actor),
    )
    return MutationResult(
        {"ok": True, "logo_code": code, "color_scheme_id": scheme, "cost": str(cost),
         "locked": command.locked, "stores_using_default": affected_stores},
        affected_scopes(command),
    )

PRICE_EFFECT_TYPES = ("percent", "flat", "set_price", "price_level", "margin_over_cost")
PRICE_LEVEL_KEYS = ("msrp", "corp1", "corp2", "corp3", "wholesale", "employee", "base")
MAX_RULE_VALUE = Decimal("9999999")

MATERIAL_RULE_FIELDS = (
    "priority", "stackable", "stores", "store_tiers", "styles", "brands",
    "categories", "excl_stores", "excl_styles", "excl_brands",
    "excl_categories", "effect_type", "effect_value", "price_level_key",
    "basis", "rounding", "floor_price", "ceiling_price", "cap_at_msrp",
    "effective_from", "effective_until",
)

def _rule_material_changed(old, new) -> bool:
    for field in MATERIAL_RULE_FIELDS:
        a, b = old.get(field), new[field]
        if isinstance(a, list) or isinstance(b, list):
            if set(a or []) != set(b or []):
                return True
        elif field in ("effect_value", "floor_price", "ceiling_price"):
            if (a is None) != (b is None):
                return True
            if a is not None and Decimal(a) != Decimal(b):
                return True
        elif a != b:
            return True
    return False


def _clean_list(values, upper=False, maxlen=100, maxitems=5000, field="list"):
    out = []
    seen = set()
    for v in values or []:
        v = " ".join(str(v).split())
        if len(v) > maxlen:
            raise InvalidCommand(f"{field}: entry exceeds {maxlen} characters: {v[:40]}...")
        if upper:
            v = v.upper()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    if len(out) > maxitems:
        raise InvalidCommand(f"{field}: too many entries ({len(out)}; max {maxitems})")
    return out


def _parse_rule_date(value, field):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise InvalidCommand(f"{field} must be YYYY-MM-DD")


def save_price_rule(cursor, actor: str, command: SavePriceRuleCommand, *, preview_activation: bool = False) -> MutationResult:
    for field in ("effect_value", "floor_price", "ceiling_price"):
        value = getattr(command, field)
        if value is not None and not value.is_finite():
            raise InvalidCommand(f"{field} must be finite")
    if not " ".join(command.name.split()):
        raise InvalidCommand("name is required")
    if command.effect_type not in PRICE_EFFECT_TYPES:
        raise InvalidCommand("Unknown effect type")
    if command.effect_type == "price_level":
        if (command.price_level_key or "") not in PRICE_LEVEL_KEYS:
            raise InvalidCommand("price_level_key required for price_level effect")
    elif command.effect_value is None:
        raise InvalidCommand("effect_value required for this effect type")
    if command.effect_value is not None:
        if abs(command.effect_value) > MAX_RULE_VALUE:
            raise InvalidCommand("effect_value is out of range")
        if command.effect_type == "set_price" and command.effect_value <= 0:
            raise InvalidCommand("set_price must be greater than 0")
        if command.effect_type == "margin_over_cost" and command.effect_value <= 0:
            raise InvalidCommand("margin_over_cost multiplier must be greater than 0")
        if command.effect_type == "percent" and (command.effect_value <= -100 or command.effect_value > 1000):
            raise InvalidCommand("percent must be above -100 and at most 1000")
    if command.floor_price is not None and (command.floor_price < 0 or command.floor_price > MAX_RULE_VALUE):
        raise InvalidCommand("floor_price must be between 0 and 9,999,999")
    if command.ceiling_price is not None and (command.ceiling_price < 0 or command.ceiling_price > MAX_RULE_VALUE):
        raise InvalidCommand("ceiling_price must be between 0 and 9,999,999")
    if (command.floor_price is not None and command.ceiling_price is not None
            and command.ceiling_price < command.floor_price):
        raise InvalidCommand("The never-above price is below the never-below price")
    basis = (command.basis or "current").strip().lower()
    if command.effect_type not in ("percent", "flat"):
        basis = "current"
    if basis != "current" and basis not in PRICE_LEVEL_KEYS:
        raise InvalidCommand("Unknown price basis")
    rounding = (command.rounding or "none").strip().lower()
    if rounding not in ("none", "99", "95", "00"):
        raise InvalidCommand("Unknown rounding choice")
    frm = _parse_rule_date(command.effective_from, "effective_from")
    until = _parse_rule_date(command.effective_until, "effective_until")
    if frm and until and until < frm:
        raise InvalidCommand("effective_until is before effective_from")
    values = {
        "rule_id": command.rule_id,
        "name": " ".join(command.name.split()),
        "priority": command.priority,
        "stackable": command.stackable,
        "stores": _clean_list(command.stores, upper=True, field="stores") or None,
        "store_tiers": _clean_list(command.store_tiers, field="store_tiers") or None,
        "styles": _clean_list(command.styles, upper=True, field="styles") or None,
        "brands": _clean_list(command.brands, field="brands") or None,
        "categories": _clean_list(command.categories, field="categories") or None,
        "excl_stores": _clean_list(command.excl_stores, upper=True, field="excl_stores") or None,
        "excl_styles": _clean_list(command.excl_styles, upper=True, field="excl_styles") or None,
        "excl_brands": _clean_list(command.excl_brands, field="excl_brands") or None,
        "excl_categories": _clean_list(command.excl_categories, field="excl_categories") or None,
        "effect_type": command.effect_type,
        "effect_value": command.effect_value,
        "price_level_key": command.price_level_key if command.effect_type == "price_level" else None,
        "basis": basis,
        "rounding": rounding,
        "floor_price": command.floor_price,
        "ceiling_price": command.ceiling_price,
        "cap_at_msrp": bool(command.cap_at_msrp),
        "effective_from": frm,
        "effective_until": until,
        "note": command.note.strip(),
    }
    deactivated = False
    if values["rule_id"]:
        cursor.execute(
            "SELECT * FROM woo.price_rule WHERE rule_id=%s FOR UPDATE",
            (values["rule_id"],),
        )
        old = cursor.fetchone()
        if not old:
            raise NotFound("Rule not found")
        material = _rule_material_changed(old, values)
        # HTTP save can keep an active rule only for label edits. Staged
        # activation below evaluates the saved content before switching on.
        new_active = bool(command.active) and bool(old["active"]) and not material
        deactivated = bool(old["active"]) and bool(command.active) and not new_active
        cursor.execute(
            """
            UPDATE woo.price_rule SET
                name=%(name)s, active=%(active)s, priority=%(priority)s, stackable=%(stackable)s,
                stores=%(stores)s, store_tiers=%(store_tiers)s, styles=%(styles)s,
                brands=%(brands)s, categories=%(categories)s,
                excl_stores=%(excl_stores)s, excl_styles=%(excl_styles)s,
                excl_brands=%(excl_brands)s, excl_categories=%(excl_categories)s,
                effect_type=%(effect_type)s, effect_value=%(effect_value)s,
                price_level_key=%(price_level_key)s, basis=%(basis)s, rounding=%(rounding)s,
                floor_price=%(floor_price)s, ceiling_price=%(ceiling_price)s, cap_at_msrp=%(cap_at_msrp)s,
                effective_from=%(effective_from)s, effective_until=%(effective_until)s,
                last_previewed_at=CASE WHEN %(material)s THEN NULL ELSE last_previewed_at END,
                note=%(note)s, updated_at=now(), updated_by=%(actor)s
             WHERE rule_id=%(rule_id)s RETURNING rule_id, active
            """,
            {**values, "active": new_active, "material": material,
             "actor": actor},
        )
        row = cursor.fetchone()
    else:
        # New rules start inactive; staged activation follows the impact check.
        cursor.execute(
            """
            INSERT INTO woo.price_rule (rule_id, name, active, priority, stackable, stores, store_tiers,
                styles, brands, categories, excl_stores, excl_styles, excl_brands, excl_categories,
                effect_type, effect_value, price_level_key, basis, rounding,
                floor_price, ceiling_price, cap_at_msrp, effective_from, effective_until, note, updated_by)
            VALUES (COALESCE(%(reserved_id)s, nextval(pg_get_serial_sequence('woo.price_rule', 'rule_id'))), %(name)s, false, %(priority)s, %(stackable)s, %(stores)s, %(store_tiers)s,
                %(styles)s, %(brands)s, %(categories)s, %(excl_stores)s, %(excl_styles)s,
                %(excl_brands)s, %(excl_categories)s, %(effect_type)s, %(effect_value)s,
                %(price_level_key)s, %(basis)s, %(rounding)s, %(floor_price)s, %(ceiling_price)s,
                %(cap_at_msrp)s, %(effective_from)s, %(effective_until)s, %(note)s, %(actor)s)
            RETURNING rule_id, active
            """,
            {**values, "actor": actor, "reserved_id": command._reserved_rule_id},
        )
        row = cursor.fetchone()
    impact = None
    if preview_activation and command.active:
        from queries import price_rule_impact
        impact = price_rule_impact(cursor, row["rule_id"])
        cursor.execute(
            "UPDATE woo.price_rule SET active = true, last_previewed_at = now() WHERE rule_id = %s",
            (row["rule_id"],),
        )
        row = {**row, "active": True}
        deactivated = False
    scoped = command.model_copy(update={"rule_id": row["rule_id"]})
    value = {"ok": True, "rule_id": row["rule_id"], "active": row["active"], "deactivated": deactivated}
    if impact is not None:
        value["price_rule_impact"] = impact
    return MutationResult(value, affected_scopes(scoped))


def set_price_rule_active(cursor, actor: str, command: SetPriceRuleActiveCommand, *, preview_activation: bool = False) -> MutationResult:
    """Flip a price rule on or off - never its content. Activation needs a
    preview stamp newer than the last edit (the app's save path clears it)."""
    cursor.execute(
        "SELECT name, active, last_previewed_at FROM woo.price_rule WHERE rule_id = %s FOR UPDATE",
        (command.rule_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise NotFound("Rule not found")
    impact = None
    if command.active and preview_activation:
        from queries import price_rule_impact
        impact = price_rule_impact(cursor, command.rule_id)
        cursor.execute("UPDATE woo.price_rule SET last_previewed_at = now() WHERE rule_id = %s", (command.rule_id,))
    if command.active and not preview_activation and row["last_previewed_at"] is None:
        raise InvalidCommand(
            "Preview this rule in the app before activating it - its settings changed since the last preview"
        )
    cursor.execute(
        "UPDATE woo.price_rule SET active = %s, updated_at = now(), updated_by = %s WHERE rule_id = %s",
        (command.active, actor, command.rule_id),
    )
    return MutationResult(
        {"ok": True, "rule_id": command.rule_id, "name": row["name"], "active": command.active,
         "was_active": bool(row["active"]), **({"price_rule_impact": impact} if impact is not None else {})},
        affected_scopes(command),
    )


def delete_price_rule(cursor, actor: str, command: DeletePriceRuleCommand) -> MutationResult:
    del actor
    cursor.execute(
        "DELETE FROM woo.price_rule WHERE rule_id = %s RETURNING name, active", (command.rule_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise NotFound("Rule not found")
    return MutationResult(
        {"ok": True, "rule_id": command.rule_id, "name": row["name"], "was_active": bool(row["active"])},
        affected_scopes(command),
    )


def set_product_mix(cursor, actor: str, command: SetProductMixCommand) -> MutationResult:
    store = mix_service.norm(command.store)
    note = " ".join(_optional_text(command.note, "note", 1000).split())
    outcome = mix_service.enable(cursor, store, command.mode, note, actor)
    outcome.update({"ok": True, "styles_in_list": mix_service.item_count(cursor, store) if command.mode == "list" else 0})
    return MutationResult(outcome, affected_scopes(command))


def disable_product_mix(cursor, actor: str, command: DisableProductMixCommand) -> MutationResult:
    store = mix_service.norm(command.store)
    mix_service.disable(cursor, store, actor)
    return MutationResult({"ok": True, "store": store, "active": False}, affected_scopes(command))


def add_mix_styles(cursor, actor: str, command: AddMixStylesCommand) -> MutationResult:
    store = mix_service.norm(command.store)
    styles = list(dict.fromkeys(mix_service.norm(s) for s in _clean_styles(command.styles, "styles")))
    outcome = mix_service.add_styles(cursor, store, styles, actor)
    return MutationResult({"ok": True, "store": store, **outcome}, affected_scopes(command))


def remove_mix_styles(cursor, actor: str, command: RemoveMixStylesCommand) -> MutationResult:
    del actor
    store = mix_service.norm(command.store)
    styles = list(dict.fromkeys(mix_service.norm(s) for s in _clean_styles(command.styles, "styles")))
    removed = mix_service.remove_styles(cursor, store, styles)
    if removed == 0:
        raise NotFound("None of those styles are in the store's list")
    return MutationResult({"ok": True, "store": store, "removed": removed}, affected_scopes(command))


def fill_missing_colors(cursor, actor: str, command: FillMissingColorsCommand) -> MutationResult:
    store = _clean(command.store, "store")
    styles = [_clean(entry.style, "style") for entry in command.entries]
    _clean_styles(styles, "styles")
    if len(styles) != len(set(styles)):
        raise InvalidCommand("Each style must appear only once")
    _bounded_assignment_count(cursor, "fdm4_store = %s", (store,), label="Store")
    outcome = fill_gaps(cursor, fdm4_store=store,
                        entries=[entry.model_dump() for entry in command.entries],
                        overwrite=command.overwrite, actor=actor)
    _bounded_assignment_count(cursor, "fdm4_store = %s", (store,), label="Store")
    return MutationResult(outcome, affected_scopes(command))


MUTATION_HANDLERS: Dict[str, Callable] = {
    "save_price_rule": save_price_rule,
    "fill_missing_colors": fill_missing_colors,
    "save_assignment": save_assignment,
    "deactivate_assignment": deactivate_assignment,
    "hard_delete_assignment": hard_delete_assignment,
    "deactivate_color": deactivate_color,
    "hard_delete_color": hard_delete_color,
    "set_style_active": set_style_active,
    "apply_to_colors": apply_to_colors,
    "copy_style": copy_style,
    "update_store_settings": update_store_settings,
    "set_store_pricing_tier": set_store_pricing_tier,
    "delete_store_pricing_tier": delete_store_pricing_tier,
    "copy_style_to_many": copy_style_to_many,
    "paste_logo_set": paste_logo_set,
    "replace_design": replace_design,
    "reorder_logo_rows": reorder_logo_rows,
    "set_styles_active": set_styles_active,
    "set_logo_name": set_logo_name,
    "clear_logo_name": clear_logo_name,
    "set_color_class": set_garment_color_class,
    "set_stock_override": set_stock_override,
    "remove_stock_override": remove_stock_override,
    "set_brand_stock_rule": set_brand_stock_rule,
    "remove_brand_stock_rule": remove_brand_stock_rule,
    "set_sync_block": set_sync_block,
    "remove_sync_block": remove_sync_block,
    "set_logo_cost": set_logo_cost,
    "set_store_extra_customers": set_store_extra_customers,
    "bulk_apply": bulk_apply,
    "set_logo_default_cost": set_logo_default_cost,
    "set_price_rule_active": set_price_rule_active,
    "delete_price_rule": delete_price_rule,
    "set_product_mix": set_product_mix,
    "disable_product_mix": disable_product_mix,
    "add_mix_styles": add_mix_styles,
    "remove_mix_styles": remove_mix_styles,
}


def dispatch_mutation(
    cursor,
    actor: str,
    command: MutationCommand,
    *, preview_activation: bool = False,
) -> MutationResult:
    for name, model in COMMAND_MODELS.items():
        if isinstance(command, model):
            if isinstance(command, (SavePriceRuleCommand, SetPriceRuleActiveCommand)):
                return MUTATION_HANDLERS[name](cursor, actor, command, preview_activation=preview_activation)
            return MUTATION_HANDLERS[name](cursor, actor, command)
    raise InvalidCommand("unsupported mutation command")


def bulk_apply_execute(cursor, *, fdm4_store, logo_code, color_scheme, placement, rows, actor,
                       option_row=1, cost_override=None, image_url=None, design_id=None) -> dict:
    """Apply a logo variant to selected (style, color) rows in one store.

    rows: [{style_code, color_code}]. Resolves the design_id via the FDM4
    art-file prefix lookup, uses an explicit image_url when provided (else
    derives one from an existing sibling assignment in the same store),
    snapshots each prior (option_row, position 1) row into logo.bulk_batch_row,
    then upserts via the UPSERT_SQL template.

    option_row selects which logo slot to write (default 1 = primary; 2/3 add
    the variant alongside an existing primary without touching it).
    cost_override sets the per-logo price (None keeps it unset).

    Runs entirely inside the caller's transaction - no commit/rollback here.
    Returns {applied, batch_id, image_url_missing}.
    """
    logo_code = logo_code.upper()
    scheme = color_scheme.upper()

    design_lookup = load_design_index(cursor)
    designs = set(design_lookup.candidates(fdm4_store, logo_code, scheme))
    wanted = str(design_id).strip() if design_id not in (None, "") else ""
    if wanted:
        if designs and wanted not in designs:
            raise ValueError(
                f"design {wanted} does not carry {logo_code}/{scheme}"
                f" (candidates: {', '.join(sorted(designs))})"
            )
        designs = {wanted}
    if not designs or len(designs) > 1:
        raise ValueError(
            f"variant {logo_code}/{scheme} did not resolve to a single design"
            + (f" (ambiguous: {', '.join(sorted(designs))}; pass design_id)" if designs else "")
        )
    design_id = next(iter(designs))

    # Use an explicit image_url when provided (validated), else derive one from
    # an existing active sibling assignment in the same store (same logo + scheme).
    provided_url = _image_url(image_url) if image_url else ""
    if provided_url:
        image_url = provided_url
    else:
        cursor.execute(
            """
            SELECT image_url FROM logo.assignment
             WHERE fdm4_store = %s AND logo_code = %s AND color_scheme_id = %s
               AND active AND NULLIF(btrim(image_url), '') IS NOT NULL
             LIMIT 1
            """,
            (fdm4_store, logo_code, scheme),
        )
        sib = cursor.fetchone()
        image_url = sib["image_url"] if sib else ""

    cursor.execute(
        """
        INSERT INTO logo.bulk_batch
            (fdm4_store, logo_code, color_scheme, placement, target, applied, created_by)
        VALUES (%s, %s, %s, %s, %s, 0, %s)
        RETURNING batch_id
        """,
        (fdm4_store, logo_code, scheme, placement, json.dumps({"rows": len(rows)}), actor),
    )
    batch_id = cursor.fetchone()["batch_id"]

    applied = 0
    image_url_missing = 0

    for r in rows:
        style = r["style_code"]
        color = r["color_code"]

        # Snapshot the prior position-1 assignment (if any).
        cursor.execute(
            """
            SELECT to_jsonb(a) AS j FROM logo.assignment a
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s AND position = 1
            """,
            (fdm4_store, style, color, option_row),
        )
        prev = cursor.fetchone()
        before_json = json.dumps(prev["j"]) if (prev and prev["j"] is not None) else None

        cursor.execute(
            """
            INSERT INTO logo.bulk_batch_row
                (batch_id, fdm4_store, product_style, garment_color_code,
                 option_row, position, before_row)
            VALUES (%s, %s, %s, %s, %s, 1, %s)
            """,
            (batch_id, fdm4_store, style, color, option_row, before_json),
        )

        if not image_url:
            image_url_missing += 1

        params = {
            "fdm4_store": fdm4_store,
            "product_style": style,
            "garment_color_code": color,
            "option_row": option_row,
            "position": 1,
            "design_id": design_id,
            "logo_code": logo_code,
            "color_scheme_id": scheme,
            "location": placement,
            "optional": False,
            "background": "",
            "cost_override": cost_override,
            "sort_order": 0,
            "image_url": image_url,
            "name_override": None,
            "active": True,
        }
        _upsert_assignment(cursor, params, f"bulk-apply:{actor}")
        cursor.execute(
            """
            SELECT to_jsonb(a) AS j
              FROM logo.assignment a
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s AND position = 1
            """,
            (fdm4_store, style, color, option_row),
        )
        after = cursor.fetchone()
        if after is None:
            raise RuntimeError("bulk assignment disappeared before snapshot")
        cursor.execute(
            """
            UPDATE logo.bulk_batch_row
               SET after_row = %s
             WHERE batch_id = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s AND position = 1
            """,
            (json.dumps(after["j"]), batch_id, style, color, option_row),
        )
        applied += 1

    cursor.execute(
        "UPDATE logo.bulk_batch SET applied = %s WHERE batch_id = %s",
        (applied, batch_id),
    )
    return {"applied": applied, "batch_id": batch_id, "image_url_missing": image_url_missing}


def _restore_row(cursor, before_row: Mapping[str, Any]) -> None:
    # before_row comes back from the jsonb column as a Python dict via RealDictCursor;
    # re-serialize to jsonb for jsonb_populate_record.
    cursor.execute("""
        INSERT INTO logo.assignment
        SELECT * FROM jsonb_populate_record(NULL::logo.assignment, %s::jsonb)
        ON CONFLICT (fdm4_store,product_style,garment_color_code,option_row,position)
        DO UPDATE SET design_id=EXCLUDED.design_id, logo_code=EXCLUDED.logo_code,
            color_scheme_id=EXCLUDED.color_scheme_id, location=EXCLUDED.location,
            optional=EXCLUDED.optional, background=EXCLUDED.background,
            cost_override=EXCLUDED.cost_override, sort_order=EXCLUDED.sort_order,
            image_url=EXCLUDED.image_url, name_override=EXCLUDED.name_override,
            active=EXCLUDED.active,
            updated_by=EXCLUDED.updated_by, updated_at=EXCLUDED.updated_at
    """, (json.dumps(before_row),))


def bulk_apply_undo(cursor, *, batch_id: int, actor: str) -> dict:
    del actor
    cursor.execute(
        """
        SELECT fdm4_store, undone_at
          FROM logo.bulk_batch
         WHERE batch_id = %s
         FOR UPDATE
        """,
        (batch_id,),
    )
    b = cursor.fetchone()
    if not b:
        raise LookupError(f"unknown batch {batch_id}")
    if b["undone_at"]:
        raise ValueError("batch already undone")
    cursor.execute(
        """
        SELECT fdm4_store, product_style, garment_color_code, option_row,
               position, before_row, after_row
          FROM logo.bulk_batch_row
         WHERE batch_id = %s
         ORDER BY product_style, garment_color_code, option_row, position
         FOR UPDATE
        """,
        (batch_id,),
    )
    restored = 0
    skipped = 0
    for r in cursor.fetchall():
        cursor.execute(
            """
            SELECT to_jsonb(a) AS j
              FROM logo.assignment a
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND option_row = %s AND position = %s
             FOR UPDATE
            """,
            (
                r["fdm4_store"],
                r["product_style"],
                r["garment_color_code"],
                r["option_row"],
                r["position"],
            ),
        )
        current = cursor.fetchone()
        current_row = current["j"] if current is not None else None
        if r["after_row"] is None:
            # Journaled but never (re)written by the batch: either the batch
            # deleted it (replace mode) or skipped it. Restore only when the
            # row is absent now and its prior state was recorded.
            if current_row is None and r["before_row"] is not None:
                _restore_row(cursor, r["before_row"])
                restored += 1
            else:
                skipped += 1
            continue
        if current_row != r["after_row"]:
            skipped += 1
            continue
        if r["before_row"] is None:
            cursor.execute("""DELETE FROM logo.assignment
                               WHERE fdm4_store=%s AND product_style=%s AND garment_color_code=%s
                                 AND option_row=%s AND position=%s""",
                           (r["fdm4_store"], r["product_style"], r["garment_color_code"],
                            r["option_row"], r["position"]))
        else:
            _restore_row(cursor, r["before_row"])
        restored += 1
    cursor.execute("UPDATE logo.bulk_batch SET undone_at=now() WHERE batch_id=%s", (batch_id,))
    return {"restored": restored, "skipped": skipped, "batch_id": batch_id}


def set_color_class(cursor, *, color_code: str, light_dark: str, actor: str) -> dict:
    if light_dark not in ("light", "dark", "both"):
        raise ValueError("light_dark must be 'light', 'dark', or 'both'")
    cursor.execute(
        """
        UPDATE logo.color_class
           SET light_dark=%s, source='manual', confidence=NULL,
               updated_at=now(), updated_by=%s
         WHERE color_code=%s
        """,
        (light_dark, actor, color_code),
    )
    if cursor.rowcount == 0:
        raise LookupError(f"unknown color_code {color_code}")
    return {"color_code": color_code, "light_dark": light_dark}


# ---------------------------------------------------------------------------
# Editor batch operations (reorder / paste / copy-to-many / design swap).
# Standalone mutations, NOT commands: they follow the bulk-apply precedent -
# explicit lock scopes taken by the route, a logo.bulk_batch header whose
# target.kind names the operation, per-row before/after journal rows, and undo
# through bulk_apply_undo.
# ---------------------------------------------------------------------------


def _open_batch(cursor, *, fdm4_store: str, target: Mapping[str, Any], actor: str) -> int:
    """One logo.bulk_batch header for a non-bulk-apply operation (kind in target)."""
    cursor.execute(
        """
        INSERT INTO logo.bulk_batch
            (fdm4_store, logo_code, color_scheme, placement, target, applied, created_by)
        VALUES (%s, NULL, NULL, NULL, %s, 0, %s)
        RETURNING batch_id
        """,
        (fdm4_store, json.dumps(target), actor),
    )
    return cursor.fetchone()["batch_id"]


def _journal_before(cursor, batch_id: int, key: Mapping[str, Any]) -> Optional[dict]:
    cursor.execute(
        """
        SELECT to_jsonb(a) AS j FROM logo.assignment a
         WHERE fdm4_store = %s AND product_style = %s AND garment_color_code = %s
           AND option_row = %s AND position = %s
         FOR UPDATE
        """,
        (key["fdm4_store"], key["product_style"], key["garment_color_code"],
         key["option_row"], key["position"]),
    )
    prev = cursor.fetchone()
    before = prev["j"] if prev is not None else None
    cursor.execute(
        """
        INSERT INTO logo.bulk_batch_row
            (batch_id, fdm4_store, product_style, garment_color_code,
             option_row, position, before_row)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (batch_id, key["fdm4_store"], key["product_style"], key["garment_color_code"],
         key["option_row"], key["position"], json.dumps(before) if before is not None else None),
    )
    return before


def _journal_after(cursor, batch_id: int, key: Mapping[str, Any]) -> None:
    cursor.execute(
        """
        UPDATE logo.bulk_batch_row
           SET after_row = (SELECT to_jsonb(a) FROM logo.assignment a
                             WHERE a.fdm4_store = %s AND a.product_style = %s
                               AND a.garment_color_code = %s AND a.option_row = %s
                               AND a.position = %s)
         WHERE batch_id = %s AND product_style = %s AND garment_color_code = %s
           AND option_row = %s AND position = %s
        """,
        (key["fdm4_store"], key["product_style"], key["garment_color_code"],
         key["option_row"], key["position"],
         batch_id, key["product_style"], key["garment_color_code"],
         key["option_row"], key["position"]),
    )


def _close_batch(cursor, batch_id: int, applied: int) -> None:
    cursor.execute("UPDATE logo.bulk_batch SET applied = %s WHERE batch_id = %s",
                   (applied, batch_id))


def _row_identity(row) -> tuple:
    """What a logo row IS, independent of which color or row number carries
    it: the FDM4 design. Thread-color schemes and placements vary per garment
    color (white thread on dark shirts, a different chest on a pocketed
    style) but operators think of them as the same logo, so a style-wide
    reorder ranks by design only. Used when a channel is reordered
    style-wide."""
    return (str(row["design_id"]).strip(),)


def reorder_option_rows(cursor, *, fdm4_store: str, product_style: str,
                        garment_color_code: str, option_rows, apply_to: str,
                        actor: str) -> dict:
    """Renumber sort_order (10, 20, 30...) so option rows display and sell in
    the given order.

    apply_to='color' renumbers only the dragged channel, by row number.
    apply_to='style' also ranks EVERY other color of the style by logo
    identity (the FDM4 design of each row's position-1 logo):
    a logo that moved first on the dragged color moves first everywhere it
    appears; logos the dragged color does not carry keep their relative
    order after the ranked ones. This matters because the storefront
    collapses identical rows across colors and orders the collapsed group by
    the LOWEST sort_order among its colors - an untouched sibling color would
    otherwise silently win."""
    store = _clean(fdm4_store, "fdm4_store")
    style = _clean(product_style, "product_style")
    color = _clean(garment_color_code, "garment_color_code")
    if apply_to not in ("color", "style"):
        raise InvalidCommand("apply_to must be 'color' or 'style'")
    order = [int(value) for value in option_rows]
    if not order or len(set(order)) != len(order) or any(v < 1 or v > 999 for v in order):
        raise InvalidCommand("option_rows must be distinct row numbers between 1 and 999")
    cursor.execute(
        """
        SELECT garment_color_code, option_row, position, design_id,
               color_scheme_id, location, sort_order
          FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
         ORDER BY garment_color_code, option_row, position
         LIMIT %s
        """,
        (store, style, MAX_ASSIGNMENT_MUTATION_ROWS + 1),
    )
    all_rows = [dict(r) for r in cursor.fetchall()]
    if len(all_rows) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            f"Reorder would touch more than the {MAX_ASSIGNMENT_MUTATION_ROWS}-row mutation limit"
        )
    by_color: Dict[str, Dict[int, list]] = {}
    for row in all_rows:
        by_color.setdefault(str(row["garment_color_code"]), {}).setdefault(int(row["option_row"]), []).append(row)
    if color not in by_color:
        raise NotFound("No assignments on that color")
    if set(order) != set(by_color[color]):
        raise Conflict("Option rows changed after they were loaded; reload before reordering")

    def identity_of(rows: list) -> tuple:
        anchor = min(rows, key=lambda r: int(r["position"]))
        return _row_identity(anchor)

    identity_rank: Dict[tuple, int] = {}
    for index, row_number in enumerate(order):
        identity_rank.setdefault(identity_of(by_color[color][row_number]), index)

    # Per color: the ordered list of option rows -> new sort_order.
    plan: Dict[str, Dict[int, int]] = {}
    plan[color] = {row_number: (index + 1) * 10 for index, row_number in enumerate(order)}
    if apply_to == "style":
        # Other colors take the GLOBAL identity rank as their sort value (not a
        # per-color dense renumbering): the storefront sorts all colors of a
        # style together and orders each collapsed group by its lowest
        # sort_order, so a color lacking the first-ranked logo must start at
        # the second rank's value, never at 10. Logos the dragged color does
        # not carry rank after it, densely, in their existing relative order.
        for other_color, channels in by_color.items():
            if other_color == color:
                continue
            extra_rank: Dict[tuple, int] = {}
            for row_number, rows in sorted(
                channels.items(),
                key=lambda item: (min(int(r["sort_order"]) for r in item[1]), item[0]),
            ):
                identity = identity_of(rows)
                if identity not in identity_rank and identity not in extra_rank:
                    extra_rank[identity] = len(order) + len(extra_rank)
            plan[other_color] = {}
            for row_number, rows in channels.items():
                identity = identity_of(rows)
                rank = identity_rank.get(identity, extra_rank.get(identity))
                plan[other_color][row_number] = (rank + 1) * 10

    batch_id = _open_batch(cursor, fdm4_store=store, actor=actor, target={
        "kind": "reorder", "style": style, "color": color,
        "apply_to": apply_to, "order": order,
    })
    updated = 0
    colors_touched = []
    for target_color in sorted(plan):
        changed_here = False
        for row_number, rows in by_color[target_color].items():
            new_sort = plan[target_color][row_number]
            for row in rows:
                key = {"fdm4_store": store, "product_style": style,
                       "garment_color_code": target_color,
                       "option_row": row_number, "position": int(row["position"])}
                _journal_before(cursor, batch_id, key)
                if int(row["sort_order"]) != new_sort:
                    cursor.execute(
                        """
                        UPDATE logo.assignment
                           SET sort_order = %s, updated_by = %s, updated_at = now()
                         WHERE fdm4_store = %s AND product_style = %s
                           AND garment_color_code = %s AND option_row = %s AND position = %s
                        """,
                        (new_sort, actor, store, style, target_color,
                         row_number, int(row["position"])),
                    )
                    updated += 1
                    changed_here = True
                _journal_after(cursor, batch_id, key)
        if changed_here or target_color == color:
            colors_touched.append(target_color)
    _close_batch(cursor, batch_id, updated)
    return {"ok": True, "batch_id": batch_id, "updated": updated, "colors": colors_touched}


def _paste_values(row: Mapping[str, Any], *, store: str, style: str, color: str,
                  option_row: int) -> Dict[str, Any]:
    """Same cleaning as _assignment_values, from a plain mapping."""
    name_override = row.get("name_override")
    return {
        "fdm4_store": store,
        "product_style": style,
        "garment_color_code": color,
        "position": int(row["position"]),
        "option_row": int(option_row),
        "design_id": _clean(row["design_id"], "design_id"),
        "logo_code": _optional_text(row.get("logo_code") or "", "logo_code", 100).upper(),
        "color_scheme_id": _optional_text(row.get("color_scheme_id") or "", "color_scheme_id", 100).upper(),
        "location": _optional_text(row.get("location") or "", "location", 200),
        "optional": bool(row.get("optional", False)),
        "background": _optional_text(row.get("background") or "", "background", 200),
        "cost_override": _decimal(row.get("cost_override")),
        "sort_order": int(row.get("sort_order") or 0),
        "image_url": _image_url(row.get("image_url") or ""),
        "name_override": (
            None if name_override in (None, "")
            else _optional_text(name_override, "name_override", 200)
        ),
        "active": bool(row.get("active", True)),
    }


def _live_colors(cursor, store: str, catalog: str, style: str) -> set:
    cursor.execute(
        """
        SELECT DISTINCT color_code FROM woo.store_product_state
         WHERE fdm4_store = %s AND catalog_id = %s AND style_code = %s
           AND kind = 'variation' AND is_active = true
           AND NULLIF(btrim(color_code), '') IS NOT NULL
         LIMIT %s
        """,
        (store, catalog, style, MAX_STYLE_COLOR_ROWS + 1),
    )
    rows = list(cursor.fetchall())
    if len(rows) > MAX_STYLE_COLOR_ROWS:
        raise InvalidCommand(f"Style exceeds the {MAX_STYLE_COLOR_ROWS}-color mutation limit")
    return {str(r["color_code"]) for r in rows}


def paste_assignments(cursor, *, fdm4_store: str, product_style: str, colors,
                      rows, overwrite: bool, as_new_rows: bool, actor: str,
                      batch_id: Optional[int] = None) -> dict:
    """Apply clipboard rows to target colors of one style. Every row is
    validated exactly like a manual save (store/style/color liveness, design
    existence and ownership, scheme/logo validity, position-1 anchor);
    invalid rows are reported, never fatal. overwrite=False skips occupied
    slots; as_new_rows appends after the target's highest option row."""
    store = _clean(fdm4_store, "fdm4_store")
    style = _clean(product_style, "product_style")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise NotFound("Store not found")
    if not _style_exists(cursor, store, catalog, style):
        raise NotFound("Target style not found")
    rows = [dict(r) for r in rows]
    if not rows or not colors:
        raise InvalidCommand("Nothing to paste")
    slots = {(int(r["option_row"]), int(r["position"])) for r in rows}
    if len(slots) != len(rows):
        raise InvalidCommand("Clipboard rows repeat an option_row/position slot")
    if len(rows) * len(colors) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            f"Paste would exceed the {MAX_ASSIGNMENT_MUTATION_ROWS}-row mutation limit"
        )
    live = _live_colors(cursor, store, catalog, style)
    own_batch = batch_id is None
    if own_batch:
        batch_id = _open_batch(cursor, fdm4_store=store, actor=actor, target={
            "kind": "paste", "style": style, "colors": list(colors),
            "overwrite": overwrite, "as_new_rows": as_new_rows,
        })
    ordered = sorted(rows, key=lambda r: (int(r["option_row"]), int(r["position"])))
    counts = {"created": 0, "updated": 0, "skipped_occupied": 0,
              "skipped_missing_color": 0, "skipped_invalid": 0}
    problems = []
    for color in colors:
        color = _clean(color, "garment_color_code")
        if color not in live:
            counts["skipped_missing_color"] += 1
            continue
        remap = {}
        if as_new_rows:
            cursor.execute(
                """
                SELECT COALESCE(max(option_row), 0) AS top FROM logo.assignment
                 WHERE fdm4_store = %s AND product_style = %s AND garment_color_code = %s
                """,
                (store, style, color),
            )
            base = int(cursor.fetchone()["top"])
            for offset, source_row in enumerate(sorted({int(r["option_row"]) for r in rows})):
                remap[source_row] = base + offset + 1
        for row in ordered:
            option_row = remap.get(int(row["option_row"]), int(row["option_row"]))
            try:
                values = _paste_values(row, store=store, style=style, color=color,
                                       option_row=option_row)
            except InvalidCommand as exc:
                counts["skipped_invalid"] += 1
                problems.append({"color": color, "option_row": option_row,
                                 "position": row.get("position"), "reason": str(exc)})
                continue
            key = {k: values[k] for k in ("fdm4_store", "product_style",
                                          "garment_color_code", "option_row", "position")}
            before = _journal_before(cursor, batch_id, key)
            if before is not None and not overwrite:
                counts["skipped_occupied"] += 1
                continue
            try:
                _validate_warehouse_keys(cursor, values)
            except InvalidCommand as exc:
                counts["skipped_invalid"] += 1
                problems.append({**key, "reason": str(exc)})
                continue
            _upsert_assignment(cursor, values, actor, overwrite=True)
            _journal_after(cursor, batch_id, key)
            counts["updated" if before is not None else "created"] += 1
    if own_batch:
        _close_batch(cursor, batch_id, counts["created"] + counts["updated"])
    return {"ok": True, "batch_id": batch_id, **counts, "problems": problems[:50]}


def _scoped_colors(cursor, *, store: str, catalog: str, style: str,
                   color_scope: str, match_color: Optional[str]) -> list:
    live = _live_colors(cursor, store, catalog, style)
    if color_scope == "match":
        if not match_color:
            raise InvalidCommand("match_color is required for color_scope='match'")
        return [match_color] if match_color in live else []
    if color_scope == "all":
        return sorted(live)
    if color_scope in ("light", "dark"):
        cursor.execute(
            """
            SELECT color_code FROM logo.color_class
             WHERE color_code = ANY(%s) AND light_dark IN (%s, 'both')
            """,
            (sorted(live), color_scope),
        )
        return sorted(str(r["color_code"]) for r in cursor.fetchall())
    raise InvalidCommand("color_scope must be match, all, light or dark")


def paste_batch(cursor, *, fdm4_store: str, styles, color_scope: str,
                match_color: Optional[str], rows, overwrite: bool,
                as_new_rows: bool, actor: str) -> dict:
    """Paste the same clipboard onto many styles of one store under a single
    undoable batch. Per-style failures (unknown style, over-limit) are
    reported in results[], not fatal."""
    store = _clean(fdm4_store, "fdm4_store")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise NotFound("Store not found")
    if not styles or not rows:
        raise InvalidCommand("Nothing to paste")
    batch_id = _open_batch(cursor, fdm4_store=store, actor=actor, target={
        "kind": "paste_batch", "styles": list(styles), "color_scope": color_scope,
        "match_color": match_color, "overwrite": overwrite, "as_new_rows": as_new_rows,
    })
    results = []
    totals = {"created": 0, "updated": 0, "skipped_occupied": 0,
              "skipped_missing_color": 0, "skipped_invalid": 0}
    budget = MAX_ASSIGNMENT_MUTATION_ROWS
    for raw_style in styles:
        style = _clean(raw_style, "product_style")
        entry = {"style": style}
        try:
            if not _style_exists(cursor, store, catalog, style):
                raise NotFound("Target style not found")
            colors = _scoped_colors(cursor, store=store, catalog=catalog, style=style,
                                    color_scope=color_scope, match_color=match_color)
            entry["colors"] = colors
            if not colors:
                entry.update({k: 0 for k in totals}); entry["skipped_missing_color"] = 1
                results.append(entry); totals["skipped_missing_color"] += 1
                continue
            cost = len(colors) * len(rows)
            if cost > budget:
                raise InvalidCommand("Batch exceeds the mutation limit; select fewer styles")
            budget -= cost
            outcome = paste_assignments(
                cursor, fdm4_store=store, product_style=style, colors=colors,
                rows=rows, overwrite=overwrite, as_new_rows=as_new_rows,
                actor=actor, batch_id=batch_id,
            )
            for key in totals:
                entry[key] = outcome[key]; totals[key] += outcome[key]
            entry["problems"] = outcome["problems"]
        except (NotFound, InvalidCommand) as exc:
            entry["error"] = str(exc)
        results.append(entry)
    _close_batch(cursor, batch_id, totals["created"] + totals["updated"])
    return {"ok": True, "batch_id": batch_id, "results": results, "totals": totals}


def fill_gaps(cursor, *, fdm4_store: str, entries, overwrite: bool, actor: str) -> dict:
    """Copy each style's OWN configured logos (from entry.source_color) onto
    its logo-less colors. One shared journal batch for the whole run, so a
    single /api/bulk-apply/undo reverts everything. All-or-nothing: any
    invalid entry aborts the transaction before anything is committed."""
    store = _clean(fdm4_store, "fdm4_store")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise NotFound("Store not found")
    if not entries:
        raise InvalidCommand("Nothing to fill")
    batch_id = _open_batch(cursor, fdm4_store=store, actor=actor, target={
        "kind": "fill_gaps",
        "styles": [str(entry.get("style", "")) for entry in entries],
        "overwrite": overwrite,
    })
    results = []
    totals = {"created": 0, "updated": 0}
    for entry in entries:
        style = _clean(entry["style"], "product_style")
        source_color = _clean(entry["source_color"], "source_color")
        if not _style_exists(cursor, store, catalog, style):
            raise NotFound(f"{style}: style not found")
        cursor.execute(
            """
            SELECT option_row, position, design_id, logo_code, color_scheme_id,
                   location, optional, background, cost_override, sort_order,
                   image_url, name_override, active
              FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s
               AND garment_color_code = %s AND active
             ORDER BY option_row, position
            """,
            (store, style, source_color),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        if not rows:
            raise InvalidCommand(
                f"{style}: source color {source_color} has no active logos"
            )
        explicit = entry.get("colors") or None
        if explicit:
            colors = [_clean(c, "garment_color_code") for c in explicit]
        else:
            live = _live_colors(cursor, store, catalog, style)
            cursor.execute(
                """
                SELECT DISTINCT garment_color_code FROM logo.assignment
                 WHERE fdm4_store = %s AND product_style = %s AND active
                """,
                (store, style),
            )
            configured = {str(row["garment_color_code"]) for row in cursor.fetchall()}
            colors = sorted(c for c in live if c not in configured)
        if source_color in colors:
            raise InvalidCommand(f"{style}: source color {source_color} is also a target")
        result = {"style": style, "source_color": source_color, "colors": colors,
                  "created": 0, "updated": 0, "skipped_occupied": 0,
                  "skipped_missing_color": 0, "skipped_invalid": 0, "problems": []}
        if colors:
            outcome = paste_assignments(
                cursor, fdm4_store=store, product_style=style, colors=colors,
                rows=rows, overwrite=overwrite, as_new_rows=False,
                actor=actor, batch_id=batch_id,
            )
            for key in ("created", "updated", "skipped_occupied",
                        "skipped_missing_color", "skipped_invalid", "problems"):
                result[key] = outcome[key]
            totals["created"] += outcome["created"]
            totals["updated"] += outcome["updated"]
        results.append(result)
    _close_batch(cursor, batch_id, totals["created"] + totals["updated"])
    return {"ok": True, "batch_id": batch_id, **totals, "results": results}


def _color_classes(cursor, codes) -> Dict[str, str]:
    if not codes:
        return {}
    cursor.execute(
        "SELECT color_code, light_dark FROM logo.color_class WHERE color_code = ANY(%s)",
        (list(codes),),
    )
    return {str(r["color_code"]): str(r["light_dark"]) for r in cursor.fetchall()}


def plan_copy_style_batch(cursor, *, fdm4_store: str, source_style: str,
                          target_styles, color_match: str):
    """Read-only plan: which source color channel feeds each target color.
    'exact' copies only colors the target shares with the source; 'like'
    also maps the target's remaining colors to a source color of the same
    light/dark class (logo.color_class), choosing the source color of that
    class with the most rows (ties: alphabetical). Returns (plan, channels)
    where channels = {source_color: [rows...]} for the executor."""
    store = _clean(fdm4_store, "fdm4_store")
    source = _clean(source_style, "product_style")
    if color_match not in ("exact", "like"):
        raise InvalidCommand("color_match must be 'exact' or 'like'")
    catalog = _catalog_for_store(cursor, store)
    if catalog is None:
        raise NotFound("Store not found")
    cursor.execute(
        """
        SELECT garment_color_code, option_row, position, design_id, logo_code,
               color_scheme_id, location, optional, background, cost_override,
               sort_order, image_url, name_override, active
          FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s
         ORDER BY garment_color_code, option_row, position
         LIMIT %s
        """,
        (store, source, MAX_ASSIGNMENT_MUTATION_ROWS + 1),
    )
    source_rows = [dict(r) for r in cursor.fetchall()]
    if len(source_rows) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            f"Source style exceeds the {MAX_ASSIGNMENT_MUTATION_ROWS}-row mutation limit"
        )
    if not source_rows:
        raise NotFound("Source style has no assignments")
    channels: Dict[str, list] = {}
    for row in source_rows:
        channels.setdefault(str(row.pop("garment_color_code")), []).append(row)
    classes = _color_classes(cursor, list(channels))
    templates: Dict[str, list] = {}
    for color in sorted(channels, key=lambda c: (-len(channels[c]), c)):
        if classes.get(color):
            templates.setdefault(classes[color], []).append(color)
    targets = []
    total_rows = 0
    for raw in target_styles:
        target = _clean(raw, "product_style")
        entry: Dict[str, Any] = {"style": target, "mappings": [], "unmatched": [], "rows": 0}
        try:
            if target == source:
                raise InvalidCommand("Source and target styles must differ")
            if not _style_exists(cursor, store, catalog, target):
                raise NotFound("Target style not found")
            live = sorted(_live_colors(cursor, store, catalog, target))
            target_classes = _color_classes(cursor, live) if color_match == "like" else {}
            for color in live:
                if color in channels:
                    entry["mappings"].append({"target_color": color, "source_color": color,
                                              "via": "exact", "rows": len(channels[color])})
                    continue
                cls = target_classes.get(color)
                template = templates.get(cls, [None])[0] if cls else None
                if template is None:
                    entry["unmatched"].append(color)
                else:
                    entry["mappings"].append({"target_color": color, "source_color": template,
                                              "via": cls, "rows": len(channels[template])})
            entry["rows"] = sum(m["rows"] for m in entry["mappings"])
            entry["existing"] = _bounded_assignment_count(
                cursor, "fdm4_store = %s AND product_style = %s", (store, target),
                label="Target style",
            )
            total_rows += entry["rows"]
        except (NotFound, InvalidCommand) as exc:
            entry["error"] = str(exc)
        targets.append(entry)
    plan = {
        "store": store, "source_style": source, "color_match": color_match,
        "source_colors": {c: len(rows) for c, rows in channels.items()},
        "source_classes": classes, "targets": targets, "total_rows": total_rows,
    }
    return plan, channels


def _clear_channel(cursor, batch_id: int, *, store: str, style: str, color: str) -> int:
    """Journal + hard-delete every row on one target color (replace mode).
    Undo re-inserts them through bulk_apply_undo (Task C6)."""
    cursor.execute(
        """
        SELECT option_row, position FROM logo.assignment
         WHERE fdm4_store = %s AND product_style = %s AND garment_color_code = %s
         ORDER BY option_row, position
        """,
        (store, style, color),
    )
    keys = [{"fdm4_store": store, "product_style": style, "garment_color_code": color,
             "option_row": r["option_row"], "position": r["position"]} for r in cursor.fetchall()]
    for key in keys:
        _journal_before(cursor, batch_id, key)
    if keys:
        cursor.execute(
            """
            DELETE FROM logo.assignment
             WHERE fdm4_store = %s AND product_style = %s AND garment_color_code = %s
            """,
            (store, style, color),
        )
    return len(keys)


def copy_style_batch(cursor, *, fdm4_store: str, source_style: str, target_styles,
                     color_match: str, mode: str, actor: str) -> dict:
    """Fan one style's logo configuration out to many styles of the same
    store, one undoable batch. Per-target errors are reported, never fatal."""
    if mode not in ("merge", "overwrite", "replace"):
        raise InvalidCommand("mode must be merge, overwrite or replace")
    plan, channels = plan_copy_style_batch(
        cursor, fdm4_store=fdm4_store, source_style=source_style,
        target_styles=target_styles, color_match=color_match,
    )
    if plan["total_rows"] > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand("Copy would exceed the mutation limit; select fewer styles")
    store, source = plan["store"], plan["source_style"]
    batch_id = _open_batch(cursor, fdm4_store=store, actor=actor, target={
        "kind": "copy_style_batch", "source_style": source,
        "styles": [t["style"] for t in plan["targets"]],
        "color_match": color_match, "mode": mode,
    })
    totals = {"created": 0, "updated": 0, "removed": 0, "skipped_occupied": 0, "skipped_invalid": 0}
    results = []
    for entry in plan["targets"]:
        result: Dict[str, Any] = {"style": entry["style"], "unmatched": entry.get("unmatched", [])}
        if "error" in entry:
            result["error"] = entry["error"]
            results.append(result)
            continue
        counts = {key: 0 for key in totals}
        problems = []
        for mapping in entry["mappings"]:
            color = mapping["target_color"]
            if mode == "replace":
                counts["removed"] += _clear_channel(cursor, batch_id, store=store,
                                                    style=entry["style"], color=color)
            outcome = paste_assignments(
                cursor, fdm4_store=store, product_style=entry["style"], colors=[color],
                rows=channels[mapping["source_color"]], overwrite=(mode != "merge"),
                as_new_rows=False, actor=actor, batch_id=batch_id,
            )
            for key in ("created", "updated", "skipped_occupied", "skipped_invalid"):
                counts[key] += outcome[key]
            problems.extend(outcome["problems"])
        result.update(counts)
        result["problems"] = problems[:50]
        results.append(result)
        for key in totals:
            totals[key] += counts[key]
    _close_batch(cursor, batch_id, totals["created"] + totals["updated"] + totals["removed"])
    return {"ok": True, "batch_id": batch_id, "results": results, "totals": totals}


def _swap_where(store: str, from_design: str, from_scheme: Optional[str], styles) -> tuple:
    clauses = ["fdm4_store = %s", "btrim(design_id) = %s"]
    params: list = [store, from_design]
    if from_scheme is not None:
        clauses.append("upper(btrim(color_scheme_id)) = %s")
        params.append(from_scheme)
    if styles:
        clauses.append("product_style = ANY(%s)")
        params.append(list(styles))
    return " AND ".join(clauses), tuple(params)


def _swap_source(fdm4_store, from_design_id, from_color_scheme_id, styles) -> tuple:
    """Clean the 'what to replace' half. The old design is NOT required to
    exist in fdm4.dec_design any more - a re-issue often retires it."""
    store = _clean(fdm4_store, "fdm4_store")
    from_design = _clean(from_design_id, "from_design_id")
    from_scheme = (
        None if from_color_scheme_id in (None, "")
        else _clean(from_color_scheme_id, "from_color_scheme_id").upper()
    )
    style_filter = [_clean(s, "styles") for s in (styles or [])]
    return store, from_design, from_scheme, style_filter


def _logo_codes_on_file(cursor, design_id: str, scheme: str) -> list:
    """Distinct FDM4 art-filename prefixes (= logo codes) for one design and
    color scheme, following the same design_pool-first / legacy-fallback /
    collision-guard path as design_resolver.validate_design_asset."""
    cursor.execute(
        """
        WITH mapped AS (
            SELECT caf.target_filename
              FROM fdm4.design_pool dp
              JOIN fdm4.cust_art_file caf ON btrim(caf.art_id) = btrim(dp.art_id)
             WHERE btrim(dp.design_id) = %s
               AND upper(btrim(caf.color_scheme_id)) = upper(%s)
        ), candidates AS (
            SELECT target_filename FROM mapped
            UNION ALL
            SELECT caf.target_filename
              FROM fdm4.cust_art_file caf
             WHERE btrim(caf.art_id) = %s
               AND upper(btrim(caf.color_scheme_id)) = upper(%s)
               AND NOT EXISTS (SELECT 1 FROM mapped)
               AND NOT EXISTS (
                   SELECT 1 FROM fdm4.design_pool collision
                    WHERE btrim(collision.art_id) = %s
                      AND NULLIF(btrim(collision.art_id), '') IS NOT NULL
               )
        )
        SELECT DISTINCT upper(regexp_replace(
                   regexp_replace(target_filename, '^.*/', ''),
                   '[^A-Za-z0-9].*$', ''
               )) AS logo_code
          FROM candidates
         WHERE NULLIF(btrim(target_filename), '') IS NOT NULL
         ORDER BY 1
        """,
        (design_id, scheme, design_id, scheme, design_id),
    )
    return [str(r["logo_code"]) for r in cursor.fetchall() if r["logo_code"]]


def design_swap_styles(cursor, *, fdm4_store, from_design_id, from_color_scheme_id=None,
                       styles=None) -> list:
    """Styles a swap would touch. The route locks these BEFORE planning; the
    plan then re-reads under the locks and refuses to proceed if a style
    appeared in between (Conflict)."""
    store, from_design, from_scheme, style_filter = _swap_source(
        fdm4_store, from_design_id, from_color_scheme_id, styles)
    where, params = _swap_where(store, from_design, from_scheme, style_filter)
    cursor.execute(
        f"""
        SELECT DISTINCT product_style FROM logo.assignment
         WHERE {where}
         ORDER BY product_style
         LIMIT %s
        """,
        params + (MAX_ASSIGNMENT_MUTATION_ROWS + 1,),
    )
    return [str(r["product_style"]) for r in cursor.fetchall()]


def plan_design_swap(cursor, *, fdm4_store, from_design_id, from_color_scheme_id,
                     to_design_id, to_color_scheme_id, to_logo_code=None,
                     styles=None) -> dict:
    """Read-only plan for moving every assignment on (from_design[, from_scheme])
    to (to_design, to_scheme, logo_code) in one store.

    Target gates (422, whole request): the new design must exist and be
    available to the store's FDM4 customer family (design_available_to_store -
    the same check a manual save applies, hoisted because the target is one
    value for every row); the logo code is derived from the design's art
    files for that scheme when exactly one code is on file, otherwise
    to_logo_code is required.

    Per row: the would-be row (same key, new design/code/scheme, new image
    value, current active flag) goes through _validate_warehouse_keys exactly
    like PUT /api/assignments; verdict ok | unchanged | invalid (+reason).

    image_url: a row's image is the storefront picture of the OLD art (the WP
    reconcile uses it verbatim, else falls back to FDM4 preview/thumb art,
    else drops the row). It is replaced by the store's newest active image
    for the NEW design/scheme when one exists, otherwise cleared - and
    validation runs against that new value, so a cleared row without FDM4
    art is reported invalid rather than vanishing from the storefront.
    """
    store, from_design, from_scheme, style_filter = _swap_source(
        fdm4_store, from_design_id, from_color_scheme_id, styles)
    to_design = _clean(to_design_id, "to_design_id")
    to_scheme = _clean(to_color_scheme_id, "to_color_scheme_id").upper()
    if _catalog_for_store(cursor, store) is None:
        raise NotFound("Store not found")
    if from_design == to_design and from_scheme == to_scheme:
        raise InvalidCommand("from and to are the same design and color scheme")
    cursor.execute(
        "SELECT 1 FROM fdm4.dec_design WHERE btrim(design_id) = %s LIMIT 1",
        (to_design,),
    )
    if cursor.fetchone() is None:
        raise InvalidCommand(f"unknown design_id {to_design}")
    if not design_available_to_store(cursor, store, to_design):
        raise InvalidCommand(
            f"design {to_design} belongs to a different FDM4 customer"
            " account and is not available to this store"
        )
    codes = _logo_codes_on_file(cursor, to_design, to_scheme)
    if to_logo_code not in (None, ""):
        logo_code = _clean(to_logo_code, "to_logo_code").upper()
        derived = False
    elif len(codes) == 1:
        logo_code, derived = codes[0], True
    elif not codes:
        raise InvalidCommand(
            f"to_logo_code is required: design {to_design} has no FDM4 art file"
            f" for color scheme {to_scheme} to derive it from"
        )
    else:
        raise InvalidCommand(
            f"to_logo_code is required: design {to_design} / scheme {to_scheme}"
            f" has {len(codes)} logo codes on file ({', '.join(codes)})"
        )
    target = {"design_id": to_design, "color_scheme_id": to_scheme,
              "logo_code": logo_code, "logo_code_derived": derived}
    cursor.execute(
        """
        SELECT image_url FROM logo.assignment
         WHERE fdm4_store = %s AND btrim(design_id) = %s
           AND upper(btrim(color_scheme_id)) = %s
           AND active AND NULLIF(btrim(image_url), '') IS NOT NULL
         ORDER BY updated_at DESC
         LIMIT 1
        """,
        (store, to_design, to_scheme),
    )
    sibling = cursor.fetchone()
    replacement = str(sibling["image_url"]) if sibling else ""
    where, params = _swap_where(store, from_design, from_scheme, style_filter)
    cursor.execute(
        f"""
        SELECT product_style, garment_color_code, option_row, position,
               design_id, logo_code, color_scheme_id, image_url, active
          FROM logo.assignment
         WHERE {where}
         ORDER BY product_style, garment_color_code, option_row, position
         LIMIT %s
        """,
        params + (MAX_ASSIGNMENT_MUTATION_ROWS + 1,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    if len(rows) > MAX_ASSIGNMENT_MUTATION_ROWS:
        raise InvalidCommand(
            f"Design swap exceeds the {MAX_ASSIGNMENT_MUTATION_ROWS}-row"
            " mutation limit; narrow it with styles"
        )
    counts = {"total": len(rows), "ok": 0, "unchanged": 0, "invalid": 0,
              "styles": 0, "image_url_replaced": 0, "image_url_cleared": 0}
    groups: list = []
    by_group: dict = {}
    for row in rows:
        style = str(row["product_style"])
        color = str(row["garment_color_code"])
        was = {
            "design_id": str(row["design_id"]).strip(),
            "logo_code": str(row["logo_code"] or "").strip().upper(),
            "color_scheme_id": str(row["color_scheme_id"] or "").strip().upper(),
        }
        current_image = str(row["image_url"] or "").strip()
        if not current_image:
            image_action, new_image = "none", ""
        elif replacement:
            image_action, new_image = "replaced", replacement
        else:
            image_action, new_image = "cleared", ""
        entry = {"option_row": int(row["option_row"]), "position": int(row["position"]),
                 "active": bool(row["active"]), "was": was,
                 "image_action": image_action, "verdict": "ok", "reason": None}
        if (was["design_id"], was["logo_code"], was["color_scheme_id"]) == (
                to_design, logo_code, to_scheme):
            entry["verdict"] = "unchanged"
        else:
            try:
                _validate_warehouse_keys(cursor, {
                    "fdm4_store": store, "product_style": style,
                    "garment_color_code": color,
                    "option_row": entry["option_row"], "position": entry["position"],
                    "design_id": to_design, "logo_code": logo_code,
                    "color_scheme_id": to_scheme, "image_url": new_image,
                    "active": entry["active"],
                })
            except InvalidCommand as exc:
                entry["verdict"], entry["reason"] = "invalid", str(exc)
        counts[entry["verdict"]] += 1
        if entry["verdict"] == "ok" and image_action != "none":
            counts[f"image_url_{image_action}"] += 1
        group = by_group.get((style, color))
        if group is None:
            group = {"style": style, "color": color, "rows": []}
            by_group[(style, color)] = group
            groups.append(group)
        group["rows"].append(entry)
    styles_touched = sorted({g["style"] for g in groups})
    counts["styles"] = len(styles_touched)
    return {
        "ok": True, "store": store,
        "source": {"design_id": from_design, "color_scheme_id": from_scheme},
        "target": target, "image_url_replacement": replacement,
        "styles": styles_touched, "groups": groups, "counts": counts,
    }


def design_swap(cursor, *, fdm4_store, from_design_id, from_color_scheme_id,
                to_design_id, to_color_scheme_id, to_logo_code=None, styles=None,
                actor, locked_styles=None) -> dict:
    """Execute plan_design_swap under the caller's locks: one logo.bulk_batch
    (target.kind = 'design_swap'), per-row before/after journal, UPDATE of
    design_id / logo_code / color_scheme_id / image_url only (placement,
    price, sort_order, name_override, active untouched). Invalid rows are
    skipped and reported; undo through bulk_apply_undo."""
    plan = plan_design_swap(
        cursor, fdm4_store=fdm4_store, from_design_id=from_design_id,
        from_color_scheme_id=from_color_scheme_id, to_design_id=to_design_id,
        to_color_scheme_id=to_color_scheme_id, to_logo_code=to_logo_code, styles=styles,
    )
    if locked_styles is not None and not set(plan["styles"]) <= set(locked_styles):
        raise Conflict("Assignments changed while the swap was being planned; retry")
    source = plan["source"]
    if plan["counts"]["total"] == 0:
        raise NotFound(
            f"No assignments in {plan['store']} use design {source['design_id']}"
            + (f" / scheme {source['color_scheme_id']}" if source["color_scheme_id"] else "")
        )
    if plan["counts"]["ok"] == 0:
        raise InvalidCommand("Nothing to swap: every matching row is unchanged or invalid")
    target = plan["target"]
    batch_id = _open_batch(cursor, fdm4_store=plan["store"], actor=actor, target={
        "kind": "design_swap", "from": source, "to": target,
        "styles": plan["styles"], "rows": plan["counts"]["total"],
    })
    applied = 0
    problems = []
    for group in plan["groups"]:
        for row in group["rows"]:
            key = {"fdm4_store": plan["store"], "product_style": group["style"],
                   "garment_color_code": group["color"],
                   "option_row": row["option_row"], "position": row["position"]}
            if row["verdict"] == "invalid":
                problems.append({**key, "reason": row["reason"]})
            if row["verdict"] != "ok":
                continue
            if _journal_before(cursor, batch_id, key) is None:
                continue
            new_image = plan["image_url_replacement"] if row["image_action"] == "replaced" else ""
            cursor.execute(
                """
                UPDATE logo.assignment
                   SET design_id = %s, logo_code = %s, color_scheme_id = %s,
                       image_url = %s, updated_by = %s, updated_at = now()
                 WHERE fdm4_store = %s AND product_style = %s
                   AND garment_color_code = %s AND option_row = %s AND position = %s
                """,
                (target["design_id"], target["logo_code"], target["color_scheme_id"],
                 new_image, actor, key["fdm4_store"], key["product_style"],
                 key["garment_color_code"], key["option_row"], key["position"]),
            )
            _journal_after(cursor, batch_id, key)
            applied += 1
    _close_batch(cursor, batch_id, applied)
    return {
        "ok": True, "batch_id": batch_id, "applied": applied,
        "unchanged": plan["counts"]["unchanged"],
        "skipped_invalid": plan["counts"]["invalid"],
        "styles": plan["styles"], "target": target, "counts": plan["counts"],
        "problems": problems[:50],
    }
