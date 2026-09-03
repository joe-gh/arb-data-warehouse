"""Allowlisted full-row snapshots, semantic diffs, and exact restoration."""

import json
from collections import defaultdict

from psycopg2.extras import Json
from numbers import Number
from typing import Any, Iterable, Mapping, Optional, Sequence

from domain import InvalidCommand
from mutations import MutationScope


ASSIGNMENT_COLUMNS = (
    "fdm4_store",
    "product_style",
    "garment_color_code",
    "position",
    "design_id",
    "logo_code",
    "color_scheme_id",
    "location",
    "optional",
    "background",
    "cost_override",
    "sort_order",
    "image_url",
    "active",
    "updated_by",
    "updated_at",
    "option_row",
    "name_override",
    "row_version",
    "catalog_id",
)
# Columns a BEFORE-trigger rewrites on every write (feed versioning). They
# are bookkeeping, not business state: restore lets the trigger stamp them
# (a restored row MUST get a new row_version so feed consumers see the
# rollback), and exactness comparisons ignore them.
TRIGGER_MANAGED_COLUMNS = frozenset({"row_version"})
ASSIGNMENT_INSERT_COLUMNS = tuple(
    column for column in ASSIGNMENT_COLUMNS
    if column not in TRIGGER_MANAGED_COLUMNS
)
STORE_SETTINGS_COLUMNS = (
    "fdm4_store",
    "enabled",
    "allows_none",
    "updated_by",
    "updated_at",
    "extra_customers",
)
STORE_PRICING_COLUMNS = (
    "fdm4_store",
    "tier_name",
    "note",
    "updated_at",
)
# Columns that legitimately differ between the rolled-back preview and the
# real apply: audit stamps, and the trigger-managed row_version (a sequence
# value, so every preview of an INSERT would otherwise hash differently and
# the apply would always report "the warehouse changed").
# created_at is stamped by the INSERT default, so a previewed insert and the
# real apply legitimately differ there too.
VOLATILE_PREVIEW_COLUMNS = (
    frozenset({
        "updated_by", "updated_at", "created_at", "created_by", "added_at", "added_by",
        "imported_at",  # product-mix list snapshot stamp (now() at enrolment)
    })
    | TRIGGER_MANAGED_COLUMNS
)

