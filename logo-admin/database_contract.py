"""Fail-closed database authority checks for write-enabled startup.

The application authenticates as ``logo_admin``, but direct grants are not the
whole authority of a PostgreSQL login: PUBLIC, role memberships, object
ownership, database/schema privileges, and SECURITY DEFINER functions all
contribute.  These checks intentionally use PostgreSQL's effective-privilege
functions one privilege at a time so a broad or inherited grant cannot hide
behind an apparently narrow role script.
"""

from collections.abc import Iterable, Mapping
import hashlib
import hmac
import re
from typing import Any

from snapshots import validate_restore_schema


EXPECTED_ROLE = "logo_admin"
EXPECTED_ROLE_SETTINGS = frozenset({
    "statement_timeout=30s",
    "lock_timeout=5s",
    "idle_in_transaction_session_timeout=30s",
    "search_path=logo,woo,fdm4,pg_catalog",
})
EXPECTED_EFFECTIVE_SETTINGS = {
    "statement_timeout": "30s",
    "lock_timeout": "5s",
    "idle_in_transaction_session_timeout": "30s",
    "search_path": "logo,woo,fdm4,pg_catalog",
}
EXPECTED_PRUNE_SOURCE_SHA256 = (
    "378f41091ba89926fda1364b2c99bd2901b8e01ddde9c8fa52f97b3f3f8c2269"
)
EXPECTED_AUDIT_SOURCE_SHA256 = (
    "0ffa5f09bd205a694dfe288347074a85195458d1eb3ae74577d4723343d7e58b"
)
RESTORE_COLUMN_CONTRACTS = {
    "logo.assignment": {
        "fdm4_store": ("text", False, None),
        "product_style": ("text", False, None),
        "garment_color_code": ("text", False, None),
        "position": ("smallint", False, "1"),
        "design_id": ("text", False, None),
        "logo_code": ("text", False, "''"),
        "color_scheme_id": ("text", False, "''"),
        "location": ("text", False, "''"),
        "optional": ("boolean", False, "false"),
        "background": ("text", False, "''"),
        "cost_override": ("numeric(12,2)", True, None),
        "sort_order": ("integer", False, "0"),
        "image_url": ("text", False, "''"),
        "active": ("boolean", False, "true"),
        "updated_by": ("text", False, "'seed'"),
        "updated_at": ("timestamp with time zone", False, "now"),
        "option_row": ("integer", False, "1"),
        "name_override": ("text", True, None),
        "row_version": ("bigint", False, None),
        "catalog_id": ("text", True, None),
    },
    "logo.store_settings": {
        "fdm4_store": ("text", False, None),
        "enabled": ("boolean", False, "true"),
        "allows_none": ("boolean", False, "false"),
        "updated_by": ("text", False, "''"),
        "updated_at": ("timestamp with time zone", False, "now"),
        # format_type() spells the column text[]; the catalog default is
        # '{}'::text[], which _normalized_sql_expression reduces to '{}'[]
        # (the ::text cast is stripped, the array brackets are kept).
        "extra_customers": ("text[]", False, "'{}'[]"),
    },
    "woo.store_pricing_tier": {
        "fdm4_store": ("text", False, None),
        "tier_name": ("text", False, None),
        "note": ("text", False, "''"),
        "updated_at": ("timestamp with time zone", False, "now"),
    },
    # Single-row tables the assistant edits through the same exact undo path.
    "logo.display_name": {
        "design_id": ("text", False, None),
        "color_scheme_id": ("text", False, None),
        "name": ("text", False, None),
        "source": ("text", False, "'manual'"),
        "locked": ("boolean", False, "false"),
        "uses": ("integer", False, "0"),
        "fdm4_description": ("text", True, None),
        "updated_at": ("timestamp with time zone", False, "now"),
        "updated_by": ("text", True, None),
        "fdm4_store": ("text", False, "''"),
    },
    "logo.color_class": {
        "color_code": ("text", False, None),
        "color_name": ("text", False, None),
        "light_dark": ("text", False, None),
        "source": ("text", False, "'ai'"),
        "confidence": ("numeric", True, None),
        "updated_at": ("timestamp with time zone", False, "now"),
        "updated_by": ("text", False, "''"),
    },
    "woo.stock_override": {
        "style_code": ("text", False, None),
        "mode": ("text", False, None),
        "note": ("text", False, "''"),
        "active": ("boolean", False, "true"),
        "updated_by": ("text", False, "''"),
        "updated_at": ("timestamp with time zone", False, "now"),
    },
    "woo.brand_stock_rule": {
        "mill_code": ("text", False, None),
        "brand_name": ("text", False, "''"),
        "mode": ("text", False, None),
        "note": ("text", False, "''"),
        "active": ("boolean", False, "true"),
        "updated_by": ("text", False, "''"),
        "updated_at": ("timestamp with time zone", False, "now"),
    },
    "woo.sync_exclusion": {
        "fdm4_store": ("text", False, None),
        "style_code": ("text", False, "''"),
        "note": ("text", False, "''"),
        "active": ("boolean", False, "true"),
        "created_at": ("timestamp with time zone", False, "now"),
        "updated_at": ("timestamp with time zone", False, "now"),
        "updated_by": ("text", False, "''"),
        "scope": ("text", False, "'full'"),
    },
    # Logo default costs, price rules and product mix (assistant tools).
    "logo.default_cost": {
        "logo_code": ('text', False, None),
        "color_scheme_id": ('text', False, None),
        "cost": ('numeric(12,2)', False, None),
        "source": ('text', False, "'vn-reference'"),
        "locked": ('boolean', False, 'false'),
        "updated_by": ('text', False, "'vn-import-20260731'"),
        "updated_at": ('timestamp with time zone', False, 'now'),
    },
    "woo.price_rule": {
        "rule_id": ('bigint', False, "nextval'woo.price_rule_rule_id_seq'::regclass"),
        "name": ('text', False, None),
        "active": ('boolean', False, 'false'),
        "priority": ('integer', False, '100'),
        "stackable": ('boolean', False, 'false'),
        "stores": ('text[]', True, None),
        "store_tiers": ('text[]', True, None),
        "styles": ('text[]', True, None),
        "brands": ('text[]', True, None),
        "categories": ('text[]', True, None),
        "effect_type": ('text', False, None),
        "effect_value": ('numeric(12,4)', True, None),
        "price_level_key": ('text', True, None),
        "floor_price": ('numeric(12,4)', True, None),
        "effective_from": ('date', True, None),
        "effective_until": ('date', True, None),
        "note": ('text', False, "''"),
        "created_at": ('timestamp with time zone', False, 'now'),
        "updated_at": ('timestamp with time zone', False, 'now'),
        "updated_by": ('text', False, "''"),
        "last_previewed_at": ('timestamp with time zone', True, None),
        "excl_stores": ('text[]', True, None),
        "excl_styles": ('text[]', True, None),
        "excl_brands": ('text[]', True, None),
        "excl_categories": ('text[]', True, None),
        "basis": ('text', False, "'current'"),
        "rounding": ('text', False, "'none'"),
        "ceiling_price": ('numeric(12,4)', True, None),
        "cap_at_msrp": ('boolean', False, 'false'),
    },
    "woo.store_mix_item": {
        "fdm4_store": ('text', False, None),
        "style_code": ('text', False, None),
        "colors": ('text[]', True, None),
        "size_excludes": ('jsonb', True, None),
        "source": ('text', False, "'manual'"),
        "added_by": ('text', False, "''"),
        "added_at": ('timestamp with time zone', False, 'now'),
        "updated_by": ('text', False, "''"),
        "updated_at": ('timestamp with time zone', False, 'now'),
    },
    "woo.store_mix_store": {
        "fdm4_store": ('text', False, None),
        "mode": ('text', False, "'list'"),
        "active": ('boolean', False, 'true'),
        "note": ('text', False, "''"),
        "created_by": ('text', False, "''"),
        "created_at": ('timestamp with time zone', False, 'now'),
        "updated_by": ('text', False, "''"),
        "updated_at": ('timestamp with time zone', False, 'now'),
        "imported_at": ('timestamp with time zone', True, None),
    },
}
AGENT_COLUMN_CONTRACTS = {
    "logo.agent_chat_session": {
        "id": ("uuid", False, None),
        "user_login": ("text", False, None),
        "title": ("text", False, "''"),
        "active_turn_id": ("uuid", True, None),
        "turn_lease_expires_at": ("timestamp with time zone", True, None),
        "created_at": ("timestamp with time zone", False, "now"),
        "updated_at": ("timestamp with time zone", False, "now"),
        "expires_at": ("timestamp with time zone", False, None),
    },
    "logo.agent_chat_message": {
        "id": ("uuid", False, None),
        "session_id": ("uuid", False, None),
        "user_login": ("text", False, None),
        "turn_id": ("uuid", False, None),
        "role": ("text", False, None),
        "status": ("text", False, None),
        "content": ("text", False, "''"),
        "replay_items": ("jsonb", False, "'[]'"),
        "created_at": ("timestamp with time zone", False, "now"),
    },
    "logo.agent_usage_daily": {
        "user_login": ("text", False, None),
        "usage_day": ("date", False, None),
        "requests": ("integer", False, "0"),
        "reserved_tokens": ("bigint", False, "0"),
        "input_tokens": ("bigint", False, "0"),
        "output_tokens": ("bigint", False, "0"),
        "updated_at": ("timestamp with time zone", False, "now"),
    },
    "logo.agent_usage_monthly": {
        "usage_month": ("date", False, None),
        "requests": ("integer", False, "0"),
        "reserved_tokens": ("bigint", False, "0"),
        "input_tokens": ("bigint", False, "0"),
        "output_tokens": ("bigint", False, "0"),
        "updated_at": ("timestamp with time zone", False, "now"),
    },
    "logo.agent_rate_window": {
        "user_login": ("text", False, None),
        "window_start": ("timestamp with time zone", False, None),
        "requests": ("integer", False, "0"),
    },
    "logo.agent_quota_reservation": {
        "id": ("uuid", False, None),
        "user_login": ("text", False, None),
        "usage_day": ("date", False, None),
        "usage_month": ("date", False, None),
        "window_start": ("timestamp with time zone", False, None),
        "reserved_tokens": ("bigint", False, None),
        "status": ("text", False, "'reserved'"),
        "input_tokens": ("bigint", False, "0"),
        "output_tokens": ("bigint", False, "0"),
        "created_at": ("timestamp with time zone", False, "now"),
        "provider_started_at": ("timestamp with time zone", True, None),
        "expires_at": (
            "timestamp with time zone",
            False,
            frozenset({"now+'15minutes'", "now+'00:15:00'"}),
        ),
        "finalized_at": ("timestamp with time zone", True, None),
    },
    "logo.agent_change_set": {
        "id": ("uuid", False, None),
        "session_id": ("uuid", False, None),
        "user_login": ("text", False, None),
        "origin": ("text", False, "'chat'"),
        "status": ("text", False, "'pending'"),
        "revision": ("integer", False, "0"),
        "preview_hash": ("text", True, None),
        "preview_diff": ("jsonb", False, "'{}'"),
        "affected_scopes": ("jsonb", False, "'[]'"),
        "contains_hard_delete": ("boolean", False, "false"),
        "created_at": ("timestamp with time zone", False, "now"),
        "updated_at": ("timestamp with time zone", False, "now"),
        "expires_at": ("timestamp with time zone", False, None),
        "applied_at": ("timestamp with time zone", True, None),
        "undone_at": ("timestamp with time zone", True, None),
    },
    "logo.agent_change_set_item": {
        "id": ("uuid", False, None),
        "change_set_id": ("uuid", False, None),
        "user_login": ("text", False, None),
        "call_id": ("text", False, None),
        "tool_name": ("text", False, None),
        "arguments": ("jsonb", False, None),
        "sort_order": ("integer", False, None),
        "created_at": ("timestamp with time zone", False, "now"),
    },
    "logo.agent_action_journal": {
        "id": ("uuid", False, None),
        "change_set_id": ("uuid", False, None),
        "user_login": ("text", False, None),
        "event_type": ("text", False, None),
        "actor": ("text", False, None),
        "preview_hash": ("text", False, None),
        "before_state": ("jsonb", False, None),
        "after_state": ("jsonb", False, None),
        "created_at": ("timestamp with time zone", False, "now"),
    },
    "logo.agent_spreadsheet_job": {
        "id": ("uuid", False, None),
        "session_id": ("uuid", False, None),
        "user_login": ("text", False, None),
        "storage_key": ("uuid", False, None),
        "change_set_id": ("uuid", True, None),
        "original_name": ("text", False, None),
        "media_type": ("text", False, None),
        "byte_size": ("bigint", False, None),
        "sha256": ("text", False, None),
        "format_name": ("text", False, None),
        "status": ("text", False, None),
        "mapping_revision": ("integer", False, "1"),
        "mapping_hash": ("text", False, None),
        "mapping": ("jsonb", False, None),
        "rejected_rows": ("jsonb", False, "'[]'"),
        "created_at": ("timestamp with time zone", False, "now"),
        "expires_at": ("timestamp with time zone", False, None),
    },
}
EXPECTED_TRIGGERS = frozenset({
    (
        "logo.assignment", "logo_assignment_audit", 29, "O",
        "logo", "audit_row",
    ),
    (
        "logo.assignment", "assignment_feed_stamp", 23, "O",
        "logo", "assignment_feed_stamp",
    ),
    (
        "logo.assignment", "assignment_feed_tombstone", 9, "O",
        "logo", "assignment_feed_tombstone",
    ),
    (
        "logo.store_settings", "logo_store_settings_audit", 29, "O",
        "logo", "audit_row",
    ),
    (
        "logo.color_class", "logo_color_class_audit", 29, "O",
        "logo", "audit_row",
    ),
    (
        "logo.display_name", "logo_display_name_audit", 29, "O",
        "logo", "audit_display_name_row",
    ),
    (
        "woo.price_rule", "price_rule_audit", 29, "O",
        "woo", "audit_price_rule_row",
    ),
    (
        "woo.store_mix_store", "store_mix_store_audit", 29, "O",
        "woo", "audit_store_mix_row",
    ),
    (
        "woo.store_mix_item", "store_mix_item_audit", 29, "O",
        "woo", "audit_store_mix_row",
    ),
})
# Schemas where every unlisted relation is readable and nothing else:
# warehouse facts (woo/fdm4) and the read-only pim/curated surfaces that
# sql/logo_admin_role.sql grants with GRANT SELECT ON ALL TABLES.
WAREHOUSE_READ_SCHEMAS = frozenset({"woo", "fdm4", "pim", "curated"})
EXPECTED_PRIMARY_KEYS = {
    "logo.assignment": (
        "assignment_pkey",
        (
            "fdm4_store",
            "product_style",
            "garment_color_code",
            "option_row",
            "position",
        ),
    ),
    "logo.store_settings": ("store_settings_pkey", ("fdm4_store",)),
    "woo.store_pricing_tier": (
        "store_pricing_tier_pkey",
        ("fdm4_store",),
    ),
    "logo.display_name": (
        "display_name_pkey",
        ("design_id", "color_scheme_id", "fdm4_store"),
    ),
    "logo.color_class": ("color_class_pkey", ("color_code",)),
    "woo.stock_override": ("stock_override_pkey", ("style_code",)),
    "woo.brand_stock_rule": ("brand_stock_rule_pkey", ("mill_code",)),
    "woo.sync_exclusion": (
        "sync_exclusion_pkey",
        ("fdm4_store", "style_code"),
    ),
    "logo.default_cost": ("default_cost_pkey", ("logo_code", "color_scheme_id")),
    "woo.price_rule": ("price_rule_pkey", ("rule_id",)),
    "woo.store_mix_store": ("store_mix_store_pkey", ("fdm4_store",)),
    "woo.store_mix_item": ("store_mix_item_pkey", ("fdm4_store", "style_code")),
}
EXPECTED_CHECKS = {
    (
        "logo.assignment",
        "logo_assignment_position_check",
        ("position",),
    ): "position>=1andposition<=3",
    (
        "logo.assignment",
        "logo_assignment_option_row_check",
        ("option_row",),
    ): "option_row>=1andoption_row<=999",
    (
        "logo.assignment",
        "assignment_option_row_check",
        ("option_row",),
    ): "option_row>=1",
    (
        "logo.color_class",
        "color_class_light_dark_check",
        ("light_dark",),
    ): "light_dark=anyarray['light','dark','both']",
    (
        "logo.color_class",
        "color_class_source_check",
        ("source",),
    ): "source=anyarray['ai','manual']",
    (
        "woo.stock_override",
        "stock_override_mode_check",
        ("mode",),
    ): "mode=anyarray['fake','real']",
    (
        "woo.brand_stock_rule",
        "brand_stock_rule_mode_check",
        ("mode",),
    ): "mode=anyarray['real','fake']",
    (
        "woo.sync_exclusion",
        "sync_exclusion_scope_check",
        ("scope",),
    ): "scope=anyarray['full','pricing']",
    ("woo.price_rule", "price_rule_basis_chk", ('basis',)): "basis=anyarray['current','msrp','corp1','corp2','corp3','wholesale','employee','base']",
    ("woo.price_rule", "price_rule_effect_type_check", ('effect_type',)): "effect_type=anyarray['percent','flat','set_price','price_level','margin_over_cost']",
    ("woo.price_rule", "price_rule_rounding_chk", ('rounding',)): "rounding=anyarray['none','99','95','00']",
    ("woo.store_mix_item", "store_mix_item_source_check", ('source',)): "source=anyarray['import','manual']",
    ("woo.store_mix_store", "store_mix_store_mode_check", ('mode',)): "mode=anyarray['all','list']",
}
EXPECTED_FOREIGN_KEYS = {
    (
        "woo.store_pricing_tier",
        "store_pricing_tier_tier_name_fkey",
        ("tier_name",),
        "woo.pricing_tier",
        ("tier_name",),
        "a",
        "a",
        "s",
    ),
}
AGENT_PRIMARY_KEYS = {
    "logo.agent_chat_session": (
        "agent_chat_session_pkey", ("id",),
    ),
    "logo.agent_chat_message": (
        "agent_chat_message_pkey", ("id",),
    ),
    "logo.agent_usage_daily": (
        "agent_usage_daily_pkey", ("user_login", "usage_day"),
    ),
    "logo.agent_usage_monthly": (
        "agent_usage_monthly_pkey", ("usage_month",),
    ),
    "logo.agent_rate_window": (
        "agent_rate_window_pkey", ("user_login", "window_start"),
    ),
    "logo.agent_quota_reservation": (
        "agent_quota_reservation_pkey", ("id",),
    ),
    "logo.agent_change_set": (
        "agent_change_set_pkey", ("id",),
    ),
    "logo.agent_change_set_item": (
        "agent_change_set_item_pkey", ("id",),
    ),
    "logo.agent_action_journal": (
        "agent_action_journal_pkey", ("id",),
    ),
    "logo.agent_spreadsheet_job": (
        "agent_spreadsheet_job_pkey", ("id",),
    ),
}
AGENT_UNIQUE_CONSTRAINTS = frozenset({
    (
        "logo.agent_chat_session",
        "agent_chat_session_id_user_login_key",
        ("id", "user_login"),
    ),
    (
        "logo.agent_chat_message",
        "agent_chat_message_session_id_turn_id_role_key",
        ("session_id", "turn_id", "role"),
    ),
    (
        "logo.agent_change_set",
        "agent_change_set_id_user_login_key",
        ("id", "user_login"),
    ),
    (
        "logo.agent_change_set_item",
        "agent_change_set_item_change_set_id_call_id_key",
        ("change_set_id", "call_id"),
    ),
    (
        "logo.agent_change_set_item",
        "agent_change_set_item_change_set_id_sort_order_key",
        ("change_set_id", "sort_order"),
    ),
    (
        "logo.agent_action_journal",
        "agent_action_journal_change_set_id_event_type_key",
        ("change_set_id", "event_type"),
    ),
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_storage_key_key",
        ("storage_key",),
    ),
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_change_set_id_key",
        ("change_set_id",),
    ),
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_id_user_login_key",
        ("id", "user_login"),
    ),
})
AGENT_FOREIGN_KEYS = frozenset({
    (
        "logo.agent_chat_message",
        "agent_chat_message_session_id_user_login_fkey",
        ("session_id", "user_login"),
        "logo.agent_chat_session",
        ("id", "user_login"),
        "a", "c", "s",
    ),
    (
        "logo.agent_change_set",
        "agent_change_set_session_id_user_login_fkey",
        ("session_id", "user_login"),
        "logo.agent_chat_session",
        ("id", "user_login"),
        "a", "r", "s",
    ),
    (
        "logo.agent_change_set_item",
        "agent_change_set_item_change_set_id_user_login_fkey",
        ("change_set_id", "user_login"),
        "logo.agent_change_set",
        ("id", "user_login"),
        "a", "c", "s",
    ),
    (
        "logo.agent_action_journal",
        "agent_action_journal_change_set_id_user_login_fkey",
        ("change_set_id", "user_login"),
        "logo.agent_change_set",
        ("id", "user_login"),
        "a", "r", "s",
    ),
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_session_id_user_login_fkey",
        ("session_id", "user_login"),
        "logo.agent_chat_session",
        ("id", "user_login"),
        "a", "r", "s",
    ),
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_change_set_id_user_login_fkey",
        ("change_set_id", "user_login"),
        "logo.agent_change_set",
        ("id", "user_login"),
        "a", "r", "s",
    ),
})
AGENT_CHECKS = {
    ("logo.agent_chat_message", "agent_chat_message_role_check", ("role",)):
        "role=anyarray['user','assistant']",
    ("logo.agent_chat_message", "agent_chat_message_status_check", ("status",)):
        "status=anyarray['complete','failed','cancelled']",
    (
        "logo.agent_chat_message",
        "agent_chat_message_replay_items_check",
        ("replay_items",),
    ): "jsonb_typeofreplay_items='array'",
    ("logo.agent_usage_daily", "agent_usage_daily_requests_check", ("requests",)):
        "requests>=0",
    (
        "logo.agent_usage_daily",
        "agent_usage_daily_reserved_tokens_check",
        ("reserved_tokens",),
    ): "reserved_tokens>=0",
    (
        "logo.agent_usage_daily",
        "agent_usage_daily_input_tokens_check",
        ("input_tokens",),
    ): "input_tokens>=0",
    (
        "logo.agent_usage_daily",
        "agent_usage_daily_output_tokens_check",
        ("output_tokens",),
    ): "output_tokens>=0",
    (
        "logo.agent_usage_monthly",
        "agent_usage_monthly_requests_check",
        ("requests",),
    ): "requests>=0",
    (
        "logo.agent_usage_monthly",
        "agent_usage_monthly_reserved_tokens_check",
        ("reserved_tokens",),
    ): "reserved_tokens>=0",
    (
        "logo.agent_usage_monthly",
        "agent_usage_monthly_input_tokens_check",
        ("input_tokens",),
    ): "input_tokens>=0",
    (
        "logo.agent_usage_monthly",
        "agent_usage_monthly_output_tokens_check",
        ("output_tokens",),
    ): "output_tokens>=0",
    (
        "logo.agent_usage_monthly",
        "agent_usage_monthly_usage_month_check",
        ("usage_month",),
    ): "date_trunc'month',usage_month=usage_month",
    ("logo.agent_rate_window", "agent_rate_window_requests_check", ("requests",)):
        "requests>=0",
    (
        "logo.agent_quota_reservation",
        "agent_quota_reservation_reserved_tokens_check",
        ("reserved_tokens",),
    ): "reserved_tokens>0",
    (
        "logo.agent_quota_reservation",
        "agent_quota_reservation_status_check",
        ("status",),
    ): "status=anyarray['reserved','reconciled','retained']",
    (
        "logo.agent_quota_reservation",
        "agent_quota_reservation_input_tokens_check",
        ("input_tokens",),
    ): "input_tokens>=0",
    (
        "logo.agent_quota_reservation",
        "agent_quota_reservation_output_tokens_check",
        ("output_tokens",),
    ): "output_tokens>=0",
    ("logo.agent_change_set", "agent_change_set_origin_check", ("origin",)):
        "origin=anyarray['chat','spreadsheet']",
    ("logo.agent_change_set", "agent_change_set_status_check", ("status",)):
        "status=anyarray['pending','applied','discarded','undone']",
    (
        "logo.agent_change_set",
        "agent_change_set_revision_check",
        ("revision",),
    ): "revision>=0",
    (
        "logo.agent_change_set",
        "agent_change_set_preview_hash_check",
        ("preview_hash",),
    ): "preview_hashisnullorpreview_hash~'^[0-9a-f]{64}$'",
    (
        "logo.agent_change_set_item",
        "agent_change_set_item_arguments_check",
        ("arguments",),
    ): "jsonb_typeofarguments='object'",
    (
        "logo.agent_change_set_item",
        "agent_change_set_item_sort_order_check",
        ("sort_order",),
    ): "sort_order>=0",
    (
        "logo.agent_action_journal",
        "agent_action_journal_event_type_check",
        ("event_type",),
    ): "event_type=anyarray['apply','undo']",
    (
        "logo.agent_action_journal",
        "agent_action_journal_preview_hash_check",
        ("preview_hash",),
    ): "preview_hash~'^[0-9a-f]{64}$'",
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_byte_size_check",
        ("byte_size",),
    ): "byte_size>=0",
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_sha256_check",
        ("sha256",),
    ): "sha256~'^[0-9a-f]{64}$'",
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_format_name_check",
        ("format_name",),
    ): "format_name=anyarray['csv','xlsx']",
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_status_check",
        ("status",),
    ): (
        "status=anyarray['mapping_processing','mapping_pending',"
        "'mapping_confirmed','staged','rejected','expired']"
    ),
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_mapping_revision_check",
        ("mapping_revision",),
    ): "mapping_revision>=1",
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_job_mapping_hash_check",
        ("mapping_hash",),
    ): "mapping_hash~'^[0-9a-f]{64}$'",
}
AGENT_EXPLICIT_INDEXES = {
    (
        "logo.agent_chat_session",
        "agent_chat_session_owner_updated_idx",
    ): (("user_login", "updated_at"), (0, 3), None),
    (
        "logo.agent_chat_message",
        "agent_chat_message_owner_session_idx",
    ): (("user_login", "session_id", "created_at", "id"), (0, 0, 3, 3), None),
    (
        "logo.agent_quota_reservation",
        "agent_quota_reservation_owner_created_idx",
    ): (("user_login", "created_at"), (0, 3), None),
    (
        "logo.agent_quota_reservation",
        "agent_quota_reservation_stale_idx",
    ): (("expires_at", "id"), (0, 0), "status='reserved'"),
    (
        "logo.agent_change_set",
        "agent_change_set_owner_status_idx",
    ): (("user_login", "status", "updated_at"), (0, 0, 3), None),
    (
        "logo.agent_action_journal",
        "agent_action_journal_owner_idx",
    ): (("user_login", "created_at"), (0, 3), None),
    (
        "logo.agent_spreadsheet_job",
        "agent_spreadsheet_owner_status_idx",
    ): (("user_login", "status", "created_at"), (0, 0, 3), None),
}

