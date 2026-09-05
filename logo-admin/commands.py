"""Strict command models for transaction-safe logo/pricing mutations."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Dict, List, Literal, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class Command(BaseModel):
    """Base class shared by every mutation surface."""

    model_config = ConfigDict(extra="forbid")


class AssignmentTarget(Command):
    fdm4_store: str = Field(min_length=1, max_length=100, description="Store code such as S_032813.")
    product_style: str = Field(min_length=1, max_length=100, description="Product style code, e.g. 246.")
    garment_color_code: str = Field(min_length=1, max_length=100, description="FDM4 garment color code of the row, e.g. 0445 (get_style lists them).")
    position: int = Field(ge=1, le=3, description="Placement slot inside the choice: 1 = first logo, 2 and 3 = companion logos that require an active position 1.")
    option_row: int = Field(default=1, ge=1, le=999, description="Which selectable choice on this color (1 = the first row shoppers see).")


class SaveAssignmentCommand(AssignmentTarget):
    design_id: str = Field(min_length=1, max_length=100, description="FDM4 design id (from search_designs); validated against the warehouse.")
    logo_code: str = Field(min_length=1, max_length=100, description="Logo code for the design + scheme (get_design shows the codes on file).")
    color_scheme_id: str = Field(min_length=1, max_length=100, description="Color scheme of the artwork, e.g. BK or WH (get_design lists them).")
    location: str = Field(default="", max_length=200, description="Placement name from get_assignment_vocab, e.g. Left Chest. Reuse existing spellings.")
    optional: bool = Field(default=False, description="True = the shopper may leave this logo off.")
    background: str = Field(default="", max_length=200, description="Legacy background tag (lb-white / lb-black) or empty. Reference only.")
    cost_override: Optional[Decimal] = Field(default=None, description="Extra charge for this row in dollars; 0 makes it free; null keeps the automatic default cost.")
    sort_order: int = Field(default=0, ge=-2147483648, le=2147483647, description="Display order among the color's rows (lower first); 0 unless reordering.")
    image_url: str = Field(default="", max_length=2048, description="Public image shoppers see; leave empty to keep the design's artwork.")
    # None means an older/cached client omitted the field: preserve the stored
    # value.  An explicit empty string clears it.
    name_override: Optional[str] = Field(default=None, max_length=200, description="Exact name shown for this row; empty string clears it; null keeps the stored value.")
    expected_updated_at: Optional[datetime] = Field(default=None, description="Optimistic-concurrency check: the row's updated_at as last read, or null to skip.")
    active: bool = Field(default=True, description="False hides the row from the website after the next sync without deleting it.")


class DeactivateAssignmentCommand(AssignmentTarget):
    pass


class HardDeleteAssignmentCommand(AssignmentTarget):
    pass


class ColorTarget(Command):
    fdm4_store: str = Field(min_length=1, max_length=100, description="Store code such as S_032813.")
    product_style: str = Field(min_length=1, max_length=100, description="Product style code.")
    garment_color_code: str = Field(min_length=1, max_length=100, description="FDM4 garment color code whose rows are targeted.")


class DeactivateColorCommand(ColorTarget):
    pass


class HardDeleteColorCommand(ColorTarget):
    pass


class SetStyleActiveCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Store code.")
    style: str = Field(min_length=1, max_length=100, description="Product style code.")
    active: bool = Field(description="True = show every valid logo row of the style; False = hide them all (kept, not deleted).")


class ApplyToColorsCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Store code.")
    style: str = Field(min_length=1, max_length=100, description="Product style code.")
    garment_color_code: str = Field(min_length=1, max_length=100, description="Color of the SOURCE row to copy from.")
    position: int = Field(ge=1, le=3, description="Position of the source row.")
    option_row: int = Field(default=1, ge=1, le=999, description="Option row of the source row.")
    overwrite: bool = Field(default=False, description="False keeps colors that already have a row in that slot; True replaces them.")


class CopyStyleCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Store code (both styles must be in it).")
    source_style: str = Field(min_length=1, max_length=100, description="Style to copy logos FROM.")
    target_style: str = Field(min_length=1, max_length=100, description="Style to copy logos TO; only colors both styles share (same color code) receive rows.")
    overwrite: bool = Field(default=False, description="False keeps the target's existing rows; True replaces occupied slots.")


class UpdateStoreSettingsCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Store code.")
    enabled: bool = Field(description="Show this store's logos on its website after the next sync.")
    allows_none: bool = Field(description="Let shoppers choose 'No logo' at checkout.")


class SetStorePricingTierCommand(Command):
    fdm4_store: str = Field(min_length=1, max_length=100, description="Store code.")
    tier_name: str = Field(min_length=1, max_length=100, description="A tier_name from list_pricing_tiers; only fills prices FDM4 leaves blank.")
    note: str = Field(default="", max_length=500, description="Optional note shown next to the assignment.")


class DeleteStorePricingTierCommand(Command):
    fdm4_store: str = Field(min_length=1, max_length=100, description="Store code whose pricing level assignment is removed (blank prices fall back to retail).")


STORE_DESC = "Store code such as S_032813 (resolve a store NAME with list_stores first)."
STYLE_LIST_DESC = "Product style codes (1-50 per call, no duplicates). Split larger jobs into several calls."


class CopyStyleToManyCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    source_style: str = Field(min_length=1, max_length=100, description="Style whose logo setup is copied FROM (must already have logo rows).")
    target_styles: List[str] = Field(min_length=1, max_length=50, description="Styles to copy TO. " + STYLE_LIST_DESC + " Must not include the source.")
    color_match: Literal["exact", "like"] = Field(default="exact", description="exact = only garment colors the target shares with the source (same color code) receive rows; like = additionally map each remaining target color to a source color of the same light/dark class (list_colors shows classes).")
    mode: Literal["merge", "overwrite"] = Field(default="merge", description="merge = keep rows the target already has in a slot; overwrite = replace occupied slots with the source rows. Rows the source does not have are never removed.")


class PasteRow(Command):
    option_row: int = Field(default=1, ge=1, le=999, description="Which selectable choice this row belongs to (1 = first).")
    position: int = Field(ge=1, le=3, description="Placement slot inside the choice: 1 = first logo, 2 and 3 = companions that need an active position 1.")
    design_id: str = Field(min_length=1, max_length=100, description="FDM4 design id (from search_designs or get_style).")
    logo_code: str = Field(min_length=1, max_length=100, description="Logo code for the design + scheme.")
    color_scheme_id: str = Field(min_length=1, max_length=100, description="Artwork color scheme, e.g. BK or WH.")
    location: str = Field(default="", max_length=200, description="Placement name from get_assignment_vocab, e.g. Left Chest.")
    optional: bool = Field(default=False, description="True = the shopper may leave this logo off.")
    background: str = Field(default="", max_length=200, description="Legacy background tag or empty.")
    cost_override: Optional[Decimal] = Field(default=None, description="Extra charge in dollars; 0 = free; null = automatic default cost.")
    sort_order: int = Field(default=0, ge=-2147483648, le=2147483647, description="Display order among the color's rows (lower first).")
    image_url: str = Field(default="", max_length=2048, description="Public image shoppers see; empty keeps the design's artwork.")
    name_override: Optional[str] = Field(default=None, max_length=200, description="Exact name shown for this row, or null for the default name.")
    active: bool = Field(default=True, description="False stages the row hidden.")


class PasteLogoSetCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    styles: List[str] = Field(min_length=1, max_length=50, description="Styles that receive the rows. " + STYLE_LIST_DESC)
    color_scope: Literal["all", "match", "light", "dark"] = Field(default="all", description="Which garment colors of each style receive the rows: all live colors, match = only match_color, light / dark = colors of that class (list_colors).")
    match_color: Optional[str] = Field(default=None, max_length=100, description="Garment color code used when color_scope is match; null otherwise.")
    rows: List[PasteRow] = Field(min_length=1, max_length=30, description="The logo rows to place on every targeted color (1-30; option_row + position must be unique). Copy them from get_style when reproducing an existing setup.")
    overwrite: bool = Field(default=False, description="False skips slots that already hold a logo; True replaces them.")
    as_new_rows: bool = Field(default=False, description="True appends the rows as NEW choices after each color's last option row instead of using the given option_row numbers.")


class ReplaceDesignCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    from_design_id: str = Field(min_length=1, max_length=100, description="Design id currently on the logo rows (need not exist in FDM4 any more).")
    from_color_scheme_id: Optional[str] = Field(default=None, max_length=100, description="Only rows with this color scheme are replaced; null = every scheme of the design.")
    to_design_id: str = Field(min_length=1, max_length=100, description="Replacement design id; must belong to the store's FDM4 customer family.")
    to_color_scheme_id: str = Field(min_length=1, max_length=100, description="Color scheme every replaced row gets, e.g. BK.")
    to_logo_code: Optional[str] = Field(default=None, max_length=100, description="Logo code for the new design + scheme; null derives it when the design has exactly one art file for that scheme (otherwise the call is rejected and names the codes on file).")
    styles: List[str] = Field(min_length=1, max_length=50, description="Styles to change; get them from list_design_usage. " + STYLE_LIST_DESC)


class ReorderLogoRowsCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    style: str = Field(min_length=1, max_length=100, description="Product style code.")
    garment_color_code: str = Field(min_length=1, max_length=100, description="Color whose choices are being reordered (get_style lists its option rows).")
    option_rows: List[int] = Field(min_length=1, max_length=100, description="EVERY option row number of that color, in the order shoppers should see them (first = shown first). Must match the rows that exist.")
    apply_to: Literal["color", "style"] = Field(default="style", description="style = also rank every other color of the style by the same logos (what the app's drag-and-drop does); color = only this color.")


class SetStylesActiveCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    styles: List[str] = Field(min_length=1, max_length=50, description="Styles whose logo rows are shown or hidden. " + STYLE_LIST_DESC)
    active: bool = Field(description="True = show every valid logo row of each style; False = hide them all (kept, not deleted).")


class SetLogoNameCommand(Command):
    design_id: str = Field(min_length=1, max_length=100, description="FDM4 design id of the logo (list_logo_names / get_style show it).")
    color_scheme_id: str = Field(min_length=1, max_length=100, description="Color scheme of the logo, e.g. BK or GD.")
    name: str = Field(min_length=1, max_length=200, description="The name shoppers should see for this logo.")
    store: Optional[str] = Field(default=None, max_length=100, description="Store code to set a name that only this store sees; null changes the shared default name every store falls back to.")


class ClearLogoNameCommand(Command):
    design_id: str = Field(min_length=1, max_length=100, description="FDM4 design id of the logo.")
    color_scheme_id: str = Field(min_length=1, max_length=100, description="Color scheme of the logo.")
    store: str = Field(min_length=1, max_length=100, description="Store whose own name for this logo is removed so it shows the shared default again. The shared default itself cannot be removed.")


class SetColorClassCommand(Command):
    color_code: str = Field(min_length=1, max_length=100, description="FDM4 garment color code, e.g. 0082 (list_colors).")
    light_dark: Literal["light", "dark", "both"] = Field(description="Class that drives Bulk Apply and like-color copies: light, dark, or both.")


class SetStockOverrideCommand(Command):
    style_code: str = Field(min_length=1, max_length=100, description="Product style code (any store) whose stock behaviour is forced.")
    mode: Literal["fake", "real"] = Field(description="fake = always show in stock at 99,999 regardless of FDM4; real = use live FDM4 stock even if its brand rule says fake.")
    note: str = Field(default="", max_length=1000, description="Optional reason shown on the Fake Inventory page.")
    active: bool = Field(default=True, description="False keeps the exception on file but switched off.")


class RemoveStockOverrideCommand(Command):
    style_code: str = Field(min_length=1, max_length=100, description="Style whose stock exception is removed (the brand rule or default applies again).")


class SetBrandStockRuleCommand(Command):
    mill_code: str = Field(min_length=1, max_length=32, description="FDM4 mill (brand) code from get_stock_rules, e.g. 22 for Arborwear.")
    mode: Literal["real", "fake"] = Field(description="real = every style of the brand uses live FDM4 stock; fake = always in stock.")
    active: bool = Field(default=True, description="False keeps the rule on file but switched off (the automatic default applies).")


class RemoveBrandStockRuleCommand(Command):
    mill_code: str = Field(min_length=1, max_length=32, description="Brand whose rule is removed (the automatic default applies again).")


class SetSyncBlockCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Store code the freeze applies to.")
    styles: List[str] = Field(default=[], max_length=50, description="Style codes to freeze in this store (1-50). Empty list = freeze the whole store.")
    scope: Literal["full", "pricing"] = Field(default="full", description="Whole-store freezes only: full = the hourly update skips the store entirely; pricing = it still runs but never rewrites an existing variation's price. Style freezes are always full.")
    note: str = Field(default="", max_length=1000, description="Optional reason shown on the Sync Blocks page.")
    active: bool = Field(default=True, description="False keeps the block on file but switched off.")


class RemoveSyncBlockCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Store code of the freeze.")
    styles: List[str] = Field(default=[], max_length=50, description="Style codes whose freezes are removed; empty list = remove the whole-store freeze.")


class SetLogoCostCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    design_id: str = Field(min_length=1, max_length=100, description="FDM4 design id of the logo whose price changes (get_style / list_design_usage show it).")
    color_scheme_id: Optional[str] = Field(default=None, max_length=100, description="Only rows with this color scheme; null = every scheme of the design.")
    cost_override: Optional[Decimal] = Field(default=None, description="Shopper charge in dollars for every matching row; 0 makes the logo free; null removes the store's override so the logo's default cost applies again.")
    styles: List[str] = Field(min_length=1, max_length=50, description="Styles whose rows change; get them from list_design_usage. " + STYLE_LIST_DESC)


class SetStoreExtraCustomersCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    customers: List[str] = Field(default=[], max_length=20, description="FDM4 customer numbers (e.g. 002165) whose designs this store may also use, replacing the current list; empty list = none besides the store's own customer.")


class BulkApplyCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    logo_code: str = Field(min_length=1, max_length=100, description="Logo code of the variant to place, e.g. A9H (get_design / list_logo_names show codes).")
    color_scheme_id: str = Field(min_length=1, max_length=100, description="Color scheme of the variant, e.g. GD.")
    design_id: Optional[str] = Field(default=None, max_length=100, description="FDM4 design id when the logo code is shared by several designs of the customer (the error names the candidates); null when the code is unique.")
    location: str = Field(min_length=1, max_length=200, description="Placement name from get_assignment_vocab, e.g. Left Chest.")
    target: Literal["light_dark", "colors"] = Field(description="light_dark = every garment color of the chosen class across the store; colors = only the listed color codes.")
    color_class: Optional[Literal["light", "dark"]] = Field(default=None, description="Required when target is light_dark: which class of garment colors receives the logo (colors classed 'both' match either).")
    color_codes: List[str] = Field(default=[], max_length=50, description="Required when target is colors: garment color codes to place the logo on.")
    styles: List[str] = Field(default=[], max_length=50, description="Optional: limit to these styles (1-50). Empty = every live style in the store.")
    option_row: int = Field(default=1, ge=1, le=999, description="Which choice slot to write (1 = primary; 2/3 add the variant alongside an existing primary). Position is always 1.")
    cost_override: Optional[Decimal] = Field(default=None, description="Shopper charge in dollars for the placed rows; null keeps the automatic default cost.")
    overwrite: bool = Field(default=False, description="False skips colors that already have a logo in that slot; True replaces them.")


class SetLogoDefaultCostCommand(Command):
    logo_code: str = Field(min_length=1, max_length=100, description="Logo code, e.g. A9H (get_style rows / list_logo_names show it).")
    color_scheme_id: str = Field(min_length=1, max_length=100, description="Color scheme, e.g. GD. Default costs are per logo code + scheme and apply in EVERY store that has no row-level override.")
    cost: Decimal = Field(ge=0, description="Default shopper charge in dollars for this logo variant; 0 makes it free everywhere it has no override.")
    locked: bool = Field(default=True, description="True keeps the value through future VN/FDM4 cost imports.")


class SetPriceRuleActiveCommand(Command):
    rule_id: int = Field(ge=1, description="Price rule id from list_price_rules.")
    active: bool = Field(description="True includes the price impact in human confirmation and activates on apply; False switches it off.")


class DeletePriceRuleCommand(Command):
    rule_id: int = Field(ge=1, description="Price rule id from list_price_rules; the rule is removed entirely.")


class SetProductMixCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    mode: Literal["all", "list"] = Field(description="all = the store follows FDM4 completely; list = a curated list decides (seeded from the store's current mix when switching, then edited with add_mix_styles / remove_mix_styles).")
    note: str = Field(default="", max_length=1000, description="Optional note shown on the Product Mix page.")


class DisableProductMixCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Store whose product-mix override is switched off (it follows FDM4 again; the saved list is kept).")


class AddMixStylesCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    styles: List[str] = Field(min_length=1, max_length=50, description="Style codes to add to the store's curated list (all colors). " + STYLE_LIST_DESC)


class RemoveMixStylesCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    styles: List[str] = Field(min_length=1, max_length=50, description="Style codes to drop from the store's curated list (the products leave the store on the next update). " + STYLE_LIST_DESC)


class SavePriceRuleCommand(Command):
    rule_id: Optional[int] = Field(default=None, ge=1, description="Existing rule id to edit; null creates a new inactive rule unless active is true.")
    _reserved_rule_id: Optional[int] = PrivateAttr(default=None)
    name: str = Field(min_length=1, max_length=200, description="Label shown in the price-rule list.")
    active: bool = Field(default=False, description="True includes the price impact in human confirmation and activates on apply; false saves inactive.")
    priority: int = Field(default=100, ge=1, le=100000, description="Lower priorities run first; preserve the existing value when editing.")
    stackable: bool = Field(default=False, description="True lets later matching rules also apply.")
    stores: List[str] = Field(default=[], max_length=5000, description="Full list of store codes; empty clears this filter. Preserve existing filters when editing.")
    store_tiers: List[str] = Field(default=[], max_length=5000, description="Full list of pricing tier names; empty clears this filter. Preserve existing filters when editing.")
    styles: List[str] = Field(default=[], max_length=5000, description="Full list of style codes; empty clears this filter. Preserve existing filters when editing.")
    brands: List[str] = Field(default=[], max_length=5000, description="Full list of brand names; empty clears this filter. Preserve existing filters when editing.")
    categories: List[str] = Field(default=[], max_length=5000, description="Full list of category names; empty clears this filter. Preserve existing filters when editing.")
    excl_stores: List[str] = Field(default=[], max_length=5000, description="Full list of store codes; empty clears this filter. Preserve existing filters when editing.")
    excl_styles: List[str] = Field(default=[], max_length=5000, description="Full list of style codes; empty clears this filter. Preserve existing filters when editing.")
    excl_brands: List[str] = Field(default=[], max_length=5000, description="Full list of brand names; empty clears this filter. Preserve existing filters when editing.")
    excl_categories: List[str] = Field(default=[], max_length=5000, description="Full list of category names; empty clears this filter. Preserve existing filters when editing.")
    effect_type: str = Field(max_length=32, description="percent, flat, set_price, price_level or margin_over_cost.")
    effect_value: Optional[Decimal] = Field(default=None, description="Percent adjustment, flat dollars, set price, or cost multiplier; required except for price_level.")
    price_level_key: Optional[str] = Field(default=None, max_length=32, description="For price_level: msrp, corp1, corp2, corp3, wholesale, employee or base.")
    basis: str = Field(default="current", max_length=16, description="For percent/flat: current price or a price-level key; other effects use current.")
    rounding: str = Field(default="none", max_length=8, description="Price ending: none, 99, 95 or 00.")
    floor_price: Optional[Decimal] = Field(default=None, description="Optional minimum price, zero or greater.")
    ceiling_price: Optional[Decimal] = Field(default=None, description="Optional maximum price, at least the floor.")
    cap_at_msrp: bool = Field(default=False, description="Cap the final price at MSRP when available.")
    effective_from: Optional[str] = Field(default=None, max_length=32, description="First effective date, YYYY-MM-DD; null has no start date.")
    effective_until: Optional[str] = Field(default=None, max_length=32, description="Last effective date, YYYY-MM-DD; null has no end date.")
    note: str = Field(default="", max_length=2000, description="Operator note; empty clears the note.")


class FillMissingColorsEntry(Command):
    style: str = Field(min_length=1, max_length=100, description="Style whose own logos are copied.")
    source_color: str = Field(min_length=1, max_length=100, description="Source garment color chosen from preview_fill_missing_colors.")
    colors: Optional[List[str]] = Field(default=None, max_length=500, description="Target garment color codes; null fills all missing colors on this style.")


class FillMissingColorsCommand(Command):
    store: str = Field(min_length=1, max_length=100, description=STORE_DESC)
    entries: List[FillMissingColorsEntry] = Field(min_length=1, max_length=50, description="One source per style, at most 50 styles.")
    overwrite: bool = Field(default=False, description="False keeps occupied slots; true replaces slots on explicitly named target colors.")


class SetExternalMixStoreCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="Known store code to enrol as an external all-products store.")
    source: Literal["external", "square", "brightsites"] = Field(default="external", description="External storefront source, recorded in the enrolment note.")


class RemoveExternalMixStoreCommand(Command):
    store: str = Field(min_length=1, max_length=100, description="External store to return to its regular FDM4 catalog.")


class CatCommand(Command):
    env: Literal["dev", "prod"] = Field(description="Environment of the copy of live categories being edited.")
    _category_state: dict = PrivateAttr(default_factory=dict)


class CatCategoryTarget(Command):
    slug: Optional[str] = Field(default=None, min_length=1, max_length=200, description="Exact web address confirmed with cat_node_lookup.")
    path: Optional[str] = Field(default=None, min_length=1, max_length=2048, description="Exact full path confirmed with cat_node_lookup; supply slug or path, never both.")

    @model_validator(mode="after")
    def one_target(self):
        if (self.slug is None) == (self.path is None):
            raise ValueError("Supply exactly one category slug or path")
        return self


class CatDecision(Command):
    old_slug: str = Field(min_length=1, max_length=200)
    action: Literal["move", "keep", "delete"]
    target_slug: Optional[str] = Field(default=None, min_length=1, max_length=200)
    make_surviving: Optional[bool] = None
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "move" and not self.target_slug:
            raise ValueError("Move requires a confirmed target_slug")
        if self.action != "move" and (self.target_slug is not None or self.make_surviving):
            raise ValueError("Only move can have a target or a surviving category")
        return self


class CatDecideCommand(CatCommand):
    rows: List[CatDecision] = Field(min_length=1, max_length=200)
    allow_products: bool = Field(default=False, description="Explicit permission to delete a category that still holds products; otherwise deletion is refused.")


class CatUndoDecisionCommand(CatCommand):
    old_slug: str = Field(min_length=1, max_length=200)


class CatMakeSurvivingCommand(CatCommand):
    old_slug: str = Field(min_length=1, max_length=200)
    target_slug: str = Field(min_length=1, max_length=200)


class CatCreateCategoryCommand(CatCommand):
    name: str = Field(min_length=1, max_length=200)
    slug: Optional[str] = Field(default=None, min_length=1, max_length=200)
    parent: Optional[CatCategoryTarget] = Field(default=None, description="Confirmed parent; null creates a top-level category.")
    description: str = Field(default="", max_length=4000)
    position: Optional[int] = Field(default=None, ge=0, le=2000)


class CatRenameCategoryCommand(CatCommand):
    category: CatCategoryTarget
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    new_slug: Optional[str] = Field(default=None, min_length=1, max_length=200, description="New web address. A person checks redirects before applying to stores.")
    description: Optional[str] = Field(default=None, max_length=4000)


class CatMoveCategoryCommand(CatCommand):
    category: CatCategoryTarget
    parent: Optional[CatCategoryTarget] = Field(default=None, description="Move into this confirmed category; null moves to the top level.")
    position: Optional[int] = Field(default=None, ge=0, le=2000)


class CatDeleteCategoryCommand(CatCommand):
    category: CatCategoryTarget
    cascade: bool = Field(description="Explicitly allow deleting the categories below this one. Related decisions become undecided.")


class CatSetStoreOverrideCommand(CatCommand):
    blog_id: int = Field(ge=1)
    kind: Literal["rename", "hide", "store_only"]
    override_id: Optional[int] = Field(default=None, ge=1)
    category: Optional[CatCategoryTarget] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    slug: Optional[str] = Field(default=None, min_length=1, max_length=200)
    parent: Optional[CatCategoryTarget] = None
    include_descendants: bool = True
    sort_order: int = Field(default=0, ge=-2147483648, le=2147483647)


    @model_validator(mode="after")
    def valid_store_override(self):
        if self.kind == "store_only":
            if self.category is not None or self.name is None:
                raise ValueError("A store-only category needs a name, not a global category target")
        else:
            if self.category is None or self.slug is not None or self.parent is not None:
                raise ValueError("Rename/hide needs a category target; web addresses and parents are only for store-only categories")
            if self.kind == "rename" and self.name is None:
                raise ValueError("Rename needs the store's category name")
            if self.kind == "hide" and self.name is not None:
                raise ValueError("Hide does not rename the category")
        return self


class CatDeleteStoreOverrideCommand(CatCommand):
    override_id: int = Field(ge=1)


class CatAcceptUncategorizedCommand(CatCommand):
    skus: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=1000)


class CatUnacceptUncategorizedCommand(CatCommand):
    skus: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1, max_length=200)


class CatRuleSpec(Command):
    source: Union[Literal["all"], List[Annotated[str, Field(min_length=1, max_length=200)]]] = Field(default="all", description="All styles, or 1-200 old category slugs to select from.")
    field: Literal["name", "brand", "mill_code", "category", "sku"]
    op: Literal["equals", "prefix", "regex"]
    value: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def bounded_source(self):
        if isinstance(self.source, list) and not 1 <= len(self.source) <= 200:
            raise ValueError("Rule source must contain 1-200 slugs")
        return self

    def service_spec(self):
        return {"from": self.source, "field": self.field, "op": self.op, "value": self.value}


class CatSetRuleCommand(CatCommand):
    category: CatCategoryTarget
    spec: CatRuleSpec
    rule_id: Optional[int] = Field(default=None, ge=1)
    priority: int = Field(default=0, ge=-2147483648, le=2147483647)
    note: str = Field(default="", max_length=1000)


class CatDeleteRuleCommand(CatCommand):
    rule_id: int = Field(ge=1)


class CatAssignStylesCommand(CatCommand):
    category: CatCategoryTarget
    skus: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(min_length=1, max_length=200)
    mode: Literal["add", "keep_out"] = Field(description="Add styles to the category or keep them out.")
    note: str = Field(default="", max_length=1000)


class CatDeleteAssignmentCommand(CatCommand):
    assignment_id: int = Field(ge=1)


MutationCommand = Union[
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
    CopyStyleToManyCommand,
    PasteLogoSetCommand,
    ReplaceDesignCommand,
    ReorderLogoRowsCommand,
    SetStylesActiveCommand,
    SetLogoNameCommand,
    ClearLogoNameCommand,
    SetColorClassCommand,
    SetStockOverrideCommand,
    RemoveStockOverrideCommand,
    SetBrandStockRuleCommand,
    RemoveBrandStockRuleCommand,
    SetSyncBlockCommand,
    RemoveSyncBlockCommand,
    SetLogoCostCommand,
    SetStoreExtraCustomersCommand,
    BulkApplyCommand,
    SetLogoDefaultCostCommand,
    SetPriceRuleActiveCommand,
    DeletePriceRuleCommand,
    SetProductMixCommand,
    DisableProductMixCommand,
    AddMixStylesCommand,
    RemoveMixStylesCommand,
]


COMMAND_MODELS: Dict[str, Type[Command]] = {
    'cat_decide': CatDecideCommand,
    'cat_undo_decision': CatUndoDecisionCommand,
    'cat_make_surviving': CatMakeSurvivingCommand,
    'cat_create_category': CatCreateCategoryCommand,
    'cat_rename_category': CatRenameCategoryCommand,
    'cat_move_category': CatMoveCategoryCommand,
    'cat_delete_category': CatDeleteCategoryCommand,
    'cat_set_store_override': CatSetStoreOverrideCommand,
    'cat_delete_store_override': CatDeleteStoreOverrideCommand,
    'cat_accept_uncategorized': CatAcceptUncategorizedCommand,
    'cat_unaccept_uncategorized': CatUnacceptUncategorizedCommand,
    'cat_set_rule': CatSetRuleCommand,
    'cat_delete_rule': CatDeleteRuleCommand,
    'cat_assign_styles': CatAssignStylesCommand,
    'cat_delete_assignment': CatDeleteAssignmentCommand,

    "set_external_mix_store": SetExternalMixStoreCommand,
    "remove_external_mix_store": RemoveExternalMixStoreCommand,
    "save_price_rule": SavePriceRuleCommand,
    "fill_missing_colors": FillMissingColorsCommand,
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
    "copy_style_to_many": CopyStyleToManyCommand,
    "paste_logo_set": PasteLogoSetCommand,
    "replace_design": ReplaceDesignCommand,
    "reorder_logo_rows": ReorderLogoRowsCommand,
    "set_styles_active": SetStylesActiveCommand,
    "set_logo_name": SetLogoNameCommand,
    "clear_logo_name": ClearLogoNameCommand,
    "set_color_class": SetColorClassCommand,
    "set_stock_override": SetStockOverrideCommand,
    "remove_stock_override": RemoveStockOverrideCommand,
    "set_brand_stock_rule": SetBrandStockRuleCommand,
    "remove_brand_stock_rule": RemoveBrandStockRuleCommand,
    "set_sync_block": SetSyncBlockCommand,
    "remove_sync_block": RemoveSyncBlockCommand,
    "set_logo_cost": SetLogoCostCommand,
    "set_store_extra_customers": SetStoreExtraCustomersCommand,
    "bulk_apply": BulkApplyCommand,
    "set_logo_default_cost": SetLogoDefaultCostCommand,
    "set_price_rule_active": SetPriceRuleActiveCommand,
    "delete_price_rule": DeletePriceRuleCommand,
    "set_product_mix": SetProductMixCommand,
    "disable_product_mix": DisableProductMixCommand,
    "add_mix_styles": AddMixStylesCommand,
    "remove_mix_styles": RemoveMixStylesCommand,
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
