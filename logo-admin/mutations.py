"""Transaction-only mutation kernel shared by HTTP and the in-app agent.

Every public function in this module accepts a caller-owned PostgreSQL cursor.
It never opens or commits a transaction and never performs non-database I/O.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Callable, Dict, Literal, Mapping, Optional
from urllib.parse import urlsplit

from commands import (
    COMMAND_MODELS,
    ApplyToColorsCommand,
    AssignmentTarget,
    ColorTarget,
    CopyStyleCommand,
    DeactivateAssignmentCommand,
    DeactivateColorCommand,
    DeleteStorePricingTierCommand,
    HardDeleteAssignmentCommand,
    HardDeleteColorCommand,
    MutationCommand,
    SaveAssignmentCommand,
    SetStorePricingTierCommand,
    SetStyleActiveCommand,
    UpdateStoreSettingsCommand,
)
from design_resolver import load_design_index, validate_design_asset
from domain import Conflict, InvalidCommand, NotFound


ScopeKind = Literal[
    "assignment_option_row",
    "assignment_color",
    "assignment_style",
    "store_settings_row",
    "store_pricing_tier_row",
]

# Exact preview/undo materializes every affected row. These hard service caps
# keep direct HTTP/MCP mutations and agent staging within a reviewable,
# journalable boundary even if warehouse data is unexpectedly dirty.
MAX_ASSIGNMENT_MUTATION_ROWS = 2_000
MAX_STYLE_COLOR_ROWS = 500


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


MUTATION_HANDLERS: Dict[str, Callable] = {
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
}


def dispatch_mutation(
    cursor,
    actor: str,
    command: MutationCommand,
) -> MutationResult:
    for name, model in COMMAND_MODELS.items():
        if isinstance(command, model):
            return MUTATION_HANDLERS[name](cursor, actor, command)
    raise InvalidCommand("unsupported mutation command")


def bulk_apply_execute(cursor, *, fdm4_store, logo_code, color_scheme, placement, rows, actor,
                       option_row=1, cost_override=None, image_url=None) -> dict:
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
    designs = design_lookup.candidates(fdm4_store, logo_code, scheme)
    if not designs or len(designs) > 1:
        raise ValueError(
            f"variant {logo_code}/{scheme} did not resolve to a single design"
            + (f" (ambiguous: {', '.join(sorted(designs))})" if designs else "")
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
        if r["after_row"] is None or current_row != r["after_row"]:
            skipped += 1
            continue
        if r["before_row"] is None:
            cursor.execute("""DELETE FROM logo.assignment
                               WHERE fdm4_store=%s AND product_style=%s AND garment_color_code=%s
                                 AND option_row=%s AND position=%s""",
                           (r["fdm4_store"], r["product_style"], r["garment_color_code"],
                            r["option_row"], r["position"]))
        else:
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
            """, (json.dumps(r["before_row"]),))
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
