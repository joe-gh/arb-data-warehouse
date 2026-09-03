"""Product-mix rules shared by the HTTP routes, MCP and the assistant.

Every function takes a caller-owned cursor and raises domain errors
(NotFound / InvalidCommand); HTTP callers translate them to status codes.
The invariants live here once: a store must be in the catalog, a list-mode
store is seeded from its current FDM4 mix before the mode flips, and an
active list-mode store is never left empty (the transform would remove
every product)."""

from typing import Any, Dict, List, Optional

from domain import InvalidCommand, NotFound

MIX_MODES = ("all", "list")


def norm(value: Any) -> str:
    return " ".join(str(value).split()).upper()


def known_store(cursor, store: str) -> None:
    """Same universe the UI store dropdown offers (woo.store_catalog)."""
    cursor.execute("SELECT 1 FROM woo.store_catalog WHERE fdm4_store = %s LIMIT 1", (store,))
    if not cursor.fetchone():
        raise NotFound(f"Unknown store code: {store}")


def registry(cursor, store: str, *, required: bool = True) -> Optional[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT fdm4_store, mode, active, note, imported_at
          FROM woo.store_mix_store WHERE fdm4_store = %s
        """,
        (store,),
    )
    row = cursor.fetchone()
    if required and (row is None or not row["active"]):
        raise NotFound(f"{store} is not using a custom product list")
    return dict(row) if row else None


def require_list_mode(registry_row: Dict[str, Any]) -> None:
    if registry_row["mode"] != "list":
        raise InvalidCommand(
            "This store follows FDM4 (mode 'all'); switch it to list mode before editing its product list"
        )


def seed_items(cursor, store: str, actor: str) -> int:
    """Copy the store's current FDM4 mix into store_mix_item (missing styles only).

    Prefers woo.store_mix_candidate (transform-maintained once the store is
    registered) and falls back to active state rows for a first enable.
    ON CONFLICT DO NOTHING is the merge contract: an existing style's colors /
    size excludes are NEVER touched, so operator removals don't resurrect.
    Virtual-catalog stores seed colors = NULL (all channels) so new FDM4 color
    channels keep flowing; normal stores seed their current color sets.
    """
    cursor.execute("SELECT 1 FROM woo.virtual_catalog_store WHERE fdm4_store = %s", (store,))
    virtual = cursor.fetchone() is not None
    cursor.execute("SELECT count(*) AS n FROM woo.store_mix_candidate WHERE fdm4_store = %s", (store,))
    if cursor.fetchone()["n"]:
        cursor.execute(
            """
            INSERT INTO woo.store_mix_item
                (fdm4_store, style_code, colors, source, added_by, updated_by)
            SELECT c.fdm4_store, upper(btrim(c.style_code)),
                   CASE WHEN %(virtual)s THEN NULL ELSE c.colors END,
                   'import', %(actor)s, %(actor)s
              FROM woo.store_mix_candidate c
             WHERE c.fdm4_store = %(store)s AND btrim(c.style_code) <> ''
            ON CONFLICT (fdm4_store, style_code) DO NOTHING
            """,
            {"store": store, "actor": actor, "virtual": virtual},
        )
    else:
        cursor.execute(
            """
            INSERT INTO woo.store_mix_item
                (fdm4_store, style_code, colors, source, added_by, updated_by)
            SELECT s.fdm4_store, upper(btrim(s.style_code)),
                   CASE WHEN %(virtual)s THEN NULL
                        ELSE array_agg(DISTINCT upper(btrim(s.color_code)))
                             FILTER (WHERE s.kind = 'variation'
                                     AND COALESCE(btrim(s.color_code), '') <> '')
                   END,
                   'import', %(actor)s, %(actor)s
              FROM woo.store_product_state s
             WHERE s.fdm4_store = %(store)s AND s.is_active
               AND COALESCE(btrim(s.style_code), '') <> ''
             GROUP BY s.fdm4_store, upper(btrim(s.style_code))
            ON CONFLICT (fdm4_store, style_code) DO NOTHING
            """,
            {"store": store, "actor": actor, "virtual": virtual},
        )
    return cursor.rowcount


def item_count(cursor, store: str) -> int:
    cursor.execute("SELECT count(*) AS n FROM woo.store_mix_item WHERE fdm4_store = %s", (store,))
    return int(cursor.fetchone()["n"])


def product_counts(cursor, store: str, styles: List[str]) -> List[Dict[str, Any]]:
    """How many live variations each style has in the store (0 = likely typo)."""
    cursor.execute(
        """
        SELECT upper(btrim(style_code)) AS style, count(*) AS products
          FROM woo.store_product_state
         WHERE fdm4_store = %s AND is_active AND kind = 'variation'
           AND upper(btrim(style_code)) = ANY(%s)
         GROUP BY 1
        """,
        (store, styles),
    )
    counts = {r["style"]: int(r["products"]) for r in cursor.fetchall()}
    return [{"style": s, "products": counts.get(s, 0)} for s in styles]


def enable(cursor, store: str, mode: str, note: str, actor: str, *, active: bool = True) -> Dict[str, Any]:
    """Create or update a store's mix override. list mode seeds the current
    FDM4 mix first and refuses to enable an empty list."""
    if mode not in MIX_MODES:
        raise InvalidCommand("mode must be 'all' or 'list'")
    known_store(cursor, store)
    existing = registry(cursor, store, required=False)
    cursor.execute(
        """
        INSERT INTO woo.store_mix_store
            (fdm4_store, mode, active, note, created_by, updated_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (fdm4_store) DO UPDATE SET
            mode = EXCLUDED.mode, active = EXCLUDED.active, note = EXCLUDED.note,
            updated_by = EXCLUDED.updated_by, updated_at = now()
        """,
        (store, mode, active, note, actor, actor),
    )
    imported = 0
    if mode == "list" and active:
        imported = seed_items(cursor, store, actor)
        if item_count(cursor, store) == 0:
            raise InvalidCommand(
                f"{store} has no products to seed - cannot enable list mode "
                "(an empty list would remove everything)"
            )
        cursor.execute(
            "UPDATE woo.store_mix_store SET imported_at = now() WHERE fdm4_store = %s", (store,),
        )
    return {
        "store": store, "mode": mode, "active": active, "imported": imported,
        "previous_mode": existing["mode"] if existing else None,
        "was_active": bool(existing["active"]) if existing else False,
    }


def disable(cursor, store: str, actor: str) -> None:
    cursor.execute(
        """
        UPDATE woo.store_mix_store
           SET active = false, updated_by = %s, updated_at = now()
         WHERE fdm4_store = %s AND active RETURNING fdm4_store
        """,
        (actor, store),
    )
    if not cursor.fetchone():
        raise NotFound(f"{store} is not using a custom product list")


def add_styles(cursor, store: str, styles: List[str], actor: str) -> Dict[str, Any]:
    reg = registry(cursor, store)
    require_list_mode(reg)
    saved = 0
    added: List[str] = []
    already: List[str] = []
    for style in styles:
        cursor.execute(
            """
            INSERT INTO woo.store_mix_item
                (fdm4_store, style_code, colors, source, added_by, updated_by)
            VALUES (%s, %s, NULL, 'manual', %s, %s)
            ON CONFLICT (fdm4_store, style_code) DO NOTHING
            """,
            (store, style, actor, actor),
        )
        saved += cursor.rowcount
        (added if cursor.rowcount else already).append(style)
    per_style = product_counts(cursor, store, styles)
    return {
        "saved": saved, "per_style": per_style, "added": added, "already_listed": already,
        "no_live_products": [p["style"] for p in per_style if p["products"] == 0],
    }


def remove_styles(cursor, store: str, styles: List[str]) -> int:
    reg = registry(cursor, store)
    require_list_mode(reg)
    cursor.execute(
        "DELETE FROM woo.store_mix_item WHERE fdm4_store = %s AND style_code = ANY(%s)",
        (store, styles),
    )
    removed = cursor.rowcount
    if removed and item_count(cursor, store) == 0:
        raise InvalidCommand(
            "This would leave the mix empty and remove every product from the store. "
            "Disable the override instead"
        )
    return removed