CRUD_ALLOWED = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
APPEND_ALLOWED = frozenset({"SELECT", "INSERT"})
CRU_ALLOWED = frozenset({"SELECT", "INSERT", "UPDATE"})
READ_ALLOWED = frozenset({"SELECT"})
TABLE_POLICIES = {
    # Current Warehouse Operations write surface. Keep this list in lockstep
    # with sql/logo_admin_role.sql and the SQL write preflight.
    "logo.assignment": CRUD_ALLOWED,
    "logo.art_record": READ_ALLOWED,
    "logo.store_settings": CRUD_ALLOWED,
    "logo.placement_vocab": CRUD_ALLOWED,
    "logo.color_class": CRUD_ALLOWED,
    "logo.bulk_batch": CRUD_ALLOWED,
    "logo.bulk_batch_row": CRUD_ALLOWED,
    "logo.style_color_order": CRUD_ALLOWED,
    "logo.default_cost": CRUD_ALLOWED,
    "logo.design_ipc": CRUD_ALLOWED,
    "logo.display_name": CRUD_ALLOWED,
    "logo.admin_session": CRUD_ALLOWED,
    "logo.image_import": CRU_ALLOWED,
    "logo.import_report": APPEND_ALLOWED,
    "logo.audit_log": APPEND_ALLOWED,
    "logo.assignment_tombstone": CRUD_ALLOWED,
    "woo.price_rule": CRUD_ALLOWED,
    "woo.price_rule_audit": APPEND_ALLOWED,
    "woo.pricing_tier": CRUD_ALLOWED,
    "woo.store_pricing_tier": CRUD_ALLOWED,
    "woo.sync_exclusion": CRUD_ALLOWED,
    "woo.store_mix_store": CRUD_ALLOWED,
    "woo.store_mix_item": CRUD_ALLOWED,
    "woo.store_mix_candidate": READ_ALLOWED,
    "woo.store_mix_audit": APPEND_ALLOWED,
    "woo.feed_consumer": CRUD_ALLOWED,
    "woo.app_flag": CRUD_ALLOWED,
    "woo.brand_stock_rule": CRUD_ALLOWED,
    "woo.stock_override": CRUD_ALLOWED,
    "woo.virtual_catalog_store": CRUD_ALLOWED,
    # Category editor (catmgr): snapshots are app-owned; audit is append-only.
    "catmgr.snapshot": CRUD_ALLOWED,
    "catmgr.wp_term": CRUD_ALLOWED,
    "catmgr.wp_term_product": CRUD_ALLOWED,
    "catmgr.node": CRUD_ALLOWED,
    "catmgr.node_store_override": CRUD_ALLOWED,
    "catmgr.slug_map": CRUD_ALLOWED,
    "catmgr.assignment_rule": CRUD_ALLOWED,
    "catmgr.product_assignment": CRUD_ALLOWED,
    "catmgr.uncategorized_ack": CRUD_ALLOWED,
    "catmgr.run": CRUD_ALLOWED,
    "catmgr.run_job": CRUD_ALLOWED,
    "catmgr.job_snapshot": CRUD_ALLOWED,
    "catmgr.redirect": CRUD_ALLOWED,
    "catmgr.audit_log": APPEND_ALLOWED,
    # Agent-local state and immutable action history.
    "logo.agent_chat_session": CRUD_ALLOWED,
    "logo.agent_chat_message": CRUD_ALLOWED,
    "logo.agent_change_set": CRUD_ALLOWED,
    "logo.agent_change_set_item": CRUD_ALLOWED,
    "logo.agent_spreadsheet_job": CRUD_ALLOWED,
    "logo.agent_usage_daily": CRUD_ALLOWED,
    "logo.agent_usage_monthly": CRUD_ALLOWED,
    "logo.agent_rate_window": CRUD_ALLOWED,
    "logo.agent_quota_reservation": CRUD_ALLOWED,
    "logo.agent_action_journal": APPEND_ALLOWED,
}
OPTIONAL_TABLE_POLICIES: dict[str, frozenset[str]] = {}
SEQUENCE_POLICIES = {
    "logo.audit_log_id_seq": frozenset({"USAGE", "SELECT"}),
    "logo.import_report_id_seq": frozenset({"USAGE", "SELECT"}),
    "woo.price_rule_rule_id_seq": frozenset({"USAGE"}),
    "woo.price_rule_audit_id_seq": frozenset({"USAGE"}),
    "woo.store_mix_audit_id_seq": frozenset({"USAGE"}),
    "logo.assignment_version_seq": frozenset({"USAGE"}),
    "catmgr.assignment_rule_rule_id_seq": frozenset({"USAGE"}),
    "catmgr.audit_log_id_seq": frozenset({"USAGE"}),
    "catmgr.node_node_id_seq": frozenset({"USAGE"}),
    "catmgr.node_store_override_override_id_seq": frozenset({"USAGE"}),
    "catmgr.product_assignment_id_seq": frozenset({"USAGE"}),
    "catmgr.redirect_id_seq": frozenset({"USAGE"}),
    "catmgr.run_job_job_id_seq": frozenset({"USAGE"}),
    "catmgr.run_run_id_seq": frozenset({"USAGE"}),
}
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")
ALLOWED_EXECUTABLE_SECURITY_DEFINERS = frozenset({
    ("logo", "prune_agent_history", ""),
})
REQUIRED_EXECUTABLE_SECURITY_DEFINERS = frozenset({
    ("logo", "prune_agent_history", ""),
})
ALLOWED_EXECUTABLE_ROUTINES = frozenset({
    ("logo", "prune_agent_history", "", "f"),
    ("logo", "repull_display_name", "text, boolean", "f"),
    (
        "woo", "eval_price_rules",
        "text, text, text, text, numeric, jsonb, numeric, date, bigint[], bigint[]",
        "f",
    ),
})
REQUIRED_EXECUTABLE_ROUTINES = frozenset({
    ("logo", "prune_agent_history", "", "f"),
    (
        "woo", "eval_price_rules",
        "text, text, text, text, numeric, jsonb, numeric, date, bigint[], bigint[]",
        "f",
    ),
})
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
# Objects installed by an extension (pg_depend deptype 'e') carry the
# extension's own ACLs -- pg_stat_statements grants SELECT on its views and
# EXECUTE on its functions to PUBLIC -- and sit outside this policy, so every
# effective-privilege enumeration skips them.  Nothing else is skipped: the app
# schemas keep their exact allowlists and PUBLIC stays forbidden everywhere
# else.  sql/diagnostics/agent-write-preflight.sql applies the same predicate.
_EXTENSION_OWNED_RELATION_SQL = """EXISTS (
       SELECT 1
         FROM pg_depend AS dependency
        WHERE dependency.classid = 'pg_class'::regclass
          AND dependency.objid = relation.oid
          AND dependency.refclassid = 'pg_extension'::regclass
          AND dependency.deptype = 'e'
   )"""
