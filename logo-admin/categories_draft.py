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
        cursor.execute("SELECT 1 FROM catmgr.node WHERE slug = %s", (slug,))
        if cursor.fetchone():
            raise DraftConflict(f"slug already in use: {slug}")
    else:
        base = slugify(name)
        slug = base
        suffix = 2
        while True:
            cursor.execute("SELECT 1 FROM catmgr.node WHERE slug = %s", (slug,))
            if not cursor.fetchone():
                break
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
            cursor.execute(
                "SELECT 1 FROM catmgr.node WHERE slug = %s AND node_id <> %s",
                (slug, node_id),
            )
            if cursor.fetchone():
                raise DraftConflict(f"slug already in use: {slug}")
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
    record_audit(cursor, actor=actor, action="node_updated", entity="node",
                 entity_key=str(node_id), detail={"changes": changes})
    return _node(cursor, node_id)


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

    _delete_subtree(node_id)
    record_audit(cursor, actor=actor, action="node_deleted", entity="node",
                 entity_key=str(node_id),
                 detail={"slug": node["slug"], "deleted": deleted})
    return {"deleted": deleted}


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
        cursor.execute(
            "SELECT blog_path FROM catmgr.snapshot WHERE blog_id = %s LIMIT 1",
            (blog_id,),
        )
        row = cursor.fetchone()
        blog_path = row["blog_path"] if row else ""
    if override_id is None:
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
        override_id = cursor.fetchone()["override_id"]
    else:
        cursor.execute(
            """
            UPDATE catmgr.node_store_override
               SET name = %s, slug = %s, parent_node_id = %s,
                   include_descendants = %s, sort_order = %s,
                   updated_by = %s, updated_at = now()
             WHERE override_id = %s AND blog_id = %s AND kind = %s
            """,
            (name, slug, parent_node_id, include_descendants, sort_order,
             actor[:100], override_id, blog_id, kind),
        )
        if cursor.rowcount == 0:
            raise DraftError(f"unknown override: {override_id}")
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
            "sort_order": override["sort_order"],
            "description": "",
            "extra": True,
        })
    return result
