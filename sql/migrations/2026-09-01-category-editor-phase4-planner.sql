-- Category Editor Phase 4: zero-category acknowledgements.
--
-- The planner blocks a preview while any style would end up with no
-- categories at all after the migration. A deliberate exception ("this style
-- is intentionally uncategorized") is recorded here and converts that style's
-- blocker into a warning.
BEGIN;

CREATE TABLE IF NOT EXISTS catmgr.uncategorized_ack (
    sku      text PRIMARY KEY CHECK (btrim(sku) <> ''),
    note     text NOT NULL DEFAULT '',
    added_by text NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON catmgr.uncategorized_ack TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON catmgr.uncategorized_ack TO logo_admin;

COMMIT;
