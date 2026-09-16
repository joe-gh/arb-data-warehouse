"""Read services for the PIM (Sales Layer) side of the warehouse.

What the assistant, the MCP and the PIM view all ask: is a style in the PIM,
under which product, what did the push decide about it and why, how fresh is
each stage of the pipeline, and what is queued to go over next. Everything is
read-only against the mirror tables the pull keeps (pim.api_*), the push's
change sets (pim.push_change_*), Woo presence (pim.woo_presence), the FDM4
item master and woo.sync_control. The rule these answers explain is the one
push_pim.py enforces: FDM4 is the source of truth; the PIM carries exactly the
FDM4 styles a counting store carries, all visible.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from queries import QueryNotFound, QueryValidationError, _clean, _optional

# Mirrors push_pim.py's PIM_ALL_PRODUCTS_BLOGS default: these stores carry the
# whole FDM4 range and say nothing about merchandising.
ALL_PRODUCTS_BLOGS = (2, 36, 59)
# push_pim.py skips presence-driven rules when the set is older than this.
PRESENCE_MAX_AGE_HOURS = 3.0
# Audit-log actions the PIM push worker and the request route exchange.
REQUEST_ACTION = "pim_push_requested"
STARTED_ACTION = "pim_push_started"
FINISHED_ACTION = "pim_push_finished"
FAILED_ACTION = "pim_push_failed"

_ACTION_WORDS = {
    "product_create": "create product",
    "variant_create": "create variant",
    "product_publish": "make product visible",
    "variant_publish": "make variant visible",
    "product_remove": "delete product",
    "variant_orphan_remove": "delete orphan variant",
    "variant_remove": "delete variant",
    "style_fill": "fill style number",
    "color_fill": "fill color code",
    "color_fix": "fix color code",
    "product_draft": "set product to draft",
}


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value)


def _rows(cursor) -> List[dict]:
    return [dict(r) for r in cursor.fetchall()]


def _key(value: Any, field: str) -> str:
    return _clean(value, field).upper()


def action_word(action: str) -> str:
    return _ACTION_WORDS.get(action, action.replace("_", " "))


# ---------------------------------------------------------------- lookups


def _fdm4_style(cursor, style: str) -> Optional[dict]:
    cursor.execute(
        """
        SELECT count(*) AS items,
               count(*) FILTER (WHERE btrim("web-active") = 'True') AS web_active,
               count(*) FILTER (WHERE btrim("web-active") = 'True'
                                  AND btrim("retail-price") ~ '^[0-9]+(\\.[0-9]+)?$'
                                  AND "retail-price"::numeric > 0
                                  AND btrim("upc-code") <> '') AS sellable,
               array_remove(array_agg(DISTINCT NULLIF(btrim("upc-code"), '')), NULL) AS upcs,
               min(NULLIF(btrim("retail-price"), '')) AS retail_min,
               max(NULLIF(btrim("retail-price"), '')) AS retail_max,
               max(NULLIF(btrim("mill-code"), '')) AS mill_code
          FROM fdm4.item
         WHERE upper(btrim("style-code")) = %s
        """,
        (style,),
    )
    row = dict(cursor.fetchone())
    cursor.execute(
        'SELECT left(btrim(description), 120) AS description FROM fdm4.style WHERE upper(btrim("style-code")) = %s LIMIT 1',
        (style,),
    )
    master = cursor.fetchone()
    if not row["items"] and not master:
        return None
    row["description"] = master["description"] if master else None
    row["upcs"] = sorted(row["upcs"] or [])[:50]
    return row


def _woo_presence(cursor, style: str, upcs: List[str]) -> List[dict]:
    cursor.execute(
        """
        SELECT w.blog_id, w.parent_sku, w.visible, w.refreshed_at,
               m.blog_path, m.blog_name, m.fdm4_store,
               NOT (w.blog_id = ANY(%(all_blogs)s)) AS counting
          FROM pim.woo_presence w
          LEFT JOIN woo.store_blog_map m ON m.blog_id = w.blog_id
         WHERE w.env = 'production'
           AND (upper(w.parent_sku) = %(style)s OR w.upcs && %(upcs)s::text[])
         ORDER BY counting DESC, w.blog_id
         LIMIT 120
        """,
        {"style": style, "upcs": [u.upper() for u in upcs], "all_blogs": list(ALL_PRODUCTS_BLOGS)},
    )
    out = []
    for r in _rows(cursor):
        r["refreshed_at"] = _iso(r["refreshed_at"])
        out.append(r)
    return out


def _pim_products_for(cursor, style: str, upcs: List[str]) -> List[dict]:
    """PIM products matched by style number / reference, or by any variant UPC."""
    cursor.execute(
        """
        WITH by_key AS (
            SELECT p.prod_ref, 'style' AS matched_by, NULL::text AS matched_upc
              FROM pim.api_product p
             WHERE p.retired_at IS NULL
               AND (upper(btrim(p.style_number)) = %(style)s OR upper(btrim(p.prod_ref)) = %(style)s)
            UNION ALL
            SELECT v.prod_ref, 'variant_upc', v.frmt_ref
              FROM pim.api_variant v
             WHERE v.retired_at IS NULL AND coalesce(btrim(v.prod_ref), '') <> ''
               AND upper(btrim(v.frmt_ref)) = ANY(%(upcs)s::text[])
        ), keys AS (
            SELECT prod_ref, min(matched_by) AS matched_by,
                   array_remove(array_agg(DISTINCT matched_upc), NULL) AS matched_upcs
              FROM by_key GROUP BY prod_ref
        )
        SELECT k.prod_ref, k.matched_by, k.matched_upcs,
               p.style_number, p.payload ->> 'prod_stat' AS status,
               left(p.payload ->> 'prod_title', 120) AS title,
               (p.payload ->> 'prod_id')::bigint AS pim_product_id,
               p.payload ->> 'prod_creation' AS created_at,
               p.payload ->> 'prod_modify' AS modified_at,
               p.pulled_at,
               (SELECT count(*) FROM pim.api_variant v WHERE v.prod_ref = k.prod_ref AND v.retired_at IS NULL) AS variants,
               (SELECT count(*) FROM pim.api_variant v WHERE v.prod_ref = k.prod_ref AND v.retired_at IS NULL
                   AND v.payload ->> 'frmt_stat' = 'V') AS variants_visible,
               (SELECT count(*) FROM pim.api_variant v WHERE v.prod_ref = k.prod_ref AND v.retired_at IS NULL
                   AND v.payload ->> 'frmt_stat' = 'D') AS variants_draft
          FROM keys k
          JOIN pim.api_product p ON p.prod_ref = k.prod_ref AND p.retired_at IS NULL
         ORDER BY k.matched_by, k.prod_ref
         LIMIT 20
        """,
        {"style": style, "upcs": [u.upper() for u in upcs]},
    )
    out = []
    for r in _rows(cursor):
        r["pulled_at"] = _iso(r["pulled_at"])
        out.append(r)
    return out


def _push_rows(cursor, style: str, upcs: List[str], limit: int) -> List[dict]:
    cursor.execute(
        """
        SELECT r.row_id, r.set_id, s.created_at AS set_created_at, s.note AS set_note,
               r.action, r.status, r.prod_ref, r.frmt_ref, r.style_code,
               left(r.result, 200) AS result, r.applied_at,
               r.before ->> 'reason' AS reason
          FROM pim.push_change_row r
          JOIN pim.push_change_set s ON s.set_id = r.set_id
         WHERE upper(coalesce(r.style_code, '')) = %(style)s
            OR upper(coalesce(r.prod_ref, '')) = %(style)s
            OR upper(coalesce(r.frmt_ref, '')) = ANY(%(upcs)s::text[])
         ORDER BY r.row_id DESC
         LIMIT %(limit)s
        """,
        {"style": style, "upcs": [u.upper() for u in upcs], "limit": limit},
    )
    out = []
    for r in _rows(cursor):
        r["set_created_at"] = _iso(r["set_created_at"])
        r["applied_at"] = _iso(r["applied_at"])
        r["what"] = action_word(r["action"])
        out.append(r)
    return out


def pim_lookup(cursor, *, q: str) -> dict:
    """Find PIM products by style number, PIM reference, or variant UPC."""
    key = _key(q, "q")
    upcs = [key] if key.isdigit() and len(key) >= 8 else []
    fdm4 = _fdm4_style(cursor, key)
    if fdm4:
        upcs = sorted(set(upcs) | set(fdm4["upcs"]))
    products = _pim_products_for(cursor, key, upcs)
    if not products and not fdm4:
        # Maybe it is a UPC FDM4 knows under another style.
        cursor.execute(
            'SELECT upper(btrim("style-code")) AS style FROM fdm4.item WHERE upper(btrim("upc-code")) = %s LIMIT 1',
            (key,),
        )
        row = cursor.fetchone()
        if row:
            return pim_lookup(cursor, q=row["style"]) | {"query": key, "resolved_from_upc": True}
    return {
        "query": key,
        "in_fdm4": fdm4 is not None,
        "fdm4": fdm4,
        "products": products,
        "found": bool(products),
    }


def pim_push_history(cursor, *, q: str, limit: int = 20) -> dict:
    """Every change the push proposed or sent for a style, reference or UPC."""
    key = _key(q, "q")
    limit = max(1, min(int(limit), 200))
    fdm4 = _fdm4_style(cursor, key)
    upcs = list(fdm4["upcs"]) if fdm4 else ([key] if key.isdigit() else [])
    rows = _push_rows(cursor, key, upcs, limit)
    return {"query": key, "rows": rows, "count": len(rows)}


def pim_explain_style(cursor, *, style: str) -> dict:
    """Why a style is, or is not, in the PIM: FDM4 eligibility, Woo presence,
    PIM match, the push's last decisions, and a plain-language verdict."""
    key = _key(style, "style")
    fdm4 = _fdm4_style(cursor, key)
    upcs = list(fdm4["upcs"]) if fdm4 else []
    presence = _woo_presence(cursor, key, upcs)
    counting = [p for p in presence if p["counting"] and p["visible"]]
    products = _pim_products_for(cursor, key, upcs)
    history = _push_rows(cursor, key, upcs, 8)
    freshness = _presence_freshness(cursor)

    by_style = [p for p in products if p["matched_by"] == "style"]
    by_upc = [p for p in products if p["matched_by"] == "variant_upc"]

    if fdm4 is None:
        verdict = ("FDM4 does not know this style. It is not eligible for the PIM; if the PIM or a "
                   "Woo store still carries it, the rule removes it.")
        state = "not_in_fdm4"
    elif not fdm4["sellable"]:
        verdict = ("FDM4 has the style but no web-active item with a price and a UPC, so the push "
                   "does not treat it as sellable and will not create it.")
        state = "not_sellable"
    elif by_style:
        p = by_style[0]
        verdict = (f"In the PIM as {p['prod_ref']} ({'visible' if p['status'] == 'V' else 'draft'}, "
                   f"{p['variants']} variants: {p['variants_visible']} visible, {p['variants_draft']} draft).")
        state = "in_pim"
        if not counting:
            verdict += " No counting store carries it, so the next push removes it."
            state = "in_pim_not_live"
    elif by_upc:
        p = by_upc[0]
        verdict = (f"Its UPC{'s' if len(p['matched_upcs']) > 1 else ''} already exist in the PIM under "
                   f"product {p['prod_ref']} (style number {p['style_number'] or 'blank'}, "
                   f"{'visible' if p['status'] == 'V' else 'draft'}). The push will not create a duplicate "
                   f"{key}; match on the variant UPC, not the product style number.")
        state = "in_pim_under_other_product"
    elif not counting:
        where = ", ".join(sorted({(p["blog_path"] or str(p["blog_id"])) for p in presence})) or "no store"
        verdict = (f"Not live on any counting store (carried by {where}). The push creates only styles a "
                   f"store outside the all-products stores carries.")
        state = "not_live"
    else:
        verdict = ("Eligible: FDM4 sells it, a counting store carries it and the PIM lacks it. "
                   "The next push creates it (visible) with its variants.")
        state = "eligible"
    if freshness["stale"]:
        verdict += " Note: the Woo presence set is stale, so presence-driven rules are paused until it refreshes."

    return {
        "style": key,
        "state": state,
        "verdict": verdict,
        "fdm4": fdm4,
        "woo": {
            "live_on_counting_stores": [
                {"blog_id": p["blog_id"], "store": p["blog_path"], "name": p["blog_name"], "fdm4_store": p["fdm4_store"]}
                for p in counting
            ],
            "all_stores": [
                {"blog_id": p["blog_id"], "store": p["blog_path"], "visible": p["visible"], "counting": p["counting"]}
                for p in presence
            ],
            "presence": freshness,
        },
        "pim": {"products": products, "matched_by_style": bool(by_style), "matched_by_upc": bool(by_upc)},
        "push_history": history,
    }


