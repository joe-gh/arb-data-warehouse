"""Closed Pydantic inputs for bounded read-only agent tools.

Field descriptions are part of the JSON schema the model sees, so they double
as the parameter documentation for every read tool.
"""

from typing import Optional, Annotated

from pydantic import BaseModel, ConfigDict, Field

STORE = "Store code such as S_032813 (resolve a store NAME with list_stores first)."
STYLE = "Product style code exactly as the catalog uses it, e.g. 246, 460510 or IS-WS203HV."


class ReadCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListStoresCommand(ReadCommand):
    pass


class ListStylesCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    q: str = Field(
        default="", max_length=100,
        description="Optional search text matched against style code and product name. Empty lists the first styles.",
    )
    active_only: bool = Field(
        default=True,
        description="True = only styles in the store's live FDM4 catalog; False also includes retired styles that still carry logo rows.",
    )
    assigned_only: bool = Field(
        default=True,
        description="True = only styles that already have logo rows; False = every catalog style (use this to find products with no logos yet).",
    )


class GetStyleCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    style: str = Field(min_length=1, max_length=100, description=STYLE)


class SearchDesignsCommand(ReadCommand):
    q: str = Field(
        default="", max_length=100,
        description="Search text matched against the design description, design id or logo code. Leave empty with a store to browse that store's designs.",
    )
    store: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional store code. With a store, results are the designs used by that store AND by the FDM4 customer that owns it (sister stores); store_uses tells them apart.",
    )


class GetDesignCommand(ReadCommand):
    design_id: str = Field(min_length=1, max_length=100, description="FDM4 design id, e.g. 4706 (from search_designs or a logo row).")


class GetAssignmentVocabCommand(ReadCommand):
    pass


class GetStoreSettingsCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)


class GetImportReportCommand(ReadCommand):
    store: Optional[str] = Field(default=None, max_length=100, description="Optional store code to limit the punch list to one store.")
    reason: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional reason code to filter on, e.g. no_color_code, no_design, no_art, orphaned_companion.",
    )
    limit: int = Field(default=100, ge=1, le=500, description="Rows per page (max 500).")
    offset: int = Field(default=0, ge=0, le=100_000, description="Rows to skip for paging.")


class GetAuditLogCommand(ReadCommand):
    store: Optional[str] = Field(default=None, max_length=100, description="Optional store code filter.")
    style: Optional[str] = Field(default=None, max_length=100, description="Optional style code filter.")
    actor: Optional[str] = Field(default=None, max_length=100, description="Optional login of the person (or process) who made the change.")
    action: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional action filter such as assignment_created, assignment_updated, assignment_deleted, sync_requested, sync_succeeded, ownership_enabled.",
    )
    before_id: Optional[int] = Field(default=None, ge=1, description="For paging: return entries older than this audit id (from the previous page).")
    limit: int = Field(default=50, ge=1, le=200, description="Entries per page (max 200), newest first.")


class ListPricingTiersCommand(ReadCommand):
    pass


class ListStorePricingTiersCommand(ReadCommand):
    pass


class FindSimilarStylesCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    style: str = Field(min_length=1, max_length=100, description="The source style whose logo set to match. " + STYLE)
    mode: str = Field(
        default="exact", pattern="^(exact|overlap)$",
        description="exact = styles whose logo set (design + scheme + position + placement) equals the source's; overlap = every style sharing at least one logo, most shared first.",
    )


class StoreLogoCoverageCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    unconfigured_only: bool = Field(
        default=True,
        description="True = only styles with at least one garment color that has no active logo; False = every live style with its counts.",
    )


class ListColorsCommand(ReadCommand):
    q: str = Field(default="", max_length=100, description="Optional search text matched against the garment color code or name.")
    cls: str = Field(
        default="", pattern="^(|light|dark|both)$",
        description="Optional class filter: light, dark or both (empty = all).",
    )
    needs_review: bool = Field(default=False, description="True = only colors whose light/dark class was guessed automatically and never confirmed by a person.")
    limit: int = Field(default=200, ge=1, le=500, description="Rows to return (max 500).")


