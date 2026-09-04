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


def zero_category_key(blog_id: int, product_id: int, sku: str) -> str:
    """Acknowledgement key of a product that would end uncategorized: its SKU,
    or PID:<blog>:<product_id> for products with no SKU at all (blank-SKU
    products used to slip past the blocker entirely)."""
    sku = str(sku or "").strip().upper()
    return sku if sku else f"PID:{int(blog_id)}:{int(product_id)}"


CATEGORY_BASE = "/product-category/"
DELETED_REDIRECT_PATH = "/store/"
# The broker parks a changing/doomed term on catmgrtmp-<term_id> during its
# temp pass; a live slug still wearing it is the leftover of a refused or
# crashed apply.
TEMP_SLUG_PREFIX = "catmgrtmp-"

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

    Store extras are implicitly store_custom under their slug and, while a
    draft re-slug is pending, under the slug their term still carries
    (previous_slug). Which BLOG the extra belongs to is resolved per blog in
    build_blog_plan (the same store-local slug may exist on several stores).
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
    cursor.execute(
        "SELECT override_id, blog_id, slug, previous_slug"
        " FROM catmgr.node_store_override WHERE kind = 'extra_node'"
    )
    for row in cursor.fetchall():
        for slug in (row["slug"], row["previous_slug"]):
            if slug and slug not in dispositions:
                dispositions[slug] = {
                    "old_slug": slug,
                    "action": "store_custom",
                    "target_node_id": None,
                    "is_primary": False,
                    "override_id": row["override_id"],
                    "implicit": True,
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
        SELECT term_id, slug, name, parent_term_id, description, sort_order,
               parked_from
          FROM catmgr.wp_term WHERE env = %s AND blog_id = %s
         ORDER BY term_id
        """,
        (env, blog_id),
    )
    terms = [dict(row) for row in cursor.fetchall()]
    for term in terms:
        # The slug the plan reasons about; the live slug is only the fence.
        term["logical"] = term["parked_from"] or term["slug"]
    return terms


def _snapshot_products(cursor, env: str, blog_id: int) -> Dict[int, Dict[str, Any]]:
    """Every product of the blog: categorized ones with their (logical) slugs
    and term ids, plus the uncategorized ones with empty sets."""
    cursor.execute(
        """
        SELECT p.product_id, p.sku, p.term_id,
               COALESCE(NULLIF(t.parked_from, ''), t.slug) AS slug
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
                                "sku": row["sku"], "slugs": set(),
                                "term_ids": set()},
        )
        product["slugs"].add(row["slug"])
        product["term_ids"].add(int(row["term_id"]))
    cursor.execute(
        "SELECT product_id, sku FROM catmgr.wp_uncategorized_product"
        " WHERE env = %s AND blog_id = %s",
        (env, blog_id),
    )
    for row in cursor.fetchall():
        products.setdefault(
            row["product_id"], {"product_id": row["product_id"],
                                "sku": row["sku"], "slugs": set(),
                                "term_ids": set()},
        )
    return products


def _elect_survivors(terms: List[Dict[str, Any]],
                     dispositions: Dict[str, Dict[str, Any]],
                     kept: Dict[int, Dict[str, Any]]) -> Dict[int, int]:
    """node_id -> term_id of the live term that survives in place on this
    blog. Deterministic: explicit primary if live here, else the term already
    carrying the node's final slug, else the lowest term_id."""
    candidates: Dict[int, List[Dict[str, Any]]] = {}
    for term in terms:
        disposition = dispositions.get(term["logical"])
        if disposition and disposition["action"] == "map":
            candidates.setdefault(disposition["target_node_id"], []).append(term)
    survivors: Dict[int, int] = {}
    for node_id, live in candidates.items():
        desired = kept.get(node_id)
        final_slug = desired["slug"] if desired else None
        explicit = [
            t for t in live
            if dispositions[t["logical"]]["is_primary"]
            and not dispositions[t["logical"]].get("implicit")
        ]
        identity = [t for t in live if final_slug and t["logical"] == final_slug]
        pool = explicit or identity or live
        survivors[node_id] = min(pool, key=lambda t: t["term_id"])["term_id"]
    return survivors


