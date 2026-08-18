-- Sync blocks: explicitly freeze whole stores or specific products (styles)
-- from the product-sync engine. A blocked item is SKIPPED entirely - no
-- create/update/price/stock changes AND exempt from deactivation (it is
-- frozen as-is in Woo, not treated as "removed from catalog").
-- style_code = '' means the ENTIRE store is blocked.
-- Managed in the Warehouse Ops "Sync Blocks" tab. Fail-open: if the engine
-- cannot read this table it applies no blocks.
BEGIN;

CREATE TABLE IF NOT EXISTS woo.sync_exclusion (
    fdm4_store text NOT NULL,
    style_code text NOT NULL DEFAULT '',    -- '' = whole store
    note       text NOT NULL DEFAULT '',
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT '',
    PRIMARY KEY (fdm4_store, style_code)
);

GRANT SELECT ON woo.sync_exclusion TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.sync_exclusion TO etl_writer, logo_admin;

COMMIT;