class ListLogoNamesCommand(ReadCommand):
    store: Optional[str] = Field(
        default=None, max_length=100,
        description="Store code to list that store's logos with the names its shoppers see (store-specific name, else the shared default). Omit to search the shared names across all stores.",
    )
    q: str = Field(default="", max_length=100, description="Optional search text matched against the name, design id, color scheme, logo code or FDM4 description.")
    limit: int = Field(default=100, ge=1, le=200, description="Rows to return (max 200).")


class GetStockRulesCommand(ReadCommand):
    store: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional store code: limits brand rules to brands that store actually carries and style exceptions to its styles.",
    )
    q: str = Field(default="", max_length=100, description="Optional search text matched against brand name, mill code or style code.")
    limit: int = Field(default=200, ge=1, le=500, description="Max brands and max style exceptions to return (each, max 500).")


class ListPriceRulesCommand(ReadCommand):
    store: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional store code: only rules that can affect this store (rules aimed at every store, or naming it, and not excluding it).",
    )


class ListSyncBlocksCommand(ReadCommand):
    store: Optional[str] = Field(default=None, max_length=100, description="Optional store code to show only that store's freezes.")


class GetProductMixCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    limit: int = Field(default=200, ge=1, le=500, description="Max curated styles to list when the store is on a curated list (max 500).")


class ListDesignUsageCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    design_id: str = Field(min_length=1, max_length=100, description="FDM4 design id to look for on the store's logo rows.")
    color_scheme_id: Optional[str] = Field(default=None, max_length=100, description="Optional color scheme to narrow to (e.g. BK); null = every scheme.")


class GetProductLinkCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    style: str = Field(min_length=1, max_length=100, description=STYLE)


class GetSyncStatusCommand(ReadCommand):
    store: Optional[str] = Field(
        default=None, max_length=100,
        description="Optional store code: adds that store's logo-sync ownership, its freezes and its recent sync events. Omit for the warehouse pipeline alone.",
    )


class PreviewPriceRuleCommand(ReadCommand):
    rule_id: int = Field(ge=1, description="Rule id from list_price_rules.")
    sample_limit: int = Field(default=200, ge=1, le=1000, description="Maximum before and after price examples.")


class CheckPriceRulesCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    style: str = Field(min_length=1, max_length=100, description=STYLE)


class ListPriceRuleDimensionsCommand(ReadCommand):
    pass


class PreviewFillMissingColorsCommand(ReadCommand):
    store: str = Field(min_length=1, max_length=100, description=STORE)
    styles: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=50, description="Style codes to check, up to 50 per call.")


class GetStyleMixCommand(ReadCommand):
    style: str = Field(min_length=1, max_length=100, description=STYLE)
    store: Optional[str] = Field(default=None, min_length=1, max_length=100, description="Optional store code; null lists stores carrying the style.")
    limit: int = Field(default=100, ge=1, le=200, description="Maximum stores returned.")


class GetHealthOverviewCommand(ReadCommand):
    pass


class CatTreeCommand(ReadCommand):
    env: str = Field(min_length=3, max_length=8, description="Configured category snapshot environment.")
    limit: int = Field(default=200, ge=1, le=500, description="Maximum paths returned.")


class CatMappingStatusCommand(ReadCommand):
    env: str = Field(min_length=3, max_length=8, description="Configured category snapshot environment.")
    limit: int = Field(default=100, ge=1, le=200, description="Maximum undecided and empty rows returned each.")


class CatPlanCheckCommand(ReadCommand):
    env: str = Field(min_length=3, max_length=8, description="Configured category snapshot environment.")
    blog_ids: Optional[list[Annotated[int, Field(ge=1)]]] = Field(default=None, max_length=200, description="Store site numbers to check; null checks all imported snapshots.")
    limit: int = Field(default=100, ge=1, le=200, description="Maximum per-store rows and nested examples returned.")


class CatRunsCommand(ReadCommand):
    env: Optional[str] = Field(default=None, min_length=3, max_length=8, description="Optional category environment filter.")
    run_id: Optional[int] = Field(default=None, ge=1, description="Optional run id whose jobs to summarize.")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum runs and per-store jobs returned.")
