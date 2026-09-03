"""Category editor: slug dispositions, assignment rules, product assignments.

The slug map is the migration's forcing function: every live slug in the
target environment needs a disposition (map / delete / store_custom) before a
plan can be previewed. Product membership for a draft node is computed as
    carried (products of old slugs mapped to it)
  ∪ rule matches ∪ explicit adds − explicit removes
at STYLE (sku) granularity; per-blog product ids resolve at plan time.
"""

import re
from typing import Any, Dict, List, Optional

from categories_draft import DraftConflict, DraftError, lock_draft
from categories_service import record_audit


RULE_FIELDS = ("name", "brand", "mill_code", "category", "sku")
RULE_OPS = ("equals", "prefix", "regex")
_MAX_REGEX_LEN = 200


# ---------------------------------------------------------------- slug map


def mapping_status(cursor, env: str) -> Dict[str, Any]:
    """Every live slug with its disposition.

    IDENTITY MAPPING IS IMPLICIT: a live slug equal to a draft node's slug
    counts as mapped (primary unless the node has an explicit primary
    elsewhere). Seeding the draft from live therefore starts at 100% mapped,
    and post-migration drift audits converge; only slugs that curation
    actually diverged from need explicit rows.
    """

    cursor.execute(
        """
        WITH live AS (
            SELECT t.slug,
                   count(DISTINCT t.blog_id)             AS blogs,
                   sum(t.count)                          AS products,
                   bool_or(t.blog_id = 1)                AS blog1,
                   max(t.name)                           AS sample_name
              FROM catmgr.wp_term t
             WHERE t.env = %s
             GROUP BY t.slug
        )
        SELECT live.slug AS old_slug, live.blogs, live.products, live.blog1,
               live.sample_name,
               COALESCE(m.action,
                        CASE WHEN implicit.node_id IS NOT NULL THEN 'map'
                             WHEN implicit_extra.override_id IS NOT NULL
                                 THEN 'store_custom' END)
                   AS action,
               COALESCE(m.target_node_id, implicit.node_id) AS target_node_id,
               COALESCE(
                   m.is_primary,
                   CASE WHEN implicit.node_id IS NOT NULL THEN
                       NOT EXISTS (SELECT 1 FROM catmgr.slug_map mp
                                    WHERE mp.target_node_id = implicit.node_id
                                      AND mp.is_primary)
                   END,
                   false) AS is_primary,
               (m.old_slug IS NULL AND (implicit.node_id IS NOT NULL
                    OR implicit_extra.override_id IS NOT NULL)) AS implicit,
               m.override_id, m.note, m.updated_by, m.updated_at,
               COALESCE(n.slug, implicit.slug) AS target_slug,
               COALESCE(n.name, implicit.name) AS target_name
          FROM live
          LEFT JOIN catmgr.slug_map m ON m.old_slug = live.slug
          LEFT JOIN catmgr.node n ON n.node_id = m.target_node_id
          LEFT JOIN catmgr.node implicit
            ON m.old_slug IS NULL AND implicit.slug = live.slug
          LEFT JOIN LATERAL (
              -- an extra's live term may still carry the slug it had before a
              -- draft re-slug (previous_slug): that is the same store custom.
              SELECT o.override_id FROM catmgr.node_store_override o
               WHERE o.kind = 'extra_node'
                 AND (o.slug = live.slug OR o.previous_slug = live.slug)
               LIMIT 1
          ) implicit_extra ON m.old_slug IS NULL AND implicit.node_id IS NULL
         ORDER BY (m.action IS NULL AND implicit.node_id IS NULL
                   AND implicit_extra.override_id IS NULL) DESC,
                  live.blog1 DESC, live.products DESC NULLS LAST, live.slug
        """,
        (env,),
    )
    slugs = [dict(row) for row in cursor.fetchall()]
    summary = {
        "total": len(slugs),
        "mapped": sum(1 for s in slugs if s["action"]),
        "unmapped": sum(1 for s in slugs if not s["action"]),
        "by_action": {},
    }
    for row in slugs:
        if row["action"]:
            summary["by_action"][row["action"]] = (
                summary["by_action"].get(row["action"], 0) + 1
            )
    return {"summary": summary, "slugs": slugs}


