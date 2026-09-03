"""Category editor: the diff engine.

Turns (snapshot, draft tree, overlays, slug map, rules, assignments) into a
DECLARATIVE per-blog plan - the desired end-state payload the WordPress broker
converges toward, addressed by snapshot term_id/product_id with expected-slug
fences. Membership is by term_id in WordPress, so renames/re-slugs of kept
terms produce no membership writes; membership changes come only from merges,
deletes, exclusions and rule/add/remove assignments.

Semantics:
- map + primary  -> the live term is UPDATED in place (term_id survives).
- map + !primary -> merge source: products carry to the target, term deleted.
- delete         -> debris: term deleted (products must be rescued elsewhere
                    or acknowledged as intentionally uncategorized).
- store_custom   -> the live term is left untouched wherever it exists.
- excluded node on a store -> its primary term is deleted there; children
  re-parent to the nearest kept ancestor.
"""

from typing import Any, Dict, List, Optional, Set

import categories_draft
import categories_mapping
from categories_draft import normalize_wp_name
from categories_draft import DraftError
from categories_service import record_audit


CATEGORY_BASE = "/product-category/"
DELETED_REDIRECT_PATH = "/store/"

# Static reminders the planner cannot automate; surfaced as preview warnings.
CODE_WARNINGS = [
    "Size-chart gate (arb-elementor-widgets/widgets/product-logos.php) matches"
    " top-level category NAMES with parent=0 - update it for the new tree"
    " before applying to production.",
    "Sales Layer/ATIC re-applies category term IDs on product updates -"
    " schedule the PIM category remap after the migration; the drift audit"
    " catches interim reverts.",
]


# ---------------------------------------------------------------- targets