_EXTENSION_OWNED_ROUTINE_SQL = """EXISTS (
       SELECT 1
         FROM pg_depend AS dependency
        WHERE dependency.classid = 'pg_proc'::regclass
          AND dependency.objid = procedure.oid
          AND dependency.refclassid = 'pg_extension'::regclass
          AND dependency.deptype = 'e'
   )"""
_TABLE_PRIVILEGE_SQL = f"""
WITH table_privilege(privilege_name) AS (
    VALUES
        ('SELECT'),
        ('INSERT'),
        ('UPDATE'),
        ('DELETE'),
        ('TRUNCATE'),
        ('REFERENCES'),
        ('TRIGGER')
)
SELECT format('%I.%I', namespace.nspname, relation.relname) AS table_name,
       table_privilege.privilege_name,
       has_table_privilege(
           current_user,
           relation.oid,
           table_privilege.privilege_name
       ) AS allowed
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
 CROSS JOIN table_privilege
 WHERE namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
   AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
   AND NOT {_EXTENSION_OWNED_RELATION_SQL}
 ORDER BY namespace.nspname, relation.relname, table_privilege.privilege_name
"""

_COLUMN_PRIVILEGE_SQL = f"""
WITH column_privilege(privilege_name) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
)
SELECT format('%I.%I', namespace.nspname, relation.relname) AS table_name,
       attribute.attname AS column_name,
       column_privilege.privilege_name,
       has_column_privilege(
           current_user,
           relation.oid,
           attribute.attnum,
           column_privilege.privilege_name
       ) AS allowed
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  JOIN pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
   AND attribute.attnum > 0
   AND NOT attribute.attisdropped
 CROSS JOIN column_privilege
 WHERE namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
   AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
   AND NOT {_EXTENSION_OWNED_RELATION_SQL}
 ORDER BY namespace.nspname, relation.relname, attribute.attnum,
          column_privilege.privilege_name
"""


def _expect(
    row: Mapping[str, Any] | None,
    key: str,
    expected: Any,
    label: str,
) -> None:
    actual = None if row is None else row.get(key)
    if actual != expected:
        raise RuntimeError(
            f"unsafe write-enabled database contract: {label}; "
            f"expected={expected!r}, actual={actual!r}"
        )


