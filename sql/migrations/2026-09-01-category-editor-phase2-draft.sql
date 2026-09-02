-- Category Editor Phase 2: the draft global tree + per-store overlays.
--
-- catmgr.node is the single working draft of the target category tree
-- (one draft, no versioning - apply runs freeze their own plan copies).
-- catmgr.node_store_override layers per-store customization over it:
--   extra_node - a store-local category grafted under a global node
--   rename     - a store-specific display name for a global node
--   exclude    - hide a global node (and optionally its subtree) on a store
-- Effective per-store tree = global nodes minus excludes, renames applied,
-- extras appended. This replaces the removed hardcoded
-- AVNC::category_valid_for_store() store gating with data.
BEGIN;

CREATE TABLE IF NOT EXISTS catmgr.node (
    node_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_id   bigint REFERENCES catmgr.node (node_id) ON DELETE RESTRICT,
    name        text    NOT NULL CHECK (btrim(name) <> ''),
    slug        text    NOT NULL UNIQUE
                CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    sort_order  integer NOT NULL DEFAULT 0,
    description text    NOT NULL DEFAULT '',
    updated_by  text    NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS node_parent ON catmgr.node (parent_id);

CREATE TABLE IF NOT EXISTS catmgr.node_store_override (
    override_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    blog_id             integer NOT NULL,
    blog_path           text    NOT NULL DEFAULT '',
    kind                text    NOT NULL
                        CHECK (kind IN ('extra_node', 'rename', 'exclude')),
    node_id             bigint REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    name                text,
    slug                text CHECK (slug IS NULL OR slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    parent_node_id      bigint REFERENCES catmgr.node (node_id) ON DELETE SET NULL,
    include_descendants boolean NOT NULL DEFAULT true,
    sort_order          integer NOT NULL DEFAULT 0,
    updated_by          text    NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT override_shape CHECK (
        (kind = 'extra_node' AND node_id IS NULL
             AND name IS NOT NULL AND btrim(name) <> ''
             AND slug IS NOT NULL)
        OR (kind = 'rename' AND node_id IS NOT NULL
             AND name IS NOT NULL AND btrim(name) <> ''
             AND slug IS NULL AND parent_node_id IS NULL)
        OR (kind = 'exclude' AND node_id IS NOT NULL
             AND name IS NULL AND slug IS NULL AND parent_node_id IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS override_rename_once
    ON catmgr.node_store_override (blog_id, node_id) WHERE kind = 'rename';
CREATE UNIQUE INDEX IF NOT EXISTS override_exclude_once
    ON catmgr.node_store_override (blog_id, node_id) WHERE kind = 'exclude';
CREATE UNIQUE INDEX IF NOT EXISTS override_extra_slug_once
    ON catmgr.node_store_override (blog_id, slug) WHERE kind = 'extra_node';
CREATE INDEX IF NOT EXISTS override_blog ON catmgr.node_store_override (blog_id);

GRANT SELECT ON catmgr.node, catmgr.node_store_override
    TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON catmgr.node, catmgr.node_store_override TO logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;

COMMIT;