# ---------------------------------------------------------------- pipeline


def _presence_freshness(cursor) -> dict:
    cursor.execute(
        """
        SELECT count(*) AS rows, max(refreshed_at) AS refreshed_at,
               coalesce(max(refreshed_at) < now() - %s * interval '1 hour', true) AS stale
          FROM pim.woo_presence WHERE env = 'production'
        """,
        (PRESENCE_MAX_AGE_HOURS,),
    )
    row = dict(cursor.fetchone())
    row["refreshed_at"] = _iso(row["refreshed_at"])
    row["max_age_hours"] = PRESENCE_MAX_AGE_HOURS
    return row


def _set_counts(cursor, set_id: int) -> List[dict]:
    cursor.execute(
        """
        SELECT action, status, count(*) AS n
          FROM pim.push_change_row WHERE set_id = %s
         GROUP BY 1, 2 ORDER BY 1, 2
        """,
        (set_id,),
    )
    return [dict(r) | {"what": action_word(r["action"])} for r in cursor.fetchall()]


def _set_samples(cursor, set_id: int, per_action: int = 5) -> List[dict]:
    cursor.execute(
        """
        SELECT action, status, prod_ref, frmt_ref, style_code,
               left(coalesce(after ->> 'prod_title', before ->> 'prod_title', after ->> 'frmt_variantname', ''), 80) AS title,
               before ->> 'reason' AS reason, left(result, 120) AS result
          FROM (
            SELECT r.*, row_number() OVER (PARTITION BY action ORDER BY row_id) AS rn
              FROM pim.push_change_row r WHERE r.set_id = %s
          ) x
         WHERE rn <= %s
         ORDER BY action, rn
        """,
        (set_id, per_action),
    )
    return [dict(r) | {"what": action_word(r["action"])} for r in cursor.fetchall()]