def _owner_is_acceptable(row: Mapping[str, Any] | None) -> bool:
    """Apply the deliberately relaxed (temporary) object-ownership rule.

    An object owner is acceptable when it is the database owner
    (``pg_database.datdba``) OR a superuser (``pg_roles.rolsuper``).  On
    production ``arb_warehouse`` is owned by ``etl_writer`` while every table
    and function in logo/woo/fdm4/catmgr/curated/pim is owned by ``postgres``,
    so a strict database-owner match can never hold there.  Arbitrary roles
    are still rejected, and a row that carries no ownership evidence fails
    closed.  ``sql/logo_admin_role.sql`` and
    ``sql/diagnostics/agent-write-preflight.sql`` apply the same rule.
    """

    if row is None:
        return False
    if bool(row.get("owner_is_superuser")):
        return True
    if "owned_by_database_owner" in row:
        return bool(row["owned_by_database_owner"])
    owner_name = row.get("owner_name")
    return owner_name is not None and owner_name == row.get("database_owner")


def _expect_acceptable_owner(
    row: Mapping[str, Any] | None,
    label: str,
) -> None:
    if _owner_is_acceptable(row):
        return
    owner_name = None if row is None else row.get("owner_name")
    database_owner = None if row is None else row.get("database_owner")
    raise RuntimeError(
        f"unsafe write-enabled database contract: {label}; "
        f"owner={owner_name!r} must be the database owner "
        f"({database_owner!r}) or a superuser"
    )


def _expected_table_privileges() -> dict[tuple[str, str], bool]:
    expected: dict[tuple[str, str], bool] = {}
    for table_name, allowed_privileges in TABLE_POLICIES.items():
        for privilege in TABLE_PRIVILEGES:
            expected[(table_name, privilege)] = privilege in allowed_privileges
    return expected


def _expected_unlisted_privilege(table_name: str, privilege: str) -> bool:
    """Return the exact policy for relations outside the write allowlist."""

    schema_name, separator, _relation_name = table_name.partition(".")
    return (
        bool(separator)
        and schema_name in WAREHOUSE_READ_SCHEMAS
        and privilege == "SELECT"
    )


def _assert_table_privileges(rows: Iterable[Mapping[str, Any]]) -> None:
    expected = _expected_table_privileges()
    actual: dict[tuple[str, str], bool] = {}
    for row in rows:
        key = (str(row["table_name"]), str(row["privilege_name"]))
        if key in actual:
            raise RuntimeError(
                "unsafe write-enabled database contract: duplicate table "
                f"privilege result for {key[0]} {key[1]}"
            )
        actual[key] = bool(row["allowed"])

    missing = sorted(set(expected) - set(actual))
    if missing:
        raise RuntimeError(
            "unsafe write-enabled database contract: incomplete table "
            f"privilege inventory; missing={missing}"
        )

    present_tables = {table_name for table_name, _privilege in actual}
    optional_expected = {
        (table_name, privilege): privilege in allowed_privileges
        for table_name, allowed_privileges in OPTIONAL_TABLE_POLICIES.items()
        if table_name in present_tables
        for privilege in TABLE_PRIVILEGES
    }
    expected.update(optional_expected)
    mismatches = sorted(
        (table_name, privilege, expected_value, actual[(table_name, privilege)])
        for (table_name, privilege), expected_value in expected.items()
        if actual.get((table_name, privilege)) != expected_value
    )
    unexpected_privileges = sorted(
        (table_name, privilege, expected_value, allowed)
        for (table_name, privilege), allowed in actual.items()
        if table_name not in TABLE_POLICIES
        and table_name not in OPTIONAL_TABLE_POLICIES
        for expected_value in (
            _expected_unlisted_privilege(table_name, privilege),
        )
        if allowed != expected_value
    )
    if mismatches or unexpected_privileges:
        raise RuntimeError(
            "unsafe write-enabled database contract: effective table "
            f"privilege mismatch {mismatches}; "
            f"unexpected_privileges={unexpected_privileges}"
        )

    incomplete_tables = sorted(
        table_name
        for table_name in present_tables
        if {
            privilege
            for candidate_table, privilege in actual
            if candidate_table == table_name
        } != set(TABLE_PRIVILEGES)
    )
    if incomplete_tables:
        missing = sorted(set(expected) - set(actual))
        raise RuntimeError(
            "unsafe write-enabled database contract: incomplete table "
            f"privilege inventory; tables={incomplete_tables}, missing={missing}"
        )


def _assert_column_privileges(rows: Iterable[Mapping[str, Any]]) -> None:
    mismatches = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        table_name = str(row["table_name"])
        column_name = str(row["column_name"])
        privilege = str(row["privilege_name"])
        key = (table_name, column_name, privilege)
        if key in seen:
            raise RuntimeError(
                "unsafe write-enabled database contract: duplicate column "
                f"privilege result for {key}"
            )
        seen.add(key)
        policy = TABLE_POLICIES.get(
            table_name,
            OPTIONAL_TABLE_POLICIES.get(table_name),
        )
        expected = (
            privilege in policy
            if policy is not None
            else _expected_unlisted_privilege(table_name, privilege)
        )
        actual = bool(row["allowed"])
        if actual != expected:
            mismatches.append((table_name, column_name, privilege, expected, actual))
    if mismatches:
        raise RuntimeError(
            "unsafe write-enabled database contract: effective column "
            f"privilege mismatch {sorted(mismatches)}"
        )


def _validate_role_authority(cursor) -> None:
    cursor.execute(
        """
        SELECT role.rolname AS role_name,
               session_user AS session_role_name,
               role.rolcanlogin AS can_login,
               role.rolinherit AS inherits,
               role.rolsuper AS superuser,
               role.rolcreatedb AS can_create_database,
               role.rolcreaterole AS can_create_role,
               role.rolreplication AS replication,
               role.rolbypassrls AS bypass_rls,
               role.rolconnlimit AS connection_limit,
               role.rolconfig AS role_settings
          FROM pg_roles AS role
         WHERE role.rolname = current_user
        """
    )
    role = cursor.fetchone()
    _expect(role, "role_name", EXPECTED_ROLE, "unexpected login role")
    _expect(
        role,
        "session_role_name",
        EXPECTED_ROLE,
        "session role must be logo_admin (SET ROLE is not allowed)",
    )
    _expect(role, "can_login", True, "logo_admin must remain a login")
    _expect(role, "connection_limit", 12, "logo_admin connection limit drift")
    for key, label in (
        ("inherits", "logo_admin must be NOINHERIT"),
        ("superuser", "logo_admin must not be superuser"),
        ("can_create_database", "logo_admin must not create databases"),
        ("can_create_role", "logo_admin must not create roles"),
        ("replication", "logo_admin must not replicate"),
        ("bypass_rls", "logo_admin must not bypass row security"),
    ):
        _expect(role, key, False, label)
    role_settings = frozenset(
        "".join(str(setting).split())
        for setting in (role.get("role_settings") if role else None) or []
    )
    if role_settings != EXPECTED_ROLE_SETTINGS:
        raise RuntimeError(
            "unsafe write-enabled database contract: role settings drift; "
            f"expected={sorted(EXPECTED_ROLE_SETTINGS)}, "
            f"actual={sorted(role_settings)}"
        )

    cursor.execute(
        """
        SELECT count(*)::integer AS database_role_setting_count
          FROM pg_db_role_setting AS setting
          JOIN pg_roles AS role ON role.oid = setting.setrole
          JOIN pg_database AS database ON database.oid = setting.setdatabase
         WHERE role.rolname = current_user
           AND database.datname = current_database()
        """
    )
    _expect(
        cursor.fetchone(),
        "database_role_setting_count",
        0,
        "database-specific role settings are forbidden",
    )

    cursor.execute(
        """
        SELECT current_setting('statement_timeout') AS statement_timeout,
               current_setting('lock_timeout') AS lock_timeout,
               current_setting(
                   'idle_in_transaction_session_timeout'
               ) AS idle_in_transaction_session_timeout,
               current_setting('search_path') AS search_path
        """
    )
    effective_settings = cursor.fetchone()
    actual_effective_settings = {
        setting_name: "".join(str(effective_settings[setting_name]).split())
        for setting_name in EXPECTED_EFFECTIVE_SETTINGS
    }
    if actual_effective_settings != EXPECTED_EFFECTIVE_SETTINGS:
        raise RuntimeError(
            "unsafe write-enabled database contract: effective session "
            f"settings drift; expected={EXPECTED_EFFECTIVE_SETTINGS}, "
            f"actual={actual_effective_settings}"
        )

    cursor.execute(
        """
        SELECT count(*)::integer AS membership_count
          FROM pg_auth_members AS membership
          JOIN pg_roles AS member_role
            ON member_role.oid = membership.member
         WHERE member_role.rolname = current_user
        """
    )
    _expect(
        cursor.fetchone(),
        "membership_count",
        0,
        "logo_admin must have no SET ROLE memberships",
    )

    cursor.execute(
        """
        SELECT has_database_privilege(
                   current_user, current_database(), 'CONNECT'
               ) AS can_connect,
               has_database_privilege(
                   current_user, current_database(), 'CREATE'
               ) AS can_create,
               has_database_privilege(
                   current_user, current_database(), 'TEMPORARY'
               ) AS can_create_temporary
        """
    )
    database_privileges = cursor.fetchone()
    _expect(database_privileges, "can_connect", True, "database CONNECT missing")
    _expect(database_privileges, "can_create", False, "database CREATE granted")
    _expect(
        database_privileges,
        "can_create_temporary",
        False,
        "database TEMPORARY granted",
    )

    cursor.execute(
        """
        SELECT schema_name,
               has_schema_privilege(
                   current_user, schema_name, 'USAGE'
               ) AS can_use,
               has_schema_privilege(
                   current_user, schema_name, 'CREATE'
               ) AS can_create
          FROM (VALUES ('logo'), ('woo'), ('fdm4')) AS required(schema_name)
         ORDER BY schema_name
        """
    )
    schema_rows = {str(row["schema_name"]): row for row in cursor.fetchall()}
    if set(schema_rows) != {"logo", "woo", "fdm4"}:
        raise RuntimeError(
            "unsafe write-enabled database contract: incomplete schema "
            f"privilege inventory {sorted(schema_rows)}"
        )
    for schema_name, row in schema_rows.items():
        _expect(row, "can_use", True, f"{schema_name} schema USAGE missing")
        _expect(row, "can_create", False, f"{schema_name} schema CREATE granted")

    cursor.execute(
        """
        SELECT has_schema_privilege(
                   current_user, 'public', 'CREATE'
               ) AS can_create
        """
    )
    _expect(
        cursor.fetchone(),
        "can_create",
        False,
        "public schema CREATE granted",
    )

    cursor.execute(
        """
        SELECT namespace.nspname AS schema_name,
               has_schema_privilege(
                   current_user, namespace.oid, 'CREATE'
               ) AS can_create
          FROM pg_namespace AS namespace
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
         ORDER BY namespace.nspname
        """
    )
    create_schemas = sorted(
        str(row["schema_name"])
        for row in cursor.fetchall()
        if bool(row["can_create"])
    )
    if create_schemas:
        raise RuntimeError(
            "unsafe write-enabled database contract: schema CREATE granted "
            f"for {create_schemas}"
        )

    cursor.execute(
        """
        SELECT (
                   SELECT count(*)
                     FROM pg_namespace AS namespace
                    WHERE namespace.nspowner = (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    )
                      AND namespace.nspname <> 'information_schema'
                      AND namespace.nspname !~ '^pg_'
               )::integer AS owned_schemas,
               (
                   SELECT count(*)
                     FROM pg_class AS relation
                     JOIN pg_namespace AS namespace
                       ON namespace.oid = relation.relnamespace
                    WHERE relation.relowner = (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    )
                      AND namespace.nspname <> 'information_schema'
                      AND namespace.nspname !~ '^pg_'
               )::integer AS owned_relations,
               (
                   SELECT count(*)
                     FROM pg_proc AS procedure
                     JOIN pg_namespace AS namespace
                       ON namespace.oid = procedure.pronamespace
                    WHERE procedure.proowner = (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    )
                      AND namespace.nspname <> 'information_schema'
                      AND namespace.nspname !~ '^pg_'
               )::integer AS owned_functions
        """
    )
    ownership = cursor.fetchone()
    for key, label in (
        ("owned_schemas", "schemas"),
        ("owned_relations", "relations"),
        ("owned_functions", "functions"),
    ):
        _expect(
            ownership,
            key,
            0,
            f"logo_admin must not own user {label}",
        )