# Tables the agent may edit through the same exact snapshot/restore path.
# ``key`` = the scope's columns (a whole primary key for one row, or a prefix
# such as fdm4_store for every row of a store); ``pk`` (default: key) = row
# identity. Column order matches the live table (database_contract pins it);
# ``types`` drives journal validation:
#   text / text? = str (nullable), bool, int, num / num?, ts / ts? (str),
#   date? (str or null), list? (JSON array or null), json? (any JSON or null)
SIMPLE_ROW_SCOPES = {
    "display_name_row": {
        "table": "logo.display_name",
        "key": ("design_id", "color_scheme_id", "fdm4_store"),
        "columns": (
            "design_id", "color_scheme_id", "name", "source", "locked", "uses",
            "fdm4_description", "updated_at", "updated_by", "fdm4_store",
        ),
        "types": {
            "design_id": "text", "color_scheme_id": "text", "name": "text",
            "source": "text", "locked": "bool", "uses": "int",
            "fdm4_description": "text?", "updated_at": "ts", "updated_by": "text?",
            "fdm4_store": "text",
        },
    },
    "color_class_row": {
        "table": "logo.color_class",
        "key": ("color_code",),
        "columns": (
            "color_code", "color_name", "light_dark", "source", "confidence",
            "updated_at", "updated_by",
        ),
        "types": {
            "color_code": "text", "color_name": "text", "light_dark": "text",
            "source": "text", "confidence": "num?", "updated_at": "ts",
            "updated_by": "text",
        },
    },
    "stock_override_row": {
        "table": "woo.stock_override",
        "key": ("style_code",),
        "columns": ("style_code", "mode", "note", "active", "updated_by", "updated_at"),
        "types": {
            "style_code": "text", "mode": "text", "note": "text", "active": "bool",
            "updated_by": "text", "updated_at": "ts",
        },
    },
    "brand_stock_rule_row": {
        "table": "woo.brand_stock_rule",
        "key": ("mill_code",),
        "columns": (
            "mill_code", "brand_name", "mode", "note", "active", "updated_by", "updated_at",
        ),
        "types": {
            "mill_code": "text", "brand_name": "text", "mode": "text", "note": "text",
            "active": "bool", "updated_by": "text", "updated_at": "ts",
        },
    },
    "sync_exclusion_row": {
        "table": "woo.sync_exclusion",
        "key": ("fdm4_store", "style_code"),
        "columns": (
            "fdm4_store", "style_code", "note", "active", "created_at", "updated_at",
            "updated_by", "scope",
        ),
        "types": {
            "fdm4_store": "text", "style_code": "text", "note": "text", "active": "bool",
            "created_at": "ts", "updated_at": "ts", "updated_by": "text", "scope": "text",
        },
    },
    "default_cost_row": {
        "table": "logo.default_cost",
        "key": ("logo_code", "color_scheme_id"),
        "columns": ("logo_code", "color_scheme_id", "cost", "source", "locked", "updated_by", "updated_at"),
        "types": {
            "logo_code": "text", "color_scheme_id": "text", "cost": "num", "source": "text",
            "locked": "bool", "updated_by": "text", "updated_at": "ts",
        },
    },
    "price_rule_row": {
        "table": "woo.price_rule",
        "key": ("rule_id",),
        "columns": (
            "rule_id", "name", "active", "priority", "stackable", "stores", "store_tiers",
            "styles", "brands", "categories", "effect_type", "effect_value", "price_level_key",
            "floor_price", "effective_from", "effective_until", "note", "created_at",
            "updated_at", "updated_by", "last_previewed_at", "excl_stores", "excl_styles",
            "excl_brands", "excl_categories", "basis", "rounding", "ceiling_price", "cap_at_msrp",
        ),
        "types": {
            "rule_id": "int", "name": "text", "active": "bool", "priority": "int", "stackable": "bool",
            "stores": "list?", "store_tiers": "list?", "styles": "list?", "brands": "list?",
            "categories": "list?", "effect_type": "text", "effect_value": "num?",
            "price_level_key": "text?", "floor_price": "num?", "effective_from": "date?",
            "effective_until": "date?", "note": "text", "created_at": "ts", "updated_at": "ts",
            "updated_by": "text", "last_previewed_at": "ts?", "excl_stores": "list?",
            "excl_styles": "list?", "excl_brands": "list?", "excl_categories": "list?",
            "basis": "text", "rounding": "text", "ceiling_price": "num?", "cap_at_msrp": "bool",
        },
    },
    "store_mix_store_row": {
        "table": "woo.store_mix_store",
        "key": ("fdm4_store",),
        "columns": (
            "fdm4_store", "mode", "active", "note", "created_by", "created_at",
            "updated_by", "updated_at", "imported_at",
        ),
        "types": {
            "fdm4_store": "text", "mode": "text", "active": "bool", "note": "text",
            "created_by": "text", "created_at": "ts", "updated_by": "text", "updated_at": "ts",
            "imported_at": "ts?",
        },
    },
    # Every curated style of one store: the scope is the store, rows are styles.
    "store_mix_items": {
        "table": "woo.store_mix_item",
        "key": ("fdm4_store",),
        "pk": ("fdm4_store", "style_code"),
        "columns": (
            "fdm4_store", "style_code", "colors", "size_excludes", "source",
            "added_by", "added_at", "updated_by", "updated_at",
        ),
        "types": {
            "fdm4_store": "text", "style_code": "text", "colors": "list?", "size_excludes": "json?",
            "source": "text", "added_by": "text", "added_at": "ts", "updated_by": "text",
            "updated_at": "ts",
        },
    },
}
for _spec in SIMPLE_ROW_SCOPES.values():
    _spec.setdefault("pk", _spec["key"])
SIMPLE_TABLE_SPECS = {spec["table"]: spec for spec in SIMPLE_ROW_SCOPES.values()}

RESTORE_COLUMNS = {
    ("logo", "assignment"): frozenset(ASSIGNMENT_COLUMNS),
    ("logo", "store_settings"): frozenset(STORE_SETTINGS_COLUMNS),
    ("woo", "store_pricing_tier"): frozenset(STORE_PRICING_COLUMNS),
    **{
        tuple(spec["table"].split(".", 1)): frozenset(spec["columns"])
        for spec in SIMPLE_ROW_SCOPES.values()
    },
}
_BASE_SCOPE_KINDS = frozenset({
    "assignment_option_row",
    "assignment_color",
    "assignment_style",
    "assignment_store",
    "store_settings_row",
    "store_pricing_tier_row",
})
SNAPSHOT_SCOPE_KINDS = _BASE_SCOPE_KINDS | frozenset(SIMPLE_ROW_SCOPES)
RESTORE_SCOPE_KINDS = _BASE_SCOPE_KINDS | frozenset(SIMPLE_ROW_SCOPES)
SCOPE_TABLE_BY_KIND = {
    "assignment_option_row": "logo.assignment",
    "assignment_color": "logo.assignment",
    "assignment_style": "logo.assignment",
    "assignment_store": "logo.assignment",
    "store_settings_row": "logo.store_settings",
    "store_pricing_tier_row": "woo.store_pricing_tier",
    **{kind: spec["table"] for kind, spec in SIMPLE_ROW_SCOPES.items()},
}
MAX_SNAPSHOT_ROWS_PER_SCOPE = 2_000
MAX_SNAPSHOT_ROWS_TOTAL = 5_000
MAX_SNAPSHOT_ROW_BYTES = 256 * 1024
MAX_SNAPSHOT_SCOPE_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 5 * 1024 * 1024
MAX_SNAPSHOT_STATE_BYTES = 6 * 1024 * 1024
MAX_SEMANTIC_DIFF_BYTES = 12 * 1024 * 1024
MAX_SNAPSHOT_SCOPE_ENTRIES = 500