def build_blog_plan(cursor, env: str, blog_id: int, *,
                    dispositions: Optional[Dict[str, Dict[str, Any]]] = None,
                    extra_memberships: Optional[Dict[int, Dict[str, Set[str]]]] = None,
                    ) -> Dict[str, Any]:
    """The declarative desired end-state for one blog."""

    cursor.execute(
        "SELECT version, blog_path, fingerprint FROM catmgr.snapshot"
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

    unmapped = sorted({t["logical"] for t in terms if t["logical"] not in dispositions})
    if unmapped:
        raise DraftError(
            f"blog {blog_id} has {len(unmapped)} unmapped slugs"
            f" (e.g. {', '.join(unmapped[:5])})"
        )

    updates: List[Dict[str, Any]] = []
    deletes: List[Dict[str, Any]] = []
    kept_current: Dict[str, str] = {}   # logical slug -> its FINAL slug on this blog
    merge_target_slug: Dict[str, Optional[str]] = {}  # logical slug -> target final slug
    nodes_with_primary_here: Set[int] = set()
    live_by_logical = {t["logical"]: t for t in terms}
    live_by_id = {t["term_id"]: t for t in terms}
    survivors = _elect_survivors(terms, dispositions, kept)

    # This blog's own store extras, reachable under their draft slug and,
    # while a re-slug is pending, under the slug the term still carries.
    extras_by_slug: Dict[str, Dict[str, Any]] = {}
    for extra in target["extras"]:
        extras_by_slug[extra["slug"]] = extra
        if extra.get("previous_slug"):
            extras_by_slug.setdefault(extra["previous_slug"], extra)
    converged_extras: Set[int] = set()

    def live_parent_slug_of(term: Dict[str, Any]) -> str:
        parent = live_by_id.get(term["parent_term_id"] or 0)
        return parent["logical"] if parent else ""

    def emit_update(term: Dict[str, Any], desired: Dict[str, Any],
                    description: Optional[str]) -> None:
        changes: Dict[str, bool] = {}
        # A parked term must get its final slug back even when the logical
        # slug already equals the target.
        if term["logical"] != desired["slug"] or term["parked_from"]:
            changes["slug"] = True
        if normalize_wp_name(term["name"]) != desired["name"]:
            changes["name"] = True
        if live_parent_slug_of(term) != desired["parent_slug"]:
            changes["parent"] = True
        final_description = term["description"] or "" if description is None else description
        if (term["description"] or "") != (final_description or ""):
            changes["description"] = True
        if int(term["sort_order"] or 0) != int(desired["sort_order"] or 0):
            changes["sort_order"] = True
        updates.append({
            "term_id": term["term_id"],
            "expected_slug": term["slug"],
            "public_slug": term["logical"],
            "set": {
                "slug": desired["slug"],
                "name": desired["name"],
                "parent_slug": desired["parent_slug"],
                "description": final_description or "",
                "sort_order": desired["sort_order"],
            },
            "changed": changes,
        })

    def emit_delete(term: Dict[str, Any], reason: str,
                    target_slug: Optional[str]) -> None:
        deletes.append({"term_id": term["term_id"],
                        "expected_slug": term["slug"],
                        "public_slug": term["logical"],
                        "reason": reason})
        merge_target_slug[term["logical"]] = target_slug

    for term in terms:
        logical = term["logical"]
        disposition = dispositions[logical]
        action = disposition["action"]
        if action == "store_custom":
            extra = extras_by_slug.get(logical)
            if extra is None:
                # Another store's custom category, or an explicit store_custom
                # slug: untouched here.
                kept_current[logical] = logical
                continue
            if extra["override_id"] in converged_extras:
                # Two live terms belong to the same extra (old slug + new slug
                # both live): the first converged in place, this one merges.
                emit_delete(term, "merge", extra["slug"])
                continue
            converged_extras.add(extra["override_id"])
            kept_current[logical] = extra["slug"]
            emit_update(term, {
                "slug": extra["slug"], "name": extra["name"],
                "parent_slug": extra["parent_slug"],
                "sort_order": extra["sort_order"],
            }, description=None)
            continue
        if action == "delete":
            emit_delete(term, "delete", None)
            continue
        node_id = disposition["target_node_id"]
        desired = kept.get(node_id)
        if survivors.get(node_id) != term["term_id"]:
            # Merge source: products carry to the survivor (if kept here).
            emit_delete(term, "merge", desired["slug"] if desired is not None else None)
            continue
        if desired is None:
            # Node excluded on this store: the term goes away here.
            emit_delete(term, "excluded", None)
            continue
        nodes_with_primary_here.add(node_id)
        kept_current[logical] = desired["slug"]
        emit_update(term, desired, description=desired["description"])

    creates: List[Dict[str, Any]] = []
    absorbed: List[str] = []
    for node_id, desired in kept.items():
        if node_id in nodes_with_primary_here:
            continue
        if desired["slug"] in live_by_logical and \
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
        if extra["override_id"] in converged_extras:
            continue  # its live term converged in place above
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

    # ----- memberships: products whose final set differs from what the term
    # operations alone would leave behind, PLUS every product on a doomed
    # term. finalize deletes a doomed term only when it is EMPTY (anything
    # still attached there means WordPress drifted), so a product that is
    # already in the merge target - or ends with no category at all - still
    # needs its explicit row to leave the doomed term first. Every row carries
    # the product's snapshot identity (sku, term ids) as the broker's fence.
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
    uncategorized_total = 0
    for product in products.values():
        if not product["slugs"]:
            uncategorized_total += 1
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
        if not final and (product["slugs"] or product["sku"] in sku_remove):
            # A product that HAD categories (or was explicitly removed) and
            # ends with none. Products that were already uncategorized and
            # nothing rescues stay as they are: no change, no blocker.
            zero_category.append({
                "product_id": product["product_id"], "sku": product["sku"],
                "key": zero_category_key(blog_id, product["product_id"], product["sku"]),
            })
        touches_doomed = any(slug in merge_target_slug for slug in product["slugs"])
        if final != after_term_ops or touches_doomed:
            memberships.append({
                "product_id": product["product_id"],
                "expected_sku": product["sku"],
                "expected_term_ids": sorted(product["term_ids"]),
                "final_slugs": sorted(final),
            })
    memberships.sort(key=lambda m: m["product_id"])

    # ----- redirects (blog 1 only; links are flat /product-category/<slug>/).
    # Every public URL that stops resolving gets one: re-slugs, merges,
    # deletes AND exclusions. A parked term's public URL is its original slug.
    redirects: List[Dict[str, str]] = []
    if blog_id == 1:
        for update in updates:
            if update["changed"].get("slug") and update["public_slug"] != update["set"]["slug"]:
                redirects.append({
                    "old_path": f"{CATEGORY_BASE}{update['public_slug']}/",
                    "new_path": f"{CATEGORY_BASE}{update['set']['slug']}/",
                })
        for delete in deletes:
            if delete["reason"] == "merge":
                target_slug = merge_target_slug.get(delete["public_slug"])
                if target_slug:
                    redirects.append({
                        "old_path": f"{CATEGORY_BASE}{delete['public_slug']}/",
                        "new_path": f"{CATEGORY_BASE}{target_slug}/",
                    })
                    continue
            redirects.append({
                "old_path": f"{CATEGORY_BASE}{delete['public_slug']}/",
                "new_path": DELETED_REDIRECT_PATH,
            })

    # ----- UNSPSC (blog 1): the network category->UNSPSC mapping is keyed by
    # slug. Re-slugs re-key their entry; merge sources hand their entry to the
    # survivor when it has none of its own.
    unspsc = {"renames": {}, "merges": {}}
    if blog_id == 1:
        unspsc["renames"] = {
            u["public_slug"]: u["set"]["slug"]
            for u in updates if u["changed"].get("slug") and u["public_slug"] != u["set"]["slug"]
        }
        unspsc["merges"] = {
            d["public_slug"]: merge_target_slug[d["public_slug"]]
            for d in deletes
            if d["reason"] == "merge" and merge_target_slug.get(d["public_slug"])
        }

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
        "uncategorized_total": uncategorized_total,
        "zero_category": len(zero_category),
        "redirects": len(redirects),
    }
    return {
        "env": env,
        "blog_id": blog_id,
        "blog_path": snapshot["blog_path"],
        "snapshot_version": snapshot["version"],
        # The live export must still hash to this right before the first
        # mutation; anything WordPress changed since the import refuses the
        # apply instead of being silently overwritten.
        "snapshot_fingerprint": snapshot["fingerprint"] or "",
        "terms": {"update": updates, "create": creates, "delete": deletes},
        "memberships": memberships,
        "redirects": redirects,
        "unspsc": unspsc,
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
    recreated: Dict[int, List[str]] = {}
    redirect_count = 0
    # changed_updates is what the operator reads as "categories changed";
    # updates alone counts every existing term the plan touches, changed or
    # not, and once read 10,217 for a cleanup that changed 2,012.
    totals = {"updates": 0, "changed_updates": 0, "creates": 0, "deletes": 0,
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
            row_recreated = _plan_recreated_slugs(plan)
            if row_recreated:
                recreated[blog_id] = row_recreated
            for zero in plan["zero_category"]:
                key = zero["key"]
                if key in acked:
                    continue
                zero_by_sku[key] = zero_by_sku.get(key, 0) + 1
                detail = zero_detail.setdefault(key, {"sku": zero["sku"], "key": key, "blogs": []})
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
    if recreated:
        blockers.append({
            "kind": "recreated_slugs",
            "message": "These slugs would be deleted and re-created as NEW terms"
                       " in the same run (new term_id; menus, ES documents and"
                       " design maps key on term_id). Map the live slug into the"
                       " node instead, or remove the node from the draft.",
            "blogs": [{"blog_id": b, "slugs": s[:20]}
                      for b, s in sorted(recreated.items())][:20],
        })
    if zero_by_sku:
        blockers.append({
            "kind": "zero_category_skus",
            "count": len(zero_by_sku),
            "sample": [
                {"sku": zero_detail[key]["sku"], "key": key, "blogs": count}
                for key, count in sorted(zero_by_sku.items())[:100]
            ],
            # Enough detail for the UI to acknowledge or rescue each one. A
            # blank-SKU product has sku "" and is keyed PID:<blog>:<product_id>.
            "skus": [
                {"sku": zero_detail[key]["sku"], "key": key,
                 "blogs": zero_by_sku[key], "where": zero_detail[key]["blogs"]}
                for key in sorted(zero_by_sku)[:500]
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


def _plan_recreated_slugs(plan: Dict[str, Any]) -> List[str]:
    """Creates whose slug a doomed live term on the same blog is vacating:
    the operator mapped the live term away (delete / merge) but left a draft
    node with the same slug, so the run would swap the term's identity. The
    design rule is mutate-in-place, never delete+recreate: a blocker."""
    doomed = {d.get("public_slug") or d["expected_slug"] for d in plan["terms"]["delete"]}
    return sorted(c["slug"] for c in plan["terms"]["create"] if c["slug"] in doomed)


def _plan_slug_collisions(plan: Dict[str, Any]) -> List[str]:
    """Two final slugs on one blog, or a final slug landing on a live term the
    plan leaves untouched (a store custom), would make the broker converge two
    categories into one. Both are blockers."""
    seen: Set[str] = set()
    collisions: Set[str] = set()
    changing = {u.get("public_slug") or u["expected_slug"] for u in plan["terms"]["update"]}
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