def _validate_table_authority(cursor) -> None:
    cursor.execute(_TABLE_PRIVILEGE_SQL)
    _assert_table_privileges(cursor.fetchall())
    cursor.execute(_COLUMN_PRIVILEGE_SQL)
    _assert_column_privileges(cursor.fetchall())

    cursor.execute(
        f"""
        WITH table_privilege(privilege_name) AS (
            VALUES
                ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
        )
        SELECT count(*)::integer AS public_privilege_count
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         CROSS JOIN table_privilege
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
           AND NOT {_EXTENSION_OWNED_RELATION_SQL}
           AND has_table_privilege(
               'public', relation.oid, table_privilege.privilege_name
           )
        """
    )
    _expect(
        cursor.fetchone(),
        "public_privilege_count",
        0,
        "PUBLIC table privileges are forbidden",
    )
    cursor.execute(
        f"""
        WITH column_privilege(privilege_name) AS (
            VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
        )
        SELECT count(*)::integer AS public_privilege_count
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_attribute AS attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND NOT attribute.attisdropped
         CROSS JOIN column_privilege
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
           AND NOT {_EXTENSION_OWNED_RELATION_SQL}
           AND has_column_privilege(
               'public', relation.oid, attribute.attnum,
               column_privilege.privilege_name
           )
        """
    )
    _expect(
        cursor.fetchone(),
        "public_privilege_count",
        0,
        "PUBLIC column privileges are forbidden",
    )

    # A direct WITH GRANT OPTION is unnecessary authority even if the base
    # privilege itself belongs in the matrix above. Memberships are forbidden,
    # so inspecting this role's direct table grants is complete.
    cursor.execute(
        """
        SELECT count(*)::integer AS grant_option_count
          FROM information_schema.role_table_grants
         WHERE grantee = current_user
           AND is_grantable = 'YES'
           AND table_schema <> 'information_schema'
           AND table_schema !~ '^pg_'
        """,
    )
    _expect(
        cursor.fetchone(),
        "grant_option_count",
        0,
        "table privilege WITH GRANT OPTION is forbidden",
    )
    cursor.execute(
        """
        SELECT count(*)::integer AS grant_option_count
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_attribute AS attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND NOT attribute.attisdropped
         CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
           AND privilege.grantee = (
               SELECT oid FROM pg_roles WHERE rolname = current_user
           )
           AND privilege.is_grantable
        """
    )
    _expect(
        cursor.fetchone(),
        "grant_option_count",
        0,
        "column privilege WITH GRANT OPTION is forbidden",
    )


def _assert_write_relation_shapes(rows: Iterable[Mapping[str, Any]]) -> None:
    relations = {str(row["table_name"]): row for row in rows}
    missing = sorted(set(TABLE_POLICIES) - set(relations))
    unsafe = sorted(
        (
            table_name,
            row["relation_kind"],
            row["persistence"],
            bool(row["is_partition"]),
            row.get("owner_name"),
            bool(row["owned_by_database_owner"]),
            bool(row.get("owner_is_superuser")),
        )
        for table_name, row in relations.items()
        if row["relation_kind"] != "r"
        or row["persistence"] != "p"
        or bool(row["is_partition"])
        or not _owner_is_acceptable(row)
    )
    if missing or unsafe:
        raise RuntimeError(
            "unsafe write-enabled database contract: writable relation shape "
            "drift (owner must be the database owner or a superuser); "
            f"missing={missing}, unsafe={unsafe}"
        )


def _assert_restore_column_contract(
    rows: Iterable[Mapping[str, Any]],
) -> None:
    actual_columns: dict[
        str, dict[str, tuple[Any, ...]]
    ] = {}
    for row in rows:
        actual_columns.setdefault(str(row["table_name"]), {})[
            str(row["column_name"])
        ] = (
            int(row["ordinal_position"]),
            str(row["formatted_type"]),
            bool(row["nullable"]),
            str(row["generated_kind"]),
            str(row["identity_kind"]),
            (
                None
                if row.get("collation_name") is None
                else str(row["collation_name"])
            ),
            (
                None
                if row.get("default_expression") is None
                else _normalized_sql_expression(row["default_expression"])
            ),
        )
    expected_columns = {
        table_name: {
            column_name: (
                ordinal_position,
                formatted_type,
                nullable,
                "",
                "",
                # Arrays of a collatable element type carry the element
                # collation, so text[] columns report "default" like text.
                "default" if formatted_type in ("text", "text[]") else None,
                (
                    None
                    if default is None
                    else _normalized_sql_expression(default)
                ),
            )
            for ordinal_position, (
                column_name,
                (formatted_type, nullable, default),
            ) in enumerate(columns.items(), start=1)
        }
        for table_name, columns in RESTORE_COLUMN_CONTRACTS.items()
    }
    if actual_columns != expected_columns:
        raise RuntimeError(
            "unsafe write-enabled database contract: exact-undo column "
            f"metadata drift; expected={expected_columns}, actual={actual_columns}"
        )


def _normalized_sql_expression(value: Any) -> str:
    normalized = re.sub(
        r"::(?:timestamp\s+(?:with|without)\s+time\s+zone|"
        r"character\s+varying|smallint|integer|bigint|text|date|jsonb|"
        r"interval|boolean|uuid|numeric)\b",
        "",
        str(value or "").lower(),
    )
    # Whitespace, parentheses, and identifier quotes are presentation only
    # (pg_get_expr quotes keyword columns such as "position").
    return re.sub(r'[\s()"]+', "", normalized)


def _normalized_check_expression(value: Any) -> str:
    return _normalized_sql_expression(value)


def _expected_agent_default(value: Any) -> str | frozenset[str] | None:
    if isinstance(value, frozenset):
        return frozenset(_normalized_sql_expression(item) for item in value)
    if value is None:
        return None
    return _normalized_sql_expression(value)


def _assert_agent_relation_signatures(
    rows: Iterable[Mapping[str, Any]],
) -> None:
    inventory = {str(row["table_name"]): row for row in rows}
    if set(inventory) != set(AGENT_COLUMN_CONTRACTS):
        raise RuntimeError(
            "unsafe write-enabled database contract: agent relation inventory "
            f"drift; expected={sorted(AGENT_COLUMN_CONTRACTS)}, "
            f"actual={sorted(inventory)}"
        )
    unsafe = []
    for table_name, row in inventory.items():
        if (
            row["relation_kind"] != "r"
            or row["persistence"] != "p"
            or bool(row["is_partition"])
            or not _owner_is_acceptable(row)
            or row["access_method"] != "heap"
            or row.get("relation_options") is not None
            or row["replica_identity"] != "d"
            or int(row["tablespace_oid"]) != 0
        ):
            unsafe.append(dict(row))
    if unsafe:
        raise RuntimeError(
            "unsafe write-enabled database contract: agent relation metadata "
            f"drift; unsafe={unsafe}"
        )


def _assert_agent_column_signatures(
    rows: Iterable[Mapping[str, Any]],
) -> None:
    actual: dict[str, dict[str, tuple[Any, ...]]] = {}
    for row in rows:
        actual.setdefault(str(row["table_name"]), {})[
            str(row["column_name"])
        ] = (
            int(row["ordinal_position"]),
            str(row["formatted_type"]),
            bool(row["nullable"]),
            str(row["generated_kind"]),
            str(row["identity_kind"]),
            None if row.get("collation_name") is None else str(
                row["collation_name"]
            ),
            (
                None
                if row.get("default_expression") is None
                else _normalized_sql_expression(row["default_expression"])
            ),
        )
    expected: dict[str, dict[str, tuple[Any, ...]]] = {}
    for table_name, columns in AGENT_COLUMN_CONTRACTS.items():
        expected[table_name] = {}
        for ordinal_position, (
            column_name,
            (formatted_type, nullable, default),
        ) in enumerate(columns.items(), start=1):
            expected[table_name][column_name] = (
                ordinal_position,
                formatted_type,
                nullable,
                "",
                "",
                "default" if formatted_type == "text" else None,
                _expected_agent_default(default),
            )

    mismatches = []
    for table_name in sorted(set(expected) | set(actual)):
        for column_name in sorted(
            set(expected.get(table_name, {})) | set(actual.get(table_name, {}))
        ):
            expected_value = expected.get(table_name, {}).get(column_name)
            actual_value = actual.get(table_name, {}).get(column_name)
            if expected_value is not None and isinstance(
                expected_value[-1], frozenset
            ):
                if (
                    actual_value is None
                    or actual_value[:-1] != expected_value[:-1]
                    or actual_value[-1] not in expected_value[-1]
                ):
                    mismatches.append(
                        (table_name, column_name, expected_value, actual_value)
                    )
            elif actual_value != expected_value:
                mismatches.append(
                    (table_name, column_name, expected_value, actual_value)
                )
    if mismatches:
        raise RuntimeError(
            "unsafe write-enabled database contract: agent column signature "
            f"drift; mismatches={mismatches}"
        )


def _assert_agent_constraint_signatures(
    rows: Iterable[Mapping[str, Any]],
) -> None:
    primary_keys = set()
    unique_constraints = set()
    foreign_keys = set()
    checks = {}
    unexpected = []
    unsafe_metadata = []
    for row in rows:
        table_name = str(row["table_name"])
        constraint_name = str(row["constraint_name"])
        constraint_type = str(row["constraint_type"])
        key_columns = tuple(str(value) for value in row.get("key_columns") or ())
        if (
            bool(row["deferrable"])
            or bool(row["initially_deferred"])
            or not bool(row["validated"])
            # PostgreSQL marks index-backed and foreign-key constraints
            # NO INHERIT; the flag is only a policy signal on CHECKs.
            or (constraint_type == "c" and bool(row["no_inherit"]))
        ):
            unsafe_metadata.append((table_name, constraint_name))
        descriptor = (table_name, constraint_name, key_columns)
        if constraint_type == "p":
            primary_keys.add(descriptor)
        elif constraint_type == "u":
            unique_constraints.add(descriptor)
        elif constraint_type == "c":
            checks[descriptor] = _normalized_check_expression(
                row.get("check_expression")
            )
        elif constraint_type == "f":
            foreign_keys.add((
                *descriptor,
                str(row.get("referenced_table") or ""),
                tuple(
                    str(value)
                    for value in row.get("referenced_columns") or ()
                ),
                str(row.get("update_action") or ""),
                str(row.get("delete_action") or ""),
                str(row.get("match_type") or ""),
            ))
        else:
            unexpected.append((table_name, constraint_name, constraint_type))

    expected_primary_keys = {
        (table_name, constraint_name, key_columns)
        for table_name, (constraint_name, key_columns)
        in AGENT_PRIMARY_KEYS.items()
    }
    if (
        primary_keys != expected_primary_keys
        or unique_constraints != set(AGENT_UNIQUE_CONSTRAINTS)
        or foreign_keys != set(AGENT_FOREIGN_KEYS)
        or checks != AGENT_CHECKS
        or unexpected
        or unsafe_metadata
    ):
        raise RuntimeError(
            "unsafe write-enabled database contract: agent constraint "
            "signature drift; "
            f"primary_keys={sorted(primary_keys)}, "
            f"unique={sorted(unique_constraints)}, "
            f"foreign_keys={sorted(foreign_keys)}, checks={checks}, "
            f"unexpected={unexpected}, unsafe_metadata={unsafe_metadata}"
        )


