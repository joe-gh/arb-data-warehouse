"""Legacy logo-sheet import: NDJSON rows -> logo.assignment (re-runnable).

Accepts the NDJSON produced by the WordPress export
(`wp arb_product_sync logo-export-current`) and applies the same resolution
rules as the one-time bulk seeder (infra/seed_logo.py - kept for CLI use):

  * product_color name  -> garment color code via fdm4."style-color"
  * logo_code (+scheme) -> design_id via fdm4.cust_art_file filename prefixes
  * misses land in logo.import_report, never silently dropped

Re-import semantics: rows are upserted with updated_by='legacy-import'. By
default rows whose current updated_by is NOT 'seed'/'legacy-import' (i.e.
edited by a human in this app) are PRESERVED and counted as skipped; pass
preserve_manual=False to force-overwrite them.
"""

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple
from urllib.parse import urlsplit

from design_resolver import DesignIndex, load_design_index


IMPORT_ACTORS = ("seed", "legacy-import")

COLOR_LOOKUP_SQL = """
    SELECT btrim("style-code") AS style,
           lower(btrim(description)) AS color_name,
           btrim("color-code") AS color_code
      FROM fdm4."style-color"
     WHERE NULLIF(btrim("style-code"), '') IS NOT NULL
       AND NULLIF(btrim(description), '') IS NOT NULL
       AND NULLIF(btrim("color-code"), '') IS NOT NULL
"""

class RowMiss(ValueError):
    """A row that cannot be imported; recorded in logo.import_report."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def load_color_lookup(cursor) -> Dict[Tuple[str, str], set]:
    cursor.execute(COLOR_LOOKUP_SQL)
    lookup: Dict[Tuple[str, str], set] = {}
    for row in cursor.fetchall():
        key = (str(row["style"]), str(row["color_name"]).strip())
        lookup.setdefault(key, set()).add(str(row["color_code"]))
    return lookup


def load_design_lookup(cursor) -> DesignIndex:
    """Compatibility wrapper around the canonical resolver."""

    return load_design_index(cursor)


def _text(row: Dict[str, Any], key: str, maximum: int = 200) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raise RowMiss("invalid_value", f"{key} must be a scalar")
    cleaned = str(value).strip()
    if "\x00" in cleaned or len(cleaned) > maximum:
        raise RowMiss("invalid_value", f"{key} is invalid")
    return cleaned


def _integer(value: Any, field: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise RowMiss("invalid_integer", f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RowMiss("invalid_integer", f"{field} must be an integer") from None
    if isinstance(value, float) and not value.is_integer():
        raise RowMiss("invalid_integer", f"{field} must be an integer")
    if isinstance(value, str) and value.strip() != str(parsed):
        raise RowMiss("invalid_integer", f"{field} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise RowMiss("invalid_integer", f"{field} is outside the supported range")
    return parsed


def _boolean(value: Any, field: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    raise RowMiss("invalid_boolean", f"{field} must be true or false")


def _cost(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RowMiss("invalid_cost", "cost must be numeric or empty")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise RowMiss("invalid_cost", "cost must be numeric or empty") from None
    if (
        not parsed.is_finite()
        or parsed < Decimal("-9999999999.99")
        or parsed > Decimal("9999999999.99")
        or parsed.as_tuple().exponent < -2
    ):
        raise RowMiss(
            "invalid_cost",
            "cost must fit numeric(12,2) and have at most two decimal places",
        )
    return parsed


def _image_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise RowMiss("invalid_image_url", "image must be an absolute HTTP(S) URL")
    try:
        parsed.port
    except ValueError:
        raise RowMiss("invalid_image_url", "image URL has an invalid port") from None
    return value


def resolve_row(
    row: Dict[str, Any],
    color_lookup: Dict[Tuple[str, str], set],
    design_lookup: DesignIndex,
) -> Dict[str, Any]:
    """Validate + resolve one NDJSON row to assignment column values, or raise RowMiss."""

    store = _text(row, "fdm4_store", 100)
    style = _text(row, "product_style", 100)
    if not store or not style:
        raise RowMiss("invalid_value", "fdm4_store and product_style are required")

    product_color = _text(row, "product_color", 100)
    logo_code = _text(row, "logo_code", 100).upper()
    scheme = _text(row, "color_scheme", 100).upper()
    if not logo_code:
        raise RowMiss("no_design", "empty logo_code")

    codes = color_lookup.get((style, product_color.lower()))
    if not codes:
        raise RowMiss("no_color_code", f"normalized color name: {product_color.lower()}")
    if len(codes) > 1:
        raise RowMiss("ambiguous_color", ",".join(sorted(codes)))
    garment_color_code = next(iter(codes))

    designs = design_lookup.candidates(store, logo_code, scheme)
    if not designs:
        raise RowMiss("no_design", f"color_scheme={scheme}")
    if len(designs) > 1:
        raise RowMiss("ambiguous_design", ",".join(sorted(designs)))
    design_id = next(iter(designs))

    image_url = _image_url(_text(row, "image", 2048))
    if not image_url and (design_id, scheme) not in design_lookup.usable_art:
        raise RowMiss(
            "no_art",
            f"design_id={design_id}; color_scheme={scheme}; no sheet image and no PREVIEW/THUMB art",
        )

    position = _integer(row.get("position"), "position", 1, 1, 3)
    option_row = _integer(row.get("option_row"), "option_row", 1, 1, 999)
    sort_order = _integer(
        row.get("sort_order"), "sort_order", 0, -2147483648, 2147483647
    )

    return {
        "fdm4_store": store,
        "product_style": style,
        "garment_color_code": garment_color_code,
        "option_row": option_row,
        "position": position,
        "design_id": design_id,
        "logo_code": logo_code,
        "color_scheme_id": scheme,
        "location": _text(row, "location", 200),
        "optional": _boolean(row.get("optional"), "optional"),
        "background": _text(row, "background", 200) or _text(row, "background_color", 200),
        "cost_override": _cost(row.get("cost")),
        "sort_order": sort_order,
        "image_url": image_url,
    }


UPSERT_SQL = """
    INSERT INTO logo.assignment (
        fdm4_store, product_style, garment_color_code, option_row, position,
        design_id, logo_code, color_scheme_id, location, optional,
        background, cost_override, sort_order, image_url, active, updated_by
    ) VALUES (
        %(fdm4_store)s, %(product_style)s, %(garment_color_code)s, %(option_row)s, %(position)s,
        %(design_id)s, %(logo_code)s, %(color_scheme_id)s, %(location)s, %(optional)s,
        %(background)s, %(cost_override)s, %(sort_order)s, %(image_url)s, true, 'legacy-import'
    )
    ON CONFLICT (fdm4_store, product_style, garment_color_code, option_row, position)
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
        active = true,
        updated_by = 'legacy-import',
        updated_at = now()
    {preserve_clause}
