import inspect
import re
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
    CatDecideCommand,
    CatUndoDecisionCommand,
    CatMakeSurvivingCommand,
    CatCreateCategoryCommand,
    CatRenameCategoryCommand,
    CatMoveCategoryCommand,
    CatDeleteCategoryCommand,
    CatSetStoreOverrideCommand,
    CatDeleteStoreOverrideCommand,
    CatAcceptUncategorizedCommand,
    CatUnacceptUncategorizedCommand,
    CatSetRuleCommand,
    CatDeleteRuleCommand,
    CatAssignStylesCommand,
    CatDeleteAssignmentCommand,

    SetExternalMixStoreCommand,
    RemoveExternalMixStoreCommand,
    SavePriceRuleCommand,
    FillMissingColorsCommand,
    COMMAND_MODELS,
    AddMixStylesCommand,
    DeletePriceRuleCommand,
    DisableProductMixCommand,
    RemoveMixStylesCommand,
    SetLogoDefaultCostCommand,
    SetPriceRuleActiveCommand,
    SetProductMixCommand,
    ApplyToColorsCommand,
    BulkApplyCommand,
    ClearLogoNameCommand,
    CopyStyleCommand,
    CopyStyleToManyCommand,
    DeactivateAssignmentCommand,
    DeactivateColorCommand,
    DeleteStorePricingTierCommand,
    HardDeleteAssignmentCommand,
    HardDeleteColorCommand,
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
from config import Settings
from db import database
import queries
import mutations
import snapshots
from read_commands import (
    CatNodeLookupCommand, CatMappingRowsCommand,
    GetProductStateCommand,
    GetChangeHistoryCommand,
    GetStockCommand,
    AuditStorePricesCommand,
    WpProductCheckCommand,
    WpStoreCheckCommand,
    GetOrderStatusCommand,
    FindIssuesCommand,
    ExplainProductCommand,

    PreviewPriceRuleCommand,
    CheckPriceRulesCommand,
    ListPriceRuleDimensionsCommand,
    PreviewFillMissingColorsCommand,
    GetStyleMixCommand,
    GetHealthOverviewCommand,
    CatTreeCommand,
    CatMappingStatusCommand,
    CatPlanCheckCommand,
    CatRunsCommand,

    FindSimilarStylesCommand,
    GetAssignmentVocabCommand,
    GetAuditLogCommand,
    GetDesignCommand,
    GetImportReportCommand,
    GetProductLinkCommand,
    GetProductMixCommand,
    GetStockRulesCommand,
    GetSyncStatusCommand,
    GetStoreSettingsCommand,
    GetStyleCommand,
    ListColorsCommand,
    ListDesignUsageCommand,
    ListLogoNamesCommand,
    ListPriceRulesCommand,
    ListPricingTiersCommand,
    ListStorePricingTiersCommand,
    ListStoresCommand,
    ListStylesCommand,
    ListSyncBlocksCommand,
    SearchDesignsCommand,
    StoreLogoCoverageCommand,
)
import staging
import wp_bridge


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
    'cat_decide',
    'cat_undo_decision',
    'cat_make_surviving',
    'cat_create_category',
    'cat_rename_category',
    'cat_move_category',
    'cat_delete_category',
    'cat_set_store_override',
    'cat_delete_store_override',
    'cat_accept_uncategorized',
    'cat_unaccept_uncategorized',
    'cat_set_rule',
    'cat_delete_rule',
    'cat_assign_styles',
    'cat_delete_assignment',

    "set_external_mix_store",
    "remove_external_mix_store",
    "save_price_rule",
    "fill_missing_colors",
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
    "copy_style_to_many",
    "paste_logo_set",
    "replace_design",
    "reorder_logo_rows",
    "set_styles_active",
    "set_logo_name",
    "clear_logo_name",
    "set_color_class",
    "set_stock_override",
    "remove_stock_override",
    "set_brand_stock_rule",
    "remove_brand_stock_rule",
    "set_sync_block",
    "remove_sync_block",
    "set_logo_cost",
    "set_store_extra_customers",
    "bulk_apply",
    "set_logo_default_cost",
    "set_price_rule_active",
    "delete_price_rule",
    "set_product_mix",
    "disable_product_mix",
    "add_mix_styles",
    "remove_mix_styles",
})

APPROVED_AGENT_READ_NAMES = frozenset({
    "cat_node_lookup", "cat_mapping_rows",
    "get_product_state",
    "get_change_history",
    "get_stock",
    "audit_store_prices",
    "wp_product_check",
    "wp_store_check",
    "get_order_status",
    "find_issues",
    "explain_product",

    "preview_price_rule",
    "check_price_rules",
    "list_price_rule_dimensions",
    "preview_fill_missing_colors",
    "get_style_mix",
    "get_health_overview",
    "cat_tree",
    "cat_mapping_status",
    "cat_plan_check",
    "cat_runs",

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
    "find_similar_styles",
    "store_logo_coverage",
    "list_colors",
    "list_logo_names",
    "get_stock_rules",
    "list_price_rules",
    "list_sync_blocks",
    "get_product_mix",
    "list_design_usage",
    "get_product_link",
    "get_sync_status",
})