def _expected_agent_indexes() -> dict[tuple[str, str], tuple[Any, ...]]:
    expected = {}
    for table_name, (index_name, key_columns) in AGENT_PRIMARY_KEYS.items():
        expected[(table_name, index_name)] = (
            True, True, key_columns, (0,) * len(key_columns), None
        )
    for table_name, index_name, key_columns in AGENT_UNIQUE_CONSTRAINTS:
        expected[(table_name, index_name)] = (
            True, False, key_columns, (0,) * len(key_columns), None
        )
    for key, (key_columns, options, predicate) in AGENT_EXPLICIT_INDEXES.items():
        expected[key] = (False, False, key_columns, options, predicate)
    return expected


def _assert_agent_index_signatures(
    rows: Iterable[Mapping[str, Any]],
) -> None:
    actual = {}
    unsafe = []
    for row in rows:
        key = (str(row["table_name"]), str(row["index_name"]))
        key_columns = tuple(str(value) for value in row.get("key_columns") or ())
        options = tuple(int(value) for value in row.get("key_options") or ())
        predicate = (
            None
            if row.get("predicate") is None
            else _normalized_sql_expression(row["predicate"])
        )
        actual[key] = (
            bool(row["is_unique"]),
            bool(row["is_primary"]),
            key_columns,
            options,
            predicate,
        )
        if (
            row["access_method"] != "btree"
            or not bool(row["is_valid"])
            or not bool(row["is_ready"])
            or not bool(row["is_live"])
            or bool(row["is_clustered"])
            or bool(row["is_replica_identity"])
            or bool(row["nulls_not_distinct"])
            or bool(row["has_expressions"])
            or int(row["key_attribute_count"]) != len(key_columns)
            or int(row["attribute_count"]) != len(key_columns)
            or int(row["tablespace_oid"]) != 0
        ):
            unsafe.append(dict(row))
    expected = _expected_agent_indexes()
    if actual != expected or unsafe:
        raise RuntimeError(
            "unsafe write-enabled database contract: agent index signature "
            f"drift; expected={expected}, actual={actual}, unsafe={unsafe}"
        )


def _assert_restore_constraint_contract(
    rows: Iterable[Mapping[str, Any]],
) -> None:
    actual_primary_keys = set()
    actual_checks = {}
    actual_foreign_keys = set()
    unexpected = []
    unsafe_metadata = []
    for row in rows:
        table_name = str(row["table_name"])
        constraint_name = str(row["constraint_name"])
        constraint_type = str(row["constraint_type"])
        key_columns = tuple(str(value) for value in row.get("key_columns") or ())
        if (
            bool(row["deferrable"])
            or bool(row["initially_deferred"])
            or not bool(row["validated"])
            # PostgreSQL marks index-backed and foreign-key constraints
            # NO INHERIT; the flag is only a policy signal on CHECKs.
            or (constraint_type == "c" and bool(row["no_inherit"]))
        ):
            unsafe_metadata.append((table_name, constraint_name))
        if constraint_type == "p":
            actual_primary_keys.add((table_name, constraint_name, key_columns))
        elif constraint_type == "c":
            actual_checks[(table_name, constraint_name, key_columns)] = (
                _normalized_check_expression(row.get("check_expression"))
            )
        elif constraint_type == "f":
            actual_foreign_keys.add((
                table_name,
                constraint_name,
                key_columns,
                str(row.get("referenced_table") or ""),
                tuple(
                    str(value)
                    for value in row.get("referenced_columns") or ()
                ),
                str(row.get("update_action") or ""),
                str(row.get("delete_action") or ""),
                str(row.get("match_type") or ""),
            ))
        else:
            unexpected.append((table_name, constraint_name, constraint_type))

    expected_primary_keys = {
        (table_name, constraint_name, key_columns)
        for table_name, (constraint_name, key_columns)
        in EXPECTED_PRIMARY_KEYS.items()
    }
    if (
        actual_primary_keys != expected_primary_keys
        or actual_checks != EXPECTED_CHECKS
        or actual_foreign_keys != EXPECTED_FOREIGN_KEYS
        or unexpected
        or unsafe_metadata
    ):
        raise RuntimeError(
            "unsafe write-enabled database contract: exact-undo constraint "
            "drift; "
            f"primary_keys={sorted(actual_primary_keys)}, "
            f"checks={actual_checks}, "
            f"foreign_keys={sorted(actual_foreign_keys)}, "
            f"unexpected={unexpected}, unsafe_metadata={unsafe_metadata}"
        )


def _validate_write_relation_shapes(cursor) -> None:
    policy_names = sorted(TABLE_POLICIES | OPTIONAL_TABLE_POLICIES)
    cursor.execute(
        """
        SELECT format('%%I.%%I', namespace.nspname, relation.relname)
                   AS table_name,
               relation.relkind AS relation_kind,
               relation.relpersistence AS persistence,
               relation.relispartition AS is_partition,
               relation_owner.rolname AS owner_name,
               database_owner.rolname AS database_owner,
               relation.relowner = database.datdba
                   AS owned_by_database_owner,
               relation_owner.rolsuper AS owner_is_superuser
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_roles AS relation_owner
            ON relation_owner.oid = relation.relowner
          JOIN pg_database AS database
            ON database.datname = current_database()
          JOIN pg_roles AS database_owner
            ON database_owner.oid = database.datdba
         WHERE format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
         ORDER BY namespace.nspname, relation.relname
        """,
        (policy_names,),
    )
    _assert_write_relation_shapes(cursor.fetchall())

    cursor.execute(
        """
        SELECT format('%%I.%%I', namespace.nspname, relation.relname)
                   AS table_name,
               attribute.attname AS column_name,
               attribute.attnum::integer AS ordinal_position,
               format_type(attribute.atttypid, attribute.atttypmod)
                   AS formatted_type,
               NOT attribute.attnotnull AS nullable,
               attribute.attgenerated AS generated_kind,
               attribute.attidentity AS identity_kind,
               pg_collation.collname AS collation_name,
               pg_get_expr(
                   default_row.adbin, default_row.adrelid, true
               ) AS default_expression
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_attribute AS attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND NOT attribute.attisdropped
          LEFT JOIN pg_collation
            ON pg_collation.oid = attribute.attcollation
          LEFT JOIN pg_attrdef AS default_row
            ON default_row.adrelid = relation.oid
           AND default_row.adnum = attribute.attnum
         WHERE format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
         ORDER BY namespace.nspname, relation.relname, attribute.attnum
        """,
        (sorted(RESTORE_COLUMN_CONTRACTS),),
    )
    _assert_restore_column_contract(cursor.fetchall())

    restore_names = sorted(RESTORE_COLUMN_CONTRACTS)
    cursor.execute(
        """
        SELECT format(
                   '%%I.%%I', source_namespace.nspname, source.relname
               ) AS table_name,
               constraint_row.conname AS constraint_name,
               constraint_row.contype AS constraint_type,
               ARRAY(
                   SELECT attribute.attname
                     FROM unnest(constraint_row.conkey)
                          WITH ORDINALITY AS key_column(attnum, ordinal_position)
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = source.oid
                      AND attribute.attnum = key_column.attnum
                    ORDER BY key_column.ordinal_position
               ) AS key_columns,
               CASE WHEN target.oid IS NULL THEN NULL ELSE format(
                   '%%I.%%I', target_namespace.nspname, target.relname
               ) END AS referenced_table,
               ARRAY(
                   SELECT attribute.attname
                     FROM unnest(constraint_row.confkey)
                          WITH ORDINALITY AS key_column(attnum, ordinal_position)
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = target.oid
                      AND attribute.attnum = key_column.attnum
                    ORDER BY key_column.ordinal_position
               ) AS referenced_columns,
               constraint_row.confupdtype AS update_action,
               constraint_row.confdeltype AS delete_action,
               constraint_row.confmatchtype AS match_type,
               constraint_row.condeferrable AS deferrable,
               constraint_row.condeferred AS initially_deferred,
               constraint_row.convalidated AS validated,
               constraint_row.connoinherit AS no_inherit,
               pg_get_expr(
                   constraint_row.conbin, constraint_row.conrelid, true
               ) AS check_expression
          FROM pg_constraint AS constraint_row
          JOIN pg_class AS source ON source.oid = constraint_row.conrelid
          JOIN pg_namespace AS source_namespace
            ON source_namespace.oid = source.relnamespace
          LEFT JOIN pg_class AS target ON target.oid = constraint_row.confrelid
          LEFT JOIN pg_namespace AS target_namespace
            ON target_namespace.oid = target.relnamespace
         WHERE (
                   format(
                       '%%I.%%I', source_namespace.nspname, source.relname
                   ) = ANY (%s)
                   AND constraint_row.contype IN ('p', 'c', 'f', 'u', 'x')
               )
            OR (
                   target.oid IS NOT NULL
                   AND format(
                       '%%I.%%I', target_namespace.nspname, target.relname
                   ) = ANY (%s)
                   AND constraint_row.contype = 'f'
               )
         ORDER BY source_namespace.nspname, source.relname,
                  constraint_row.conname
        """,
        (restore_names, restore_names),
    )
    _assert_restore_constraint_contract(cursor.fetchall())

    cursor.execute(
        """
        SELECT count(*)::integer AS unsafe_unique_index_count
          FROM pg_index AS index_row
          JOIN pg_class AS relation
            ON relation.oid = index_row.indrelid
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         WHERE format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
           AND index_row.indisunique
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_constraint AS constraint_row
                WHERE constraint_row.conindid = index_row.indexrelid
           )
        """,
        (restore_names,),
    )
    _expect(
        cursor.fetchone(),
        "unsafe_unique_index_count",
        0,
        "standalone unique indexes are forbidden on exact-undo tables",
    )


