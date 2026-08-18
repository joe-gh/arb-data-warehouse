-- ============================================================================
-- Durable PIM (Sales Layer) mirror schema - the third warehouse source.
--
-- Three-source architecture: fdm4.* (raw ERP, reset hourly) + logo.* (durable
-- logo merchandising) + pim.* (durable product enrichment) -> woo.* projection.
--
-- Phase 1 (mirror): the WP-side arb-pim-mirror captures every SUCCESSFUL
-- Sales Layer ingest (POST /slwc/v1/ingest-product) and writes it here.
-- Sales Layer keeps writing WooCommerce directly and is unaffected; this
-- schema fills with authoritative enrichment data so the projection flip
-- (reconcile applies pim.* to Woo; Sales Layer stops writing Woo) can happen
-- later as a deliberate, separate step.
--
-- Apply as a database owner:
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -d arb_warehouse -f pim_schema.sql
-- Safe to reapply.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS pim;
GRANT USAGE ON SCHEMA pim TO woo_reader, insights_reader;

-- Append-only log of accepted ingests (raw payloads, full audit/replay trail).
CREATE TABLE IF NOT EXISTS pim.ingest_event (
    id           bigserial   PRIMARY KEY,
    received_at  timestamptz NOT NULL DEFAULT now(),
    source       text        NOT NULL DEFAULT 'saleslayer',
    env          text        NOT NULL DEFAULT '',       -- ARB_ENVIRONMENT of the mirroring WP box
    blog_id      integer     NOT NULL,
    fdm4_store   text,                                  -- from the Store Sync Map; NULL when unmapped
    sku_parent   text        NOT NULL DEFAULT '',
    payload_md5  text        NOT NULL DEFAULT '',
    payload      jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS pim_event_sku      ON pim.ingest_event (sku_parent);
CREATE INDEX IF NOT EXISTS pim_event_store    ON pim.ingest_event (fdm4_store);
CREATE INDEX IF NOT EXISTS pim_event_received ON pim.ingest_event (received_at);
GRANT SELECT ON pim.ingest_event TO woo_reader, insights_reader;

-- Latest accepted enrichment per (blog, parent SKU). Only ONE environment's
-- mirror ever writes (the env that receives Sales Layer pushes - prod; other
-- boxes lack the ARB_WH_PG_PIM_* constants), so blog_id is stable here.
-- fdm4_store is the env-agnostic join key for the woo.* projection.
CREATE TABLE IF NOT EXISTS pim.product_state (
    blog_id           integer     NOT NULL,
    sku_parent        text        NOT NULL,
    fdm4_store        text,
    env               text        NOT NULL DEFAULT '',
    name              text        NOT NULL DEFAULT '',
    description       text        NOT NULL DEFAULT '',
    short_description text        NOT NULL DEFAULT '',
    payload_md5       text        NOT NULL DEFAULT '',
    payload           jsonb       NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (blog_id, sku_parent)
);
CREATE INDEX IF NOT EXISTS pim_state_store ON pim.product_state (fdm4_store);
CREATE INDEX IF NOT EXISTS pim_state_sku   ON pim.product_state (sku_parent);
GRANT SELECT ON pim.product_state TO woo_reader, insights_reader;

-- Convenience view for Insights / the future projection flip: latest
-- enrichment fields per store+SKU without digging into the raw payloads.
CREATE OR REPLACE VIEW pim.v_enrichment AS
SELECT fdm4_store,
       blog_id,
       sku_parent,
       name,
       description,
       short_description,
       payload -> 'parent'     AS parent,
       payload -> 'variations' AS variations,
       updated_at
  FROM pim.product_state;
GRANT SELECT ON pim.v_enrichment TO woo_reader, insights_reader;

COMMIT;
