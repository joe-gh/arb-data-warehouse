-- Category Editor Phase 3: the slug map (disposition of every live slug),
-- assignment rules, and explicit style-level product assignments.
--
-- slug_map is the migration's forcing function: preview refuses to run while
-- any live slug in the target environment lacks a row here. ON DELETE CASCADE
-- from catmgr.node means deleting a draft node returns its mapped slugs to
-- "unmapped" (the row disappears), so nothing dangles silently.
BEGIN;

CREATE TABLE IF NOT EXISTS catmgr.slug_map (
    old_slug       text PRIMARY KEY,
    action         text NOT NULL
                   CHECK (action IN ('map', 'delete', 'store_custom')),
    target_node_id bigint REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    is_primary     boolean NOT NULL DEFAULT false,
    override_id    bigint REFERENCES catmgr.node_store_override (override_id)
                   ON DELETE CASCADE,
    note           text NOT NULL DEFAULT '',
    updated_by     text NOT NULL DEFAULT '',
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT slug_map_shape CHECK (
        (action = 'map' AND target_node_id IS NOT NULL AND override_id IS NULL)
        OR (action = 'delete' AND target_node_id IS NULL
            AND override_id IS NULL AND NOT is_primary)
        OR (action = 'store_custom' AND target_node_id IS NULL
            AND NOT is_primary)
    )
);
-- Exactly one primary (in-place survivor) per target node.
CREATE UNIQUE INDEX IF NOT EXISTS slug_map_one_primary
    ON catmgr.slug_map (target_node_id) WHERE is_primary;
CREATE INDEX IF NOT EXISTS slug_map_target ON catmgr.slug_map (target_node_id);

CREATE TABLE IF NOT EXISTS catmgr.assignment_rule (
    rule_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id    bigint NOT NULL REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    kind       text   NOT NULL DEFAULT 'filter' CHECK (kind IN ('filter')),
    spec       jsonb  NOT NULL,
    priority   integer NOT NULL DEFAULT 0,
    note       text    NOT NULL DEFAULT '',
    updated_by text    NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assignment_rule_node ON catmgr.assignment_rule (node_id);

CREATE TABLE IF NOT EXISTS catmgr.product_assignment (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id  bigint NOT NULL REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    sku      text   NOT NULL CHECK (btrim(sku) <> ''),
    mode     text   NOT NULL CHECK (mode IN ('add', 'remove')),
    source   text   NOT NULL CHECK (source IN ('manual', 'csv', 'ai', 'rule')),
    note     text   NOT NULL DEFAULT '',
    added_by text   NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (node_id, sku, mode)
);
CREATE INDEX IF NOT EXISTS product_assignment_sku ON catmgr.product_assignment (sku);

GRANT SELECT ON catmgr.slug_map, catmgr.assignment_rule, catmgr.product_assignment
    TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON catmgr.slug_map, catmgr.assignment_rule, catmgr.product_assignment
    TO logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;

COMMIT;