# Scope key columns that are integers (every other key column is text).
INTEGER_SCOPE_KEYS = frozenset({"option_row", "rule_id"})

SCOPE_KEY_COLUMNS = {
    "assignment_store": ("fdm4_store",),
    "assignment_style": ("fdm4_store", "product_style"),
    "assignment_color": (
        "fdm4_store", "product_style", "garment_color_code",
    ),
    "assignment_option_row": (
        "fdm4_store", "product_style", "garment_color_code", "option_row",
    ),
    "store_settings_row": ("fdm4_store",),
    "store_pricing_tier_row": ("fdm4_store",),
    **{kind: spec["key"] for kind, spec in SIMPLE_ROW_SCOPES.items()},
}


def validate_restore_schema(cursor) -> None:
    """Fail write-enabled startup if exact-undo column coverage has drifted."""

    for (schema, table), expected in RESTORE_COLUMNS.items():
        cursor.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
             ORDER BY ordinal_position
            """,
            (schema, table),
        )
        actual = frozenset(str(row["column_name"]) for row in cursor.fetchall())
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise RuntimeError(
                f"exact-undo schema mismatch for {schema}.{table}; "
                f"missing={missing}, unexpected={unexpected}"
            )


def scope_dict(scope: MutationScope) -> dict:
    return {"kind": scope.kind, "key": dict(scope.key)}


def scope_from_dict(value: Mapping[str, Any]) -> MutationScope:
    kind = str(value.get("kind", ""))
    if kind not in SNAPSHOT_SCOPE_KINDS or kind not in RESTORE_SCOPE_KINDS:
        raise InvalidCommand("unknown snapshot scope")
    key = value.get("key")
    if not isinstance(key, Mapping):
        raise InvalidCommand("snapshot scope key is invalid")
    expected_keys = SCOPE_KEY_COLUMNS[kind]
    if set(key) != set(expected_keys):
        raise InvalidCommand("snapshot scope key columns are invalid")
    for name in expected_keys:
        item = key[name]
        if name in INTEGER_SCOPE_KEYS:
            if type(item) is not int:
                raise InvalidCommand(f"snapshot {name} key is invalid")
        elif not isinstance(item, str):
            raise InvalidCommand("snapshot text key is invalid")
    return MutationScope(kind, dict(key))  # type: ignore[arg-type]


def _scope_token(scope: MutationScope) -> str:
    return json.dumps(scope_dict(scope), sort_keys=True, separators=(",", ":"))


def compact_scopes(scopes: Iterable[MutationScope]) -> tuple[MutationScope, ...]:
    """Deduplicate scopes and remove assignment scopes contained by broader ones."""

    unique = {_scope_token(scope): scope for scope in scopes}
    values = list(unique.values())
    # A whole-store scope contains every narrower assignment scope of that store.
    stores = {str(s.key["fdm4_store"]) for s in values if s.kind == "assignment_store"}
    values = [
        s for s in values
        if not (s.kind in {"assignment_style", "assignment_color", "assignment_option_row"}
                and str(s.key["fdm4_store"]) in stores)
    ]
    styles = {
        (str(s.key["fdm4_store"]), str(s.key["product_style"]))
        for s in values
        if s.kind == "assignment_style"
    }
    colors = {
        (
            str(s.key["fdm4_store"]),
            str(s.key["product_style"]),
            str(s.key["garment_color_code"]),
        )
        for s in values
        if s.kind == "assignment_color"
        and (str(s.key["fdm4_store"]), str(s.key["product_style"])) not in styles
    }
    kept: list[MutationScope] = []
    for scope in values:
        if scope.kind in {"assignment_color", "assignment_option_row"}:
            style_key = (
                str(scope.key["fdm4_store"]),
                str(scope.key["product_style"]),
            )
            if style_key in styles:
                continue
        if scope.kind == "assignment_option_row":
            color_key = (
                str(scope.key["fdm4_store"]),
                str(scope.key["product_style"]),
                str(scope.key["garment_color_code"]),
            )
            if color_key in colors:
                continue
        kept.append(scope)
    return tuple(sorted(kept, key=_scope_token))


def lock_scopes(cursor, scopes: Iterable[MutationScope]) -> tuple[MutationScope, ...]:
    """Serialize application mutations, including scopes that have no rows yet.

    Row locks cannot protect an empty settings/pricing row or a new assignment
    key. Transaction-scoped advisory locks close that phantom window. Every
    application HTTP, MCP, preview, apply, and undo path uses these same stable
    scope tokens in sorted order, so overlapping operations cannot race or
    deadlock by taking locks in different orders.
    """

    compacted = compact_scopes(scopes)
    lock_set = {_scope_token(scope): scope for scope in compacted}
    # Every assignment mutation also takes its style ancestor. Without this,
    # an option-row write and a simultaneous color/style write use different
    # advisory keys even though their row sets overlap.
    for scope in compacted:
        if scope.kind.startswith("assignment_"):
            # ...and its store ancestor, so a whole-store write (bulk apply)
            # and any narrower write in that store serialize on one key.
            store_scope = MutationScope("assignment_store", {"fdm4_store": scope.key["fdm4_store"]})
            lock_set[_scope_token(store_scope)] = store_scope
            if scope.kind == "assignment_store":
                continue
            ancestor = MutationScope(
                "assignment_style",
                {
                    "fdm4_store": scope.key["fdm4_store"],
                    "product_style": scope.key["product_style"],
                },
            )
            lock_set[_scope_token(ancestor)] = ancestor
    for token in sorted(lock_set):
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (token,),
        )
    return compacted


def lock_scope_tables(cursor, scopes: Iterable[MutationScope]) -> tuple[str, ...]:
    """Exclude non-cooperating writers while exact apply/undo is in flight.

    Advisory scope locks serialize application/MCP routes, but imports and ETL
    do not share that protocol. SHARE ROW EXCLUSIVE conflicts with their DML
    table locks and closes phantom insert/delete windows. Call this only after
    ``lock_scopes`` so cooperating writers use one global lock order.
    """

    tables = tuple(sorted({
        SCOPE_TABLE_BY_KIND[scope.kind]
        for scope in compact_scopes(scopes)
    }))
    for table in tables:
        # Names come only from the closed mapping above, never user input.
        cursor.execute(
            f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE"
        )
    return tables


def _assignment_where(scope: MutationScope) -> tuple[str, tuple]:
    key = scope.key
    if scope.kind == "assignment_store":
        return ("fdm4_store = %s", (key["fdm4_store"],))
    if scope.kind == "assignment_style":
        return (
            "fdm4_store = %s AND product_style = %s",
            (key["fdm4_store"], key["product_style"]),
        )
    if scope.kind == "assignment_color":
        return (
            "fdm4_store = %s AND product_style = %s "
            "AND garment_color_code = %s",
            (
                key["fdm4_store"],
                key["product_style"],
                key["garment_color_code"],
            ),
        )
    if scope.kind == "assignment_option_row":
        return (
            "fdm4_store = %s AND product_style = %s "
            "AND garment_color_code = %s AND option_row = %s",
            (
                key["fdm4_store"],
                key["product_style"],
                key["garment_color_code"],
                key["option_row"],
            ),
        )
    raise InvalidCommand("not an assignment scope")


def _bounded_snapshot_rows(
    cursor,
    source_sql: str,
    params: tuple,
) -> tuple[list[dict], int]:
    """Keep oversized rowsets inside PostgreSQL and return only a sentinel."""

    cursor.execute(
        f"""
        WITH source AS MATERIALIZED (
            {source_sql}
        ), measured AS MATERIALIZED (
            SELECT ordinal, row,
                   octet_length(row::text)::bigint AS row_bytes
              FROM source
        ), stats AS (
            SELECT count(*)::integer AS row_count,
                   coalesce(max(row_bytes), 0)::bigint AS max_row_bytes,
                   coalesce(sum(row_bytes), 0)::bigint AS scope_bytes
              FROM measured
        )
        SELECT CASE
                   WHEN stats.row_count <= %s
                    AND stats.max_row_bytes <= %s
                    AND stats.scope_bytes <= %s
                   THEN measured.row
                   ELSE NULL
               END AS row,
               stats.row_count, stats.max_row_bytes, stats.scope_bytes
          FROM stats
          LEFT JOIN measured
            ON stats.row_count <= %s
           AND stats.max_row_bytes <= %s
           AND stats.scope_bytes <= %s
         ORDER BY measured.ordinal NULLS LAST
        """,
        params + (
            MAX_SNAPSHOT_ROWS_PER_SCOPE,
            MAX_SNAPSHOT_ROW_BYTES,
            MAX_SNAPSHOT_SCOPE_BYTES,
            MAX_SNAPSHOT_ROWS_PER_SCOPE,
            MAX_SNAPSHOT_ROW_BYTES,
            MAX_SNAPSHOT_SCOPE_BYTES,
        ),
    )
    result_rows = list(cursor.fetchall())
    stats = result_rows[0] if result_rows else {
        "row_count": 0,
        "max_row_bytes": 0,
        "scope_bytes": 0,
    }
    if int(stats["row_count"]) > MAX_SNAPSHOT_ROWS_PER_SCOPE:
        raise InvalidCommand("Affected scope exceeds the exact-snapshot row limit")
    if int(stats["max_row_bytes"]) > MAX_SNAPSHOT_ROW_BYTES:
        raise InvalidCommand("Affected row exceeds the exact-snapshot byte limit")
    if int(stats["scope_bytes"]) > MAX_SNAPSHOT_SCOPE_BYTES:
        raise InvalidCommand("Affected scope exceeds the exact-snapshot byte limit")
    return (
        [dict(row["row"]) for row in result_rows if row.get("row") is not None],
        int(stats["scope_bytes"]),
    )


def _snapshot_one(cursor, scope: MutationScope, *, for_update: bool) -> dict:
    lock = " FOR UPDATE" if for_update else ""
    if scope.kind.startswith("assignment_"):
        where, params = _assignment_where(scope)
        rows, snapshot_bytes = _bounded_snapshot_rows(
            cursor,
            f"""
            SELECT row_number() OVER (
                       ORDER BY garment_color_code, option_row, position
                   ) AS ordinal,
                   to_jsonb(locked) AS row
              FROM (
                  SELECT * FROM logo.assignment
                   WHERE {where}
                   ORDER BY garment_color_code, option_row, position
                   LIMIT %s{lock}
              ) AS locked
            """,
            params + (MAX_SNAPSHOT_ROWS_PER_SCOPE + 1,),
        )
        table = "logo.assignment"
    elif scope.kind == "store_settings_row":
        rows, snapshot_bytes = _bounded_snapshot_rows(
            cursor,
            f"""
            SELECT 1 AS ordinal, to_jsonb(locked) AS row
              FROM (
                  SELECT * FROM logo.store_settings
                   WHERE fdm4_store = %s{lock}
              ) AS locked
            """,
            (scope.key["fdm4_store"],),
        )
        table = "logo.store_settings"
    elif scope.kind == "store_pricing_tier_row":
        rows, snapshot_bytes = _bounded_snapshot_rows(
            cursor,
            f"""
            SELECT 1 AS ordinal, to_jsonb(locked) AS row
              FROM (
                  SELECT * FROM woo.store_pricing_tier
                   WHERE fdm4_store = %s{lock}
              ) AS locked
            """,
            (scope.key["fdm4_store"],),
        )
        table = "woo.store_pricing_tier"
    elif scope.kind in SIMPLE_ROW_SCOPES:
        spec = SIMPLE_ROW_SCOPES[scope.kind]
        where = " AND ".join(f"{column} = %s" for column in spec["key"])
        order = ", ".join(spec["pk"])
        rows, snapshot_bytes = _bounded_snapshot_rows(
            cursor,
            f"""
            SELECT row_number() OVER (ORDER BY {order}) AS ordinal,
                   to_jsonb(snapshot_row) AS row
              FROM (
                  SELECT * FROM {spec["table"]}
                   WHERE {where}
                   ORDER BY {order}
                   LIMIT %s{lock}
              ) AS snapshot_row
            """,
            tuple(scope.key[column] for column in spec["key"]) + (MAX_SNAPSHOT_ROWS_PER_SCOPE + 1,),
        )
        table = spec["table"]
    else:
        raise InvalidCommand("unsupported snapshot scope")
    return {
        "scope": scope_dict(scope),
        "table": table,
        "rows": rows,
        "_bytes": snapshot_bytes,
    }


def snapshot_scopes(
    cursor,
    scopes: Iterable[MutationScope],
    *,
    for_update: bool = False,
) -> list[dict]:
    snapshots: list[dict] = []
    total_rows = 0
    total_bytes = 0
    for scope in compact_scopes(scopes):
        snapshot = _snapshot_one(cursor, scope, for_update=for_update)
        total_rows += len(snapshot["rows"])
        total_bytes += int(snapshot.get("_bytes", 0))
        if total_rows > MAX_SNAPSHOT_ROWS_TOTAL:
            raise InvalidCommand(
                "Change-set exceeds the total exact-snapshot row limit"
            )
        if total_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
            raise InvalidCommand(
                "Change-set exceeds the total exact-snapshot byte limit"
            )
        snapshot.pop("_bytes", None)
        snapshots.append(snapshot)
    validate_snapshot_state(snapshots, expected_scopes=compact_scopes(scopes))
    return snapshots


def _semantic_rows(rows: Sequence[Mapping[str, Any]], ignored: set[str]) -> list[dict]:
    return [
        {key: value for key, value in row.items() if key not in ignored}
        for row in rows
    ]


def _row_key(table: str, row: Mapping[str, Any]) -> str:
    if table == "logo.assignment":
        values = [
            row.get("fdm4_store"),
            row.get("product_style"),
            row.get("garment_color_code"),
            row.get("option_row"),
            row.get("position"),
        ]
    elif table in SIMPLE_TABLE_SPECS:
        values = [row.get(column) for column in SIMPLE_TABLE_SPECS[table]["pk"]]
    else:
        values = [row.get("fdm4_store")]
    return json.dumps(values, separators=(",", ":"), default=str)


def diff_states(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    ignored_columns: set[str] | frozenset[str] = frozenset(),
) -> dict:
    ignored = set(ignored_columns)
    before_by_scope = {_scope_token(scope_from_dict(e["scope"])): e for e in before}
    after_by_scope = {_scope_token(scope_from_dict(e["scope"])): e for e in after}
    changes = []
    for token in sorted(set(before_by_scope) | set(after_by_scope)):
        left = before_by_scope.get(token, {"rows": [], "table": ""})
        right = after_by_scope.get(token, {"rows": [], "table": left["table"]})
        table = str(right.get("table") or left.get("table"))
        left_rows = {
            _row_key(table, row): row
            for row in _semantic_rows(left.get("rows", []), ignored)
        }
        right_rows = {
            _row_key(table, row): row
            for row in _semantic_rows(right.get("rows", []), ignored)
        }
        for row_key in sorted(set(left_rows) | set(right_rows)):
            old = left_rows.get(row_key)
            new = right_rows.get(row_key)
            if old != new:
                changes.append({
                    "table": table,
                    "key": json.loads(row_key),
                    "before": old,
                    "after": new,
                })
    result = {"changes": changes, "count": len(changes)}
    if json_size_bytes(result) > MAX_SEMANTIC_DIFF_BYTES:
        raise InvalidCommand("Semantic preview exceeds the byte limit")
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def json_size_bytes(value: Any) -> int:
    return sum(
        len(chunk.encode("utf-8"))
        for chunk in json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).iterencode(value)
    )


def _validate_row_types(table: str, row: Mapping[str, Any]) -> None:
    expected = RESTORE_COLUMNS[tuple(table.split(".", 1))]
    if set(row) != set(expected):
        raise InvalidCommand("Journal row columns do not match the restore schema")
    if table == "logo.assignment":
        text_columns = set(ASSIGNMENT_COLUMNS) - {
            "position", "option_row", "optional", "cost_override",
            "sort_order", "active", "updated_at", "name_override",
            "row_version", "catalog_id",
        }
        for column in text_columns:
            if not isinstance(row[column], str):
                raise InvalidCommand("Journal assignment text value is invalid")
        if row["name_override"] is not None and not isinstance(
            row["name_override"], str
        ):
            raise InvalidCommand("Journal assignment name override is invalid")
        for column in ("position", "option_row", "sort_order"):
            if type(row[column]) is not int:
                raise InvalidCommand("Journal assignment integer value is invalid")
        for column in ("optional", "active"):
            if type(row[column]) is not bool:
                raise InvalidCommand("Journal assignment boolean value is invalid")
        cost = row["cost_override"]
        if cost is not None and (not isinstance(cost, Number) or isinstance(cost, bool)):
            raise InvalidCommand("Journal assignment cost value is invalid")
    elif table == "logo.store_settings":
        if not isinstance(row["fdm4_store"], str) or not isinstance(
            row["updated_by"], str
        ):
            raise InvalidCommand("Journal store-settings text value is invalid")
        if type(row["enabled"]) is not bool or type(row["allows_none"]) is not bool:
            raise InvalidCommand("Journal store-settings boolean value is invalid")
        extra = row.get("extra_customers")
        if not isinstance(extra, list) or not all(
            isinstance(item, str) for item in extra
        ):
            raise InvalidCommand(
                "Journal row extra_customers must be a list of strings"
            )
    elif table == "woo.store_pricing_tier":
        if any(
            not isinstance(row[column], str)
            for column in ("fdm4_store", "tier_name", "note")
        ):
            raise InvalidCommand("Journal pricing text value is invalid")
    elif table in SIMPLE_TABLE_SPECS:
        for column, kind in SIMPLE_TABLE_SPECS[table]["types"].items():
            value = row[column]
            is_number = isinstance(value, Number) and not isinstance(value, bool)
            ok = (
                isinstance(value, str) if kind in {"text", "ts"}
                else value is None or isinstance(value, str) if kind in {"text?", "ts?", "date?"}
                else type(value) is bool if kind == "bool"
                else type(value) is int if kind == "int"
                else is_number if kind == "num"
                else value is None or is_number if kind == "num?"
                else value is None or isinstance(value, list) if kind == "list?"
                else value is None or isinstance(value, (dict, list, str, Number, bool)) if kind == "json?"
                else False
            )
            if not ok:
                raise InvalidCommand(f"Journal {table} value for {column} is invalid")
    if not isinstance(row["updated_at"], str):
        raise InvalidCommand("Journal timestamp value is invalid")


def _row_is_within_scope(
    table: str,
    row: Mapping[str, Any],
    scope: MutationScope,
) -> bool:
    if table in SIMPLE_TABLE_SPECS:
        return all(
            str(row.get(column)) == str(scope.key[column])
            for column in SIMPLE_TABLE_SPECS[table]["key"]
        )
    if str(row.get("fdm4_store")) != str(scope.key["fdm4_store"]):
        return False
    if table != "logo.assignment" or scope.kind == "assignment_store":
        return True
    if str(row.get("product_style")) != str(scope.key["product_style"]):
        return False
    if scope.kind in {"assignment_color", "assignment_option_row"} and str(
        row.get("garment_color_code")
    ) != str(scope.key["garment_color_code"]):
        return False
    if scope.kind == "assignment_option_row" and int(
        row.get("option_row", -1)
    ) != int(scope.key["option_row"]):
        return False
    return True


def validate_snapshot_state(
    state: Sequence[Mapping[str, Any]],
    *,
    expected_scopes: Optional[Iterable[MutationScope]] = None,
) -> tuple[MutationScope, ...]:
    """Validate an entire journal state before any lock or restore DML."""

    if isinstance(state, (str, bytes)) or not isinstance(state, Sequence):
        raise InvalidCommand("Journal snapshot is not a sequence")
    entries = list(state)
    if len(entries) > MAX_SNAPSHOT_SCOPE_ENTRIES:
        raise InvalidCommand("Journal snapshot has too many scope entries")
    scopes: list[MutationScope] = []
    seen_scopes: set[str] = set()
    seen_rows: set[tuple[str, str]] = set()
    total_rows = 0
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {
            "scope", "table", "rows",
        }:
            raise InvalidCommand("Journal snapshot entry is invalid")
        if not isinstance(entry["scope"], Mapping):
            raise InvalidCommand("Journal snapshot scope is invalid")
        scope = scope_from_dict(entry["scope"])
        token = _scope_token(scope)
        if token in seen_scopes:
            raise InvalidCommand("Journal snapshot repeats a scope")
        seen_scopes.add(token)
        scopes.append(scope)
        table = str(entry["table"])
        if table != SCOPE_TABLE_BY_KIND[scope.kind]:
            raise InvalidCommand("Journal snapshot table does not match its scope")
        rows = entry["rows"]
        if not isinstance(rows, list) or len(rows) > MAX_SNAPSHOT_ROWS_PER_SCOPE:
            raise InvalidCommand("Journal snapshot rows are invalid")
        total_rows += len(rows)
        if total_rows > MAX_SNAPSHOT_ROWS_TOTAL:
            raise InvalidCommand("Journal snapshot exceeds the restore row limit")
        for row in rows:
            if not isinstance(row, Mapping):
                raise InvalidCommand("Journal snapshot row is invalid")
            _validate_row_types(table, row)
            if not _row_is_within_scope(table, row, scope):
                raise InvalidCommand("Journal row falls outside its declared scope")
            if json_size_bytes(row) > MAX_SNAPSHOT_ROW_BYTES:
                raise InvalidCommand("Journal row exceeds the restore byte limit")
            row_identity = (table, _row_key(table, row))
            if row_identity in seen_rows:
                raise InvalidCommand("Journal snapshot repeats a business row")
            seen_rows.add(row_identity)
    compacted = compact_scopes(scopes)
    if len(compacted) != len(scopes) or {
        _scope_token(scope) for scope in compacted
    } != seen_scopes:
        raise InvalidCommand("Journal snapshot contains overlapping scopes")
    if expected_scopes is not None:
        expected = {
            _scope_token(scope) for scope in compact_scopes(expected_scopes)
        }
        if expected != seen_scopes:
            raise InvalidCommand("Journal snapshot scopes do not match the change-set")
    if json_size_bytes(entries) > MAX_SNAPSHOT_STATE_BYTES:
        raise InvalidCommand("Journal snapshot exceeds the restore byte limit")
    return tuple(compacted)


def _strip_trigger_managed(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_trigger_managed(item)
            for key, item in value.items()
            if key not in TRIGGER_MANAGED_COLUMNS
        }
    if isinstance(value, list):
        return [_strip_trigger_managed(item) for item in value]
    return value


def states_equal(left: Any, right: Any) -> bool:
    return canonical_json(_strip_trigger_managed(left)) == canonical_json(
        _strip_trigger_managed(right)
    )


def _delete_scope(cursor, scope: MutationScope) -> None:
    if scope.kind.startswith("assignment_"):
        where, params = _assignment_where(scope)
        # Companion slots depend semantically on their position-1 anchor.
        # Delete them first so restore remains safe if that relationship is
        # promoted to a deployed constraint/trigger later.
        cursor.execute(
            f"DELETE FROM logo.assignment WHERE {where} AND position > 1",
            params,
        )
        cursor.execute(
            f"DELETE FROM logo.assignment WHERE {where} AND position = 1",
            params,
        )
    elif scope.kind == "store_settings_row":
        cursor.execute(
            "DELETE FROM logo.store_settings WHERE fdm4_store = %s",
            (scope.key["fdm4_store"],),
        )
    elif scope.kind == "store_pricing_tier_row":
        cursor.execute(
            "DELETE FROM woo.store_pricing_tier WHERE fdm4_store = %s",
            (scope.key["fdm4_store"],),
        )
    elif scope.kind in SIMPLE_ROW_SCOPES:
        spec = SIMPLE_ROW_SCOPES[scope.kind]
        where = " AND ".join(f"{column} = %s" for column in spec["key"])
        cursor.execute(
            f"DELETE FROM {spec['table']} WHERE {where}",
            tuple(scope.key[column] for column in spec["key"]),
        )
    else:
        raise InvalidCommand("unsupported restore scope")


def _insert_simple(cursor, table: str, row: Mapping[str, Any]) -> None:
    spec = SIMPLE_TABLE_SPECS[table]
    columns = spec["columns"]
    values = []
    for column in columns:
        value = row[column]
        if spec["types"].get(column) == "json?" and value is not None:
            value = Json(value)
        values.append(value)
    cursor.execute(
        f"""
        INSERT INTO {table} ({', '.join(columns)})
        VALUES ({', '.join(['%s'] * len(columns))})
        """,
        tuple(values),
    )


def _insert_assignment(cursor, row: Mapping[str, Any]) -> None:
    cursor.execute(
        f"""
        INSERT INTO logo.assignment ({', '.join(ASSIGNMENT_INSERT_COLUMNS)})
        VALUES ({', '.join(['%s'] * len(ASSIGNMENT_INSERT_COLUMNS))})
        """,
        tuple(row[column] for column in ASSIGNMENT_INSERT_COLUMNS),
    )


def _insert_settings(cursor, row: Mapping[str, Any]) -> None:
    cursor.execute(
        f"""
        INSERT INTO logo.store_settings ({', '.join(STORE_SETTINGS_COLUMNS)})
        VALUES ({', '.join(['%s'] * len(STORE_SETTINGS_COLUMNS))})
        """,
        tuple(row[column] for column in STORE_SETTINGS_COLUMNS),
    )


def _insert_pricing(cursor, row: Mapping[str, Any]) -> None:
    cursor.execute(
        f"""
        INSERT INTO woo.store_pricing_tier ({', '.join(STORE_PRICING_COLUMNS)})
        VALUES ({', '.join(['%s'] * len(STORE_PRICING_COLUMNS))})
        """,
        tuple(row[column] for column in STORE_PRICING_COLUMNS),
    )


def restore_state(
    cursor,
    state: Sequence[Mapping[str, Any]],
    *,
    expected_scopes: Optional[Iterable[MutationScope]] = None,
) -> None:
    """Replace each allowlisted scope with its exact recorded rows."""

    entries = list(state)
    validate_snapshot_state(entries, expected_scopes=expected_scopes)
    for entry in entries:
        _delete_scope(cursor, scope_from_dict(entry["scope"]))

    assignments: list[Mapping[str, Any]] = []
    settings: list[Mapping[str, Any]] = []
    pricing: list[Mapping[str, Any]] = []
    simple: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for entry in entries:
        table = entry.get("table")
        if table == "logo.assignment":
            assignments.extend(entry.get("rows", []))
        elif table == "logo.store_settings":
            settings.extend(entry.get("rows", []))
        elif table == "woo.store_pricing_tier":
            pricing.extend(entry.get("rows", []))
        elif table in SIMPLE_TABLE_SPECS:
            for row in entry.get("rows", []):
                simple[str(table)][_row_key(str(table), row)] = row
        else:
            raise InvalidCommand("unsupported table in journal snapshot")

    unique_assignments = {
        _row_key("logo.assignment", row): row for row in assignments
    }
    for row in sorted(
        unique_assignments.values(),
        key=lambda value: (
            value["fdm4_store"],
            value["product_style"],
            value["garment_color_code"],
            value["option_row"],
            value["position"],
        ),
    ):
        _insert_assignment(cursor, row)
    for row in {str(r["fdm4_store"]): r for r in settings}.values():
        _insert_settings(cursor, row)
    for row in {str(r["fdm4_store"]): r for r in pricing}.values():
        _insert_pricing(cursor, row)
    for table in sorted(simple):
        for key in sorted(simple[table]):
            _insert_simple(cursor, table, simple[table][key])
