-- Virtual-catalog stores: project the entire sellable FDM4 catalog at retail
-- for a named store, instead of that store's FDM4 web catalog. See the synthetic
-- branches added to woo.refresh_product_state() in woo_transform.sql.
--
-- This migration only creates the (empty) flag table. Activating a store is a
-- deliberate INSERT run separately once the transform is deployed and tested.
BEGIN;

CREATE TABLE IF NOT EXISTS woo.virtual_catalog_store (
    fdm4_store text NOT NULL PRIMARY KEY,
    catalog_id text NOT NULL,
    note       text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON woo.virtual_catalog_store TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.virtual_catalog_store TO etl_writer;

COMMIT;
