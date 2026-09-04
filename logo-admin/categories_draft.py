"""Category editor: the draft global tree + per-store overlays.

One working draft (catmgr.node) shared by every operator; per-store
customization lives in catmgr.node_store_override (extra_node / rename /
exclude). Every mutation first takes a transaction-scoped advisory lock on the
single draft scope, so concurrent editors serialize instead of interleaving,
and every mutation writes a catmgr.audit_log row.

All functions take an open cursor and never commit; the route owns the
transaction.
"""

import html
import re
from typing import Any, Dict, List, Optional

from psycopg2 import errors as pg_errors

from categories_service import record_audit


class DraftError(ValueError):
    """Invalid draft operation (bad parent, slug clash, cycle, ...)."""


class DraftConflict(RuntimeError):
    """The operation conflicts with existing draft state (409)."""


_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def lock_draft(cursor) -> None:
    """Serialize all draft mutations behind one advisory scope."""

    cursor.execute("SELECT pg_advisory_xact_lock(hashtext('catmgr_draft'))")


def clean_category_name(name: str) -> str:
    """Undo the historical double-encoded entity debris in category names.

    Mirrors the cleanup in the old curated-categories exporter: decode HTML
    entities until stable, collapse whitespace, then strip the dangling
    trailing '&'/'&amp;' fragments left by the old split-on-ampersand bug.
    """

    name = str(name or "")
    for _ in range(5):
        decoded = html.unescape(name)
        if decoded == name:
            break
        name = decoded
    name = re.sub(r"\s+", " ", name).strip()
    stripped = re.sub(r"(\s*&\s*(amp;?)*)+$", "", name, flags=re.I).strip()
    return stripped or name


def normalize_wp_name(name: str) -> str:
    """Decode WordPress's kses-normalized entity storage for comparison.

    WP stores term names with & encoded as &amp; (wp_filter_kses on
    pre_term_name re-encodes even a clean submitted name), so equality checks
    against draft names must compare the DECODED form - otherwise
    encoding-only differences read as perpetual renames. Unlike
    clean_category_name() this does NOT strip trailing '&' debris: a live
    "Batteries &" really is different from the draft's "Batteries" and must
    still converge.
    """

    name = str(name or "")
    for _ in range(5):
        decoded = html.unescape(name)
        if decoded == name:
            break
        name = decoded
    return re.sub(r"\s+", " ", name).strip()


def slugify(value: str) -> str:
    value = clean_category_name(value).lower()
    value = re.sub(r"['’]", "", value)          # apostrophes vanish: men's -> mens
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "category"


# Live-slug identity: a term the broker parked on catmgrtmp-<id> keeps its
# original slug in parked_from, and that original is the slug every
# disposition, mapping and lineage lookup must see.
LOGICAL_SLUG_SQL = "COALESCE(NULLIF(t.parked_from, ''), t.slug)"


def slug_is_live(cursor, slug: str, blog_id: Optional[int] = None) -> bool:
    """Does any snapshot (or one blog's) hold a term whose logical slug is this?"""
    if blog_id is None:
        cursor.execute(
            f"SELECT 1 FROM catmgr.wp_term t WHERE {LOGICAL_SLUG_SQL} = %s LIMIT 1",
            (slug,),
        )
    else:
        cursor.execute(
            f"SELECT 1 FROM catmgr.wp_term t WHERE t.blog_id = %s"
            f" AND {LOGICAL_SLUG_SQL} = %s LIMIT 1",
            (blog_id, slug),
        )
    return cursor.fetchone() is not None


def _slug_owner(cursor, slug: str, *, exclude_node_id: Optional[int] = None,
                exclude_override_id: Optional[int] = None,
                blog_id: Optional[int] = None) -> Optional[str]:
    """Who already owns a slug in the draft.

    Global nodes are unique across the whole draft. Store extras are per blog
    (WordPress taxonomy uniqueness is per site, and the unique index is
    (blog_id, slug)): the same store-local slug may exist on several stores.
    A global node may never share a slug with ANY store extra - an apply
    would converge the store's own term into the global category - and an
    extra may not share a slug with a global node or another extra on the
    same blog. blog_id=None means "checking for a global node"."""
    cursor.execute(
        "SELECT node_id, name FROM catmgr.node WHERE slug = %s AND node_id IS DISTINCT FROM %s",
        (slug, exclude_node_id),
    )
    row = cursor.fetchone()
    if row:
        return f"category {row['name']!r}"
    if blog_id is None:
        cursor.execute(
            """
            SELECT override_id, blog_id, name FROM catmgr.node_store_override
             WHERE kind = 'extra_node' AND slug = %s AND override_id IS DISTINCT FROM %s
             ORDER BY blog_id LIMIT 1
            """,
            (slug, exclude_override_id),
        )
    else:
        cursor.execute(
            """
            SELECT override_id, blog_id, name FROM catmgr.node_store_override
             WHERE kind = 'extra_node' AND slug = %s AND blog_id = %s
               AND override_id IS DISTINCT FROM %s
            """,
            (slug, blog_id, exclude_override_id),
        )
    row = cursor.fetchone()
    if row:
        return f"store extra {row['name']!r} on blog {row['blog_id']}"
    return None