def blog_target(cursor, blog_id: int) -> Dict[str, Any]:
    """One store's target tree, planner-shaped."""

    nodes = categories_draft.list_nodes(cursor)
    by_id = {n["node_id"]: n for n in nodes}
    effective = categories_draft.effective_tree(cursor, blog_id)

    kept: Dict[int, Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    for entry in effective:
        if entry.get("extra"):
            extras.append(entry)
        else:
            kept[entry["node_id"]] = entry

    def nearest_kept_parent_slug(node_id: int) -> str:
        current = by_id.get(node_id, {}).get("parent_id")
        while current is not None:
            if current in kept:
                return kept[current]["slug"]
            current = by_id.get(current, {}).get("parent_id")
        return ""

    target: Dict[int, Dict[str, Any]] = {}
    for node_id, entry in kept.items():
        target[node_id] = {
            "node_id": node_id,
            "slug": entry["slug"],
            "name": entry["name"],
            "parent_slug": nearest_kept_parent_slug(node_id),
            "sort_order": entry["sort_order"],
            "description": entry.get("description", ""),
        }
    extra_targets = []
    for extra in extras:
        graft = extra.get("parent_id")
        extra_targets.append({
            "override_id": extra["override_id"],
            "slug": extra["slug"],
            "previous_slug": extra.get("previous_slug"),
            "name": extra["name"],
            "parent_slug": kept[graft]["slug"] if graft in kept else "",
            "sort_order": extra.get("sort_order", 0),
        })
    return {"kept": target, "extras": extra_targets, "all_nodes": by_id}


def load_dispositions(cursor) -> Dict[str, Dict[str, Any]]:
    """Explicit slug_map rows PLUS implicit identity mappings.

    A live slug equal to a draft node's slug is implicitly map+primary to
    that node (non-primary when the node already has an explicit primary
    elsewhere). Explicit rows always win. This is what makes a freshly
    seeded draft 100% mapped and a converged blog re-plan to zero changes.
    """

    cursor.execute(
        """
        SELECT m.old_slug, m.action, m.target_node_id, m.is_primary,
               m.override_id
          FROM catmgr.slug_map m
        """
    )
    dispositions = {row["old_slug"]: dict(row) for row in cursor.fetchall()}
    explicit_primary_nodes = {
        row["target_node_id"] for row in dispositions.values()
        if row["action"] == "map" and row["is_primary"]
    }
    cursor.execute("SELECT node_id, slug FROM catmgr.node")
    for row in cursor.fetchall():
        slug = row["slug"]
        if slug in dispositions:
            continue
        dispositions[slug] = {
            "old_slug": slug,
            "action": "map",
            "target_node_id": row["node_id"],
            "is_primary": row["node_id"] not in explicit_primary_nodes,
            "override_id": None,
            "implicit": True,
        }
    # Store-local extra nodes: a live term carrying an extra's slug is that
    # extra, materialized - implicitly store_custom (left untouched).
    cursor.execute(
        "SELECT override_id, blog_id, slug, previous_slug"
        " FROM catmgr.node_store_override WHERE kind = 'extra_node'"
    )
    for row in cursor.fetchall():
        slug = row["slug"]
        if slug and slug not in dispositions:
            dispositions[slug] = {
                "old_slug": slug,
                "action": "store_custom",
                "target_node_id": None,
                "is_primary": False,
                "override_id": row["override_id"],
                "implicit": True,
            }
        # The slug the extra's live term STILL carries after a draft re-slug:
        # store_custom everywhere, but on the extra's own blog the planner
        # converges that term in place to the new slug.
        previous = row["previous_slug"]
        if previous and previous not in dispositions:
            dispositions[previous] = {
                "old_slug": previous,
                "action": "store_custom",
                "target_node_id": None,
                "is_primary": False,
                "override_id": row["override_id"],
                "implicit": True,
                "extra_reslug": {"blog_id": row["blog_id"], "new_slug": slug,
                                 "override_id": row["override_id"]},
            }
    return dispositions


def load_extra_memberships(cursor, env: str) -> Dict[int, Dict[str, Set[str]]]:
    """Per node: rule-matched skus, explicit adds, explicit removes."""

    result: Dict[int, Dict[str, Set[str]]] = {}

    def bucket(node_id: int) -> Dict[str, Set[str]]:
        if node_id not in result:
            result[node_id] = {"rule": set(), "add": set(), "remove": set()}
        return result[node_id]

    for rule in categories_mapping.list_rules(cursor):
        outcome = categories_mapping.evaluate_rule(
            cursor, env, rule["spec"], limit=1000000,
        )
        bucket(rule["node_id"])["rule"].update(outcome["skus"])
    cursor.execute(
        "SELECT node_id, sku, mode FROM catmgr.product_assignment"
    )
    for row in cursor.fetchall():
        bucket(row["node_id"])[row["mode"]].add(row["sku"])
    return result


# ---------------------------------------------------------------- per blog


def _snapshot_terms(cursor, env: str, blog_id: int) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT term_id, slug, name, parent_term_id, description, sort_order
          FROM catmgr.wp_term WHERE env = %s AND blog_id = %s
         ORDER BY term_id
        """,
        (env, blog_id),
    )
    return [dict(row) for row in cursor.fetchall()]


def _snapshot_products(cursor, env: str, blog_id: int) -> Dict[int, Dict[str, Any]]:
    cursor.execute(
        """
        SELECT p.product_id, p.sku, t.slug
          FROM catmgr.wp_term_product p
          JOIN catmgr.wp_term t ON t.env = p.env AND t.blog_id = p.blog_id
                                AND t.term_id = p.term_id
         WHERE p.env = %s AND p.blog_id = %s
        """,
        (env, blog_id),
    )
    products: Dict[int, Dict[str, Any]] = {}
    for row in cursor.fetchall():
        product = products.setdefault(
            row["product_id"], {"product_id": row["product_id"],
                                "sku": row["sku"], "slugs": set()},
        )
        product["slugs"].add(row["slug"])
    return products


def build_blog_plan(cursor, env: str, blog_id: int, *,
                    dispositions: Optional[Dict[str, Dict[str, Any]]] = None,
                    extra_memberships: Optional[Dict[int, Dict[str, Set[str]]]] = None,
                    ) -> Dict[str, Any]:
    """The declarative desired end-state for one blog."""

    cursor.execute(
        "SELECT version, blog_path FROM catmgr.snapshot"
        " WHERE env = %s AND blog_id = %s",
        (env, blog_id),
    )
    snapshot = cursor.fetchone()
    if snapshot is None:
        raise DraftError(f"no snapshot for {env} blog {blog_id}")

    if dispositions is None:
        dispositions = load_dispositions(cursor)
    if extra_memberships is None:
        extra_memberships = load_extra_memberships(cursor, env)
    target = blog_target(cursor, blog_id)
    kept = target["kept"]                       # node_id -> desired term
    terms = _snapshot_terms(cursor, env, blog_id)

    unmapped = [t["slug"] for t in terms if t["slug"] not in dispositions]
    if unmapped:
        raise DraftError(
            f"blog {blog_id} has {len(unmapped)} unmapped slugs"
            f" (e.g. {', '.join(sorted(unmapped)[:5])})"
        )

    updates: List[Dict[str, Any]] = []
    deletes: List[Dict[str, Any]] = []
    kept_current: Dict[str, str] = {}   # live slug -> its FINAL slug on this blog
    merge_target_slug: Dict[str, Optional[str]] = {}  # live slug -> target final slug
    nodes_with_primary_here: Set[int] = set()
    live_by_slug = {t["slug"]: t for t in terms}
    live_by_id = {t["term_id"]: t for t in terms}

    extra_by_override = {e["override_id"]: e for e in target["extras"]}
    reslugged_extras: Set[int] = set()   # override_ids converged in place here
    for term in terms:
        disposition = dispositions[term["slug"]]
        action = disposition["action"]
        reslug = disposition.get("extra_reslug")
        if (action == "store_custom" and reslug and reslug["blog_id"] == blog_id
                and reslug["override_id"] in extra_by_override):
            extra = extra_by_override[reslug["override_id"]]
            if extra["slug"] in live_by_slug:
                # The new slug already exists live (converged earlier, or a
                # collision): the old term merges into it.
                deletes.append({"term_id": term["term_id"],
                                "expected_slug": term["slug"],
                                "reason": "merge"})
                merge_target_slug[term["slug"]] = extra["slug"]
                continue
            reslugged_extras.add(extra["override_id"])
            kept_current[term["slug"]] = extra["slug"]
            live_parent = live_by_id.get(term["parent_term_id"] or 0)
            live_parent_slug = live_parent["slug"] if live_parent else ""
            changes = {"slug": True}
            if normalize_wp_name(term["name"]) != extra["name"]:
                changes["name"] = True
            if live_parent_slug != extra["parent_slug"]:
                changes["parent"] = True
            if int(term["sort_order"] or 0) != int(extra["sort_order"] or 0):
                changes["sort_order"] = True
            updates.append({
                "term_id": term["term_id"],
                "expected_slug": term["slug"],
                "set": {
                    "slug": extra["slug"],
                    "name": extra["name"],
                    "parent_slug": extra["parent_slug"],
                    "description": term["description"] or "",
                    "sort_order": extra["sort_order"],
                },
                "changed": changes,
            })
            continue
        if action == "store_custom":
            kept_current[term["slug"]] = term["slug"]
            continue
        if action == "delete":
            deletes.append({"term_id": term["term_id"],
                            "expected_slug": term["slug"],
                            "reason": "delete"})
            merge_target_slug[term["slug"]] = None
            continue
        node_id = disposition["target_node_id"]
        desired = kept.get(node_id)
        is_primary_here = disposition["is_primary"]
        if (not is_primary_here and desired is not None
                and term["slug"] == desired["slug"]
                and node_id not in nodes_with_primary_here):
            # The live term already carries the node's final slug (typically
            # the post-apply steady state while an explicit primary still
            # names the pre-migration slug): it IS the node - converge it in
            # place instead of treating it as a merge source.
            is_primary_here = True
        if is_primary_here:
            if node_id in nodes_with_primary_here:
                # Two live terms claim the same node on this blog (old primary
                # slug and final slug coexisting): the first one wins, this
                # one merges into it.
                deletes.append({"term_id": term["term_id"],
                                "expected_slug": term["slug"],
                                "reason": "merge"})
                merge_target_slug[term["slug"]] = desired["slug"] if desired else None
                continue
            if desired is None:
                # Node excluded on this store: the term goes away here.
                deletes.append({"term_id": term["term_id"],
                                "expected_slug": term["slug"],
                                "reason": "excluded"})
                merge_target_slug[term["slug"]] = None
                continue
            nodes_with_primary_here.add(node_id)
            kept_current[term["slug"]] = desired["slug"]
            changes = {}
            if term["slug"] != desired["slug"]:
                changes["slug"] = True
            if normalize_wp_name(term["name"]) != desired["name"]:
                changes["name"] = True
            live_parent = live_by_id.get(term["parent_term_id"] or 0)
            live_parent_slug = live_parent["slug"] if live_parent else ""
            if live_parent_slug != desired["parent_slug"]:
                changes["parent"] = True
            if (term["description"] or "") != (desired["description"] or ""):
                changes["description"] = True
            if int(term["sort_order"] or 0) != int(desired["sort_order"] or 0):
                changes["sort_order"] = True
            updates.append({
                "term_id": term["term_id"],
                "expected_slug": term["slug"],
                "set": {
                    "slug": desired["slug"],
                    "name": desired["name"],
                    "parent_slug": desired["parent_slug"],
                    "description": desired["description"],
                    "sort_order": desired["sort_order"],
                },
                "changed": changes,
            })
        else:
            # Merge source: products carry to the target (if kept here).
            deletes.append({"term_id": term["term_id"],
                            "expected_slug": term["slug"],
                            "reason": "merge"})
            merge_target_slug[term["slug"]] = (
                desired["slug"] if desired is not None else None
            )

    creates: List[Dict[str, Any]] = []
    absorbed: List[str] = []
    for node_id, desired in kept.items():
        if node_id in nodes_with_primary_here:
            continue
        if desired["slug"] in live_by_slug and \
                dispositions.get(desired["slug"], {}).get("action") == "store_custom":
            # A store-custom live term already owns this slug here. Creating
            # the node would converge INTO that term (the broker updates an
            # existing slug in place), silently turning the store's custom
            # category into the global one. Surface it as a collision instead:
            # the operator maps the slug into the node or renames the node.
            absorbed.append(desired["slug"])
            continue
        creates.append({
            "slug": desired["slug"],
            "name": desired["name"],
            "parent_slug": desired["parent_slug"],
            "description": desired["description"],
            "sort_order": desired["sort_order"],
        })
    for extra in target["extras"]:
        if extra["slug"] in live_by_slug or extra["override_id"] in reslugged_extras:
            continue  # store_custom live term stays as-is / converged in place
        creates.append({
            "slug": extra["slug"],
            "name": extra["name"],
            "parent_slug": extra["parent_slug"],
            "description": "",
            "sort_order": extra["sort_order"],
        })

    # Creates must land parents-first so parent_slug resolves.
    create_order: Dict[str, int] = {}
    creating = {c["slug"]: c for c in creates}

    def depth_of(slug: str) -> int:
        depth = 0
        current = creating.get(slug)
        while current is not None and current["parent_slug"]:
            depth += 1
            current = creating.get(current["parent_slug"])
            if depth > 10:
                break
        return depth

    for c in creates:
        create_order[c["slug"]] = depth_of(c["slug"])
    creates.sort(key=lambda c: (create_order[c["slug"]], c["slug"]))

    # ----- memberships: only products whose final set differs from what the
    # term operations alone would leave behind.
    node_final_slug_here = {n: d["slug"] for n, d in kept.items()}
    sku_extra_add: Dict[str, Set[str]] = {}
    sku_remove: Dict[str, Set[str]] = {}
    for node_id, buckets in extra_memberships.items():
        final_slug = node_final_slug_here.get(node_id)
        if final_slug is None:
            continue
        for sku in (buckets["rule"] | buckets["add"]) - buckets["remove"]:
            sku_extra_add.setdefault(sku, set()).add(final_slug)
        for sku in buckets["remove"]:
            sku_remove.setdefault(sku, set()).add(final_slug)

    memberships: List[Dict[str, Any]] = []
    products = _snapshot_products(cursor, env, blog_id)
    zero_category: List[Dict[str, Any]] = []
    for product in products.values():
        after_term_ops: Set[str] = set()
        final: Set[str] = set()
        for slug in product["slugs"]:
            kept_slug = kept_current.get(slug)
            if kept_slug is not None:
                after_term_ops.add(kept_slug)
                final.add(kept_slug)
                continue
            target_slug = merge_target_slug.get(slug)
            if target_slug:
                final.add(target_slug)
        final |= sku_extra_add.get(product["sku"], set())
        final -= sku_remove.get(product["sku"], set())
        if not final:
            zero_category.append({"product_id": product["product_id"],
                                  "sku": product["sku"]})
        if final != after_term_ops:
            memberships.append({
                "product_id": product["product_id"],
                "expected_sku": product["sku"],
                "final_slugs": sorted(final),
            })
    memberships.sort(key=lambda m: m["product_id"])

    # ----- redirects (blog 1 only; links are flat /product-category/<slug>/)
    redirects: List[Dict[str, str]] = []
    if blog_id == 1:
        for update in updates:
            if update["changed"].get("slug"):
                redirects.append({
                    "old_path": f"{CATEGORY_BASE}{update['expected_slug']}/",
                    "new_path": f"{CATEGORY_BASE}{update['set']['slug']}/",
                })
        for delete in deletes:
            if delete["reason"] == "merge":
                target_slug = merge_target_slug.get(delete["expected_slug"])
                if target_slug:
                    redirects.append({
                        "old_path": f"{CATEGORY_BASE}{delete['expected_slug']}/",
                        "new_path": f"{CATEGORY_BASE}{target_slug}/",
                    })
            elif delete["reason"] == "delete":
                redirects.append({
                    "old_path": f"{CATEGORY_BASE}{delete['expected_slug']}/",
                    "new_path": DELETED_REDIRECT_PATH,
                })

    stats = {
        "terms_total": len(terms),
        "updates": len(updates),
        "changed_updates": sum(1 for u in updates if u["changed"]),
        "renames": sum(1 for u in updates if u["changed"].get("name")),
        "reslugs": sum(1 for u in updates if u["changed"].get("slug")),
        "creates": len(creates),
        "deletes": len(deletes),
        "membership_changes": len(memberships),
        "products_total": len(products),
        "zero_category": len(zero_category),
        "redirects": len(redirects),
    }
    return {
        "env": env,
        "blog_id": blog_id,
        "blog_path": snapshot["blog_path"],
        "snapshot_version": snapshot["version"],
        "terms": {"update": updates, "create": creates, "delete": deletes},
        "memberships": memberships,
        "redirects": redirects,
        "zero_category": zero_category,
        # Live slugs that stay exactly as they are (store customs and
        # unchanged kept terms): a final slug landing on one of these is a
        # collision the broker would silently absorb.
        "kept_slugs": sorted(
            slug for slug, final in kept_current.items() if slug == final
        ),
        "absorbed_slugs": sorted(absorbed),
        "stats": stats,
    }


# ---------------------------------------------------------------- preview


def list_acks(cursor) -> List[Dict[str, Any]]:
    cursor.execute(
        "SELECT sku, note, added_by, added_at FROM catmgr.uncategorized_ack"
        " ORDER BY sku"
    )
    return [dict(row) for row in cursor.fetchall()]


def set_acks(cursor, *, skus: List[str], note: str = "", actor: str) -> int:
    from psycopg2.extras import execute_values

    clean = sorted({str(sku).strip().upper() for sku in skus if str(sku).strip()})
    if not clean:
        raise DraftError("no skus given")
    execute_values(
        cursor,
        """
        INSERT INTO catmgr.uncategorized_ack (sku, note, added_by)
        VALUES %s
        ON CONFLICT (sku) DO UPDATE
           SET note = EXCLUDED.note, added_by = EXCLUDED.added_by,
               added_at = now()
        """,
        [(sku, str(note or ""), actor[:100]) for sku in clean],
    )
    record_audit(cursor, actor=actor, action="uncategorized_acked",
                 entity="ack", entity_key=",".join(clean[:10]),
                 detail={"count": len(clean), "note": note})
    return len(clean)


def delete_ack(cursor, sku: str, *, actor: str) -> None:
    cursor.execute(
        "DELETE FROM catmgr.uncategorized_ack WHERE sku = %s RETURNING sku",
        (str(sku).strip().upper(),),
    )
    if cursor.fetchone() is None:
        raise DraftError(f"no acknowledgement for sku {sku!r}")
    record_audit(cursor, actor=actor, action="uncategorized_unacked",
                 entity="ack", entity_key=sku, detail={})


def preview(cursor, env: str, blog_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Aggregate blockers, warnings, and per-blog stats for a plan."""

    cursor.execute(
        "SELECT blog_id FROM catmgr.snapshot WHERE env = %s ORDER BY blog_id",
        (env,),
    )
    known = [row["blog_id"] for row in cursor.fetchall()]
    if blog_ids:
        unknown = sorted(set(blog_ids) - set(known))
        if unknown:
            raise DraftError(
                f"no {env} snapshot for blog(s) {', '.join(map(str, unknown))};"
                " import them first"
            )
        blog_ids = [b for b in known if b in set(blog_ids)]
    else:
        blog_ids = known

    mapping = categories_mapping.mapping_status(cursor, env)
    unmapped = [s["old_slug"] for s in mapping["slugs"] if not s["action"]]

    dispositions = load_dispositions(cursor)
    extra_memberships = load_extra_memberships(cursor, env)
    acked = {row["sku"] for row in list_acks(cursor)}

    blogs: List[Dict[str, Any]] = []
    blocked_blogs: List[Dict[str, Any]] = []
    collisions: Dict[int, List[str]] = {}
    zero_by_sku: Dict[str, int] = {}
    zero_detail: Dict[str, Dict[str, Any]] = {}
    changed_blog1_slugs: List[Dict[str, str]] = []
    redirect_count = 0
    totals = {"updates": 0, "creates": 0, "deletes": 0,
              "membership_changes": 0, "reslugs": 0, "renames": 0}

    if not unmapped:
        for blog_id in blog_ids:
            try:
                plan = build_blog_plan(
                    cursor, env, blog_id,
                    dispositions=dispositions,
                    extra_memberships=extra_memberships,
                )
            except DraftError as exc:
                blocked_blogs.append({"blog_id": blog_id, "error": str(exc)})
                continue
            row_collisions = _plan_slug_collisions(plan)
            if row_collisions:
                collisions[blog_id] = row_collisions
            for zero in plan["zero_category"]:
                if zero["sku"] and zero["sku"] not in acked:
                    zero_by_sku[zero["sku"]] = zero_by_sku.get(zero["sku"], 0) + 1
                    detail = zero_detail.setdefault(zero["sku"], {"sku": zero["sku"], "blogs": []})
                    if len(detail["blogs"]) < 10:
                        detail["blogs"].append({"blog_id": blog_id, "product_id": zero["product_id"]})
            if blog_id == 1:
                changed_blog1_slugs = [
                    {"from": u["expected_slug"], "to": u["set"]["slug"]}
                    for u in plan["terms"]["update"]
                    if u["changed"].get("slug")
                ]
            redirect_count += len(plan["redirects"])
            for key in totals:
                totals[key] += plan["stats"].get(key, 0)
            blogs.append({
                "blog_id": blog_id,
                "blog_path": plan["blog_path"],
                "snapshot_version": plan["snapshot_version"],
                "stats": plan["stats"],
            })

    blockers: List[Dict[str, Any]] = []
    if unmapped:
        blockers.append({
            "kind": "unmapped_slugs",
            "count": len(unmapped),
            "sample": sorted(unmapped)[:50],
        })
    if blocked_blogs:
        blockers.append({"kind": "plan_errors", "blogs": blocked_blogs[:20]})
    if collisions:
        blockers.append({
            "kind": "slug_collisions",
            "blogs": [{"blog_id": b, "slugs": s[:20]}
                      for b, s in sorted(collisions.items())][:20],
        })
    if zero_by_sku:
        blockers.append({
            "kind": "zero_category_skus",
            "count": len(zero_by_sku),
            "sample": [
                {"sku": sku, "blogs": count}
                for sku, count in sorted(zero_by_sku.items())[:100]
            ],
            # Enough detail for the UI to acknowledge or rescue each one.
            "skus": [
                {"sku": sku, "blogs": zero_by_sku[sku],
                 "where": zero_detail[sku]["blogs"]}
                for sku in sorted(zero_by_sku)[:500]
            ],
        })

    warnings: List[Dict[str, Any]] = [
        {"kind": "code_item", "message": message} for message in CODE_WARNINGS
    ]
    if changed_blog1_slugs:
        warnings.append({
            "kind": "blog1_slug_changes",
            "message": "Blog-1 category slugs change: check custom CSS keyed on"
                       " product-cat-{slug} body classes and the UNSPSC mapping"
                       " (rewritten automatically at apply).",
            "changes": changed_blog1_slugs[:100],
        })
    warnings.append({"kind": "redirects", "count": redirect_count})

    return {
        "env": env,
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "totals": totals,
        "blogs": blogs,
    }


def _plan_slug_collisions(plan: Dict[str, Any]) -> List[str]:
    """Two final slugs on one blog, or a final slug landing on a live term the
    plan leaves untouched (a store custom), would make the broker converge two
    categories into one. Both are blockers."""
    seen: Set[str] = set()
    collisions: Set[str] = set()
    changing = {u["expected_slug"] for u in plan["terms"]["update"]}
    kept = set(plan.get("kept_slugs") or []) - changing
    finals = (
        [u["set"]["slug"] for u in plan["terms"]["update"]]
        + [c["slug"] for c in plan["terms"]["create"]]
    )
    for slug in finals:
        if slug in seen or slug in kept:
            collisions.add(slug)
        seen.add(slug)
    collisions.update(plan.get("absorbed_slugs") or [])
    return sorted(collisions)
