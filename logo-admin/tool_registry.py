"""Capability registry for the in-app agent's bounded tool surface."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Literal, Mapping, Type

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from authorization import (
    AccessContext,
    assert_agent_callable,
    required_tier,
)
from commands import (
    COMMAND_MODELS,
    ApplyToColorsCommand,
    CopyStyleCommand,
    DeactivateAssignmentCommand,
    DeactivateColorCommand,
    DeleteStorePricingTierCommand,
    HardDeleteAssignmentCommand,
    HardDeleteColorCommand,
    SaveAssignmentCommand,
    SetStorePricingTierCommand,
    SetStyleActiveCommand,
    UpdateStoreSettingsCommand,
)
from config import Settings
from db import database
import queries
import mutations
import snapshots
from read_commands import (
    GetAssignmentVocabCommand,
    GetAuditLogCommand,
    GetDesignCommand,
    GetImportReportCommand,
    GetStoreSettingsCommand,
    GetStyleCommand,
    ListPricingTiersCommand,
    ListStorePricingTiersCommand,
    ListStoresCommand,
    ListStylesCommand,
    SearchDesignsCommand,
)
import staging


ToolKind = Literal["read", "write"]
ToolHandler = Callable[..., object]


class ToolRegistryError(RuntimeError):
    pass


class UnknownTool(ToolRegistryError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    command_model: Type[BaseModel]
    kind: ToolKind
    handler: ToolHandler | None
    agent_enabled: bool
    mcp_enabled: bool
    bounded: bool
    transactional: bool
    undoable: bool


@dataclass(frozen=True)
class AgentWriteContract:
    """Immutable trust anchor for one approved model-facing write."""

    command_model: Type[BaseModel]
    handler: ToolHandler
    scope_kinds: frozenset[str]
    snapshot_scope_kinds: frozenset[str]
    restore_scope_kinds: frozenset[str]


APPROVED_AGENT_WRITE_NAMES = frozenset({
    "save_assignment",
    "deactivate_assignment",
    "hard_delete_assignment",
    "deactivate_color",
    "hard_delete_color",
    "set_style_active",
    "apply_to_colors",
    "copy_style",
    "update_store_settings",
    "set_store_pricing_tier",
    "delete_store_pricing_tier",
})

APPROVED_AGENT_READ_NAMES = frozenset({
    "list_stores",
    "list_styles",
    "get_style",
    "search_designs",
    "get_design",
    "get_assignment_vocab",
    "get_store_settings",
    "get_import_report",
    "get_audit_log",
    "list_pricing_tiers",
    "list_store_pricing_tiers",
})

EXCLUDED_AGENT_TOOLS = frozenset({
    "export_assignments_csv",
    "export_audit_log_csv",
    "get_product_link",
    "sync_to_wordpress",
    "upload_image",
    "import_assignments_csv",
    "import_legacy_ndjson",
    "mirror_legacy_images",
})


def _model_arguments(command: BaseModel) -> dict:
    return command.model_dump(mode="python")


def _list_stores(cursor, command, settings):
    del command, settings
    return queries.list_stores(cursor)


def _list_styles(cursor, command, settings):
    del settings
    return queries.list_styles(cursor, **_model_arguments(command))


def _get_style(cursor, command, settings):
    del settings
    return queries.get_style(cursor, **_model_arguments(command))


def _search_designs(cursor, command, settings):
    del settings
    return queries.search_designs(cursor, **_model_arguments(command))


def _get_design(cursor, command, settings):
    return queries.get_design(
        cursor,
        **_model_arguments(command),
        fdm4_art_base=settings.fdm4_art_base,
    )


def _get_assignment_vocab(cursor, command, settings):
    del command, settings
    return queries.get_assignment_vocab(cursor)


def _get_store_settings(cursor, command, settings):
    del settings
    return queries.get_store_settings(cursor, **_model_arguments(command))


def _get_import_report(cursor, command, settings):
    del settings
    return queries.get_import_report(cursor, **_model_arguments(command))


def _get_audit_log(cursor, command, settings):
    del settings
    return queries.get_audit_log(cursor, **_model_arguments(command))


def _list_pricing_tiers(cursor, command, settings):
    del command, settings
    return queries.list_pricing_tiers(cursor)


def _list_store_pricing_tiers(cursor, command, settings):
    del command, settings
    return queries.list_store_pricing_tiers(cursor)


CANONICAL_AGENT_READ_CONTRACTS = {
    "list_stores": (ListStoresCommand, _list_stores),
    "list_styles": (ListStylesCommand, _list_styles),
    "get_style": (GetStyleCommand, _get_style),
    "search_designs": (SearchDesignsCommand, _search_designs),
    "get_design": (GetDesignCommand, _get_design),
    "get_assignment_vocab": (
        GetAssignmentVocabCommand,
        _get_assignment_vocab,
    ),
    "get_store_settings": (GetStoreSettingsCommand, _get_store_settings),
    "get_import_report": (GetImportReportCommand, _get_import_report),
    "get_audit_log": (GetAuditLogCommand, _get_audit_log),
    "list_pricing_tiers": (ListPricingTiersCommand, _list_pricing_tiers),
    "list_store_pricing_tiers": (
        ListStorePricingTiersCommand,
        _list_store_pricing_tiers,
    ),
}


def _canonical_write_contract(
    model: Type[BaseModel],
    handler: ToolHandler,
    *scope_kinds: str,
) -> AgentWriteContract:
    """Build a frozen contract without consulting any mutable registry."""

    exact_scopes = frozenset(scope_kinds)
    return AgentWriteContract(
        command_model=model,
        handler=handler,
        scope_kinds=exact_scopes,
        snapshot_scope_kinds=exact_scopes,
        restore_scope_kinds=exact_scopes,
    )


# This mapping is intentionally independent of COMMAND_MODELS,
# MUTATION_HANDLERS, and COMMAND_SCOPE_KINDS. Those runtime registries are
# checked against this immutable trust anchor, so changing them in concert
# cannot silently widen or rewire the model-facing write surface.
CANONICAL_AGENT_WRITE_CONTRACTS: Mapping[str, AgentWriteContract] = (
    MappingProxyType({
        "save_assignment": _canonical_write_contract(
            SaveAssignmentCommand,
            mutations.save_assignment,
            "assignment_option_row",
        ),
        "deactivate_assignment": _canonical_write_contract(
            DeactivateAssignmentCommand,
            mutations.deactivate_assignment,
            "assignment_option_row",
        ),
        "hard_delete_assignment": _canonical_write_contract(
            HardDeleteAssignmentCommand,
            mutations.hard_delete_assignment,
            "assignment_option_row",
        ),
        "deactivate_color": _canonical_write_contract(
            DeactivateColorCommand,
            mutations.deactivate_color,
            "assignment_color",
        ),
        "hard_delete_color": _canonical_write_contract(
            HardDeleteColorCommand,
            mutations.hard_delete_color,
            "assignment_color",
        ),
        "set_style_active": _canonical_write_contract(
            SetStyleActiveCommand,
            mutations.set_style_active,
            "assignment_style",
        ),
        "apply_to_colors": _canonical_write_contract(
            ApplyToColorsCommand,
            mutations.apply_to_colors,
            "assignment_style",
        ),
        "copy_style": _canonical_write_contract(
            CopyStyleCommand,
            mutations.copy_style,
            "assignment_style",
        ),
        "update_store_settings": _canonical_write_contract(
            UpdateStoreSettingsCommand,
            mutations.update_store_settings,
            "store_settings_row",
        ),
        "set_store_pricing_tier": _canonical_write_contract(
            SetStorePricingTierCommand,
            mutations.set_store_pricing_tier,
            "store_pricing_tier_row",
        ),
        "delete_store_pricing_tier": _canonical_write_contract(
            DeleteStorePricingTierCommand,
            mutations.delete_store_pricing_tier,
            "store_pricing_tier_row",
        ),
    })
)


def _read_spec(
    name: str,
    description: str,
    model: Type[BaseModel],
    handler: ToolHandler,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        command_model=model,
        kind="read",
        handler=handler,
        agent_enabled=True,
        mcp_enabled=True,
        bounded=True,
        transactional=False,
        undoable=False,
    )


def _write_spec(
    name: str,
    description: str,
    model: Type[BaseModel],
    handler: ToolHandler,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        command_model=model,
        kind="write",
        handler=handler,
        agent_enabled=True,
        mcp_enabled=True,
        bounded=True,
        transactional=True,
        undoable=True,
    )


TOOL_SPECS: tuple[ToolSpec, ...] = (
    _read_spec(
        "list_stores",
        "List warehouse stores, display names, catalog counts, and logo settings.",
        ListStoresCommand,
        _list_stores,
    ),
    _read_spec(
        "list_styles",
        "Search at most 100 product styles in one store.",
        ListStylesCommand,
        _list_styles,
    ),
    _read_spec(
        "get_style",
        "Get one style's colors, assignments, and store settings.",
        GetStyleCommand,
        _get_style,
    ),
    _read_spec(
        "search_designs",
        "Search at most 100 FDM4 logo designs.",
        SearchDesignsCommand,
        _search_designs,
    ),
    _read_spec(
        "get_design",
        "Get one design's colorways, assets, and placements.",
        GetDesignCommand,
        _get_design,
    ),
    _read_spec(
        "get_assignment_vocab",
        "Get placement and background values used by logo assignments.",
        GetAssignmentVocabCommand,
        _get_assignment_vocab,
    ),
    _read_spec(
        "get_store_settings",
        "Get one store's logo enablement and no-logo settings.",
        GetStoreSettingsCommand,
        _get_store_settings,
    ),
    _read_spec(
        "get_import_report",
        "Read a bounded page of the logo import punch list.",
        GetImportReportCommand,
        _get_import_report,
    ),
    _read_spec(
        "get_audit_log",
        "Read a bounded keyset page of logo audit history.",
        GetAuditLogCommand,
        _get_audit_log,
    ),
    _read_spec(
        "list_pricing_tiers",
        "List configured warehouse pricing-tier definitions.",
        ListPricingTiersCommand,
        _list_pricing_tiers,
    ),
    _read_spec(
        "list_store_pricing_tiers",
        "List current store-to-pricing-tier assignments.",
        ListStorePricingTiersCommand,
        _list_store_pricing_tiers,
    ),
    _write_spec(
        "save_assignment",
        "Stage saving one validated logo assignment.",
        SaveAssignmentCommand,
        mutations.save_assignment,
    ),
    _write_spec(
        "deactivate_assignment",
        "Stage deactivating one assignment (position 1 includes companions).",
        DeactivateAssignmentCommand,
        mutations.deactivate_assignment,
    ),
    _write_spec(
        "hard_delete_assignment",
        "Stage permanently deleting one assignment (position 1 includes companions).",
        HardDeleteAssignmentCommand,
        mutations.hard_delete_assignment,
    ),
    _write_spec(
        "deactivate_color",
        "Stage deactivating all assignments for one store/style/color.",
        DeactivateColorCommand,
        mutations.deactivate_color,
    ),
    _write_spec(
        "hard_delete_color",
        "Stage permanently deleting all assignments for one store/style/color.",
        HardDeleteColorCommand,
        mutations.hard_delete_color,
    ),
    _write_spec(
        "set_style_active",
        "Stage activating or deactivating all valid assignments on a style.",
        SetStyleActiveCommand,
        mutations.set_style_active,
    ),
    _write_spec(
        "apply_to_colors",
        "Stage copying one assignment slot across active colors on a style.",
        ApplyToColorsCommand,
        mutations.apply_to_colors,
    ),
    _write_spec(
        "copy_style",
        "Stage copying assignments from one style to another in the same store.",
        CopyStyleCommand,
        mutations.copy_style,
    ),
    _write_spec(
        "update_store_settings",
        "Stage changing one store's logo enablement settings.",
        UpdateStoreSettingsCommand,
        mutations.update_store_settings,
    ),
    _write_spec(
        "set_store_pricing_tier",
        "Stage setting one store's fallback pricing tier.",
        SetStorePricingTierCommand,
        mutations.set_store_pricing_tier,
    ),
    _write_spec(
        "delete_store_pricing_tier",
        "Stage deleting one store's fallback pricing-tier assignment.",
        DeleteStorePricingTierCommand,
        mutations.delete_store_pricing_tier,
    ),
)


def _validate_mutation_contracts() -> None:
    """Fail closed if any write lacks one canonical, restorable contract."""

    canonical_names = frozenset(CANONICAL_AGENT_WRITE_CONTRACTS)
    if canonical_names != APPROVED_AGENT_WRITE_NAMES:
        raise ToolRegistryError(
            "canonical agent write contracts are not exact: "
            f"missing={sorted(APPROVED_AGENT_WRITE_NAMES - canonical_names)}, "
            f"unexpected={sorted(canonical_names - APPROVED_AGENT_WRITE_NAMES)}"
        )

    models = frozenset(COMMAND_MODELS)
    handlers = frozenset(mutations.MUTATION_HANDLERS)
    scope_mappings = frozenset(mutations.COMMAND_SCOPE_KINDS)
    for label, names in (
        ("models", models),
        ("handlers", handlers),
        ("scope mappings", scope_mappings),
    ):
        if names != APPROVED_AGENT_WRITE_NAMES:
            raise ToolRegistryError(
                f"agent write {label} are not exact: "
                f"missing={sorted(APPROVED_AGENT_WRITE_NAMES - names)}, "
                f"unexpected={sorted(names - APPROVED_AGENT_WRITE_NAMES)}"
            )

    canonical_scope_kinds = frozenset(
        scope_kind
        for contract in CANONICAL_AGENT_WRITE_CONTRACTS.values()
        for scope_kind in contract.scope_kinds
    )
    snapshot_kinds = frozenset(snapshots.SNAPSHOT_SCOPE_KINDS)
    restore_kinds = frozenset(snapshots.RESTORE_SCOPE_KINDS)
    table_kinds = frozenset(snapshots.SCOPE_TABLE_BY_KIND)
    if (
        snapshot_kinds != canonical_scope_kinds
        or restore_kinds != canonical_scope_kinds
        or table_kinds != canonical_scope_kinds
    ):
        raise ToolRegistryError(
            "snapshot/restore scope declarations are not the canonical set"
        )

    for name in sorted(APPROVED_AGENT_WRITE_NAMES):
        contract = CANONICAL_AGENT_WRITE_CONTRACTS[name]
        if COMMAND_MODELS[name] is not contract.command_model:
            raise ToolRegistryError(
                f"write command model is not canonical: {name}"
            )
        if mutations.MUTATION_HANDLERS[name] is not contract.handler:
            raise ToolRegistryError(
                f"write mutation handler is not canonical: {name}"
            )
        scope_kinds = frozenset(mutations.COMMAND_SCOPE_KINDS[name])
        if scope_kinds != contract.scope_kinds:
            raise ToolRegistryError(
                f"write scope mapping is not canonical: {name}"
            )
        if (
            contract.snapshot_scope_kinds != contract.scope_kinds
            or contract.restore_scope_kinds != contract.scope_kinds
        ):
            raise ToolRegistryError(
                f"canonical snapshot/restore contract is inconsistent: {name}"
            )
        missing_snapshot = contract.snapshot_scope_kinds - snapshot_kinds
        missing_restore = contract.restore_scope_kinds - restore_kinds
        if missing_snapshot or missing_restore:
            raise ToolRegistryError(
                f"write scope is not fully restorable: {name}; "
                f"missing_snapshot={sorted(missing_snapshot)}, "
                f"missing_restore={sorted(missing_restore)}"
            )


def validate_registry(
    specs: tuple[ToolSpec, ...] = TOOL_SPECS,
    writes_enabled: bool = False,
) -> None:
    names: set[str] = set()
    for spec in specs:
        if spec.name in names:
            raise ToolRegistryError(f"duplicate tool: {spec.name}")
        names.add(spec.name)
        assert_agent_callable(spec.name)
        if spec.name in EXCLUDED_AGENT_TOOLS and spec.agent_enabled:
            raise ToolRegistryError(f"excluded agent tool: {spec.name}")
        if spec.agent_enabled and (not spec.bounded or spec.handler is None):
            raise ToolRegistryError(f"unsafe agent tool: {spec.name}")
        if spec.kind == "read" and spec.agent_enabled:
            contract = CANONICAL_AGENT_READ_CONTRACTS.get(spec.name)
            if contract is None:
                raise ToolRegistryError(f"unapproved agent read: {spec.name}")
            model, handler = contract
            if spec.command_model is not model:
                raise ToolRegistryError(
                    f"read command model is not canonical: {spec.name}"
                )
            if spec.handler is not handler:
                raise ToolRegistryError(
                    f"read handler is not canonical: {spec.name}"
                )
        if spec.kind == "write" and spec.agent_enabled:
            contract = CANONICAL_AGENT_WRITE_CONTRACTS.get(spec.name)
            if contract is None:
                raise ToolRegistryError(f"unapproved agent write: {spec.name}")
            if not spec.transactional or not spec.undoable:
                raise ToolRegistryError(f"unsafe agent write: {spec.name}")
            if spec.command_model is not contract.command_model:
                raise ToolRegistryError(
                    f"write command model is not canonical: {spec.name}"
                )
            if spec.handler is not contract.handler:
                raise ToolRegistryError(
                    f"write mutation handler is not canonical: {spec.name}"
                )

    available_reads = {
        spec.name
        for spec in specs
        if spec.kind == "read" and spec.agent_enabled
    }
    if available_reads != APPROVED_AGENT_READ_NAMES:
        raise ToolRegistryError(
            "agent read registry is not exact: "
            f"missing={sorted(APPROVED_AGENT_READ_NAMES - available_reads)}, "
            f"unexpected={sorted(available_reads - APPROVED_AGENT_READ_NAMES)}"
        )
    if frozenset(CANONICAL_AGENT_READ_CONTRACTS) != APPROVED_AGENT_READ_NAMES:
        raise ToolRegistryError("canonical agent read contracts are not exact")

    _validate_mutation_contracts()

    if writes_enabled:
        available_writes = {
            spec.name
            for spec in specs
            if spec.kind == "write" and spec.agent_enabled
        }
        missing = APPROVED_AGENT_WRITE_NAMES - available_writes
        unexpected = available_writes - APPROVED_AGENT_WRITE_NAMES
        if missing:
            raise ToolRegistryError(
                "agent writes are enabled but registry is incomplete: "
                + ", ".join(sorted(missing))
            )
        if unexpected:
            raise ToolRegistryError(
                "agent write registry is not exact: "
                f"unexpected={sorted(unexpected)}"
            )


def _strict_schema(model: Type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def close_objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                properties = node.get("properties", {})
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                close_objects(value)
        elif isinstance(node, list):
            for value in node:
                close_objects(value)

    close_objects(schema)
    return schema


def openai_schema(spec: ToolSpec) -> dict:
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": _strict_schema(spec.command_model),
        "strict": True,
    }


def agent_tool_schemas(writes_enabled: bool = False) -> list[dict]:
    validate_registry(TOOL_SPECS, writes_enabled=writes_enabled)
    return [
        openai_schema(spec)
        for spec in TOOL_SPECS
        if spec.agent_enabled
        and (spec.kind == "read" or writes_enabled)
    ]


def get_agent_tool(name: str, writes_enabled: bool = False) -> ToolSpec:
    for spec in TOOL_SPECS:
        if spec.name == name and spec.agent_enabled:
            if spec.kind == "write" and not writes_enabled:
                break
            return spec
    raise UnknownTool("Unknown or unavailable tool")


def execute_read_tool(
    name: str,
    arguments: dict,
    context: AccessContext,
    settings: Settings,
) -> dict:
    """Validate and execute one read without ASGI or dependency overrides."""

    del context
    spec = get_agent_tool(name, writes_enabled=False)
    if spec.kind != "read" or spec.handler is None:
        raise UnknownTool("Unknown or unavailable tool")
    command = spec.command_model.model_validate(arguments)
    with database.cursor() as cursor:
        result = spec.handler(cursor, command, settings)
    return jsonable_encoder(result)


def execute_agent_tool(
    name: str,
    arguments: dict,
    context: AccessContext,
    settings: Settings,
    *,
    session_id=None,
    call_id: str = "",
) -> dict:
    """Execute a bounded read or stage one approved write.

    The model-facing dispatcher has no lifecycle operation. A write reaches
    only the rollback-preview staging path and returns confirmation metadata;
    apply/discard/undo remain separate human HTTP routes.
    """

    spec = get_agent_tool(
        name,
        writes_enabled=settings.agent_writes_enabled,
    )
    required_tier(name)
    command = spec.command_model.model_validate(arguments)
    if spec.kind == "read":
        if spec.handler is None:
            raise UnknownTool("Unknown or unavailable tool")
        with database.cursor() as cursor:
            result = spec.handler(cursor, command, settings)
        return jsonable_encoder(result)

    if not settings.agent_writes_enabled or session_id is None or not call_id:
        raise UnknownTool("Unknown or unavailable tool")
    change_set = staging.get_or_create_pending_change_set(
        session_id,
        context.user_login,
    )
    staged = staging.stage_write(
        change_set["id"],
        name,
        command.model_dump(mode="json"),
        call_id,
        context.user_login,
        max_items=settings.agent_max_change_set_items,
    )
    change_count = int((staged.get("preview_diff") or {}).get("count", 0))
    return jsonable_encoder({
        "staged": True,
        "change_set_id": staged["id"],
        "revision": staged["revision"],
        "preview_hash": staged["preview_hash"],
        "contains_hard_delete": staged["contains_hard_delete"],
        "summary": f"Staged {change_count} net row change(s) for human review",
    })