def _extra_live_slug(cursor, blog_id: int, candidates: List[Optional[str]],
                     new_slug: str) -> Optional[str]:
    """The slug a store extra's term currently carries on its own blog, if it
    is not already the wanted slug. Lineage is read from the live snapshot,
    never carried forward blindly: after an apply converged the term to the
    new slug the lineage clears itself, and a second re-slug starts from what
    is actually live (blog 7 crew-shop -> crew-store -> crew-market)."""
    seen = []
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate != new_slug and candidate not in seen:
            seen.append(candidate)
    if not seen:
        return None
    cursor.execute(
        f"""
        SELECT {LOGICAL_SLUG_SQL} AS slug FROM catmgr.wp_term t
         WHERE t.blog_id = %s AND {LOGICAL_SLUG_SQL} = ANY(%s)
         ORDER BY (t.env = 'prod') DESC, t.env, t.term_id LIMIT 1
        """,
        (blog_id, seen),
    )
    row = cursor.fetchone()
    return row["slug"] if row else None


def _validate_slug(slug: str) -> str:
    slug = str(slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise DraftError(
            f"invalid slug {slug!r}: lowercase letters, digits and single hyphens only"
        )
    return slug


# ---------------------------------------------------------------- reads


def list_nodes(cursor) -> List[Dict[str, Any]]:
    cursor.execute(
        """
        SELECT node_id, parent_id, name, slug, sort_order, description,
               updated_by, updated_at
          FROM catmgr.node
         ORDER BY parent_id NULLS FIRST, sort_order, name, node_id
        """
    )
    return [dict(row) for row in cursor.fetchall()]


def _node(cursor, node_id: int) -> Dict[str, Any]:
    cursor.execute(
        """
        SELECT node_id, parent_id, name, slug, sort_order, description,
               updated_by, updated_at
          FROM catmgr.node WHERE node_id = %s
        """,
        (node_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DraftError(f"unknown node: {node_id}")
    return dict(row)


def _assert_no_cycle(cursor, node_id: int, new_parent_id: Optional[int]) -> None:
    seen = set()
    current = new_parent_id
    while current is not None:
        if current == node_id:
            raise DraftError("cannot move a node under its own subtree")
        if current in seen:
            raise DraftError("draft tree contains a parent cycle")
        seen.add(current)
        cursor.execute(
            "SELECT parent_id FROM catmgr.node WHERE node_id = %s", (current,)
        )
        row = cursor.fetchone()
        if row is None:
            raise DraftError(f"unknown parent node: {current}")
        current = row["parent_id"]


def _resequence_siblings(cursor, parent_id: Optional[int],
                         moved_node_id: int, position: Optional[int]) -> None:
    """Renumber one sibling group 10,20,30... placing the moved node."""

    if parent_id is None:
        cursor.execute(
            """
            SELECT node_id FROM catmgr.node
             WHERE parent_id IS NULL AND node_id <> %s
             ORDER BY sort_order, name, node_id
            """,
            (moved_node_id,),
        )
    else:
        cursor.execute(
            """
            SELECT node_id FROM catmgr.node
             WHERE parent_id = %s AND node_id <> %s
             ORDER BY sort_order, name, node_id
            """,
            (parent_id, moved_node_id),
        )
    siblings = [row["node_id"] for row in cursor.fetchall()]
    if position is None or position < 0 or position > len(siblings):
        position = len(siblings)
    siblings.insert(position, moved_node_id)
    for index, sibling_id in enumerate(siblings):
        cursor.execute(
            "UPDATE catmgr.node SET sort_order = %s WHERE node_id = %s",
            ((index + 1) * 10, sibling_id),
        )


# ---------------------------------------------------------------- mutations


def create_node(cursor, *, parent_id: Optional[int], name: str,
                slug: Optional[str] = None, description: str = "",
                position: Optional[int] = None, actor: str) -> Dict[str, Any]:
    lock_draft(cursor)
    name = clean_category_name(name)
    if not name:
        raise DraftError("a category name is required")
    if parent_id is not None:
        _node(cursor, parent_id)  # must exist
    if slug:
        slug = _validate_slug(slug)
        owner = _slug_owner(cursor, slug)
        if owner:
            raise DraftConflict(f"slug already in use by {owner}: {slug}")
    else:
        base = slugify(name)
        slug = base
        suffix = 2
        while _slug_owner(cursor, slug):
            slug = f"{base}-{suffix}"
            suffix += 1
    cursor.execute(
        """
        INSERT INTO catmgr.node (parent_id, name, slug, description, updated_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING node_id
        """,
        (parent_id, name, slug, str(description or ""), actor[:100]),
    )
    node_id = cursor.fetchone()["node_id"]
    _resequence_siblings(cursor, parent_id, node_id, position)
    record_audit(cursor, actor=actor, action="node_created", entity="node",
                 entity_key=str(node_id),
                 detail={"name": name, "slug": slug, "parent_id": parent_id})
    return _node(cursor, node_id)


def update_node(cursor, node_id: int, *, name: Optional[str] = None,
                slug: Optional[str] = None, description: Optional[str] = None,
                actor: str) -> Dict[str, Any]:
    lock_draft(cursor)
    node = _node(cursor, node_id)
    changes: Dict[str, Any] = {}
    if name is not None:
        name = clean_category_name(name)
        if not name:
            raise DraftError("a category name is required")
        if name != node["name"]:
            changes["name"] = {"from": node["name"], "to": name}
    if slug is not None:
        slug = _validate_slug(slug)
        if slug != node["slug"]:
            owner = _slug_owner(cursor, slug, exclude_node_id=node_id)
            if owner:
                raise DraftConflict(f"slug already in use by {owner}: {slug}")
            changes["slug"] = {"from": node["slug"], "to": slug}
    if description is not None and description != node["description"]:
        changes["description"] = True
    if not changes:
        return node
    cursor.execute(
        """
        UPDATE catmgr.node
           SET name = COALESCE(%s, name),
               slug = COALESCE(%s, slug),
               description = COALESCE(%s, description),
               updated_by = %s,
               updated_at = now()
         WHERE node_id = %s
        """,
        (name, slug, description, actor[:100], node_id),
    )
    if "slug" in changes:
        _carry_identity_mapping(cursor, node_id, changes["slug"]["from"], actor=actor)
    record_audit(cursor, actor=actor, action="node_updated", entity="node",
                 entity_key=str(node_id), detail={"changes": changes})
    return _node(cursor, node_id)


def _carry_identity_mapping(cursor, node_id: int, old_slug: str, *, actor: str) -> None:
    """Re-slugging a node must stay an in-place rename of its live term.

    Live slugs equal to a node's slug are mapped implicitly; the moment the
    node's slug changes that identity is gone and the old live slug would
    surface as "unmapped", inviting a delete+create. So when the old slug is
    live somewhere and has no explicit disposition yet, record the explicit
    map (primary unless the node already has one) that the identity implied.
    """
    if not slug_is_live(cursor, old_slug):
        return
    cursor.execute("SELECT 1 FROM catmgr.slug_map WHERE old_slug = %s", (old_slug,))
    if cursor.fetchone() is not None:
        return
    cursor.execute(
        "SELECT old_slug FROM catmgr.slug_map WHERE target_node_id = %s AND is_primary",
        (node_id,),
    )
    primary = cursor.fetchone()
    is_primary = primary is None
    if primary is not None and not slug_is_live(cursor, primary["old_slug"]):
        # The explicit primary names a slug no store carries any more (a
        # previous re-slug already converged every live term away from it):
        # the slug that IS live now is the node's identity. Demote the stale
        # row so the second re-slug (b -> c) updates the surviving term in
        # place instead of deleting it and creating c.
        cursor.execute(
            "UPDATE catmgr.slug_map SET is_primary = false, updated_by = %s,"
            " updated_at = now() WHERE old_slug = %s",
            (actor[:100], primary["old_slug"]),
        )
        record_audit(cursor, actor=actor, action="mapping_primary_demoted",
                     entity="slug_map", entity_key=primary["old_slug"],
                     detail={"node_id": node_id, "reason": "no longer live"})
        is_primary = True
    cursor.execute(
        """
        INSERT INTO catmgr.slug_map
            (old_slug, action, target_node_id, is_primary, note, updated_by)
        VALUES (%s, 'map', %s, %s, 'carried from the category''s previous slug', %s)
        """,
        (old_slug, node_id, is_primary, actor[:100]),
    )
    record_audit(cursor, actor=actor, action="mapping_carried", entity="slug_map",
                 entity_key=old_slug,
                 detail={"node_id": node_id, "is_primary": is_primary})


def move_node(cursor, node_id: int, *, parent_id: Optional[int],
              position: Optional[int], actor: str) -> Dict[str, Any]:
    lock_draft(cursor)
    node = _node(cursor, node_id)
    if parent_id is not None:
        _assert_no_cycle(cursor, node_id, parent_id)
    cursor.execute(
        "UPDATE catmgr.node SET parent_id = %s, updated_by = %s, updated_at = now()"
        " WHERE node_id = %s",
        (parent_id, actor[:100], node_id),
    )
    _resequence_siblings(cursor, parent_id, node_id, position)
    if node["parent_id"] != parent_id:
        # Close the gap the node left behind in its old sibling group.
        _resequence_siblings(cursor, node["parent_id"], -1, None)
    record_audit(cursor, actor=actor, action="node_moved", entity="node",
                 entity_key=str(node_id),
                 detail={"from_parent": node["parent_id"], "to_parent": parent_id,
                         "position": position})
    return _node(cursor, node_id)


def delete_node(cursor, node_id: int, *, cascade: bool = False,
                actor: str) -> Dict[str, Any]:
    lock_draft(cursor)
    node = _node(cursor, node_id)
    cursor.execute(
        "SELECT count(*) AS n FROM catmgr.node WHERE parent_id = %s", (node_id,)
    )
    child_count = cursor.fetchone()["n"]
    if child_count and not cascade:
        raise DraftConflict(
            f"node {node_id} has {child_count} children; move them or pass cascade"
        )
    deleted: List[int] = []

    def _delete_subtree(target: int) -> None:
        cursor.execute(
            "SELECT node_id FROM catmgr.node WHERE parent_id = %s", (target,)
        )
        for row in cursor.fetchall():
            _delete_subtree(row["node_id"])
        cursor.execute("DELETE FROM catmgr.node WHERE node_id = %s", (target,))
        deleted.append(target)

    # Explicit slug_map rows pointing at the subtree cascade away with it:
    # every live slug they covered needs a new disposition before the next
    # preview. Report them so the operator is not surprised by an
    # "unmapped slugs" blocker later.
    cursor.execute(
        "SELECT old_slug FROM catmgr.slug_map WHERE target_node_id = ANY(%s)"
        " ORDER BY old_slug",
        (list(_subtree_ids(cursor, node_id)),),
    )
    unmapped_slugs = [row["old_slug"] for row in cursor.fetchall()]
    _delete_subtree(node_id)
    record_audit(cursor, actor=actor, action="node_deleted", entity="node",
                 entity_key=str(node_id),
                 detail={"slug": node["slug"], "deleted": deleted,
                         "unmapped_slugs": unmapped_slugs})
    return {"deleted": deleted, "unmapped_slugs": unmapped_slugs}


def _subtree_ids(cursor, node_id: int) -> List[int]:
    ids = [node_id]
    frontier = [node_id]
    while frontier:
        cursor.execute(
            "SELECT node_id FROM catmgr.node WHERE parent_id = ANY(%s)", (frontier,)
        )
        frontier = [row["node_id"] for row in cursor.fetchall()]
        ids.extend(frontier)
    return ids


def seed_from_snapshot(cursor, *, env: str, blog_id: int, actor: str,
                       force: bool = False) -> Dict[str, Any]:
    """Replace the draft with one blog's live snapshot tree."""

    lock_draft(cursor)
    cursor.execute("SELECT count(*) AS n FROM catmgr.node")
    existing = cursor.fetchone()["n"]
    if existing and not force:
        raise DraftConflict(
            f"draft already has {existing} nodes; pass force to replace it"
        )
    cursor.execute(
        """
        SELECT term_id, slug, name, parent_term_id, description, sort_order
          FROM catmgr.wp_term
         WHERE env = %s AND blog_id = %s
         ORDER BY parent_term_id, sort_order, name
        """,
        (env, blog_id),
    )
    terms = [dict(row) for row in cursor.fetchall()]
    if not terms:
        raise DraftError(
            f"no snapshot for {env} blog {blog_id}; import it first"
        )
    cursor.execute("DELETE FROM catmgr.node_store_override")
    cursor.execute("DELETE FROM catmgr.node")

    by_term_id = {t["term_id"]: t for t in terms}
    node_ids: Dict[int, int] = {}

    def _insert(term: Dict[str, Any]) -> int:
        term_id = term["term_id"]
        if term_id in node_ids:
            return node_ids[term_id]
        parent_term = term["parent_term_id"] or 0
        parent_node: Optional[int] = None
        if parent_term and parent_term in by_term_id:
            parent_node = _insert(by_term_id[parent_term])
        cursor.execute(
            """
            INSERT INTO catmgr.node
                (parent_id, name, slug, sort_order, description, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING node_id
            """,
            (parent_node, clean_category_name(term["name"]), term["slug"],
             int(term["sort_order"] or 0), term["description"] or "",
             actor[:100]),
        )
        node_id = cursor.fetchone()["node_id"]
        node_ids[term_id] = node_id
        return node_id

    for term in terms:
        _insert(term)
    record_audit(cursor, actor=actor, action="draft_seeded", entity="draft",
                 entity_key=f"{env}:{blog_id}",
                 detail={"nodes": len(node_ids), "forced": bool(force)})
    return {"nodes": len(node_ids)}


# ---------------------------------------------------------------- overrides


def list_overrides(cursor, blog_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if blog_id is None:
        cursor.execute(
            """
            SELECT o.*, n.slug AS node_slug, n.name AS node_name
              FROM catmgr.node_store_override o
              LEFT JOIN catmgr.node n ON n.node_id = o.node_id
             ORDER BY o.blog_id, o.kind, o.override_id
            """
        )
    else:
        cursor.execute(
            """
            SELECT o.*, n.slug AS node_slug, n.name AS node_name
              FROM catmgr.node_store_override o
              LEFT JOIN catmgr.node n ON n.node_id = o.node_id
             WHERE o.blog_id = %s
             ORDER BY o.kind, o.override_id
            """,
            (blog_id,),
        )
    return [dict(row) for row in cursor.fetchall()]


def set_override(cursor, *, blog_id: int, kind: str,
                 override_id: Optional[int] = None,
                 node_id: Optional[int] = None, name: Optional[str] = None,
                 slug: Optional[str] = None,
                 parent_node_id: Optional[int] = None,
                 include_descendants: bool = True, sort_order: int = 0,
                 blog_path: str = "", actor: str) -> Dict[str, Any]:
    lock_draft(cursor)
    if kind not in ("extra_node", "rename", "exclude"):
        raise DraftError(f"unknown override kind: {kind!r}")
    if kind == "extra_node":
        name = clean_category_name(name or "")
        if not name:
            raise DraftError("extra_node requires a name")
        slug = _validate_slug(slug or slugify(name))
        node_id = None
        if parent_node_id is not None:
            _node(cursor, parent_node_id)
    else:
        if node_id is None:
            raise DraftError(f"{kind} requires node_id")
        _node(cursor, node_id)
        slug = None
        parent_node_id = None
        if kind == "rename":
            name = clean_category_name(name or "")
            if not name:
                raise DraftError("rename requires a name")
        else:
            name = None
    if not blog_path:
        # Paths match across environments; prefer prod's spelling when both exist.
        cursor.execute(
            "SELECT blog_path FROM catmgr.snapshot WHERE blog_id = %s"
            " ORDER BY (env = 'prod') DESC, env LIMIT 1",
            (blog_id,),
        )
        row = cursor.fetchone()
        blog_path = row["blog_path"] if row else ""
    if kind == "extra_node":
        owner = _slug_owner(cursor, slug, exclude_override_id=override_id,
                            blog_id=blog_id)
        if owner:
            raise DraftConflict(f"slug already in use by {owner}: {slug}")
    previous_slug = None
    if override_id is None:
        try:
            cursor.execute(
                """
                INSERT INTO catmgr.node_store_override
                    (blog_id, blog_path, kind, node_id, name, slug, parent_node_id,
                     include_descendants, sort_order, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING override_id
                """,
                (blog_id, blog_path, kind, node_id, name, slug, parent_node_id,
                 include_descendants, sort_order, actor[:100]),
            )
        except pg_errors.UniqueViolation as exc:
            raise DraftConflict(
                f"blog {blog_id} already has a {kind.replace('_', ' ')} override"
                + (f" for slug {slug}" if slug else " for that category")
            ) from exc
        override_id = cursor.fetchone()["override_id"]
    else:
        cursor.execute(
            "SELECT slug, previous_slug FROM catmgr.node_store_override"
            " WHERE override_id = %s AND blog_id = %s AND kind = %s",
            (override_id, blog_id, kind),
        )
        current = cursor.fetchone()
        if current is None:
            raise DraftError(f"unknown override: {override_id}")
        # A store extra's lineage is whatever slug its term carries live on
        # its own blog right now (the current draft slug, or the previous one
        # if the last re-slug was never applied). The planner converges that
        # term in place (term_id kept, redirect on blog 1); once an apply has
        # landed the lineage reads as empty again, so a second re-slug starts
        # from the live slug rather than the original one.
        previous_slug = current["previous_slug"]
        if kind == "extra_node":
            previous_slug = _extra_live_slug(
                cursor, blog_id, [current["slug"], current["previous_slug"]], slug,
            )
        try:
            cursor.execute(
                """
                UPDATE catmgr.node_store_override
                   SET name = %s, slug = %s, parent_node_id = %s,
                       include_descendants = %s, sort_order = %s,
                       previous_slug = %s,
                       updated_by = %s, updated_at = now()
                 WHERE override_id = %s AND blog_id = %s AND kind = %s
                """,
                (name, slug, parent_node_id, include_descendants, sort_order,
                 previous_slug, actor[:100], override_id, blog_id, kind),
            )
        except pg_errors.UniqueViolation as exc:
            raise DraftConflict(f"blog {blog_id} already has an override for slug {slug}") from exc
    record_audit(cursor, actor=actor, action="override_saved", entity="override",
                 entity_key=str(override_id),
                 detail={"blog_id": blog_id, "kind": kind, "node_id": node_id,
                         "name": name, "slug": slug})
    rows = list_overrides(cursor, blog_id)
    return next(r for r in rows if r["override_id"] == override_id)


def delete_override(cursor, override_id: int, *, actor: str) -> None:
    lock_draft(cursor)
    cursor.execute(
        "DELETE FROM catmgr.node_store_override WHERE override_id = %s"
        " RETURNING blog_id, kind",
        (override_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise DraftError(f"unknown override: {override_id}")
    record_audit(cursor, actor=actor, action="override_deleted",
                 entity="override", entity_key=str(override_id),
                 detail={"blog_id": row["blog_id"], "kind": row["kind"]})


# ---------------------------------------------------------------- effective


def effective_tree(cursor, blog_id: int) -> List[Dict[str, Any]]:
    """One store's tree: global nodes minus excludes, renames applied,
    extra nodes appended under their graft points."""

    nodes = list_nodes(cursor)
    overrides = list_overrides(cursor, blog_id)

    excluded_roots = set()
    only_self_excluded = set()
    renames: Dict[int, str] = {}
    for override in overrides:
        if override["kind"] == "exclude":
            if override["include_descendants"]:
                excluded_roots.add(override["node_id"])
            else:
                only_self_excluded.add(override["node_id"])
        elif override["kind"] == "rename":
            renames[override["node_id"]] = override["name"]

    children: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for node in nodes:
        children.setdefault(node["parent_id"], []).append(node)

    result: List[Dict[str, Any]] = []

    def _walk(parent_id: Optional[int], suppressed: bool) -> None:
        for node in children.get(parent_id, []):
            node_suppressed = suppressed or node["node_id"] in excluded_roots
            hidden = node_suppressed or node["node_id"] in only_self_excluded
            if not hidden:
                entry = dict(node)
                if node["node_id"] in renames:
                    entry["name"] = renames[node["node_id"]]
                    entry["renamed"] = True
                result.append(entry)
            if not node_suppressed:
                _walk(node["node_id"], node_suppressed)

    _walk(None, False)

    kept_ids = {entry["node_id"] for entry in result}
    for override in overrides:
        if override["kind"] != "extra_node":
            continue
        graft = override["parent_node_id"]
        if graft is not None and graft not in kept_ids:
            continue  # graft point hidden on this store
        result.append({
            "node_id": None,
            "override_id": override["override_id"],
            "parent_id": graft,
            "name": override["name"],
            "slug": override["slug"],
            "previous_slug": override.get("previous_slug"),
            "sort_order": override["sort_order"],
            "description": "",
            "extra": True,
        })
    return result