"""


def import_rows(
    cursor,
    lines: Iterable[str],
    user_login: str,
    preserve_manual: bool,
    report: Callable[[Dict[str, Any], str, str, int], None],
    max_rows: int,
) -> Dict[str, int]:
    """Import NDJSON lines inside the caller's transaction. Returns counters."""

    color_lookup = load_color_lookup(cursor)
    design_lookup = load_design_lookup(cursor)

    preserve_clause = (
        "WHERE logo.assignment.updated_by = ANY(%(actors)s)" if preserve_manual else ""
    )
    sql = UPSERT_SQL.format(preserve_clause=preserve_clause)

    counts = {"imported": 0, "skipped_manual": 0, "misses": 0, "rows": 0}
    seen: Set[Tuple[str, str, str, int, int]] = set()
    for line_number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        counts["rows"] += 1
        if counts["rows"] > max_rows:
            raise RowMiss("invalid_csv", f"import may contain at most {max_rows} rows")
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise RowMiss("invalid_value", "each NDJSON line must be an object")
            values = resolve_row(raw, color_lookup, design_lookup)
            key = (
                values["fdm4_store"],
                values["product_style"],
                values["garment_color_code"],
                values["option_row"],
                values["position"],
            )
            if key in seen:
                raise RowMiss("duplicate_row", "duplicate assignment key in NDJSON")
            if values["position"] > 1:
                cursor.execute(
                    """
                    SELECT 1 FROM logo.assignment
                     WHERE fdm4_store = %s AND product_style = %s
                       AND garment_color_code = %s AND option_row = %s
                       AND position = 1 AND active = true
                     LIMIT 1
                    """,
                    key[:4],
                )
                if cursor.fetchone() is None:
                    raise RowMiss(
                        "orphaned_companion",
                        "position 2/3 requires an active position-1 assignment in the same option row",
                    )
            # Per-row placement differences within a style/position are valid
            # legacy data (pocket vs chest logo rows); the projection collapses
            # the placements[] label to the first non-empty location, so no
            # conflict gate here.
            seen.add(key)
        except RowMiss as exc:
            source = raw if isinstance(locals().get("raw"), dict) else {}
            report(source, exc.reason, exc.detail, line_number)
            counts["misses"] += 1
            continue
        except json.JSONDecodeError as exc:
            report({}, "invalid_csv", f"invalid JSON: {exc.msg}", line_number)
            counts["misses"] += 1
            continue

        params: Dict[str, Any] = dict(values)
        if preserve_manual:
            params["actors"] = list(IMPORT_ACTORS)
        cursor.execute("SAVEPOINT legacy_import_row")
        try:
            cursor.execute(sql, params)
            applied = cursor.rowcount > 0
            cursor.execute("RELEASE SAVEPOINT legacy_import_row")
        except Exception as exc:
            cursor.execute("ROLLBACK TO SAVEPOINT legacy_import_row")
            cursor.execute("RELEASE SAVEPOINT legacy_import_row")
            report(values, "database_error", f"database rejected row ({type(exc).__name__})", line_number)
            counts["misses"] += 1
            continue
        if applied:
            counts["imported"] += 1
        else:
            counts["skipped_manual"] += 1

    del user_login  # recorded via report(); assignment rows carry 'legacy-import'
    return counts
