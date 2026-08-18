-- Canonical media mapping: every unique product image referenced by pim.*
-- data, published once to the locked S3 bucket (arborwear-product-media) and
-- served globally via CloudFront at https://assets.arborwear.com/<s3_key>.
--
-- Written by infra/publish-product-media.py (runs on the warehouse box using
-- its arb-warehouse-media-publisher instance role; WP side is never touched -
-- source bytes are fetched as plain public HTTP GETs).
--
-- cdn_url is stored explicitly so consumers (feed, PIM phase 2, Emblem) never
-- have to re-derive keys; keys are nonetheless human-readable:
--   products/<SKU_PARENT>/<original-basename>[-<md58>]
BEGIN;

CREATE TABLE IF NOT EXISTS pim.media_object (
    source_url   text        NOT NULL PRIMARY KEY,
    s3_key       text        NOT NULL,
    cdn_url      text        NOT NULL,
    sku_parent   text        NOT NULL DEFAULT '',
    content_md5  text        NOT NULL DEFAULT '',
    bytes        bigint      NOT NULL DEFAULT 0,
    content_type text        NOT NULL DEFAULT '',
    published_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_object_sku ON pim.media_object (sku_parent);
CREATE UNIQUE INDEX IF NOT EXISTS media_object_key ON pim.media_object (s3_key);

GRANT SELECT ON pim.media_object TO woo_reader, insights_reader, logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON pim.media_object TO pim_writer;

COMMIT;

-- 2026-08-02 addendum (applied live same day): the feed serves PIM content at
-- read time via the logo-admin app, whose pool authenticates as logo_admin.
GRANT SELECT ON pim.product_state, pim.ingest_event TO logo_admin;
GRANT USAGE ON SCHEMA pim TO logo_admin;