def _validate_agent_schema_signatures(cursor) -> None:
    agent_tables = sorted(AGENT_COLUMN_CONTRACTS)
    cursor.execute(
        """
        SELECT format('%I.%I', namespace.nspname, relation.relname)
                   AS table_name,
               relation.relkind AS relation_kind,
               relation.relpersistence AS persistence,
               relation.relispartition AS is_partition,
               owner.rolname AS owner_name,
               database_owner.rolname AS database_owner,
               owner.rolsuper AS owner_is_superuser,
               access_method.amname AS access_method,
               relation.reloptions AS relation_options,
               relation.relreplident AS replica_identity,
               relation.reltablespace AS tablespace_oid
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_roles AS owner ON owner.oid = relation.relowner
          LEFT JOIN pg_am AS access_method
            ON access_method.oid = relation.relam
          JOIN pg_database AS database
            ON database.datname = current_database()
          JOIN pg_roles AS database_owner
            ON database_owner.oid = database.datdba
         WHERE namespace.nspname = 'logo'
           AND relation.relname LIKE 'agent\\_%' ESCAPE '\\'
           AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
         ORDER BY namespace.nspname, relation.relname
        """,
    )
    _assert_agent_relation_signatures(cursor.fetchall())

    cursor.execute(
        """
        SELECT format('%%I.%%I', namespace.nspname, relation.relname)
                   AS table_name,
               attribute.attname AS column_name,
               attribute.attnum::integer AS ordinal_position,
               format_type(attribute.atttypid, attribute.atttypmod)
                   AS formatted_type,
               NOT attribute.attnotnull AS nullable,
               attribute.attgenerated AS generated_kind,
               attribute.attidentity AS identity_kind,
               pg_collation.collname AS collation_name,
               pg_get_expr(
                   default_row.adbin, default_row.adrelid, true
               ) AS default_expression
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_attribute AS attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum > 0
           AND NOT attribute.attisdropped
          LEFT JOIN pg_collation
            ON pg_collation.oid = attribute.attcollation
          LEFT JOIN pg_attrdef AS default_row
            ON default_row.adrelid = relation.oid
           AND default_row.adnum = attribute.attnum
         WHERE format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
         ORDER BY namespace.nspname, relation.relname, attribute.attnum
        """,
        (agent_tables,),
    )
    _assert_agent_column_signatures(cursor.fetchall())

    cursor.execute(
        """
        SELECT format(
                   '%%I.%%I', source_namespace.nspname, source.relname
               ) AS table_name,
               constraint_row.conname AS constraint_name,
               constraint_row.contype AS constraint_type,
               ARRAY(
                   SELECT attribute.attname::text
                     FROM unnest(constraint_row.conkey)
                          WITH ORDINALITY AS key_column(attnum, ordinal_position)
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = source.oid
                      AND attribute.attnum = key_column.attnum
                    ORDER BY key_column.ordinal_position
               ) AS key_columns,
               CASE WHEN target.oid IS NULL THEN NULL ELSE format(
                   '%%I.%%I', target_namespace.nspname, target.relname
               ) END AS referenced_table,
               ARRAY(
                   SELECT attribute.attname::text
                     FROM unnest(constraint_row.confkey)
                          WITH ORDINALITY AS key_column(attnum, ordinal_position)
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = target.oid
                      AND attribute.attnum = key_column.attnum
                    ORDER BY key_column.ordinal_position
               ) AS referenced_columns,
               constraint_row.confupdtype AS update_action,
               constraint_row.confdeltype AS delete_action,
               constraint_row.confmatchtype AS match_type,
               constraint_row.condeferrable AS deferrable,
               constraint_row.condeferred AS initially_deferred,
               constraint_row.convalidated AS validated,
               constraint_row.connoinherit AS no_inherit,
               pg_get_expr(
                   constraint_row.conbin, constraint_row.conrelid, true
               ) AS check_expression
          FROM pg_constraint AS constraint_row
          JOIN pg_class AS source ON source.oid = constraint_row.conrelid
          JOIN pg_namespace AS source_namespace
            ON source_namespace.oid = source.relnamespace
          LEFT JOIN pg_class AS target ON target.oid = constraint_row.confrelid
          LEFT JOIN pg_namespace AS target_namespace
            ON target_namespace.oid = target.relnamespace
         WHERE (
                   format(
                       '%%I.%%I', source_namespace.nspname, source.relname
                   ) = ANY (%s)
                   AND constraint_row.contype IN ('p', 'u', 'c', 'f', 'x')
               )
            OR (
                   target.oid IS NOT NULL
                   AND format(
                       '%%I.%%I', target_namespace.nspname, target.relname
                   ) = ANY (%s)
                   AND constraint_row.contype = 'f'
               )
         ORDER BY source_namespace.nspname, source.relname,
                  constraint_row.conname
        """,
        (agent_tables, agent_tables),
    )
    _assert_agent_constraint_signatures(cursor.fetchall())

    cursor.execute(
        """
        SELECT format('%%I.%%I', namespace.nspname, relation.relname)
                   AS table_name,
               index_class.relname AS index_name,
               index_row.indisunique AS is_unique,
               index_row.indisprimary AS is_primary,
               index_row.indisvalid AS is_valid,
               index_row.indisready AS is_ready,
               index_row.indislive AS is_live,
               index_row.indisclustered AS is_clustered,
               index_row.indisreplident AS is_replica_identity,
               index_row.indnullsnotdistinct AS nulls_not_distinct,
               index_method.amname AS access_method,
               index_row.indexprs IS NOT NULL AS has_expressions,
               index_row.indnkeyatts AS key_attribute_count,
               index_row.indnatts AS attribute_count,
               index_class.reltablespace AS tablespace_oid,
               ARRAY(
                   SELECT attribute.attname::text
                     FROM unnest(index_row.indkey::smallint[])
                          WITH ORDINALITY AS key_column(attnum, ordinal_position)
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = relation.oid
                      AND attribute.attnum = key_column.attnum
                    WHERE key_column.ordinal_position <= index_row.indnkeyatts
                    ORDER BY key_column.ordinal_position
               ) AS key_columns,
               index_row.indoption::smallint[] AS key_options,
               pg_get_expr(
                   index_row.indpred, index_row.indrelid, true
               ) AS predicate
          FROM pg_index AS index_row
          JOIN pg_class AS relation ON relation.oid = index_row.indrelid
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_class AS index_class
            ON index_class.oid = index_row.indexrelid
          JOIN pg_am AS index_method ON index_method.oid = index_class.relam
         WHERE format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
         ORDER BY namespace.nspname, relation.relname, index_class.relname
        """,
        (agent_tables,),
    )
    _assert_agent_index_signatures(cursor.fetchall())


def _assert_trigger_inventory(rows: Iterable[Mapping[str, Any]]) -> None:
    trigger_rows = list(rows)
    actual_triggers = frozenset(
        (
            str(row["table_name"]),
            str(row["trigger_name"]),
            int(row["trigger_type"]),
            str(row["enabled"]),
            str(row["function_schema"]),
            str(row["function_name"]),
        )
        for row in trigger_rows
    )
    malformed = [
        dict(row)
        for row in trigger_rows
        if row["argument_types"] != ""
        or int(row["argument_count"]) != 0
        or not bool(row["no_when_clause"])
        or not bool(row["not_constraint_trigger"])
    ]
    if actual_triggers != EXPECTED_TRIGGERS or malformed:
        raise RuntimeError(
            "unsafe write-enabled database contract: trigger inventory drift; "
            f"expected={sorted(EXPECTED_TRIGGERS)}, "
            f"actual={sorted(actual_triggers)}, malformed={malformed}"
        )


def _assert_audit_function_contract(audit: Mapping[str, Any] | None) -> None:
    if audit is None:
        raise RuntimeError(
            "unsafe write-enabled database contract: logo.audit_row() is missing"
        )
    _expect_acceptable_owner(audit, "audit trigger function owner drift")
    for key, expected, label in (
        ("procedure_kind", "f", "kind"),
        ("argument_count", 0, "arguments"),
        ("language_name", "plpgsql", "language"),
        ("security_definer", False, "security mode"),
        ("fixed_settings", None, "fixed settings"),
        ("function_result", "trigger", "result"),
        ("source_sha256", EXPECTED_AUDIT_SOURCE_SHA256, "body"),
        ("app_execute", False, "application EXECUTE"),
        ("public_execute", False, "PUBLIC EXECUTE"),
    ):
        _expect(audit, key, expected, f"audit trigger function {label} drift")


def _validate_write_semantics(cursor) -> None:
    policy_names = sorted(TABLE_POLICIES | OPTIONAL_TABLE_POLICIES)
    cursor.execute(
        """
        SELECT format('%%I.%%I', namespace.nspname, relation.relname)
                   AS table_name,
               trigger.tgname AS trigger_name,
               trigger.tgtype::integer AS trigger_type,
               trigger.tgenabled AS enabled,
               function_namespace.nspname AS function_schema,
               function_row.proname AS function_name,
               oidvectortypes(function_row.proargtypes) AS argument_types,
               trigger.tgnargs AS argument_count,
               trigger.tgqual IS NULL AS no_when_clause,
               trigger.tgconstraint = 0 AS not_constraint_trigger
          FROM pg_trigger AS trigger
          JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_proc AS function_row ON function_row.oid = trigger.tgfoid
          JOIN pg_namespace AS function_namespace
            ON function_namespace.oid = function_row.pronamespace
         WHERE NOT trigger.tgisinternal
           AND format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
         ORDER BY namespace.nspname, relation.relname, trigger.tgname
        """,
        (policy_names,),
    )
    _assert_trigger_inventory(cursor.fetchall())

    cursor.execute(
        """
        SELECT count(*)::integer AS rule_count
          FROM pg_rewrite AS rewrite
          JOIN pg_class AS relation ON relation.oid = rewrite.ev_class
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         WHERE rewrite.rulename <> '_RETURN'
           AND format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
        """,
        (policy_names,),
    )
    _expect(cursor.fetchone(), "rule_count", 0, "writable rewrite rules exist")

    cursor.execute(
        """
        SELECT count(*)::integer AS unsafe_rls_count
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         WHERE format('%%I.%%I', namespace.nspname, relation.relname) = ANY (%s)
           AND (
               relation.relrowsecurity
               OR relation.relforcerowsecurity
               OR EXISTS (
                   SELECT 1 FROM pg_policy AS policy
                    WHERE policy.polrelid = relation.oid
               )
           )
        """,
        (policy_names,),
    )
    _expect(
        cursor.fetchone(),
        "unsafe_rls_count",
        0,
        "RLS is forbidden on writable tables",
    )

    cursor.execute(
        """
        SELECT function_owner.rolname AS owner_name,
               database_owner.rolname AS database_owner,
               procedure.prokind AS procedure_kind,
               procedure.pronargs AS argument_count,
               language.lanname AS language_name,
               procedure.prosecdef AS security_definer,
               procedure.proconfig AS fixed_settings,
               pg_get_function_result(procedure.oid) AS function_result,
               encode(sha256(convert_to(
                   procedure.prosrc, 'UTF8'
               )), 'hex') AS source_sha256,
               has_function_privilege(
                   current_user, procedure.oid, 'EXECUTE'
               ) AS app_execute,
               has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               ) AS public_execute,
               function_owner.rolsuper AS owner_is_superuser
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
          JOIN pg_roles AS function_owner
            ON function_owner.oid = procedure.proowner
          JOIN pg_language AS language
            ON language.oid = procedure.prolang
          JOIN pg_database AS database
            ON database.datname = current_database()
          JOIN pg_roles AS database_owner
            ON database_owner.oid = database.datdba
         WHERE procedure.oid = to_regprocedure('logo.audit_row()')
           AND namespace.nspname = 'logo'
        """
    )
    _assert_audit_function_contract(cursor.fetchone())


def _validate_sequence_authority(cursor) -> None:
    cursor.execute(
        """
        WITH sequence_privilege(privilege_name) AS (
            VALUES ('USAGE'), ('SELECT'), ('UPDATE')
        )
        SELECT format('%I.%I', namespace.nspname, relation.relname)
                   AS sequence_name,
               sequence_privilege.privilege_name,
               has_sequence_privilege(
                   current_user,
                   relation.oid,
                   sequence_privilege.privilege_name
               ) AS actual,
               has_sequence_privilege(
                   'public',
                   relation.oid,
                   sequence_privilege.privilege_name
               ) AS public_actual
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         CROSS JOIN sequence_privilege
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND relation.relkind = 'S'
         ORDER BY namespace.nspname, relation.relname,
                  sequence_privilege.privilege_name
        """
    )
    rows = cursor.fetchall()
    actual = {
        (str(row["sequence_name"]), str(row["privilege_name"])):
            bool(row["actual"])
        for row in rows
    }
    expected = {
        (sequence_name, privilege): privilege in allowed_privileges
        for sequence_name, allowed_privileges in SEQUENCE_POLICIES.items()
        for privilege in SEQUENCE_PRIVILEGES
    }
    missing = sorted(set(expected) - set(actual))
    mismatches = sorted(
        (sequence_name, privilege, expected_value, actual.get(key))
        for key, expected_value in expected.items()
        for sequence_name, privilege in (key,)
        if actual.get(key) != expected_value
    )
    unexpected_authority = sorted(
        key
        for key, allowed in actual.items()
        if key[0] not in SEQUENCE_POLICIES and allowed
    )
    public_authority = sorted(
        (str(row["sequence_name"]), str(row["privilege_name"]))
        for row in rows
        if bool(row["public_actual"])
    )
    if missing or mismatches or unexpected_authority or public_authority:
        raise RuntimeError(
            "unsafe write-enabled database contract: sequence privilege "
            f"mismatch; missing={missing}, mismatches={mismatches}, "
            f"unexpected={unexpected_authority}, public={public_authority}"
        )

    cursor.execute(
        """
        SELECT count(*)::integer AS grant_option_count
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         CROSS JOIN LATERAL aclexplode(coalesce(
             relation.relacl,
             acldefault('s', relation.relowner)
         )) AS privilege
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND relation.relkind = 'S'
           AND privilege.grantee = (
               SELECT oid FROM pg_roles WHERE rolname = current_user
           )
           AND privilege.is_grantable
        """
    )
    _expect(
        cursor.fetchone(),
        "grant_option_count",
        0,
        "sequence privilege WITH GRANT OPTION is forbidden",
    )

    cursor.execute(
        """
        SELECT count(*)::integer AS sequence_count
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         WHERE namespace.nspname = 'logo'
           AND relation.relkind = 'S'
           AND relation.relname LIKE 'agent\\_%' ESCAPE '\\'
        """
    )
    _expect(
        cursor.fetchone(),
        "sequence_count",
        0,
        "agent IDs must remain application-generated UUIDs",
    )


def _normalized_function_settings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple("".join(str(setting).split()) for setting in value)


