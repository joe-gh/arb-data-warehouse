-- PIM (Sales Layer api2) direct pull - raw landing tables.
--
-- The puller (infra/pull_pim.py) writes the COMPLETE API payload per entity
-- here, incrementally by modification date. Nothing downstream reads these
-- tables yet: the push-fed pim.product_state mirror, the phase-2 projection,
-- and the media publisher are all unchanged. Wiring happens in a later,
-- separate step once pull parity is proven.
--
-- Policy note: FDM4 is the source of truth for skus, colors, sizes, prices,
-- and stock. The PIM is the content authority only (names, descriptions,
-- attribute metadata, imagery). pim.api_product_content encodes that policy
-- as the ONLY sanctioned read surface over the raw payloads.
--
-- Apply as postgres on the warehouse box:
--   sudo -u postgres psql -d arb_warehouse -f 2026-08-20-pim-api-pull.sql

BEGIN;

CREATE TABLE IF NOT EXISTS pim.api_product (
    prod_ref     text PRIMARY KEY,
    style_number text NOT NULL DEFAULT '',
    prod_modify  timestamptz,
    payload      jsonb NOT NULL,
    pulled_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pim.api_variant (
    frmt_ref    text PRIMARY KEY,
    prod_ref    text NOT NULL DEFAULT '',
    frmt_modify timestamptz,
    payload     jsonb NOT NULL,
    pulled_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_variant_prod ON pim.api_variant (prod_ref);

CREATE TABLE IF NOT EXISTS pim.api_image (
    image_id    bigint PRIMARY KEY,
    reference   text NOT NULL DEFAULT '',
    modified_on timestamptz,
    payload     jsonb NOT NULL,
    pulled_at   timestamptz NOT NULL DEFAULT now()
);

-- One watermark row per entity; the puller re-reads a small overlap window
-- behind the watermark so clock skew never drops a row.
CREATE TABLE IF NOT EXISTS pim.api_pull_state (
    entity     text PRIMARY KEY,
    watermark  timestamptz,
    last_run   timestamptz,
    last_count integer NOT NULL DEFAULT 0,
    note       text NOT NULL DEFAULT ''
);

-- Content-only view: everything the PIM is authoritative for, and nothing
-- FDM4 owns. Price, sale price, and inventory fields are deliberately absent;
-- refs and the style number are carried as join keys only.
CREATE OR REPLACE VIEW pim.api_product_content AS
SELECT
    prod_ref,
    style_number,
    prod_modify,
    payload->>'prod_title'             AS title,
    payload->>'prod_description'       AS description,
    payload->>'prod_shortdescription'  AS short_description,
    payload->>'prod_specifications'    AS specifications,
    payload->>'prod_brand'             AS brand,
    payload->>'prod_gender'            AS gender,
    payload->>'cat_ref'                AS category_ref,
    payload->>'prod_image'             AS primary_image,
    payload->>'prod_alternateproductimag'  AS alt_image_1,
    payload->>'prod_alternateproductimag1' AS alt_image_2,
    payload->>'prod_alternateproductimag2' AS alt_image_3,
    payload->>'prod_alternateproductimag3' AS alt_image_4,
    payload->>'prod_detailproductimag'     AS detail_image,
    payload->>'prod_productvideo'          AS product_video,
    payload->>'prod_tags'              AS tags,
    -- The attribute vocabulary: enrichment Woo/FDM4 do not carry.
    payload - ARRAY[
        'prod_id','prod_ref','prod_stylenumber','cat_ref','prod_stat',
        'prod_modify','prod_creation','prod_retailprice','prod_saleprice',
        'prod_inventory','prod_instock','prod_sourceblogid',
        'prod_title','prod_description','prod_shortdescription',
        'prod_specifications','prod_brand','prod_gender','prod_tags',
        'prod_image','prod_alternateproductimag','prod_alternateproductimag1',
        'prod_alternateproductimag2','prod_alternateproductimag3',
        'prod_detailproductimag','prod_productvideo'
    ]                                   AS attributes
FROM pim.api_product;

GRANT SELECT ON pim.api_product, pim.api_variant, pim.api_image,
               pim.api_pull_state, pim.api_product_content TO logo_admin;

COMMIT;