def _slug_exists_in_snapshots(cursor, old_slug: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM catmgr.wp_term WHERE slug = %s LIMIT 1", (old_slug,)
    )
    return cursor.fetchone() is not None


def set_mapping(cursor, *, old_slug: str, action: str,
                target_node_id: Optional[int] = None,
                is_primary: Optional[bool] = None,
                override_id: Optional[int] = None,
                note: str = "", actor: str) -> Dict[str, Any]:
    lock_draft(cursor)
    old_slug = str(old_slug or "").strip()
    if not old_slug:
        raise DraftError("old_slug is required")
    if action not in ("map", "delete", "store_custom"):
        raise DraftError(f"unknown mapping action: {action!r}")
    if not _slug_exists_in_snapshots(cursor, old_slug):
        raise DraftError(f"slug {old_slug!r} does not exist in any snapshot")

    if action == "map":
        if target_node_id is None:
            raise DraftError("map requires target_node_id")
        cursor.execute(
            "SELECT 1 FROM catmgr.node WHERE node_id = %s", (target_node_id,)
        )
        if cursor.fetchone() is None:
            raise DraftError(f"unknown node: {target_node_id}")
        override_id = None
        if is_primary is None:
            # First mapping into a node becomes the in-place survivor.
            cursor.execute(
                """
                SELECT 1 FROM catmgr.slug_map
                 WHERE target_node_id = %s AND is_primary AND old_slug <> %s
                """,
                (target_node_id, old_slug),
            )
            is_primary = cursor.fetchone() is None
        elif is_primary:
            cursor.execute(
                """
                SELECT old_slug FROM catmgr.slug_map
                 WHERE target_node_id = %s AND is_primary AND old_slug <> %s
                """,
                (target_node_id, old_slug),
            )
            other = cursor.fetchone()
            if other:
                raise DraftConflict(
                    f"node already has primary slug {other['old_slug']!r};"
                    " demote it first"
                )
    else:
        target_node_id = None
        is_primary = False
        if action == "delete":
            override_id = None
        elif override_id is not None:
            cursor.execute(
                "SELECT 1 FROM catmgr.node_store_override WHERE override_id = %s",
                (override_id,),
            )
            if cursor.fetchone() is None:
                raise DraftError(f"unknown override: {override_id}")

    cursor.execute(
        """
        INSERT INTO catmgr.slug_map
            (old_slug, action, target_node_id, is_primary, override_id, note,
             updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (old_slug) DO UPDATE
           SET action = EXCLUDED.action,
               target_node_id = EXCLUDED.target_node_id,
               is_primary = EXCLUDED.is_primary,
               override_id = EXCLUDED.override_id,
               note = EXCLUDED.note,
               updated_by = EXCLUDED.updated_by,
               updated_at = now()
        """,
        (old_slug, action, target_node_id, bool(is_primary), override_id,
         str(note or ""), actor[:100]),
    )
    record_audit(cursor, actor=actor, action="mapping_set", entity="slug_map",
                 entity_key=old_slug,
                 detail={"action": action, "target_node_id": target_node_id,
                         "is_primary": bool(is_primary)})
    cursor.execute(
        """
        SELECT m.*, n.slug AS target_slug, n.name AS target_name
          FROM catmgr.slug_map m
          LEFT JOIN catmgr.node n ON n.node_id = m.target_node_id
         WHERE m.old_slug = %s
        """,
        (old_slug,),
    )
    return dict(cursor.fetchone())