def _assert_prune_contract(row: Mapping[str, Any] | None) -> None:
    if row is None:
        raise RuntimeError(
            "unsafe write-enabled database contract: "
            "logo.prune_agent_history() is missing"
        )
    _expect_acceptable_owner(row, "retention owner drift")
    if row.get("owner_name") == EXPECTED_ROLE:
        raise RuntimeError(
            "unsafe write-enabled database contract: retention function "
            "must not be owned by logo_admin"
        )
    _expect(row, "procedure_kind", "f", "retention object is not a function")
    _expect(row, "argument_count", 0, "retention function must take no arguments")
    _expect(row, "language_name", "plpgsql", "retention language drift")
    _expect(row, "security_definer", True, "retention must be SECURITY DEFINER")
    _expect(
        row,
        "function_result",
        "TABLE(journals_deleted bigint, change_sets_deleted bigint)",
        "retention result contract drift",
    )
    _expect(
        row,
        "source_sha256",
        EXPECTED_PRUNE_SOURCE_SHA256,
        "retention function body drift",
    )
    settings = _normalized_function_settings(row.get("fixed_settings"))
    if settings != ("search_path=pg_catalog,logo",):
        raise RuntimeError(
            "unsafe write-enabled database contract: retention fixed "
            f"settings drift; actual={settings!r}"
        )
    _expect(row, "app_execute", True, "retention EXECUTE missing")
    _expect(row, "public_execute", False, "retention EXECUTE granted to PUBLIC")
    _expect(
        row,
        "app_execute_grantable",
        False,
        "retention EXECUTE must not be grantable by logo_admin",
    )


def _assert_security_definer_inventory(
    rows: Iterable[Mapping[str, Any]],
) -> None:
    inventory: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (
            str(row["schema_name"]),
            str(row["function_name"]),
            str(row["argument_types"]),
        )
        if key in inventory:
            raise RuntimeError(
                "unsafe write-enabled database contract: duplicate "
                f"SECURITY DEFINER signature {key}"
            )
        inventory[key] = row

    missing = sorted(REQUIRED_EXECUTABLE_SECURITY_DEFINERS - set(inventory))
    unexpected = sorted(set(inventory) - ALLOWED_EXECUTABLE_SECURITY_DEFINERS)
    unsafe = []
    for key, row in inventory.items():
        if not _owner_is_acceptable(row):
            unsafe.append((key, "owner"))
        if bool(row.get("public_execute")):
            unsafe.append((key, "PUBLIC EXECUTE"))
        if bool(row.get("app_execute_grantable")):
            unsafe.append((key, "EXECUTE WITH GRANT OPTION"))
    if missing or unexpected or unsafe:
        raise RuntimeError(
            "unsafe write-enabled database contract: SECURITY DEFINER "
            f"inventory mismatch; missing={missing}, unexpected={unexpected}, "
            f"unsafe={unsafe}"
        )


def _assert_callable_inventory(rows: Iterable[Mapping[str, Any]]) -> None:
    inventory = frozenset(
        (
            str(row["schema_name"]),
            str(row["routine_name"]),
            str(row["argument_types"]),
            str(row["routine_kind"]),
        )
        for row in rows
    )
    missing = sorted(REQUIRED_EXECUTABLE_ROUTINES - inventory)
    unexpected = sorted(inventory - ALLOWED_EXECUTABLE_ROUTINES)
    if missing or unexpected:
        raise RuntimeError(
            "unsafe write-enabled database contract: callable routine "
            f"inventory mismatch; missing={missing}, unexpected={unexpected}"
        )


def _validate_callable_inventory(cursor) -> None:
    cursor.execute(
        f"""
        SELECT namespace.nspname AS schema_name,
               procedure.proname AS routine_name,
               oidvectortypes(procedure.proargtypes) AS argument_types,
               procedure.prokind AS routine_kind
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND procedure.prokind IN ('f', 'p')
           AND has_function_privilege(
               current_user, procedure.oid, 'EXECUTE'
           )
           AND NOT {_EXTENSION_OWNED_ROUTINE_SQL}
         ORDER BY namespace.nspname, procedure.proname,
                  oidvectortypes(procedure.proargtypes)
        """
    )
    _assert_callable_inventory(cursor.fetchall())


def _validate_security_definer_inventory(cursor) -> None:
    cursor.execute(
        """
        SELECT namespace.nspname AS schema_name,
               procedure.proname AS function_name,
               oidvectortypes(procedure.proargtypes) AS argument_types,
               function_owner.rolname AS owner_name,
               database_owner.rolname AS database_owner,
               has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               ) AS public_execute,
               EXISTS (
                   SELECT 1
                     FROM aclexplode(coalesce(
                         procedure.proacl,
                         acldefault('f', procedure.proowner)
                     )) AS privilege
                    WHERE privilege.grantee = (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    )
                      AND privilege.privilege_type = 'EXECUTE'
                      AND privilege.is_grantable
               ) AS app_execute_grantable,
               function_owner.rolsuper AS owner_is_superuser
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
          JOIN pg_roles AS function_owner
            ON function_owner.oid = procedure.proowner
          JOIN pg_database AS database
            ON database.datname = current_database()
          JOIN pg_roles AS database_owner
            ON database_owner.oid = database.datdba
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND procedure.prosecdef
           AND has_function_privilege(
               current_user, procedure.oid, 'EXECUTE'
           )
         ORDER BY namespace.nspname, procedure.proname,
                  oidvectortypes(procedure.proargtypes)
        """
    )
    _assert_security_definer_inventory(cursor.fetchall())


def _assert_repull_contract(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_definition_sha256: str | None = None,
) -> None:
    functions = list(rows)
    if not functions:
        return
    if len(functions) != 1:
        raise RuntimeError(
            "unsafe write-enabled database contract: expected at most one "
            f"logo.repull_display_name overload, actual={len(functions)}"
        )
    row = functions[0]
    _expect(row, "argument_types", "text, boolean", "repull signature drift")
    _expect_acceptable_owner(row, "repull owner drift")
    _expect(row, "procedure_kind", "f", "repull object is not a function")
    _expect(row, "language_name", "plpgsql", "repull language drift")
    _expect(
        row,
        "security_definer",
        False,
        "repull must remain SECURITY INVOKER",
    )
    _expect(row, "function_result", "integer", "repull result contract drift")
    _expect(row, "app_execute", True, "repull EXECUTE missing")
    _expect(row, "public_execute", False, "repull EXECUTE granted to PUBLIC")
    _expect(
        row,
        "app_execute_grantable",
        False,
        "repull EXECUTE must not be grantable by logo_admin",
    )
    fixed_settings = _normalized_function_settings(row.get("fixed_settings"))
    if fixed_settings not in {(), ("search_path=pg_catalog,logo,fdm4",)}:
        raise RuntimeError(
            "unsafe write-enabled database contract: repull fixed settings "
            f"drift; actual={fixed_settings!r}"
        )
    definition = str(row.get("definition") or "")
    if expected_definition_sha256 is None:
        raise RuntimeError(
            "unsafe write-enabled database contract: "
            "AGENT_REPULL_FUNCTION_SHA256 is required while "
            "logo.repull_display_name exists"
        )
    actual_definition_sha256 = hashlib.sha256(
        definition.encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(
        actual_definition_sha256,
        expected_definition_sha256,
    ):
        raise RuntimeError(
            "unsafe write-enabled database contract: repull definition "
            f"SHA-256 drift; actual={actual_definition_sha256}"
        )
    normalized_definition = definition.lower()
    required_references = ("logo.display_name", "fdm4.design_pool")
    if any(reference not in normalized_definition for reference in required_references):
        raise RuntimeError(
            "unsafe write-enabled database contract: repull definition no "
            "longer references the reviewed source/target contract"
        )
    if re.search(r"\bexecute\b", normalized_definition):
        raise RuntimeError(
            "unsafe write-enabled database contract: dynamic SQL is "
            "forbidden in repull"
        )


def _validate_repull_function(
    cursor,
    *,
    expected_definition_sha256: str | None,
) -> None:
    cursor.execute(
        """
        SELECT oidvectortypes(procedure.proargtypes) AS argument_types,
               function_owner.rolname AS owner_name,
               database_owner.rolname AS database_owner,
               procedure.prokind AS procedure_kind,
               language.lanname AS language_name,
               procedure.prosecdef AS security_definer,
               procedure.proconfig AS fixed_settings,
               pg_get_function_result(procedure.oid) AS function_result,
               encode(sha256(convert_to(
                   procedure.prosrc, 'UTF8'
               )), 'hex') AS source_sha256,
               has_function_privilege(
                   current_user, procedure.oid, 'EXECUTE'
               ) AS app_execute,
               has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               ) AS public_execute,
               EXISTS (
                   SELECT 1
                     FROM aclexplode(coalesce(
                         procedure.proacl,
                         acldefault('f', procedure.proowner)
                     )) AS privilege
                    WHERE privilege.grantee = (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    )
                      AND privilege.privilege_type = 'EXECUTE'
                      AND privilege.is_grantable
               ) AS app_execute_grantable,
               function_owner.rolsuper AS owner_is_superuser,
               pg_get_functiondef(procedure.oid) AS definition
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
          JOIN pg_roles AS function_owner
            ON function_owner.oid = procedure.proowner
          JOIN pg_language AS language
            ON language.oid = procedure.prolang
          JOIN pg_database AS database
            ON database.datname = current_database()
          JOIN pg_roles AS database_owner
            ON database_owner.oid = database.datdba
         WHERE namespace.nspname = 'logo'
           AND procedure.proname = 'repull_display_name'
         ORDER BY oidvectortypes(procedure.proargtypes)
        """
    )
    _assert_repull_contract(
        cursor.fetchall(),
        expected_definition_sha256=expected_definition_sha256,
    )


def _validate_retention_function(cursor) -> None:
    cursor.execute(
        """
        SELECT function_owner.rolname AS owner_name,
               database_owner.rolname AS database_owner,
               procedure.prokind AS procedure_kind,
               procedure.pronargs AS argument_count,
               language.lanname AS language_name,
               procedure.prosecdef AS security_definer,
               procedure.proconfig AS fixed_settings,
               pg_get_function_result(procedure.oid) AS function_result,
               encode(sha256(convert_to(
                   procedure.prosrc, 'UTF8'
               )), 'hex') AS source_sha256,
               has_function_privilege(
                   current_user, procedure.oid, 'EXECUTE'
               ) AS app_execute,
               has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               ) AS public_execute,
               EXISTS (
                   SELECT 1
                     FROM aclexplode(coalesce(
                         procedure.proacl,
                         acldefault('f', procedure.proowner)
                     )) AS privilege
                    WHERE privilege.grantee = (
                        SELECT oid FROM pg_roles WHERE rolname = current_user
                    )
                      AND privilege.privilege_type = 'EXECUTE'
                      AND privilege.is_grantable
               ) AS app_execute_grantable,
               function_owner.rolsuper AS owner_is_superuser
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
          JOIN pg_roles AS function_owner
            ON function_owner.oid = procedure.proowner
          JOIN pg_language AS language
            ON language.oid = procedure.prolang
          JOIN pg_database AS database
            ON database.datname = current_database()
          JOIN pg_roles AS database_owner
            ON database_owner.oid = database.datdba
         WHERE procedure.oid = to_regprocedure('logo.prune_agent_history()')
           AND namespace.nspname = 'logo'
        """
    )
    _assert_prune_contract(cursor.fetchone())


def validate_write_database_contract(
    cursor,
    *,
    expected_repull_sha256: str | None = None,
) -> None:
    """Reject write-enabled startup unless database authority is exact."""

    validate_restore_schema(cursor)
    _validate_role_authority(cursor)
    _validate_table_authority(cursor)
    _validate_write_relation_shapes(cursor)
    _validate_agent_schema_signatures(cursor)
    _validate_write_semantics(cursor)
    _validate_sequence_authority(cursor)
    _validate_callable_inventory(cursor)
    _validate_security_definer_inventory(cursor)
    _validate_retention_function(cursor)
    _validate_repull_function(
        cursor,
        expected_definition_sha256=expected_repull_sha256,
    )
