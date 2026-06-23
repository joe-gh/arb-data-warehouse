#!/usr/bin/env python3
"""
Pull one complete product (style) from FDM4 OpenEdge and write it as JSON.

The output file contains BOTH the SQL queries used (under _meta.queries) and
the assembled data, so it doubles as documentation of the pull process.

Usage:
  JAVA_HOME=/opt/homebrew/opt/openjdk python3 db-test/product_pull.py 400240
  -> writes db-test/product_400240.json

Read-only; uses the same env file / driver jar as explore.py.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from explore import load_env, connect, run_select  # noqa: E402

ITEMS_OF_STYLE = "SELECT \"item-number\" FROM PUB.\"item\" WHERE \"style-code\" = '{s}'"

QUERIES = {
    "style": (
        "SELECT \"style-code\", description, web_description, \"catalog-description\", "
        "\"item-status\", \"web-active\", brand_code, season_code, \"division-code\", style_price "
        "FROM PUB.\"style\" WHERE \"style-code\" = '{s}'"
    ),
    "colors": (
        "SELECT \"color-code\", description, \"color-alias\" "
        "FROM PUB.\"style-color\" WHERE \"style-code\" = '{s}'"
    ),
    "sizes": (
        "SELECT \"size-code\", description "
        "FROM PUB.\"style-size\" WHERE \"style-code\" = '{s}'"
    ),
    "items": (
        "SELECT \"item-number\", \"upc-code\", \"color-code\", \"size-code\", "
        "\"active\", \"web-active\", \"web_item_desc\" "
        "FROM PUB.\"item\" WHERE \"style-code\" = '{s}'"
    ),
    "prices": (
        "SELECT \"item-number\", \"base-price\", \"sale-price\", \"effective-date\", currency "
        "FROM PUB.\"price-list\" WHERE \"item-number\" IN (" + ITEMS_OF_STYLE + ")"
    ),
    "stock": (
        "SELECT \"item-number\", SUM(\"inv-bal\") AS on_hand, SUM(allocated) AS allocated, "
        "SUM(backordered) AS backordered, SUM(\"on-order\") AS on_order "
        "FROM PUB.\"item-balance\" WHERE \"item-number\" IN (" + ITEMS_OF_STYLE + ") "
        "GROUP BY \"item-number\""
    ),
    "channel_pricing": (
        "SELECT site_id, catalog_id, reg_price, sale_price, item_status, active, img, description "
        "FROM PUB.\"catalog_product\" WHERE product_id = '{s}'"
    ),
}


def rows_as_dicts(cols, rows):
    return [dict(zip(cols, r)) for r in rows]


def fetch(conn, sql):
    cols, rows = run_select(conn, sql, max_rows=5000)
    return rows_as_dicts(cols, rows)


def split_extent(value):
    """OpenEdge 'extent' (array) columns arrive semicolon-packed: '80;75;0'."""
    if value is None:
        return []
    return [v for v in str(value).split(";")]


def pull(style):
    cfg = load_env()
    jar = cfg.get("OPENEDGE_JAR")
    conn = connect(cfg, cfg.get("DB1_NAME", "fdm4"), jar)

    q = {k: v.format(s=style) for k, v in QUERIES.items()}

    style_rows = fetch(conn, q["style"])
    if not style_rows:
        sys.exit(f"Style {style} not found in PUB.style")

    colors = {r["color-code"]: r["description"] for r in fetch(conn, q["colors"])}
    sizes = {r["size-code"]: r["description"] for r in fetch(conn, q["sizes"])}
    items = fetch(conn, q["items"])
    prices = {r["item-number"]: r for r in fetch(conn, q["prices"])}
    stock = {r["item-number"]: r for r in fetch(conn, q["stock"])}
    channels = fetch(conn, q["channel_pricing"])
    conn.close()

    variations = []
    for it in items:
        num = it["item-number"]
        p = prices.get(num, {})
        s = stock.get(num, {})
        on_hand = s.get("ON_HAND")
        allocated = s.get("ALLOCATED")
        variations.append({
            "item_number": num,
            "sku_upc": it["upc-code"],          # = WooCommerce variation SKU
            "stock_available": (on_hand - (allocated or 0)) if on_hand is not None else None,
            "color_code": it["color-code"],
            "color": colors.get(it["color-code"]),
            "size_code": it["size-code"],
            "size": sizes.get(it["size-code"]),
            "active": it["active"],
            "web_active": it["web-active"],
            "web_item_desc": it["web_item_desc"],
            "base_price": p.get("base-price"),
            "sale_price_tiers": split_extent(p.get("sale-price")),  # tier->channel mapping TBD w/ FDM4
            "price_effective_dates": split_extent(p.get("effective-date")),
            "currency": p.get("currency"),
            "stock_on_hand": s.get("ON_HAND"),
            "stock_allocated": s.get("ALLOCATED"),
            "stock_backordered": s.get("BACKORDERED"),
            "stock_on_order": s.get("ON_ORDER"),
        })

    st = style_rows[0]
    doc = {
        "_meta": {
            "style": style,
            "source": "FDM4 / Progress OpenEdge 12.8.9, schema PUB (read-only JDBC)",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "notes": [
                "sku_upc on each variation equals the WooCommerce variation SKU (IPC).",
                "sale_price_tiers / price_effective_dates are OpenEdge extent (array) columns, semicolon-unpacked; tier semantics to confirm with FDM4.",
                "stock figures are summed across warehouse bins from item-balance; interpretation of inv-bal to confirm with FDM4.",
                "channel_pricing rows come from catalog_product per site_id (B2B/Retail/Tradeshow/S_<customer> store catalogs).",
            ],
            "queries": q,
        },
        "style": {
            "style_code": st["style-code"],
            "name": st["description"],
            "web_description": st["web_description"],
            "catalog_description": st["catalog-description"],
            "item_status": st["item-status"],
            "web_active": st["web-active"],
            "brand_code": st["brand_code"],
            "season_code": st["season_code"],
            "division_code": st["division-code"],
        },
        "channel_pricing": channels,
        "colors": colors,
        "sizes": sizes,
        "variations": variations,
        "summary": {
            "variation_count": len(variations),
            "color_count": len(colors),
            "size_count": len(sizes),
            "variations_web_active": sum(1 for v in variations if v["web_active"]),
            "total_on_hand": sum(v["stock_on_hand"] or 0 for v in variations),
        },
    }
    return doc


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: product_pull.py <style-code>")
    style = sys.argv[1]
    doc = pull(style)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"product_{style}.json")
    with open(out, "w") as fh:
        json.dump(doc, fh, indent=2, default=str)
    print(f"Wrote {out}")
    print(json.dumps(doc["summary"], indent=2, default=str))


if __name__ == "__main__":
    main()