def clear_mapping(cursor, old_slug: str, *, actor: str) -> None:
    lock_draft(cursor)
    cursor.execute(
        "DELETE FROM catmgr.slug_map WHERE old_slug = %s RETURNING action",
        (old_slug,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DraftError(f"no mapping for slug {old_slug!r}")
    record_audit(cursor, actor=actor, action="mapping_cleared",
                 entity="slug_map", entity_key=old_slug,
                 detail={"was": row["action"]})


def bulk_set(cursor, rows: List[Dict[str, Any]], *, actor: str) -> List[Dict[str, Any]]:
    results = []
    for row in rows:
        try:
            saved = set_mapping(
                cursor,
                old_slug=row.get("old_slug"),
                action=row.get("action"),
                target_node_id=row.get("target_node_id"),
                is_primary=row.get("is_primary"),
                override_id=row.get("override_id"),
                note=row.get("note", ""),
                actor=actor,
            )
            results.append({"ok": True, "old_slug": saved["old_slug"],
                            "action": saved["action"]})
        except (DraftError, DraftConflict) as exc:
            results.append({"ok": False,
                            "old_slug": str(row.get("old_slug", "")),
                            "error": str(exc)})
    return results


def auto_suggest(cursor, env: str) -> List[Dict[str, Any]]:
    """Unmapped live slugs that exactly match a draft node's NAME (slug-exact
    matches are already implicit identity mappings and need no suggestion)."""

    cursor.execute(
        """
        WITH live AS (
            SELECT DISTINCT t.slug, max(t.name) AS name
              FROM catmgr.wp_term t
             WHERE t.env = %s
               AND NOT EXISTS (SELECT 1 FROM catmgr.slug_map m
                                WHERE m.old_slug = t.slug)
               AND NOT EXISTS (SELECT 1 FROM catmgr.node ni
                                WHERE ni.slug = t.slug)
             GROUP BY t.slug
        )
        SELECT live.slug AS old_slug, n.node_id, n.slug AS node_slug,
               n.name AS node_name, 'name_exact' AS reason
          FROM live
          JOIN catmgr.node n
            ON lower(btrim(n.name)) = lower(btrim(live.name))
         ORDER BY live.slug
        """,
        (env,),
    )
    seen = set()
    suggestions = []
    for row in cursor.fetchall():
        if row["old_slug"] in seen:
            continue  # slug matched multiple nodes ambiguously - keep first (slug_exact sorts naturally via join order)
        seen.add(row["old_slug"])
        suggestions.append(dict(row))
    return suggestions


# ---------------------------------------------------------------- rules


def _validate_spec(spec: Any) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise DraftError("rule spec must be an object")
    source = spec.get("from", "all")
    if source != "all":
        if (not isinstance(source, list) or not source
                or not all(isinstance(item, str) and item.strip() for item in source)):
            raise DraftError("spec.from must be 'all' or a non-empty slug list")
        source = [item.strip() for item in source]
    field = spec.get("field")
    if field not in RULE_FIELDS:
        raise DraftError(f"spec.field must be one of {', '.join(RULE_FIELDS)}")
    op = spec.get("op")
    if op not in RULE_OPS:
        raise DraftError(f"spec.op must be one of {', '.join(RULE_OPS)}")
    value = spec.get("value")
    if not isinstance(value, str) or not value.strip():
        raise DraftError("spec.value is required")
    value = value.strip()
    if op == "regex":
        if len(value) > _MAX_REGEX_LEN:
            raise DraftError(f"regex longer than {_MAX_REGEX_LEN} characters")
        try:
            re.compile(value)
        except re.error as exc:
            raise DraftError(f"invalid regex: {exc}") from exc
    return {"from": source, "field": field, "op": op, "value": value}


def list_rules(cursor, node_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if node_id is None:
        cursor.execute(
            """
            SELECT r.*, n.slug AS node_slug FROM catmgr.assignment_rule r
              JOIN catmgr.node n ON n.node_id = r.node_id
             ORDER BY r.node_id, r.priority, r.rule_id
            """
        )
    else:
        cursor.execute(
            """
            SELECT r.*, n.slug AS node_slug FROM catmgr.assignment_rule r
              JOIN catmgr.node n ON n.node_id = r.node_id
             WHERE r.node_id = %s
             ORDER BY r.priority, r.rule_id
            """,
            (node_id,),
        )
    return [dict(row) for row in cursor.fetchall()]


def set_rule(cursor, *, node_id: int, spec: Any, priority: int = 0,
             note: str = "", rule_id: Optional[int] = None,
             actor: str) -> Dict[str, Any]:
    from psycopg2.extras import Json

    lock_draft(cursor)
    cursor.execute("SELECT 1 FROM catmgr.node WHERE node_id = %s", (node_id,))
    if cursor.fetchone() is None:
        raise DraftError(f"unknown node: {node_id}")
    clean = _validate_spec(spec)
    if rule_id is None:
        cursor.execute(
            """
            INSERT INTO catmgr.assignment_rule
                (node_id, spec, priority, note, updated_by)
            VALUES (%s, %s, %s, %s, %s) RETURNING rule_id
            """,
            (node_id, Json(clean), int(priority), str(note or ""), actor[:100]),
        )
        rule_id = cursor.fetchone()["rule_id"]
    else:
        cursor.execute(
            """
            UPDATE catmgr.assignment_rule
               SET node_id = %s, spec = %s, priority = %s, note = %s,
                   updated_by = %s, updated_at = now()
             WHERE rule_id = %s
            """,
            (node_id, Json(clean), int(priority), str(note or ""),
             actor[:100], rule_id),
        )
        if cursor.rowcount == 0:
            raise DraftError(f"unknown rule: {rule_id}")
    record_audit(cursor, actor=actor, action="rule_saved", entity="rule",
                 entity_key=str(rule_id),
                 detail={"node_id": node_id, "spec": clean})
    rows = list_rules(cursor, node_id)
    return next(r for r in rows if r["rule_id"] == rule_id)


def delete_rule(cursor, rule_id: int, *, actor: str) -> None:
    lock_draft(cursor)
    cursor.execute(
        "DELETE FROM catmgr.assignment_rule WHERE rule_id = %s RETURNING node_id",
        (rule_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DraftError(f"unknown rule: {rule_id}")
    record_audit(cursor, actor=actor, action="rule_deleted", entity="rule",
                 entity_key=str(rule_id), detail={"node_id": row["node_id"]})


def _rule_condition(field: str, op: str):
    column = {
        "name": "s.name", "brand": "s.brand", "mill_code": "s.mill_code",
        "category": "s.category", "sku": "u.sku",
    }[field]
    if op == "equals":
        return f"lower(btrim({column})) = lower(btrim(%s))"
    if op == "prefix":
        return f"lower(btrim({column})) LIKE lower(btrim(%s)) || '%%'"
    return f"{column} ~* %s"


def evaluate_rule(cursor, env: str, spec: Any, *, limit: int = 50) -> Dict[str, Any]:
    clean = _validate_spec(spec)
    params: List[Any] = [env]
    universe = """
        SELECT DISTINCT p.sku
          FROM catmgr.wp_term_product p
          JOIN catmgr.wp_term t ON t.env = p.env AND t.blog_id = p.blog_id
                                AND t.term_id = p.term_id
         WHERE p.env = %s AND p.sku <> ''
    """
    if clean["from"] != "all":
        universe += " AND t.slug = ANY(%s)"
        params.append(list(clean["from"]))
    condition = _rule_condition(clean["field"], clean["op"])
    params.append(clean["value"])
    cursor.execute(
        f"""
        WITH universe AS ({universe}),
        matched AS (
            SELECT DISTINCT u.sku
              FROM universe u
              JOIN woo.store_product_state s
                ON upper(btrim(s.sku)) = u.sku AND s.kind = 'parent'
             WHERE {condition}
        )
        SELECT (SELECT count(*) FROM matched) AS total,
               (SELECT array_agg(sku ORDER BY sku)
                  FROM (SELECT sku FROM matched ORDER BY sku LIMIT {int(limit)}) x
               ) AS skus
        """,
        params,
    )
    row = cursor.fetchone()
    return {"count": int(row["total"] or 0), "skus": list(row["skus"] or [])}


# ---------------------------------------------------------------- assignments


def list_assignments(cursor, node_id: int) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, node_id, sku, mode, source, note, added_by, added_at
          FROM catmgr.product_assignment
         WHERE node_id = %s ORDER BY mode, sku
        """,
        (node_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def set_assignments(cursor, *, node_id: int, skus: List[str], mode: str,
                    source: str = "manual", note: str = "",
                    actor: str) -> Dict[str, Any]:
    lock_draft(cursor)
    if mode not in ("add", "remove"):
        raise DraftError("mode must be add or remove")
    if source not in ("manual", "csv", "ai", "rule"):
        raise DraftError("bad source")
    cursor.execute("SELECT 1 FROM catmgr.node WHERE node_id = %s", (node_id,))
    if cursor.fetchone() is None:
        raise DraftError(f"unknown node: {node_id}")
    clean = sorted({str(sku).strip().upper() for sku in skus if str(sku).strip()})
    if not clean:
        raise DraftError("no skus given")
    opposite = "remove" if mode == "add" else "add"
    cursor.execute(
        "DELETE FROM catmgr.product_assignment"
        " WHERE node_id = %s AND mode = %s AND sku = ANY(%s)",
        (node_id, opposite, clean),
    )
    from psycopg2.extras import execute_values
    execute_values(
        cursor,
        """
        INSERT INTO catmgr.product_assignment
            (node_id, sku, mode, source, note, added_by)
        VALUES %s
        ON CONFLICT (node_id, sku, mode) DO UPDATE
           SET source = EXCLUDED.source, note = EXCLUDED.note,
               added_by = EXCLUDED.added_by, added_at = now()
        """,
        [(node_id, sku, mode, source, str(note or ""), actor[:100])
         for sku in clean],
    )
    record_audit(cursor, actor=actor, action="assignments_set",
                 entity="assignment", entity_key=str(node_id),
                 detail={"mode": mode, "source": source, "count": len(clean)})
    return {"node_id": node_id, "mode": mode, "count": len(clean)}


def delete_assignment(cursor, assignment_id: int, *, actor: str) -> None:
    lock_draft(cursor)
    cursor.execute(
        "DELETE FROM catmgr.product_assignment WHERE id = %s"
        " RETURNING node_id, sku, mode",
        (assignment_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DraftError(f"unknown assignment: {assignment_id}")
    record_audit(cursor, actor=actor, action="assignment_deleted",
                 entity="assignment", entity_key=str(assignment_id),
                 detail=dict(row))


# ---------------------------------------------------------------- membership


def effective_membership(cursor, env: str, node_id: int,
                         *, sample: int = 50) -> Dict[str, Any]:
    """Style-level membership: carried ∪ rules ∪ adds − removes."""

    cursor.execute("SELECT 1 FROM catmgr.node WHERE node_id = %s", (node_id,))
    if cursor.fetchone() is None:
        raise DraftError(f"unknown node: {node_id}")

    cursor.execute(
        """
        SELECT DISTINCT p.sku
          FROM catmgr.slug_map m
          JOIN catmgr.wp_term t ON t.slug = m.old_slug AND t.env = %s
          JOIN catmgr.wp_term_product p ON p.env = t.env
                                        AND p.blog_id = t.blog_id
                                        AND p.term_id = t.term_id
         WHERE m.action = 'map' AND m.target_node_id = %s AND p.sku <> ''
        """,
        (env, node_id),
    )
    carried = {row["sku"] for row in cursor.fetchall()}

    rule_results = []
    rule_skus: set = set()
    for rule in list_rules(cursor, node_id):
        outcome = evaluate_rule(cursor, env, rule["spec"], limit=100000)
        rule_results.append({"rule_id": rule["rule_id"],
                             "count": outcome["count"]})
        rule_skus.update(outcome["skus"])

    added = set()
    removed = set()
    for row in list_assignments(cursor, node_id):
        (added if row["mode"] == "add" else removed).add(row["sku"])

    final = (carried | rule_skus | added) - removed
    return {
        "node_id": node_id,
        "carried_count": len(carried),
        "rules": rule_results,
        "added_count": len(added),
        "removed_count": len(removed),
        "final_count": len(final),
        "final_sample": sorted(final)[:sample],
    }