def _change_set(cursor, set_id: int, with_samples: bool = False) -> Optional[dict]:
    cursor.execute(
        "SELECT set_id, created_at, created_by, note, status FROM pim.push_change_set WHERE set_id = %s",
        (set_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    out = dict(row)
    out["created_at"] = _iso(out["created_at"])
    out["counts"] = _set_counts(cursor, set_id)
    out["total"] = sum(int(c["n"]) for c in out["counts"])
    out["applied"] = sum(int(c["n"]) for c in out["counts"] if c["status"] == "applied")
    out["failed"] = sum(int(c["n"]) for c in out["counts"] if c["status"] == "failed")
    out["pending"] = sum(int(c["n"]) for c in out["counts"] if c["status"] in ("proposed", "approved"))
    if with_samples:
        out["samples"] = _set_samples(cursor, set_id)
    return out


def pim_recent_pushes(cursor, *, limit: int = 10) -> dict:
    """The latest push change sets with what each proposed and sent."""
    limit = max(1, min(int(limit), 50))
    cursor.execute("SELECT set_id FROM pim.push_change_set ORDER BY set_id DESC LIMIT %s", (limit,))
    sets = [_change_set(cursor, int(r["set_id"])) for r in cursor.fetchall()]
    return {"sets": [s for s in sets if s]}


def pim_summary(cursor) -> dict:
    """PIM mirror totals: products and variants by status, orphans, and what
    the rule says should be there."""
    cursor.execute(
        """
        SELECT
          (SELECT count(*) FROM pim.api_product WHERE retired_at IS NULL) AS products,
          (SELECT count(*) FROM pim.api_product WHERE retired_at IS NULL AND payload ->> 'prod_stat' = 'V') AS products_visible,
          (SELECT count(*) FROM pim.api_product WHERE retired_at IS NULL AND payload ->> 'prod_stat' = 'D') AS products_draft,
          (SELECT count(*) FROM pim.api_variant WHERE retired_at IS NULL) AS variants,
          (SELECT count(*) FROM pim.api_variant WHERE retired_at IS NULL AND payload ->> 'frmt_stat' = 'V') AS variants_visible,
          (SELECT count(*) FROM pim.api_variant WHERE retired_at IS NULL AND payload ->> 'frmt_stat' = 'D') AS variants_draft,
          (SELECT count(*) FROM pim.api_variant WHERE retired_at IS NULL AND coalesce(btrim(prod_ref), '') = '') AS orphan_variants,
          (SELECT count(DISTINCT upper(parent_sku)) FROM pim.woo_presence
            WHERE env = 'production' AND visible AND NOT (blog_id = ANY(%(all_blogs)s))) AS live_parent_skus,
          (SELECT max(pulled_at) FROM pim.api_product) AS mirror_pulled_at
        """,
        {"all_blogs": list(ALL_PRODUCTS_BLOGS)},
    )
    out = dict(cursor.fetchone())
    out["mirror_pulled_at"] = _iso(out["mirror_pulled_at"])
    out["all_products_blogs"] = list(ALL_PRODUCTS_BLOGS)
    out["rule"] = ("The PIM carries exactly the FDM4 styles that a store outside the all-products stores "
                   "carries, all visible. Not in FDM4, or carried by no such store: removed.")
    return out


def pim_pipeline_status(cursor) -> dict:
    """Freshness of every stage the PIM depends on: FDM4 pull into the
    warehouse, Woo presence, the PIM mirror pull, and the last push run."""
    cursor.execute(
        """
        SELECT id, status, requested_at, started_at, finished_at, rows_loaded, refresh_version,
               left(coalesce(error, ''), 200) AS error
          FROM woo.sync_control WHERE op = 'pull' ORDER BY id DESC LIMIT 1
        """
    )
    latest_pull = cursor.fetchone()
    cursor.execute(
        """
        SELECT id, finished_at, rows_loaded, refresh_version,
               EXTRACT(EPOCH FROM (finished_at - started_at))::int AS duration_s
          FROM woo.sync_control WHERE op = 'pull' AND status = 'success' ORDER BY id DESC LIMIT 1
        """
    )
    last_ok_pull = cursor.fetchone()
    cursor.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'success') AS ok_24h,
               count(*) FILTER (WHERE status NOT IN ('success', 'running', 'requested')) AS failed_24h
          FROM woo.sync_control WHERE op = 'pull' AND requested_at > now() - interval '24 hours'
        """
    )
    pull_24h = dict(cursor.fetchone())
    cursor.execute("SELECT enabled, updated_at FROM woo.app_flag WHERE name = 'fdm4_load'")
    flag = cursor.fetchone()
    cursor.execute("SELECT entity, watermark, last_run, last_count, note FROM pim.api_pull_state ORDER BY entity")
    pim_pull = []
    for r in _rows(cursor):
        r["watermark"] = _iso(r["watermark"])
        r["last_run"] = _iso(r["last_run"])
        pim_pull.append(r)
    cursor.execute("SELECT max(set_id) AS set_id FROM pim.push_change_set")
    latest_set_id = cursor.fetchone()["set_id"]
    latest_set = _change_set(cursor, int(latest_set_id)) if latest_set_id else None
    cursor.execute(
        """
        SELECT s.set_id FROM pim.push_change_set s
         WHERE EXISTS (SELECT 1 FROM pim.push_change_row r WHERE r.set_id = s.set_id AND r.status = 'applied')
         ORDER BY s.set_id DESC LIMIT 1
        """
    )
    row = cursor.fetchone()
    last_sending_set = _change_set(cursor, int(row["set_id"])) if row else None

    def _stamp(r, field):
        return _iso(r[field]) if r and r.get(field) else None

    fdm4 = {
        "latest_run": {
            "id": latest_pull["id"] if latest_pull else None,
            "status": latest_pull["status"] if latest_pull else None,
            "started_at": _stamp(latest_pull, "started_at"),
            "finished_at": _stamp(latest_pull, "finished_at"),
            "error": latest_pull["error"] if latest_pull else None,
        },
        "last_success_at": _stamp(last_ok_pull, "finished_at"),
        "last_success_rows": last_ok_pull["rows_loaded"] if last_ok_pull else None,
        "last_success_duration_s": last_ok_pull["duration_s"] if last_ok_pull else None,
        "ok_24h": pull_24h["ok_24h"],
        "failed_24h": pull_24h["failed_24h"],
        "load_in_progress": bool(flag and flag["enabled"]),
        "schedule": "hourly at :00 (about 10 minutes)",
    }
    presence = _presence_freshness(cursor) | {"schedule": "hourly at :07 from the production site"}
    mirror = {"entities": pim_pull, "schedule": "hourly at :25 (incremental), Sunday 03:40 full"}
    push = {
        "latest_set": latest_set,
        "last_sending_set": last_sending_set,
        "schedule": "hourly at :45 (automatic; caps leave big batches proposed for a person)",
    }
    return {
        "now": _iso(datetime.datetime.now(datetime.timezone.utc)),
        "fdm4_pull": fdm4,
        "woo_presence": presence,
        "pim_mirror": mirror,
        "push": push,
        "summary": pim_summary(cursor),
    }


# ---------------------------------------------------------------- requests


def _request_rows(cursor, request_id: Optional[str], limit: int) -> List[dict]:
    """Requests and their outcomes, newest first, from the audit log."""
    cursor.execute(
        """
        WITH req AS (
            SELECT id, at, actor, detail
              FROM logo.audit_log
             WHERE action = %(req)s
               AND (%(rid)s IS NULL OR detail ->> 'request_id' = %(rid)s)
             ORDER BY id DESC LIMIT %(limit)s
        )
        SELECT r.id, r.at AS requested_at, r.actor, r.detail AS request,
               s.at AS started_at,
               f.action AS outcome_action, f.at AS finished_at, f.detail AS outcome
          FROM req r
          LEFT JOIN LATERAL (
              SELECT at FROM logo.audit_log a
               WHERE a.action = %(started)s AND a.detail ->> 'request_id' = r.detail ->> 'request_id'
               ORDER BY id DESC LIMIT 1) s ON true
          LEFT JOIN LATERAL (
              SELECT action, at, detail FROM logo.audit_log a
               WHERE a.action IN (%(finished)s, %(failed)s) AND a.detail ->> 'request_id' = r.detail ->> 'request_id'
               ORDER BY id DESC LIMIT 1) f ON true
         ORDER BY r.id DESC
        """,
        {"req": REQUEST_ACTION, "started": STARTED_ACTION, "finished": FINISHED_ACTION,
         "failed": FAILED_ACTION, "rid": request_id, "limit": limit},
    )
    out = []
    for r in _rows(cursor):
        if r["outcome_action"] == FINISHED_ACTION:
            state = "finished"
        elif r["outcome_action"] == FAILED_ACTION:
            state = "failed"
        elif r["started_at"]:
            state = "running"
        else:
            state = "queued"
        item = {
            "request_id": (r["request"] or {}).get("request_id"),
            "mode": (r["request"] or {}).get("mode"),
            "styles": (r["request"] or {}).get("styles") or [],
            "requested_by": r["actor"],
            "requested_at": _iso(r["requested_at"]),
            "started_at": _iso(r["started_at"]),
            "finished_at": _iso(r["finished_at"]),
            "state": state,
            "outcome": r["outcome"] or None,
        }
        set_id = (r["outcome"] or {}).get("set_id")
        if set_id:
            item["set"] = _change_set(cursor, int(set_id), with_samples=True)
        out.append(item)
    return out


def pim_requests(cursor, *, limit: int = 10) -> dict:
    """Recent on-demand preview / push requests and their outcomes."""
    limit = max(1, min(int(limit), 50))
    return {"requests": _request_rows(cursor, None, limit)}


def pim_request_status(cursor, *, request_id: str) -> dict:
    rid = _clean(request_id, "request_id", 64)
    rows = _request_rows(cursor, rid, 1)
    if not rows:
        raise QueryNotFound("request not found")
    return rows[0]


def pim_preview(cursor) -> dict:
    """What the next push would send: the most recent finished preview (a dry
    diff run on demand) and, as a fallback, the latest automatic set."""
    rows = _request_rows(cursor, None, 20)
    preview = next((r for r in rows if r["mode"] == "preview" and r["state"] == "finished"), None)
    cursor.execute("SELECT max(set_id) AS set_id FROM pim.push_change_set")
    latest = cursor.fetchone()["set_id"]
    return {
        "preview": preview,
        "latest_set": _change_set(cursor, int(latest), with_samples=True) if latest else None,
    }


def validate_styles(cursor, styles: List[str]) -> List[str]:
    """Upper-cased, de-duplicated style codes that FDM4 or the PIM knows."""
    cleaned: List[str] = []
    for raw in styles:
        code = _optional(raw, "style", 100).upper()
        if not code or code in cleaned:
            continue
        cursor.execute(
            """
            SELECT 1 WHERE EXISTS (SELECT 1 FROM fdm4.item WHERE upper(btrim("style-code")) = %(s)s)
                        OR EXISTS (SELECT 1 FROM fdm4.style WHERE upper(btrim("style-code")) = %(s)s)
                        OR EXISTS (SELECT 1 FROM pim.api_product WHERE retired_at IS NULL
                                     AND (upper(btrim(style_number)) = %(s)s OR upper(btrim(prod_ref)) = %(s)s))
            """,
            {"s": code},
        )
        if cursor.fetchone() is None:
            raise QueryValidationError(f"Unknown style {code}: neither FDM4 nor the PIM knows it")
        cleaned.append(code)
        if len(cleaned) > 100:
            raise QueryValidationError("At most 100 styles per request")
    return cleaned