# Never model-callable: file exports/imports, uploads, and anything that
# writes across the WordPress boundary. (get_product_link is a read-only,
# soft-failing WordPress GET shared through wp_bridge, so it is allowed.)
EXCLUDED_AGENT_TOOLS = frozenset({
    "export_assignments_csv",
    "export_audit_log_csv",
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


def _find_similar_styles(cursor, command, settings):
    del settings
    return queries.find_similar_styles(
        cursor, fdm4_store=command.store, product_style=command.style, mode=command.mode,
    )


def _store_logo_coverage(cursor, command, settings):
    del settings
    return queries.store_logo_coverage(
        cursor, fdm4_store=command.store, unconfigured_only=command.unconfigured_only,
    )


def _list_colors(cursor, command, settings):
    del settings
    return queries.list_colors(cursor, **_model_arguments(command))


def _list_logo_names(cursor, command, settings):
    del settings
    return queries.list_logo_names(cursor, **_model_arguments(command))


def _get_stock_rules(cursor, command, settings):
    del settings
    return queries.get_stock_rules(cursor, **_model_arguments(command))


def _list_price_rules(cursor, command, settings):
    del settings
    return queries.list_price_rules(cursor, **_model_arguments(command))


def _list_sync_blocks(cursor, command, settings):
    del settings
    return queries.list_sync_blocks(cursor, **_model_arguments(command))


def _get_product_mix(cursor, command, settings):
    del settings
    return queries.get_product_mix(cursor, **_model_arguments(command))


def _list_design_usage(cursor, command, settings):
    del settings
    return queries.list_design_usage(cursor, **_model_arguments(command))


def _get_product_link(cursor, command, settings):
    del cursor, settings
    args = _model_arguments(command)
    return wp_bridge.product_link(str(args["store"]).strip(), str(args["style"]).strip())


def _get_sync_status(cursor, command, settings):
    del settings
    return wp_bridge.sync_status_report(cursor, _model_arguments(command).get("store"))


def _preview_price_rule(cursor, command, settings):
    del settings
    return queries.price_rule_impact(cursor, **_model_arguments(command))


def _check_price_rules(cursor, command, settings):
    del settings
    return queries.check_price_rules(cursor, **_model_arguments(command))


def _list_price_rule_dimensions(cursor, command, settings):
    del settings
    return queries.list_price_rule_dimensions(cursor, **_model_arguments(command))


def _preview_fill_missing_colors(cursor, command, settings):
    del settings
    return queries.preview_fill_missing_colors(cursor, **_model_arguments(command))


def _get_style_mix(cursor, command, settings):
    del settings
    return queries.get_style_mix(cursor, **_model_arguments(command))


def _get_health_overview(cursor, command, settings):
    del settings
    return queries.get_health_overview(cursor, **_model_arguments(command))


def _cat_node_lookup(cursor, command, settings):
    del settings
    return queries.cat_node_lookup(cursor, **_model_arguments(command))


def _cat_mapping_rows(cursor, command, settings):
    del settings
    return queries.cat_mapping_rows(cursor, **_model_arguments(command))


def _cat_tree(cursor, command, settings):
    del settings
    return queries.cat_tree(cursor, **_model_arguments(command))


def _cat_mapping_status(cursor, command, settings):
    del settings
    return queries.cat_mapping_status(cursor, **_model_arguments(command))


def _cat_plan_check(cursor, command, settings):
    del settings
    return queries.cat_plan_check(cursor, **_model_arguments(command))


def _cat_runs(cursor, command, settings):
    del settings
    return queries.cat_runs(cursor, **_model_arguments(command))


def _get_product_state(cursor, command, settings):
    del settings
    return queries.get_product_state(cursor, **_model_arguments(command))


def _category_read_allowed(context, settings) -> bool:
    """The editor's own visibility rule: category data is readable when the
    feature is on and the login is on CATMGR_VIEW_USERS, or that list is empty."""
    login = context.user_login.strip().lower()
    allowed = getattr(settings, "catmgr_view_users", frozenset())
    return bool(getattr(settings, "catmgr_enabled", False)) and (not allowed or login in allowed)


def _get_change_history(cursor, command, settings, *, context):
    return queries.get_change_history(cursor, **_model_arguments(command), user_login=context.user_login, category_access=_category_read_allowed(context, settings))


def _get_stock(cursor, command, settings):
    del settings
    return queries.get_stock(cursor, **_model_arguments(command))


def _audit_store_prices(cursor, command, settings):
    del settings
    return queries.audit_store_prices(cursor, **_model_arguments(command))


def _wp_product_check(cursor, command, settings):
    del settings
    return queries.wp_product_check(cursor, **_model_arguments(command))


def _wp_store_check(cursor, command, settings):
    del settings
    return queries.wp_store_check(cursor, **_model_arguments(command))


def _get_order_status(cursor, command, settings):
    del settings
    return queries.get_order_status(cursor, **_model_arguments(command))


def _find_issues(cursor, command, settings, *, context):
    return queries.find_issues(cursor, **_model_arguments(command), category_access=_category_read_allowed(context, settings))


def _explain_product(cursor, command, settings):
    del settings
    return queries.explain_product(cursor, **_model_arguments(command))


CANONICAL_AGENT_READ_CONTRACTS = {
    "get_product_state": (GetProductStateCommand, _get_product_state),
    "get_change_history": (GetChangeHistoryCommand, _get_change_history),
    "get_stock": (GetStockCommand, _get_stock),
    "audit_store_prices": (AuditStorePricesCommand, _audit_store_prices),
    "wp_product_check": (WpProductCheckCommand, _wp_product_check),
    "wp_store_check": (WpStoreCheckCommand, _wp_store_check),
    "get_order_status": (GetOrderStatusCommand, _get_order_status),
    "find_issues": (FindIssuesCommand, _find_issues),
    "explain_product": (ExplainProductCommand, _explain_product),

    "preview_price_rule": (PreviewPriceRuleCommand, _preview_price_rule),
    "check_price_rules": (CheckPriceRulesCommand, _check_price_rules),
    "list_price_rule_dimensions": (ListPriceRuleDimensionsCommand, _list_price_rule_dimensions),
    "preview_fill_missing_colors": (PreviewFillMissingColorsCommand, _preview_fill_missing_colors),
    "get_style_mix": (GetStyleMixCommand, _get_style_mix),
    "get_health_overview": (GetHealthOverviewCommand, _get_health_overview),
    "cat_node_lookup": (CatNodeLookupCommand, _cat_node_lookup),
    "cat_mapping_rows": (CatMappingRowsCommand, _cat_mapping_rows),
    "cat_tree": (CatTreeCommand, _cat_tree),
    "cat_mapping_status": (CatMappingStatusCommand, _cat_mapping_status),
    "cat_plan_check": (CatPlanCheckCommand, _cat_plan_check),
    "cat_runs": (CatRunsCommand, _cat_runs),

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
    "find_similar_styles": (FindSimilarStylesCommand, _find_similar_styles),
    "store_logo_coverage": (StoreLogoCoverageCommand, _store_logo_coverage),
    "list_colors": (ListColorsCommand, _list_colors),
    "list_logo_names": (ListLogoNamesCommand, _list_logo_names),
    "get_stock_rules": (GetStockRulesCommand, _get_stock_rules),
    "list_price_rules": (ListPriceRulesCommand, _list_price_rules),
    "list_sync_blocks": (ListSyncBlocksCommand, _list_sync_blocks),
    "get_product_mix": (GetProductMixCommand, _get_product_mix),
    "list_design_usage": (ListDesignUsageCommand, _list_design_usage),
    "get_product_link": (GetProductLinkCommand, _get_product_link),
    "get_sync_status": (GetSyncStatusCommand, _get_sync_status),
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
        'cat_decide': _canonical_write_contract(CatDecideCommand, mutations.cat_decide, 'catmgr_slug_map_row'),
        'cat_undo_decision': _canonical_write_contract(CatUndoDecisionCommand, mutations.cat_undo_decision, 'catmgr_slug_map_row'),
        'cat_make_surviving': _canonical_write_contract(CatMakeSurvivingCommand, mutations.cat_make_surviving, 'catmgr_slug_map_row'),
        'cat_create_category': _canonical_write_contract(CatCreateCategoryCommand, mutations.cat_create_category, 'catmgr_draft'),
        'cat_rename_category': _canonical_write_contract(CatRenameCategoryCommand, mutations.cat_rename_category, 'catmgr_draft'),
        'cat_move_category': _canonical_write_contract(CatMoveCategoryCommand, mutations.cat_move_category, 'catmgr_draft'),
        'cat_delete_category': _canonical_write_contract(CatDeleteCategoryCommand, mutations.cat_delete_category, 'catmgr_draft', 'catmgr_rule_row', 'catmgr_assignment_row'),
        'cat_set_store_override': _canonical_write_contract(CatSetStoreOverrideCommand, mutations.cat_set_store_override, 'catmgr_override_row', 'catmgr_slug_map_row', 'catmgr_draft'),
        'cat_delete_store_override': _canonical_write_contract(CatDeleteStoreOverrideCommand, mutations.cat_delete_store_override, 'catmgr_override_row', 'catmgr_slug_map_row'),
        'cat_accept_uncategorized': _canonical_write_contract(CatAcceptUncategorizedCommand, mutations.cat_accept_uncategorized, 'catmgr_ack_row'),
        'cat_unaccept_uncategorized': _canonical_write_contract(CatUnacceptUncategorizedCommand, mutations.cat_unaccept_uncategorized, 'catmgr_ack_row'),
        'cat_set_rule': _canonical_write_contract(CatSetRuleCommand, mutations.cat_set_rule, 'catmgr_rule_row'),
        'cat_delete_rule': _canonical_write_contract(CatDeleteRuleCommand, mutations.cat_delete_rule, 'catmgr_rule_row'),
        'cat_assign_styles': _canonical_write_contract(CatAssignStylesCommand, mutations.cat_assign_styles, 'catmgr_assignment_row'),
        'cat_delete_assignment': _canonical_write_contract(CatDeleteAssignmentCommand, mutations.cat_delete_assignment, 'catmgr_assignment_row'),
        "set_external_mix_store": _canonical_write_contract(SetExternalMixStoreCommand, mutations.set_external_mix_store, "virtual_catalog_store_row", "store_mix_store_row"),
        "remove_external_mix_store": _canonical_write_contract(RemoveExternalMixStoreCommand, mutations.remove_external_mix_store, "virtual_catalog_store_row"),
        "save_price_rule": _canonical_write_contract(SavePriceRuleCommand, mutations.save_price_rule, "price_rule_row"),
        "fill_missing_colors": _canonical_write_contract(FillMissingColorsCommand, mutations.fill_missing_colors, "assignment_store"),
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
        "copy_style_to_many": _canonical_write_contract(
            CopyStyleToManyCommand,
            mutations.copy_style_to_many,
            "assignment_style",
        ),
        "paste_logo_set": _canonical_write_contract(
            PasteLogoSetCommand,
            mutations.paste_logo_set,
            "assignment_style",
        ),
        "replace_design": _canonical_write_contract(
            ReplaceDesignCommand,
            mutations.replace_design,
            "assignment_style",
        ),
        "reorder_logo_rows": _canonical_write_contract(
            ReorderLogoRowsCommand,
            mutations.reorder_logo_rows,
            "assignment_style",
        ),
        "set_styles_active": _canonical_write_contract(
            SetStylesActiveCommand,
            mutations.set_styles_active,
            "assignment_style",
        ),
        "set_logo_name": _canonical_write_contract(
            SetLogoNameCommand, mutations.set_logo_name, "display_name_row",
        ),
        "clear_logo_name": _canonical_write_contract(
            ClearLogoNameCommand, mutations.clear_logo_name, "display_name_row",
        ),
        "set_color_class": _canonical_write_contract(
            SetColorClassCommand, mutations.set_garment_color_class, "color_class_row",
        ),
        "set_stock_override": _canonical_write_contract(
            SetStockOverrideCommand, mutations.set_stock_override, "stock_override_row",
        ),
        "remove_stock_override": _canonical_write_contract(
            RemoveStockOverrideCommand, mutations.remove_stock_override, "stock_override_row",
        ),
        "set_brand_stock_rule": _canonical_write_contract(
            SetBrandStockRuleCommand, mutations.set_brand_stock_rule, "brand_stock_rule_row",
        ),
        "remove_brand_stock_rule": _canonical_write_contract(
            RemoveBrandStockRuleCommand, mutations.remove_brand_stock_rule, "brand_stock_rule_row",
        ),
        "set_sync_block": _canonical_write_contract(
            SetSyncBlockCommand, mutations.set_sync_block, "sync_exclusion_row",
        ),
        "remove_sync_block": _canonical_write_contract(
            RemoveSyncBlockCommand, mutations.remove_sync_block, "sync_exclusion_row",
        ),
        "set_logo_cost": _canonical_write_contract(
            SetLogoCostCommand, mutations.set_logo_cost, "assignment_style",
        ),
        "set_store_extra_customers": _canonical_write_contract(
            SetStoreExtraCustomersCommand, mutations.set_store_extra_customers, "store_settings_row",
        ),
        "bulk_apply": _canonical_write_contract(
            BulkApplyCommand, mutations.bulk_apply, "assignment_store",
        ),
        "set_logo_default_cost": _canonical_write_contract(
            SetLogoDefaultCostCommand, mutations.set_logo_default_cost, "default_cost_row",
        ),
        "set_price_rule_active": _canonical_write_contract(
            SetPriceRuleActiveCommand, mutations.set_price_rule_active, "price_rule_row",
        ),
        "delete_price_rule": _canonical_write_contract(
            DeletePriceRuleCommand, mutations.delete_price_rule, "price_rule_row",
        ),
        "set_product_mix": _canonical_write_contract(
            SetProductMixCommand, mutations.set_product_mix, "store_mix_store_row", "store_mix_items",
        ),
        "disable_product_mix": _canonical_write_contract(
            DisableProductMixCommand, mutations.disable_product_mix, "store_mix_store_row",
        ),
        "add_mix_styles": _canonical_write_contract(
            AddMixStylesCommand, mutations.add_mix_styles, "store_mix_items",
        ),
        "remove_mix_styles": _canonical_write_contract(
            RemoveMixStylesCommand, mutations.remove_mix_styles, "store_mix_items",
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
        "List every store: FDM4 store code, display name, catalog, product counts and logo settings. Call this first to turn a store NAME into its code.",
        ListStoresCommand,
        _list_stores,
    ),
    _read_spec(
        "list_styles",
        "Find product styles in one store (max 100): style code, product name, brand and whether logos are configured. Use it to get a style code from a product name.",
        ListStylesCommand,
        _list_styles,
    ),
    _read_spec(
        "get_style",
        "One product's full logo setup in one store: every garment color, every logo row (option row, position, design, scheme, logo code, placement, display name, name override, active, sort order) and its cost (cost_override, default_cost, effective_cost with its source), plus the store's logo settings.",
        GetStyleCommand,
        _get_style,
    ),
    _read_spec(
        "search_designs",
        "Search FDM4 logo designs (max 100) by description, design id or logo code. With a store and no query it browses the designs used by that store and by the FDM4 customer that owns it; store_uses > 0 marks designs actually assigned in the store.",
        SearchDesignsCommand,
        _search_designs,
    ),
    _read_spec(
        "get_design",
        "One design's details: color schemes (colorways) with preview images and artwork files, logo codes on file, and the placements FDM4 defined for it.",
        GetDesignCommand,
        _get_design,
    ),
    _read_spec(
        "get_assignment_vocab",
        "The placement names and background tags logo rows use, with usage counts. Use it to spell a placement the way the app does.",
        GetAssignmentVocabCommand,
        _get_assignment_vocab,
    ),
    _read_spec(
        "get_store_settings",
        "One store's two logo switches: whether its logos are enabled at all and whether shoppers may choose 'No logo'.",
        GetStoreSettingsCommand,
        _get_store_settings,
    ),
    _read_spec(
        "get_import_report",
        "The legacy-sheet import punch list: rows that could not be imported and why (no_color_code, no_design, no_art, orphaned_companion ...), paged.",
        GetImportReportCommand,
        _get_import_report,
    ),
    _read_spec(
        "get_audit_log",
        "Change history for logo data, newest first: who changed what and when, with field-level diffs. Filter by store, style, actor or action; page with before_id.",
        GetAuditLogCommand,
        _get_audit_log,
    ),
    _read_spec(
        "list_pricing_tiers",
        "The pricing levels a store can be assigned (tier name, price-list key, whether it is MSRP). A level only fills prices FDM4 leaves blank.",
        ListPricingTiersCommand,
        _list_pricing_tiers,
    ),
    _read_spec(
        "find_similar_styles",
        "Styles in a store that carry the same logo set as a given style (exact) or share at least one logo (overlap). Use it before suggesting where to copy a setup.",
        FindSimilarStylesCommand,
        _find_similar_styles,
    ),
    _read_spec(
        "store_logo_coverage",
        "Per live style of a store: how many garment colors have at least one active logo and which colors still have none. Answers 'what still needs logos?'.",
        StoreLogoCoverageCommand,
        _store_logo_coverage,
    ),
    _read_spec(
        "list_colors",
        "Garment colors with their light/dark class (dark garments get the white logo, light get the black one), who set it, and how many styles use each color.",
        ListColorsCommand,
        _list_colors,
    ),
    _read_spec(
        "list_logo_names",
        "The names shoppers see for logos. With a store: its logos with the effective name (store-specific or shared default) and whether it is store-specific. Without a store: search the shared names.",
        ListLogoNamesCommand,
        _list_logo_names,
    ),
    _read_spec(
        "get_stock_rules",
        "Fake Inventory configuration: brand rules (real, fake = always in stock, or automatic) and style exceptions, optionally limited to the brands and styles one store carries.",
        GetStockRulesCommand,
        _get_stock_rules,
    ),
    _read_spec(
        "list_price_rules",
        "Price rules in evaluation order with targets, exceptions, effect, rounding, floors/caps, active flag and last preview, plus the stores whose prices are frozen. Optionally only the rules that can touch one store.",
        ListPriceRulesCommand,
        _list_price_rules,
    ),
    _read_spec(
        "list_sync_blocks",
        "Sync Blocks (freezes): whole-store, price-only or single-style rows the hourly update skips, with notes and on/off state.",
        ListSyncBlocksCommand,
        _list_sync_blocks,
    ),
    _read_spec(
        "get_product_mix",
        "A store's Product Mix state: not enrolled (follows FDM4), all (follows FDM4 completely) or a curated list with its styles, color and size trims, and how many new FDM4 products are waiting.",
        GetProductMixCommand,
        _get_product_mix,
    ),
    _read_spec(
        "list_store_pricing_tiers",
        "Which stores are assigned which pricing level, with notes.",
        ListStorePricingTiersCommand,
        _list_store_pricing_tiers,
    ),
    _read_spec(
        "list_design_usage",
        "Which styles of a store carry a given design (optionally one color scheme): per style the row count, active rows, colors, schemes and logo codes, plus style_codes ready to pass to replace_design. Use it before replacing a design.",
        ListDesignUsageCommand,
        _list_design_usage,
    ),
    _read_spec(
        "get_product_link",
        "The website links for a style's product in a store: the shopper-facing page (view_url) and the WordPress edit screen (edit_url). ok=false means the store has no live product for that style or WordPress did not answer.",
        GetProductLinkCommand,
        _get_product_link,
    ),
    _read_spec(
        "get_sync_status",
        "Whether the sync pipeline is running and when it last ran: the latest FDM4 warehouse pull and the latest WooCommerce reconcile per environment with status, timing and errors, plus 24-hour success/failure counts. With a store: whether the app owns that store's logo sync (logo_sync_ownership.owned), its recent sync/ownership events, active freezes, logos-enabled switch and the time of its last logo edit. Use it for 'did the sync run?' and 'why isn't this on the site?'.",
        GetSyncStatusCommand,
        _get_sync_status,
    ),
    _write_spec(
        "save_assignment",
        "Stage adding or updating one logo row (store, style, color, option row, position). Validated against FDM4 designs; the person confirms the review card before anything changes.",
        SaveAssignmentCommand,
        mutations.save_assignment,
    ),
    _write_spec(
        "deactivate_assignment",
        "Stage hiding one logo row from the website (kept, reversible). Position 1 also hides its companion positions 2-3.",
        DeactivateAssignmentCommand,
        mutations.deactivate_assignment,
    ),
    _write_spec(
        "hard_delete_assignment",
        "Stage permanently deleting one logo row (irreversible; position 1 takes its companions). Prefer deactivate unless deletion is explicitly wanted.",
        HardDeleteAssignmentCommand,
        mutations.hard_delete_assignment,
    ),
    _write_spec(
        "deactivate_color",
        "Stage hiding every logo row on one garment color of a style (all option rows), reversibly.",
        DeactivateColorCommand,
        mutations.deactivate_color,
    ),
    _write_spec(
        "hard_delete_color",
        "Stage permanently deleting every logo row on one garment color of a style (irreversible).",
        HardDeleteColorCommand,
        mutations.hard_delete_color,
    ),
    _write_spec(
        "set_style_active",
        "Stage showing or hiding every valid logo row of a style at once.",
        SetStyleActiveCommand,
        mutations.set_style_active,
    ),
    _write_spec(
        "apply_to_colors",
        "Stage copying one logo row to every live garment color of the same style (same option row and position); occupied slots are kept unless overwrite.",
        ApplyToColorsCommand,
        mutations.apply_to_colors,
    ),
    _write_spec(
        "copy_style",
        "Stage copying a style's whole logo setup onto another style in the same store, matching by identical color codes.",
        CopyStyleCommand,
        mutations.copy_style,
    ),
    _write_spec(
        "update_store_settings",
        "Stage changing a store's logo switches: logos enabled, and whether shoppers may choose 'No logo'.",
        UpdateStoreSettingsCommand,
        mutations.update_store_settings,
    ),
    _write_spec(
        "set_store_pricing_tier",
        "Stage assigning a store a pricing level (from list_pricing_tiers); it only fills prices FDM4 leaves blank.",
        SetStorePricingTierCommand,
        mutations.set_store_pricing_tier,
    ),
    _write_spec(
        "delete_store_pricing_tier",
        "Stage removing a store's pricing level so blank prices fall back to retail.",
        DeleteStorePricingTierCommand,
        mutations.delete_store_pricing_tier,
    ),
    _write_spec(
        "copy_style_to_many",
        "Copy one style's complete logo setup to up to 50 other styles of the same store in one reviewable change. exact = only colors both styles share; like = also fill the target's other colors from a source color of the same light/dark class. merge keeps rows the targets already have; overwrite replaces occupied slots. Never removes rows. Stages a proposal; the person confirms.",
        CopyStyleToManyCommand,
        mutations.copy_style_to_many,
    ),
    _write_spec(
        "paste_logo_set",
        "Place the same set of logo rows on up to 50 styles at once: on all their live colors, on one matching color, or on light/dark colors only. Rows are validated exactly like a manual save (design ownership, scheme, placement, position-1 anchor); invalid rows are reported per style, not fatal. Stages a proposal; the person confirms.",
        PasteLogoSetCommand,
        mutations.paste_logo_set,
    ),
    _write_spec(
        "replace_design",
        "Swap every logo row that uses one design (optionally one color scheme) for another design + scheme on the named styles (max 50 per call; find them with list_design_usage). Only design, logo code, scheme and image change; placement, cost, order, names and active flags stay. Rows that would be invalid are skipped and reported. Stages a proposal; the person confirms.",
        ReplaceDesignCommand,
        mutations.replace_design,
    ),
    _write_spec(
        "reorder_logo_rows",
        "Change the order shoppers see a color's logo choices in: give every option row of that color in the wanted order. apply_to style (default) also ranks every other color of the style by the same designs, like the app's drag-and-drop. Stages a proposal; the person confirms.",
        ReorderLogoRowsCommand,
        mutations.reorder_logo_rows,
    ),
    _write_spec(
        "set_styles_active",
        "Show or hide every logo row on up to 50 styles of a store in one change (rows are kept, never deleted). Styles without rows are reported, not fatal. Stages a proposal; the person confirms.",
        SetStylesActiveCommand,
        mutations.set_styles_active,
    ),
    _write_spec(
        "set_logo_name",
        "Set the name shoppers see for one logo (design + color scheme): the shared default every store falls back to (store null) or a name only one store sees. Hand-set names are locked so FDM4 re-pulls never overwrite them. Stages a proposal; the person confirms.",
        SetLogoNameCommand,
        mutations.set_logo_name,
    ),
    _write_spec(
        "clear_logo_name",
        "Remove one store's own name for a logo so that store shows the shared default again. The shared default cannot be removed. Stages a proposal; the person confirms.",
        ClearLogoNameCommand,
        mutations.clear_logo_name,
    ),
    _write_spec(
        "set_color_class",
        "Set a garment color's light/dark class (light, dark or both), which drives Bulk Apply and like-color copies, and marks it as confirmed by a person. Stages a proposal; the person confirms.",
        SetColorClassCommand,
        mutations.set_garment_color_class,
    ),
    _write_spec(
        "set_stock_override",
        "Add or update a Fake Inventory style exception: force one style (every store) to fake stock (always in stock at 99,999) or real FDM4 stock, overriding its brand rule; can be saved switched off. Stages a proposal; the person confirms.",
        SetStockOverrideCommand,
        mutations.set_stock_override,
    ),
    _write_spec(
        "remove_stock_override",
        "Remove a style's Fake Inventory exception so its brand rule (or the automatic default) applies again. Stages a proposal; the person confirms.",
        RemoveStockOverrideCommand,
        mutations.remove_stock_override,
    ),
    _write_spec(
        "set_brand_stock_rule",
        "Add or update a Fake Inventory brand rule by FDM4 mill code: every style of the brand shows real FDM4 stock or always in stock (style exceptions still win). Stages a proposal; the person confirms.",
        SetBrandStockRuleCommand,
        mutations.set_brand_stock_rule,
    ),
    _write_spec(
        "remove_brand_stock_rule",
        "Remove a brand's Fake Inventory rule so the automatic default applies again. Stages a proposal; the person confirms.",
        RemoveBrandStockRuleCommand,
        mutations.remove_brand_stock_rule,
    ),
    _write_spec(
        "set_sync_block",
        "Freeze the hourly product update for a whole store (full, or pricing-only) or for up to 50 named styles in a store, with a note; can be saved switched off. Reports how many products each style freezes (0 usually means a typo). Stages a proposal; the person confirms.",
        SetSyncBlockCommand,
        mutations.set_sync_block,
    ),
    _write_spec(
        "remove_sync_block",
        "Remove a store's whole-store freeze or the freezes on named styles so the hourly update runs for them again. Stages a proposal; the person confirms.",
        RemoveSyncBlockCommand,
        mutations.remove_sync_block,
    ),
    _write_spec(
        "set_logo_cost",
        "Set one shopper charge for a logo (design, optionally one color scheme) on every row of the named styles in a store, or clear the store's override (null) so the logo's default cost applies. Rows are updated in place; nothing else about them changes. Get the styles from list_design_usage; max 50 per call. Stages a proposal; the person confirms.",
        SetLogoCostCommand,
        mutations.set_logo_cost,
    ),
    _write_spec(
        "set_store_extra_customers",
        "Replace the list of other FDM4 customer numbers whose designs a store may use (why a save can be refused as 'belongs to a different customer'). Empty list = only the store's own customer. Stages a proposal; the person confirms.",
        SetStoreExtraCustomersCommand,
        mutations.set_store_extra_customers,
    ),
    _write_spec(
        "bulk_apply",
        "The Bulk Apply page as one staged change: put one logo variant (logo code + scheme + placement) on every light or dark garment color across a store, or on the listed color codes, optionally limited to named styles. Skips colors that already have a logo in that slot unless overwrite is true. Whole-store scope: the review shows every row; very large stores may exceed the exact-undo row cap, then use styles or paste_logo_set. Stages a proposal; the person confirms.",
        BulkApplyCommand,
        mutations.bulk_apply,
    ),
    _write_spec(
        "set_logo_default_cost",
        "Set a logo variant's DEFAULT shopper charge (per logo code + color scheme) - the price every store pays for it unless a row has its own override; 0 makes it free by default. Locked by default so cost re-imports keep it. Global to all stores: prefer set_logo_cost for one store. Stages a proposal; the person confirms.",
        SetLogoDefaultCostCommand,
        mutations.set_logo_default_cost,
    ),
    _write_spec(
        "set_price_rule_active",
        "Switch a price rule on or off. With active=true, includes evaluated price impact in human confirmation and stamps the preview while activating in the apply transaction. Refuses unknown rules and requires fresh confirmation if the impact changes.",
        SetPriceRuleActiveCommand,
        mutations.set_price_rule_active,
    ),
    _write_spec(
        "delete_price_rule",
        "Remove a price rule entirely (list_price_rules shows ids). Editing or creating rules stays in the app. Stages a proposal; the person confirms.",
        DeletePriceRuleCommand,
        mutations.delete_price_rule,
    ),
    _write_spec(
        "set_product_mix",
        "Enrol a store in Product Mix or change its mode: all = follow FDM4 completely; list = a curated list decides which products the store carries. Switching to list snapshots the store's current mix into the list first (never an empty list). Stages a proposal; the person confirms.",
        SetProductMixCommand,
        mutations.set_product_mix,
    ),
    _write_spec(
        "disable_product_mix",
        "Switch a store's Product Mix override off so it follows FDM4 again; its saved list is kept for later. Stages a proposal; the person confirms.",
        DisableProductMixCommand,
        mutations.disable_product_mix,
    ),
    _write_spec(
        "add_mix_styles",
        "Add up to 50 styles (all their colors) to a list-mode store's curated product list; reports how many live products each style has (0 = likely a typo). Stages a proposal; the person confirms.",
        AddMixStylesCommand,
        mutations.add_mix_styles,
    ),
    _write_spec(
        "remove_mix_styles",
        "Drop up to 50 styles from a list-mode store's curated list so those products leave the store on the next update. Refuses to empty the list (use disable_product_mix instead). Stages a proposal; the person confirms.",
        RemoveMixStylesCommand,
        mutations.remove_mix_styles,
    ),
    _read_spec("preview_price_rule", 'Show the products and stores affected by a price rule, with a bounded sample of before and after prices. Does not save a preview stamp or activate anything.', PreviewPriceRuleCommand, _preview_price_rule),
    _read_spec("check_price_rules", 'Check which active price rules apply to one store and style and show the resulting prices. Does not change rules.', CheckPriceRulesCommand, _check_price_rules),
    _read_spec("list_price_rule_dimensions", 'List valid brands, categories and pricing tiers for targeting price rules. Does not change targeting.', ListPriceRuleDimensionsCommand, _list_price_rule_dimensions),
    _read_spec("preview_fill_missing_colors", 'Plan filling missing garment colors for up to 50 styles from their own logos. When configured colors differ, a person must choose a source; no logos are changed.', PreviewFillMissingColorsCommand, _preview_fill_missing_colors),
    _read_spec("get_style_mix", 'Show which stores carry a style and whether it follows FDM4 or a curated list. With a store, show its color and size settings. Does not change the mix.', GetStyleMixCommand, _get_style_mix),
    _read_spec("get_health_overview", 'Show a bounded overview of warehouse pipeline runs, product state, pricing, freezes and feed consumers. Does not start any work.', GetHealthOverviewCommand, _get_health_overview),
    _read_spec("cat_node_lookup", "Confirm a draft category by exact path or web address, returning its store and product counts. Never guess a target; missing or ambiguous targets are refused.", CatNodeLookupCommand, _cat_node_lookup),
    _read_spec("cat_mapping_rows", "Inspect up to 200 full decision rows, including stores and product counts, filtered by undecided, empty, store_only or exact old slugs. Use before deciding rows.", CatMappingRowsCommand, _cat_mapping_rows),
    _read_spec("cat_tree", 'Read category draft paths with per-slug store and product counts from an environment snapshot. Capped; requires category-view access. Never changes the draft.', CatTreeCommand, _cat_tree),
    _read_spec("cat_mapping_status", 'Read category mapping totals and capped lists of undecided and empty rows. Requires category-view access; never changes mappings.', CatMappingStatusCommand, _cat_mapping_status),
    _read_spec("cat_plan_check", 'Check the category plan for blockers, warnings, totals and per-store changes. Capped; requires category-view access. Never creates or starts a run.', CatPlanCheckCommand, _cat_plan_check),
    _read_spec("cat_runs", 'Read recent category runs and optionally one run’s capped per-store job summary. Requires category-view access. Never starts, retries or changes a run.', CatRunsCommand, _cat_runs),
    _write_spec("save_price_rule", "Create or edit a price rule; rejects invalid effects, targeting, dates and price bounds. With active=true, includes the evaluated impact in human confirmation and stamps the preview while activating in the apply transaction. Material edits saved inactive clear the prior preview stamp.", SavePriceRuleCommand, mutations.save_price_rule),
    _write_spec("fill_missing_colors", "Copy each style's own source-color logos onto missing garment colors, up to 50 styles. Requires an explicit source color, refuses unknown styles and oversized store snapshots, and records an undoable fill-gaps batch in the editor history.", FillMissingColorsCommand, mutations.fill_missing_colors),
    _read_spec("get_product_state", 'Show a store product parent and its variations with prices, projected stock and active flags. Refuses unknown stores and products.', GetProductStateCommand, _get_product_state),
    _read_spec("get_change_history", 'Show recent changes by every recorded actor, newest first, with source and actor counts. Change-set cards are limited to your own; category history requires category access.', GetChangeHistoryCommand, _get_change_history),
    _read_spec("get_stock", 'Show live inventory by item and warehouse, with available stock calculated as on-hand minus committed, floored at zero per warehouse. Refuses unknown stock.', GetStockCommand, _get_stock),
    _read_spec("audit_store_prices", 'Evaluate the active price-rule chain for a store, showing counts per rule, the biggest price changes and freezes. Evaluates at most 50,001 candidates.', AuditStorePricesCommand, _audit_store_prices),
    _read_spec("wp_product_check", 'Read the WordPress product status, price, stock, categories and sync timestamp for a store product. Returns an unavailable reason when the site cannot be read.', WpProductCheckCommand, _wp_product_check),
    _read_spec("wp_store_check", 'Read a WordPress store: the network category freeze flag, product counts and the most recent product-sync summary (the site keeps only its last run, not a history). Returns an unavailable reason for each failed WordPress section.', WpStoreCheckCommand, _wp_store_check),
    _read_spec("get_order_status", 'Read order status, totals, item SKUs, embellishment codes, payment method code and sync state. Excludes customer information, addresses and notes.', GetOrderStatusCommand, _get_order_status),
    _read_spec("find_issues", 'Check logo gaps, colors, expiring rules, old freezes, stale inventory exceptions, categories and WordPress disagreements. Each check reports its own failure.', FindIssuesCommand, _find_issues),
    _read_spec("explain_product", 'Explain the expected visibility, pricing, inventory rules and blockers for a store product, and compare WordPress when available. Each section can fail independently.', ExplainProductCommand, _explain_product),
    _write_spec('cat_decide', 'Stage up to 200 decisions from cat_mapping_rows: move into a confirmed target_slug, keep for this store only, or delete. Refuses delete when a row still holds products unless allow_products=true. make_surviving=true refuses an existing survivor; use cat_make_surviving to replace it. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatDecideCommand, mutations.cat_decide),
    _write_spec('cat_undo_decision', 'Undo an explicit decision for one old slug in the draft. Automatic decisions have no explicit row to clear. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatUndoDecisionCommand, mutations.cat_undo_decision),
    _write_spec('cat_make_surviving', 'Make this old slug the surviving category for the confirmed target web address, demoting the previous explicit survivor. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatMakeSurvivingCommand, mutations.cat_make_surviving),
    _write_spec('cat_create_category', 'Create a category in the draft, under a confirmed parent or at the top level. Omit slug for an automatic web address. Refuses drafts over 2,000 total structural rows. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatCreateCategoryCommand, mutations.cat_create_category),
    _write_spec('cat_rename_category', 'Rename a draft category, change its web address or description. A web address change carries the live identity mapping for redirect planning; a person checks the plan before apply. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatRenameCategoryCommand, mutations.cat_rename_category),
    _write_spec('cat_move_category', 'Move a draft category into a confirmed parent or to the top level; position is zero-based. Sibling order is included in exact undo. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatMoveCategoryCommand, mutations.cat_move_category),
    _write_spec('cat_delete_category', 'Delete a draft category; cascade must explicitly allow deleting its children. Related mapping rows become undecided; rules and style assignments also disappear and are included in undo. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatDeleteCategoryCommand, mutations.cat_delete_category),
    _write_spec('cat_set_store_override', 'Rename or hide a category on one store, or add a store-only category. Confirm category and parent targets first. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatSetStoreOverrideCommand, mutations.cat_set_store_override),
    _write_spec('cat_delete_store_override', 'Delete a store override from the draft, including any decisions linked to that override. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatDeleteStoreOverrideCommand, mutations.cat_delete_store_override),
    _write_spec('cat_accept_uncategorized', 'Accept up to 200 products having no category in the draft plan, with an optional note. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatAcceptUncategorizedCommand, mutations.cat_accept_uncategorized),
    _write_spec('cat_unaccept_uncategorized', 'Remove acceptance for up to 200 products with no category so Check the plan can block again. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatUnacceptUncategorizedCommand, mutations.cat_unaccept_uncategorized),
    _write_spec('cat_set_rule', 'Create or edit a product rule for a confirmed category. Validates the filter and includes evaluate_rule match count and a bounded sample in review. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatSetRuleCommand, mutations.cat_set_rule),
    _write_spec('cat_delete_rule', 'Delete a product rule from the draft with exact undo. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatDeleteRuleCommand, mutations.cat_delete_rule),
    _write_spec('cat_assign_styles', 'Add up to 200 styles to a confirmed draft category, or keep them out. Replaces the opposite decision and includes both row identities in undo. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatAssignStylesCommand, mutations.cat_assign_styles),
    _write_spec('cat_delete_assignment', 'Delete one explicit style assignment from a draft category with exact undo. Draft only; stages a proposal for human review. After confirmation suggest Check the plan.', CatDeleteAssignmentCommand, mutations.cat_delete_assignment),
    _write_spec("set_external_mix_store", "Enrol a known store as an external all-products store with stock 9999 on the next refresh. Refuses existing external stores and active curated lists; stages a review with exact undo.", SetExternalMixStoreCommand, mutations.set_external_mix_store),
    _write_spec("remove_external_mix_store", "Return an external store to its regular FDM4 catalog on the next refresh, which may hide many products. Keeps its product-mix registry; stages a review with exact undo.", RemoveExternalMixStoreCommand, mutations.remove_external_mix_store),
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


_LOOKAROUND = re.compile(r"\(\?[=!<]")


def _strict_schema(model: Type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def openai_safe(node):
        """OpenAI strict mode rejects regex lookaround (pydantic emits one for
        Decimal-as-string). Drop such patterns; the server still validates."""
        if isinstance(node, dict):
            pattern = node.get("pattern")
            if isinstance(pattern, str) and _LOOKAROUND.search(pattern):
                node.pop("pattern", None)
            for value in node.values():
                openai_safe(value)
        elif isinstance(node, list):
            for value in node:
                openai_safe(value)

    openai_safe(schema)

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


def validate_write_tool_allowlist(write_tools) -> frozenset:
    """AGENT_WRITE_TOOLS may only name approved writes; fail closed otherwise."""
    names = frozenset(str(n).strip().lower() for n in (write_tools or ()) if str(n).strip())
    unknown = names - APPROVED_AGENT_WRITE_NAMES
    if unknown:
        raise ToolRegistryError(
            "AGENT_WRITE_TOOLS names tools that are not approved writes: "
            + ", ".join(sorted(unknown))
        )
    return names


def _write_allowed(spec: ToolSpec, write_tools) -> bool:
    if spec.kind != "write":
        return True
    allowed = validate_write_tool_allowlist(write_tools)
    return not allowed or spec.name in allowed


def _offered_to(spec: ToolSpec, context, settings) -> bool:
    """Category tools are listed only for a caller the gate would accept, so
    the model is never offered a tool that can only answer "Not found"."""
    if context is None or settings is None or not spec.name.startswith("cat_"):
        return True
    try:
        _assert_read_access(spec.name, context, settings)
    except UnknownTool:
        return False
    return True


def agent_tool_schemas(writes_enabled: bool = False, write_tools=None, *, context=None, settings=None) -> list[dict]:
    validate_registry(TOOL_SPECS, writes_enabled=writes_enabled)
    return [
        openai_schema(spec)
        for spec in TOOL_SPECS
        if spec.agent_enabled
        and (spec.kind == "read" or writes_enabled)
        and _write_allowed(spec, write_tools)
        and _offered_to(spec, context, settings)
    ]


def get_agent_tool(name: str, writes_enabled: bool = False, write_tools=None) -> ToolSpec:
    for spec in TOOL_SPECS:
        if spec.name == name and spec.agent_enabled:
            if spec.kind == "write" and not writes_enabled:
                break
            if not _write_allowed(spec, write_tools):
                break
            return spec
    raise UnknownTool("Unknown or unavailable tool")


def _handler_wants_context(handler) -> bool:
    """Reads that scope by the caller (change history, issue checks) declare a
    keyword-only `context`; every dispatcher must pass it the same way."""
    try:
        return "context" in inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False


def _run_read_handler(spec, cursor, command, settings, context):
    if _handler_wants_context(spec.handler):
        return spec.handler(cursor, command, settings, context=context)
    return spec.handler(cursor, command, settings)


def _assert_read_access(name, context, settings):
    """Category tools, reads and writes alike, follow the editor's own
    visibility rule: everyone who can open the app when CATMGR_VIEW_USERS is
    empty, otherwise only the logins listed."""
    if not name.startswith("cat_"):
        return
    login = context.user_login.strip().lower()
    allowed = settings.catmgr_view_users
    if not settings.catmgr_enabled or (allowed and login not in allowed):
        raise UnknownTool("Not found")


def execute_read_tool(
    name: str,
    arguments: dict,
    context: AccessContext,
    settings: Settings,
) -> dict:
    """Validate and execute one read without ASGI or dependency overrides."""

    _assert_read_access(name, context, settings)
    spec = get_agent_tool(name, writes_enabled=False)
    if spec.kind != "read" or spec.handler is None:
        raise UnknownTool("Unknown or unavailable tool")
    command = spec.command_model.model_validate(arguments)
    with database.cursor() as cursor:
        result = _run_read_handler(spec, cursor, command, settings, context)
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

    _assert_read_access(name, context, settings)
    spec = get_agent_tool(
        name,
        writes_enabled=settings.agent_writes_enabled,
        write_tools=getattr(settings, "agent_write_tools", None),
    )
    required_tier(name)
    command = spec.command_model.model_validate(arguments)
    if spec.kind == "read":
        _assert_read_access(name, context, settings)
        if spec.handler is None:
            raise UnknownTool("Unknown or unavailable tool")
        with database.cursor() as cursor:
            result = _run_read_handler(spec, cursor, command, settings, context)
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
