-- Store -> WordPress blog mapping, for operator-facing display in the
-- Warehouse Ops app ("blog 59 · /square/"). Source of truth is PROD's
-- arb_store_sync_map + arb_blogs; refresh by re-running the export/seed
-- documented in the app README (mappings change only at store onboarding).
BEGIN;

CREATE TABLE IF NOT EXISTS woo.store_blog_map (
    blog_id    integer NOT NULL PRIMARY KEY,
    fdm4_store text    NOT NULL,
    blog_path  text    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS store_blog_map_store_idx ON woo.store_blog_map (fdm4_store);

GRANT SELECT ON woo.store_blog_map TO woo_reader, insights_reader, logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.store_blog_map TO etl_writer;

COMMIT;
