-- ============================================================================
-- Warehouse transform: raw FDM4 (schema `fdm4`, all-TEXT) -> Woo-facing
-- desired-state (schema `woo`). Idempotent + reproducible: apply this file to
-- (re)create the objects, then call woo.refresh_product_state() after each
-- nightly FDM4 load.
--
--   base tables   : fdm4.*          (loaded by db-test/load_dump.py)
--   query tables  : woo.store_product_state   (what the sync engine reads)
--                   woo.store_catalog         (catalogs per store, for mapping UI)
--
-- CATALOG-AWARE: a store (site_id) can host several catalogs (catalog_id) - a
-- real one plus "clone"/demo catalogs (e.g. S_002384_public-web vs
-- S_002384_Demowebstore) with different prices. Desired state is therefore keyed
-- by (fdm4_store, catalog_id, sku); the sync engine selects ONE catalog per blog.
--
-- CHANGE-TRACKING (delta pull): refresh is an UPSERT, not a full rebuild, so a
-- row's row_version only advances when its content actually changes. Removed
-- rows are tombstoned (is_active=false, bumped version) rather than deleted, so
-- the per-store delta still carries the removal. content_hash = md5(payload);
-- structural_hash / stockprice_hash split it so the Woo engine can route
-- stock/price-only changes to its fast path. content_hash changes IFF either
-- component changes (the component fields together cover the full payload).
--
-- Apply:  sudo -u postgres psql -d arb_warehouse -f woo_transform.sql
-- Refresh: SELECT woo.refresh_product_state();   (returns active row count)
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS woo;
GRANT USAGE ON SCHEMA woo TO woo_reader, insights_reader;

-- Monotonic version stamped on rows when their content changes (delta watermark).
CREATE SEQUENCE IF NOT EXISTS woo.state_version_seq AS bigint;
GRANT USAGE, SELECT ON SEQUENCE woo.state_version_seq TO etl_writer;

-- Desired state: one row per (FDM4 store, catalog, sku). Parents (sku = style
-- code) and variations (sku = UPC) both land here.
CREATE TABLE IF NOT EXISTS woo.store_product_state (
    fdm4_store   text        NOT NULL,
    catalog_id   text        NOT NULL,
    sku          text        NOT NULL,
    kind         text        NOT NULL,            -- 'parent' | 'variation'
    style_code   text,
    parent_sku   text,
    name         text,
    status       text,
    color_code   text,
    color        text,
    size_code    text,
    size         text,
    price        numeric(12,2),
    stock        numeric,
    payload      jsonb       NOT NULL,
    content_hash text        NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fdm4_store, catalog_id, sku)
);

-- Change-tracking columns (added here so existing installs self-heal on apply).
ALTER TABLE woo.store_product_state
    ADD COLUMN IF NOT EXISTS is_active       boolean     NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS row_version     bigint      NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS changed_at      timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS structural_hash text,
    ADD COLUMN IF NOT EXISTS stockprice_hash text;

-- Brand metadata (resolved from the FDM4 mill master, fdm4.mill). Carried on each
-- row for later use (e.g. an Arborwear-vs-3rd-party attribute in Woo). mill_code=22
-- is Arborwear's own line; every other code is a 3rd-party brand (Port Authority,
-- Gildan, YETI, ...). DELIBERATELY NOT part of payload / content_hash / structural_
-- or stockprice_hash: populating brand must NOT advance row_version, or every row
-- would look "changed" and trigger a full re-sync of all stores. It rides along as
-- pure metadata until we choose to surface it.
ALTER TABLE woo.store_product_state
    ADD COLUMN IF NOT EXISTS mill_code       text,
    ADD COLUMN IF NOT EXISTS brand           text;

-- Additional FDM4 metadata (2026-07-07). Resolved per-item in the transform and, like
-- brand, kept OUT of every hash (content/structural/stockprice) so populating them never
-- advances row_version or triggers a re-sync. Present for later piece-by-piece use in Woo.
-- Sources: item/style (category, cost, name, origin, HS, lifecycle, weight, size group),
-- fdm4.vendor (supplier name), fdm4.dec_design (design/logo name), fdm4."price-list"
-- (corporate/wholesale/employee/MSRP price matrix as jsonb).
ALTER TABLE woo.store_product_state
    ADD COLUMN IF NOT EXISTS category        text,
    ADD COLUMN IF NOT EXISTS item_name       text,
    ADD COLUMN IF NOT EXISTS origin_country  text,
    ADD COLUMN IF NOT EXISTS harmonization   text,
    ADD COLUMN IF NOT EXISTS item_status     text,
    ADD COLUMN IF NOT EXISTS web_active      text,
    ADD COLUMN IF NOT EXISTS ean_code        text,
    ADD COLUMN IF NOT EXISTS def_cost        numeric(12,2),
    ADD COLUMN IF NOT EXISTS weight          numeric,
    ADD COLUMN IF NOT EXISTS street_date     text,
    ADD COLUMN IF NOT EXISTS size_group      text,
    ADD COLUMN IF NOT EXISTS vendor_number   text,
    ADD COLUMN IF NOT EXISTS vendor_name     text,
    ADD COLUMN IF NOT EXISTS design_id       text,
    ADD COLUMN IF NOT EXISTS design_name     text,
    ADD COLUMN IF NOT EXISTS price_levels    jsonb;

CREATE INDEX IF NOT EXISTS sps_storecat     ON woo.store_product_state (fdm4_store, catalog_id);
CREATE INDEX IF NOT EXISTS sps_style        ON woo.store_product_state (style_code);
CREATE INDEX IF NOT EXISTS sps_sku          ON woo.store_product_state (sku);
-- delta range scan: "rows for this store/catalog newer than my watermark"
CREATE INDEX IF NOT EXISTS sps_storecat_ver ON woo.store_product_state (fdm4_store, catalog_id, row_version);

GRANT SELECT ON woo.store_product_state TO woo_reader, insights_reader;

-- Catalogs available per store + a suggested-primary flag (for the Store Sync
-- Map UI to default the catalog choice). One row per (store, catalog).
CREATE TABLE IF NOT EXISTS woo.store_catalog (
    fdm4_store text    NOT NULL,
    catalog_id text    NOT NULL,
    products   integer NOT NULL DEFAULT 0,
    suggested  boolean NOT NULL DEFAULT false,    -- best guess at the "real" catalog
    PRIMARY KEY (fdm4_store, catalog_id)
);
GRANT SELECT ON woo.store_catalog TO woo_reader, insights_reader;

-- Stores that should project the ENTIRE sellable FDM4 catalog at retail price,
-- instead of their own FDM4 web catalog (which may be empty/partial). Used for
-- retail stores whose catalog we do not want to hand-build in FDM4's B2B UI.
-- Empty table = feature inert for every store. Keyed by fdm4_store, so a single
-- row covers the store on every Woo environment reading this shared warehouse.
CREATE TABLE IF NOT EXISTS woo.virtual_catalog_store (
    fdm4_store text NOT NULL PRIMARY KEY,
    catalog_id text NOT NULL,
    note       text NOT NULL DEFAULT '',
    -- When set, every synthetic variation projects this stock instead of live
    -- item-balance availability (for stores whose Woo products exist only to
    -- feed a POS catalog and must always look purchasable). NULL = real stock.
    stock_override numeric,
    created_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT ON woo.virtual_catalog_store TO woo_reader, insights_reader;

-- Pricing-tier fallback config. When a store's FDM4 catalog price is missing/0
-- (transform would fall back to MSRP/retail) AND the store is assigned a
-- non-MSRP tier here, the transform uses that tier's computed price from the
-- price list instead. FDM4's published price always wins; this only fills
-- blanks. Empty store_pricing_tier = inert. (Seeded via the migration.)
CREATE TABLE IF NOT EXISTS woo.pricing_tier (
    tier_name        text    NOT NULL PRIMARY KEY,
    price_levels_key text    NOT NULL,
    is_msrp          boolean NOT NULL DEFAULT false,
    sort_order       integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS woo.store_pricing_tier (
    fdm4_store text        NOT NULL PRIMARY KEY,
    tier_name  text        NOT NULL REFERENCES woo.pricing_tier(tier_name),
    note       text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- Price rules (managed in Warehouse Ops "Price Rules" tab). Evaluated by
-- woo.eval_price_rules inside refresh_product_state AFTER the tier fallback.
-- Full DDL + evaluator function: sql/migrations/2026-07-31-price-rules.sql
-- (kept there as canonical to avoid drift; the migration is idempotent).

GRANT SELECT ON woo.pricing_tier, woo.store_pricing_tier TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.pricing_tier, woo.store_pricing_tier TO logo_admin;

-- ----------------------------------------------------------------------------
-- Rebuild the desired-state from the raw tables. SECURITY DEFINER so the
-- extractor role (etl_writer) can call it while it runs as the owner.
--
-- UPSERT semantics (not DELETE+INSERT): a row's row_version/changed_at advance
-- ONLY when its content_hash changes; unchanged rows keep their version so the
-- per-store delta stays small. Rows that disappear from the source are
-- tombstoned (is_active=false) with a fresh version so the delta carries the
-- removal to Woo. Atomic.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION woo.refresh_product_state()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = woo, fdm4, pg_catalog
AS $$
DECLARE
    total integer;
BEGIN
    -- Precompute available stock per item ONCE into an indexed temp table:
    -- available = max(0, on-hand - committed) per (item-number, warehouse),
    -- summed across warehouses. Prefer the LIVE fdm4."inv-balance" table
    -- (intraday on-hand/committed; pulled hourly since 2026-07-29) - the daily
    -- fdm4."item-balance" rows are only its NIGHTLY snapshot, which made Woo
    -- lag FDM4-side sales by up to a day. Fallback keeps the snapshot method
    -- so the transform still applies before the first inv-balance pull.
    -- (Materialized + indexed up front: inlined, the sort re-planned badly and
    -- slowed refresh ~4.5x.)
    IF to_regclass('fdm4."inv-balance"') IS NOT NULL THEN
        CREATE TEMP TABLE _bal ON COMMIT DROP AS
            SELECT "item-number" AS item_number,
                   SUM(GREATEST(0, COALESCE(NULLIF("inv-bal", '')::numeric, 0)
                                 - COALESCE(NULLIF("committed", '')::numeric, 0))) AS stock
            FROM fdm4."inv-balance"
            GROUP BY "item-number";
    ELSE
        CREATE TEMP TABLE _bal ON COMMIT DROP AS
            SELECT "item-number" AS item_number, SUM(GREATEST(0, on_hand - committed)) AS stock
            FROM (
                SELECT DISTINCT ON ("item-number", warehouse)
                       "item-number",
                       COALESCE(NULLIF("inv-bal", '')::numeric, 0)   AS on_hand,
                       COALESCE(NULLIF("committed", '')::numeric, 0) AS committed
                FROM fdm4."item-balance"
                ORDER BY "item-number", warehouse, "trans-date" DESC
            ) latest
            GROUP BY "item-number";
    END IF;
    CREATE INDEX ON _bal (item_number);
    ANALYZE _bal;

    -- Brand lookup: mill-code -> brand name from the FDM4 mill master (fdm4.mill,
    -- loaded by the pull). Deduped to one row per code + indexed. Guarded with
    -- to_regclass so the transform still applies cleanly before the first pull that
    -- includes the mill table (brand is simply NULL until then).
    IF to_regclass('fdm4.mill') IS NOT NULL THEN
        CREATE TEMP TABLE _mill ON COMMIT DROP AS
            SELECT DISTINCT ON (btrim("mill-code"))
                   btrim("mill-code") AS mill_code, description AS brand
            FROM fdm4.mill
            WHERE "mill-code" IS NOT NULL AND btrim("mill-code") <> ''
            ORDER BY btrim("mill-code"), description;
    ELSE
        CREATE TEMP TABLE _mill (mill_code text, brand text) ON COMMIT DROP;
    END IF;
    CREATE INDEX ON _mill (mill_code);
    ANALYZE _mill;

    -- Supplier lookup: vend-number -> vend-name (fdm4.vendor). Guarded + deduped like _mill.
    IF to_regclass('fdm4.vendor') IS NOT NULL THEN
        CREATE TEMP TABLE _vendor ON COMMIT DROP AS
            SELECT DISTINCT ON (btrim("vend-number"))
                   btrim("vend-number") AS vend_number, "vend-name" AS vend_name
            FROM fdm4.vendor
            WHERE "vend-number" IS NOT NULL AND btrim("vend-number") <> ''
            ORDER BY btrim("vend-number"), "vend-name";
    ELSE
        CREATE TEMP TABLE _vendor (vend_number text, vend_name text) ON COMMIT DROP;
    END IF;
    CREATE INDEX ON _vendor (vend_number);

    -- Decoration/logo master: design_id -> design name (fdm4.dec_design). Guarded + deduped.
    IF to_regclass('fdm4.dec_design') IS NOT NULL THEN
        CREATE TEMP TABLE _design ON COMMIT DROP AS
            SELECT DISTINCT ON (btrim(design_id))
                   btrim(design_id) AS design_id, description AS design_name
            FROM fdm4.dec_design
            WHERE design_id IS NOT NULL AND btrim(design_id) <> ''
            ORDER BY btrim(design_id), description;
    ELSE
        CREATE TEMP TABLE _design (design_id text, design_name text) ON COMMIT DROP;
    END IF;
    CREATE INDEX ON _design (design_id);

    -- Corporate price matrix from fdm4."price-list": base-price + a ';'-delimited pct-of-base
    -- array; positions 1..6 = Corp1,Corp2,Corp3,Wholesale,Employee,MSRP (per PUB.price-categ #1).
    -- Stored as jsonb of computed dollar prices. All numeric casts are regex-guarded so a
    -- malformed row can never abort the refresh.
    IF to_regclass('fdm4.price-list') IS NOT NULL THEN
        CREATE TEMP TABLE _price ON COMMIT DROP AS
            SELECT item_number,
                   CASE WHEN base IS NULL THEN NULL ELSE jsonb_strip_nulls(jsonb_build_object(
                       'base',      base,
                       'corp1',     CASE WHEN p1 IS NOT NULL THEN round(base*p1/100,2) END,
                       'corp2',     CASE WHEN p2 IS NOT NULL THEN round(base*p2/100,2) END,
                       'corp3',     CASE WHEN p3 IS NOT NULL THEN round(base*p3/100,2) END,
                       'wholesale', CASE WHEN p4 IS NOT NULL THEN round(base*p4/100,2) END,
                       'employee',  CASE WHEN p5 IS NOT NULL THEN round(base*p5/100,2) END,
                       'msrp',      CASE WHEN p6 IS NOT NULL THEN round(base*p6/100,2) END
                   )) END AS price_levels
            FROM (
                SELECT btrim("item-number") AS item_number,
                       CASE WHEN btrim("base-price")            ~ '^[0-9]+(\.[0-9]+)?$' THEN "base-price"::numeric END            AS base,
                       CASE WHEN split_part("sale-price",';',1) ~ '^[0-9]+(\.[0-9]+)?$' THEN split_part("sale-price",';',1)::numeric END AS p1,
                       CASE WHEN split_part("sale-price",';',2) ~ '^[0-9]+(\.[0-9]+)?$' THEN split_part("sale-price",';',2)::numeric END AS p2,
                       CASE WHEN split_part("sale-price",';',3) ~ '^[0-9]+(\.[0-9]+)?$' THEN split_part("sale-price",';',3)::numeric END AS p3,
                       CASE WHEN split_part("sale-price",';',4) ~ '^[0-9]+(\.[0-9]+)?$' THEN split_part("sale-price",';',4)::numeric END AS p4,
                       CASE WHEN split_part("sale-price",';',5) ~ '^[0-9]+(\.[0-9]+)?$' THEN split_part("sale-price",';',5)::numeric END AS p5,
                       CASE WHEN split_part("sale-price",';',6) ~ '^[0-9]+(\.[0-9]+)?$' THEN split_part("sale-price",';',6)::numeric END AS p6
                FROM fdm4."price-list"
                WHERE "item-number" IS NOT NULL AND btrim("item-number") <> ''
            ) pl;
    ELSE
        CREATE TEMP TABLE _price (item_number text, price_levels jsonb) ON COMMIT DROP;
    END IF;
    CREATE INDEX ON _price (item_number);

    -- Pricing-tier fallback: stores with a non-MSRP tier assignment, mapped to
    -- the price_levels key to use when a catalog price is missing. Empty unless
    -- store_pricing_tier is populated, so this is inert by default.
    CREATE TEMP TABLE _store_tier ON COMMIT DROP AS
        SELECT spt.fdm4_store, pt.price_levels_key AS price_key
          FROM woo.store_pricing_tier spt
          JOIN woo.pricing_tier pt ON pt.tier_name = spt.tier_name
         WHERE NOT pt.is_msrp;
    CREATE INDEX ON _store_tier (fdm4_store);

    -- Per-style attributes resolved once (vendor name, design name, street date) so both
    -- parent and variation rows can join them by style_code. fdm4.style always exists.
    CREATE TEMP TABLE _style_attrs ON COMMIT DROP AS
        SELECT s."style-code" AS style_code,
               NULLIF(btrim(s."vend-number"),'')  AS vendor_number,
               v.vend_name                         AS vendor_name,
               NULLIF(NULLIF(btrim(s.deco_design_id),''),'0') AS design_id,
               dd.design_name                                  AS design_name,
               NULLIF(btrim(s."street-date"),'')  AS street_date
        FROM fdm4.style s
        LEFT JOIN _vendor v  ON v.vend_number = btrim(s."vend-number")
        LEFT JOIN _design dd ON dd.design_id  = btrim(s.deco_design_id);
    CREATE INDEX ON _style_attrs (style_code);
    ANALYZE _vendor; ANALYZE _design; ANALYZE _price; ANALYZE _style_attrs;

    -- Freshly-computed desired set into a temp table (same extraction as before,
    -- now also computing the structural / stock-price / content hashes).
    -- Union of all catalog branches, materialized ONCE so the price-rules pass
    -- below can branch without duplicating the branch SQL.
    CREATE TEMP TABLE _base ON COMMIT DROP AS
    SELECT * FROM (
        -- Parents: one per (store, catalog, product) from the per-store storeData JSON.
        SELECT
            d.fdm4_store, d.catalog_id, d.sku, 'parent'::text AS kind, d.style_code,
            NULL::text AS parent_sku, d.name, d.status,
            NULL::text AS color_code, NULL::text AS color, NULL::text AS size_code, NULL::text AS size,
            NULLIF(d.price_text, '')::numeric AS price, NULL::numeric AS stock,
            d.mill_code, d.brand,
            d.category, d.item_name, d.origin_country, d.harmonization, d.item_status, NULL::text AS web_active,
            NULL::text AS ean_code, NULL::numeric AS def_cost, NULL::numeric AS weight, d.street_date, NULL::text AS size_group,
            d.vendor_number, d.vendor_name, d.design_id, d.design_name, NULL::jsonb AS price_levels,
            jsonb_build_object('kind','parent','name',d.name,'status',d.status,'price',d.price_text) AS payload,
            jsonb_build_object('kind','parent','name',d.name,'status',d.status)                      AS structural_payload,
            jsonb_build_object('price',d.price_text)                                                 AS stockprice_payload
        FROM (
            SELECT DISTINCT ON (d.site_id, d.catalog_id, d.product_id)
                d.site_id      AS fdm4_store,
                d.catalog_id   AS catalog_id,
                d.product_id   AS sku,
                d.product_id   AS style_code,
                s.description   AS name,
                s."item-status" AS status,
                s."mill-code"   AS mill_code,
                m.brand         AS brand,
                s."product-code"   AS category,
                s.description      AS item_name,
                s."origin-country" AS origin_country,
                s.harmonization    AS harmonization,
                s."item-status"    AS item_status,
                sa.vendor_number   AS vendor_number,
                sa.vendor_name     AS vendor_name,
                sa.design_id       AS design_id,
                sa.design_name     AS design_name,
                sa.street_date     AS street_date,
                CASE
                    WHEN btrim(d.detail_value::jsonb #>> '{product,0,customPrice}')
                         ~ '^[0-9]+(\.[0-9]+)?$'
                    THEN btrim(d.detail_value::jsonb #>> '{product,0,customPrice}')
                END AS price_text
            FROM fdm4.catalog_product_detail d
            JOIN fdm4.style s ON s."style-code" = d.product_id
            LEFT JOIN _mill m ON m.mill_code = btrim(s."mill-code")
            LEFT JOIN _style_attrs sa ON sa.style_code = s."style-code"
            WHERE d.detail_type = 'storeData'
              AND d.site_id ~ '^S_'
              AND pg_input_is_valid(d.detail_value, 'jsonb')
              -- Virtual-catalog stores are generated wholesale below instead.
              AND d.site_id NOT IN (SELECT fdm4_store FROM woo.virtual_catalog_store)
            -- Deterministic pick among duplicate storeData rows so payload (and thus
            -- content_hash / row_version) is stable run-to-run for change-tracking.
            ORDER BY d.site_id, d.catalog_id, d.product_id, d.detail_value
        ) d

        UNION ALL

        -- Variations: each (store, catalog) offers the items of the style whose
        -- colour is listed in that catalog's storeData colour set.
        SELECT
            v.fdm4_store, v.catalog_id, v.sku, 'variation'::text AS kind, v.style_code,
            v.parent_sku, NULL::text AS name, NULL::text AS status,
            v.color_code, v.color, v.size_code, v.size,
            NULLIF(v.price_text, '')::numeric AS price, v.stock,
            v.mill_code, v.brand,
            v.category, v.item_name, v.origin_country, v.harmonization, v.item_status, v.web_active,
            v.ean_code, v.def_cost, v.weight, v.street_date, v.size_group,
            v.vendor_number, v.vendor_name, v.design_id, v.design_name, v.price_levels,
            jsonb_build_object('kind','variation','style',v.style_code,'color',v.color,'size',v.size,
                               'price',v.price_text,'stock',v.stock,'active',v.active) AS payload,
            jsonb_build_object('kind','variation','style',v.style_code,'color',v.color,'size',v.size,
                               'active',v.active)                                      AS structural_payload,
            jsonb_build_object('price',v.price_text,'stock',v.stock)                    AS stockprice_payload
        FROM (
            SELECT DISTINCT ON (sd.fdm4_store, sd.catalog_id, i."upc-code")
                sd.fdm4_store,
                sd.catalog_id,
                i."upc-code"   AS sku,
                i."style-code" AS style_code,
                i."style-code" AS parent_sku,
                i."color-code" AS color_code,
                sc.description AS color,
                i."size-code"  AS size_code,
                ss.description AS size,
                -- Price: prefer the store catalog's customPrice (per-colour, then
                -- product-level); but when the catalog price is missing or 0, fall
                -- back to the item master retail-price. FDM4 sometimes ships catalog
                -- customPrice=0 for items that DO have a real retail-price, which
                -- otherwise lands in Woo (and on orders) as $0.
                CASE
                    -- Item-master sale-price markdown (per Nikki 2026-07-20): an
                    -- across-the-board sale for every customer + public web. When
                    -- the item sale price is a valid amount BELOW the store's
                    -- otherwise-resolved price, it wins -- lower-only, so it can
                    -- never raise a contract/tier price.
                    WHEN btrim(i."sale-price") ~ '^[0-9]+(\.[0-9]+)?$'
                     AND i."sale-price"::numeric > 0
                     AND i."sale-price"::numeric < NULLIF(
                             CASE
                                 WHEN COALESCE(NULLIF(sd.color_price, ''), NULLIF(sd.prod_price, ''), '0')::numeric > 0
                                     THEN COALESCE(NULLIF(sd.color_price, ''), sd.prod_price)
                                 ELSE COALESCE(
                                     pr.price_levels ->> st.price_key,
                                     CASE WHEN btrim(i."retail-price") ~ '^[0-9]+(\.[0-9]+)?$'
                                          THEN btrim(i."retail-price") END
                                 )
                             END, '')::numeric
                        THEN i."sale-price"
                    WHEN COALESCE(NULLIF(sd.color_price, ''), NULLIF(sd.prod_price, ''), '0')::numeric > 0
                        THEN COALESCE(NULLIF(sd.color_price, ''), sd.prod_price)
                    -- Blank catalog price: use the store's configured tier price
                    -- (fallback fix for FDM4-unpublished stores), else retail.
                    ELSE COALESCE(
                        pr.price_levels ->> st.price_key,
                        CASE WHEN btrim(i."retail-price") ~ '^[0-9]+(\.[0-9]+)?$'
                             THEN btrim(i."retail-price") END
                    )
                END            AS price_text,
                bal.stock      AS stock,
                i.active       AS active,
                i."mill-code"  AS mill_code,
                m.brand        AS brand,
                i."product-category" AS category,
                i."item-name"        AS item_name,
                i."origin-country"   AS origin_country,
                i.harmonization      AS harmonization,
                i."item-status"      AS item_status,
                i."web-active"       AS web_active,
                i."ean-code"         AS ean_code,
                CASE WHEN btrim(i."def-cost") ~ '^[0-9]+(\.[0-9]+)?$' THEN i."def-cost"::numeric END AS def_cost,
                CASE WHEN btrim(i.weight)     ~ '^[0-9]+(\.[0-9]+)?$' THEN i.weight::numeric     END AS weight,
                ss."size-group-id"   AS size_group,
                sa.vendor_number     AS vendor_number,
                sa.vendor_name       AS vendor_name,
                sa.design_id         AS design_id,
                sa.design_name       AS design_name,
                sa.street_date       AS street_date,
                pr.price_levels      AS price_levels
            FROM (
                SELECT d.site_id AS fdm4_store, d.catalog_id, d.product_id,
                       CASE
                           WHEN btrim(d.detail_value::jsonb #>> '{product,0,customPrice}')
                                ~ '^[0-9]+(\.[0-9]+)?$'
                           THEN btrim(d.detail_value::jsonb #>> '{product,0,customPrice}')
                       END AS prod_price,
                       col ->> 'colorCode'                                  AS color_code,
                       CASE
                           WHEN btrim(col ->> 'customPrice') ~ '^[0-9]+(\.[0-9]+)?$'
                           THEN btrim(col ->> 'customPrice')
                       END                                                  AS color_price
                FROM fdm4.catalog_product_detail d
                CROSS JOIN LATERAL jsonb_array_elements(d.detail_value::jsonb #> '{product,0,color}') AS col
                WHERE d.detail_type = 'storeData'
                  AND d.site_id ~ '^S_'
                  AND pg_input_is_valid(d.detail_value, 'jsonb')
                  -- Virtual-catalog stores are generated wholesale below instead.
                  AND d.site_id NOT IN (SELECT fdm4_store FROM woo.virtual_catalog_store)
            ) sd
            JOIN fdm4.item i
              ON i."style-code" = sd.product_id
             AND i."color-code" = sd.color_code
            LEFT JOIN fdm4."style-color" sc
              ON sc."style-code" = i."style-code" AND sc."color-code" = i."color-code"
            LEFT JOIN fdm4."style-size" ss
              ON ss."style-code" = i."style-code" AND ss."size-code" = i."size-code"
            -- Available stock per item, precomputed once into _bal above (latest
            -- item-balance snapshot per warehouse, on-hand - committed, summed). See the
            -- _bal note for why this is materialized instead of an inline subquery.
            LEFT JOIN _bal bal ON bal.item_number = i."item-number"
            LEFT JOIN _mill m ON m.mill_code = btrim(i."mill-code")
            LEFT JOIN _style_attrs sa ON sa.style_code = i."style-code"
            LEFT JOIN _price pr ON pr.item_number = i."item-number"
            LEFT JOIN _store_tier st ON st.fdm4_store = sd.fdm4_store
            WHERE i."upc-code" IS NOT NULL AND i."upc-code" <> ''   -- skip items with no barcode
              -- Honor FDM4's web flag on webstores (2026-08-26): unchecking
              -- 'web' on an item (discontinued flow) removes it from every
              -- regular store even while it lingers in a store catalog.
              -- Virtual-catalog stores (Square/Davey) deliberately do NOT
              -- check this flag - their Woo products exist so FDM4 order
              -- pulls can match line items, discontinued included.
              AND btrim(i."web-active") = 'True'
            -- Deterministic pick among duplicate (store,catalog,upc) rows (e.g. dup
            -- colour entries in storeData) so payload / content_hash / row_version is
            -- stable run-to-run for change-tracking.
            ORDER BY sd.fdm4_store, sd.catalog_id, i."upc-code",
                     CASE
                         WHEN COALESCE(NULLIF(sd.color_price, ''), NULLIF(sd.prod_price, ''), '0')::numeric > 0
                             THEN COALESCE(NULLIF(sd.color_price, ''), sd.prod_price)
                         ELSE COALESCE(
                             pr.price_levels ->> st.price_key,
                             CASE WHEN btrim(i."retail-price") ~ '^[0-9]+(\.[0-9]+)?$'
                                  THEN btrim(i."retail-price") END
                         )
                     END, i."color-code", i."size-code"
        ) v

        UNION ALL

        -- Synthetic parents: for stores in woo.virtual_catalog_store, every
        -- web-active style that has at least one priced item. Bypasses FDM4's
        -- per-store catalog entirely (see the NOT IN exclusions above).
        SELECT
            vcs.fdm4_store, vcs.catalog_id, s."style-code" AS sku, 'parent'::text AS kind,
            s."style-code" AS style_code, NULL::text AS parent_sku, s.description AS name, s."item-status" AS status,
            NULL::text AS color_code, NULL::text AS color, NULL::text AS size_code, NULL::text AS size,
            NULL::numeric AS price, NULL::numeric AS stock,
            s."mill-code" AS mill_code, m.brand AS brand,
            s."product-code" AS category, s.description AS item_name, s."origin-country" AS origin_country,
            s.harmonization AS harmonization, s."item-status" AS item_status, NULL::text AS web_active,
            NULL::text AS ean_code, NULL::numeric AS def_cost, NULL::numeric AS weight,
            sa.street_date AS street_date, NULL::text AS size_group,
            sa.vendor_number AS vendor_number, sa.vendor_name AS vendor_name,
            sa.design_id AS design_id, sa.design_name AS design_name, NULL::jsonb AS price_levels,
            jsonb_build_object('kind','parent','name',s.description,'status',s."item-status",'price',NULL) AS payload,
            jsonb_build_object('kind','parent','name',s.description,'status',s."item-status")             AS structural_payload,
            jsonb_build_object('price',NULL)                                                              AS stockprice_payload
        FROM woo.virtual_catalog_store vcs
        CROSS JOIN fdm4.style s
        LEFT JOIN _mill m        ON m.mill_code  = btrim(s."mill-code")
        LEFT JOIN _style_attrs sa ON sa.style_code = s."style-code"
        WHERE EXISTS (
            SELECT 1 FROM fdm4.item i
             WHERE i."style-code" = s."style-code"
               AND i."upc-code" IS NOT NULL AND i."upc-code" <> ''
               -- No web-active check here (2026-08-26): virtual-catalog
               -- stores back FDM4 order pulls, so discontinued (web
               -- unchecked) items must stay matchable.
               AND btrim(i."retail-price") ~ '^[0-9]+(\.[0-9]+)?$' AND i."retail-price"::numeric > 0
        )

        UNION ALL

        -- Synthetic variations: for stores in woo.virtual_catalog_store, every
        -- web-active item with a positive item-master retail price, priced at
        -- that retail price (no catalog customPrice, so no per-store override).
        SELECT
            vcs.fdm4_store, vcs.catalog_id, i."upc-code" AS sku, 'variation'::text AS kind,
            i."style-code" AS style_code, i."style-code" AS parent_sku, NULL::text AS name, NULL::text AS status,
            i."color-code" AS color_code, sc.description AS color, i."size-code" AS size_code, ss.description AS size,
            i."retail-price"::numeric AS price, COALESCE(vcs.stock_override, bal.stock) AS stock,
            i."mill-code" AS mill_code, m.brand AS brand,
            i."product-category" AS category, i."item-name" AS item_name, i."origin-country" AS origin_country,
            i.harmonization AS harmonization, i."item-status" AS item_status, i."web-active" AS web_active,
            i."ean-code" AS ean_code,
            CASE WHEN btrim(i."def-cost") ~ '^[0-9]+(\.[0-9]+)?$' THEN i."def-cost"::numeric END AS def_cost,
            CASE WHEN btrim(i.weight)     ~ '^[0-9]+(\.[0-9]+)?$' THEN i.weight::numeric     END AS weight,
            sa.street_date AS street_date, ss."size-group-id" AS size_group,
            sa.vendor_number AS vendor_number, sa.vendor_name AS vendor_name,
            sa.design_id AS design_id, sa.design_name AS design_name, pr.price_levels AS price_levels,
            jsonb_build_object('kind','variation','style',i."style-code",'color',sc.description,'size',ss.description,
                               'price',i."retail-price",'stock',COALESCE(vcs.stock_override, bal.stock),'active',i.active) AS payload,
            jsonb_build_object('kind','variation','style',i."style-code",'color',sc.description,'size',ss.description,
                               'active',i.active)                                                         AS structural_payload,
            jsonb_build_object('price',i."retail-price",'stock',COALESCE(vcs.stock_override, bal.stock))  AS stockprice_payload
        FROM woo.virtual_catalog_store vcs
        CROSS JOIN fdm4.item i
        LEFT JOIN fdm4."style-color" sc ON sc."style-code" = i."style-code" AND sc."color-code" = i."color-code"
        LEFT JOIN fdm4."style-size"  ss ON ss."style-code" = i."style-code" AND ss."size-code"  = i."size-code"
        LEFT JOIN _bal bal        ON bal.item_number = i."item-number"
        LEFT JOIN _mill m         ON m.mill_code     = btrim(i."mill-code")
        LEFT JOIN _style_attrs sa ON sa.style_code   = i."style-code"
        LEFT JOIN _price pr       ON pr.item_number  = i."item-number"
        WHERE i."upc-code" IS NOT NULL AND i."upc-code" <> ''
          -- No web-active check (see the synthetic-parents note above).
          AND btrim(i."retail-price") ~ '^[0-9]+(\.[0-9]+)?$' AND i."retail-price"::numeric > 0
    ) b;

    -- Product Mix Overrides (woo.store_mix_store / store_mix_item - the
    -- Warehouse Ops "Product Mix" tab). v1 is remove-only: mode='list' stores
    -- project ONLY their listed styles, optionally restricted to listed color
    -- channels (colors[]; NULL = all) minus per-color size exclusions.
    -- mode='all' stores (Square) follow FDM4 completely - candidate refresh
    -- only, never filtered. Stores not registered are untouched.
    -- FAST PATH: with no active override store this block executes nothing -
    -- byte-identical to the pre-mix pipeline (same pattern as price rules).
    IF EXISTS (SELECT 1 FROM woo.store_mix_store WHERE active) THEN
        -- Drift set FIRST (from the unfiltered _base): what FDM4 would
        -- currently give each active override store. Full rewrite each run -
        -- the table is small (≤ a few thousand style rows).
        DELETE FROM woo.store_mix_candidate;
        INSERT INTO woo.store_mix_candidate (fdm4_store, style_code, colors)
        SELECT b.fdm4_store,
               upper(btrim(b.style_code)),
               NULLIF(array_agg(DISTINCT upper(btrim(b.color_code)))
                          FILTER (WHERE b.color_code IS NOT NULL
                                    AND btrim(b.color_code) <> ''), '{}')
        FROM _base b
        JOIN woo.store_mix_store m
          ON m.fdm4_store = b.fdm4_store AND m.active
        WHERE btrim(COALESCE(b.style_code, '')) <> ''
        GROUP BY b.fdm4_store, upper(btrim(b.style_code));

        -- Remove-only filter for mode='list' stores. A row survives when its
        -- style is listed AND (it is the parent, or its color channel is
        -- included and its size is not excluded for that color). NULL-safe:
        -- absent size_excludes / missing color key coalesce to "not excluded".
        -- Belt-and-braces: a list-mode store with ZERO mix items is skipped
        -- entirely (never filtered) - the API guarantees at least one item on
        -- every path, so an empty list can only mean hand-edited SQL, and the
        -- transform must be structurally unable to wipe a store's whole mix.
        DELETE FROM _base b
        USING woo.store_mix_store m
        WHERE m.fdm4_store = b.fdm4_store
          AND m.active
          AND m.mode = 'list'
          AND EXISTS (
              SELECT 1 FROM woo.store_mix_item g
              WHERE g.fdm4_store = m.fdm4_store
          )
          AND NOT EXISTS (
              SELECT 1
              FROM woo.store_mix_item it
              WHERE it.fdm4_store = b.fdm4_store
                AND it.style_code = upper(btrim(COALESCE(b.style_code, '')))
                AND (b.kind = 'parent'
                     OR ( (it.colors IS NULL
                           OR upper(btrim(COALESCE(b.color_code, ''))) = ANY (it.colors))
                          AND NOT COALESCE(
                                (it.size_excludes -> upper(btrim(COALESCE(b.color_code, ''))))
                                    ? upper(btrim(COALESCE(b.size_code, ''))),
                                false) ))
          );
    ELSIF EXISTS (SELECT 1 FROM woo.store_mix_candidate) THEN
        -- No active override stores left: clear the stale drift set once.
        DELETE FROM woo.store_mix_candidate;
    END IF;

    -- PIM enrichment projection (pim.product_state - the Sales Layer mirror).
    -- PHASE 2, ships DARK: gated by the woo.app_flag row 'pim_projection'
    -- (absent/false = one EXISTS probe and nothing else - same fast-path
    -- pattern as price rules / product mix). When enabled, PARENT rows gain a
    -- 'pim' object (name, description, short_description, images, payload_md5)
    -- in payload AND structural_payload BEFORE hashing, so PIM content changes
    -- version-bump structural_hash and reach the Woo engine (which applies
    -- them only behind its own product_sync_pim_content feature flag) and any
    -- feed consumers. Join: pim rows are keyed (blog_id, sku_parent) with
    -- fdm4_store stamped when the Store Sync Map knew the blog; we prefer the
    -- stamped store and fall back blog->store via woo.store_blog_map, deduping
    -- per (store, sku) by LOWEST blog_id (S_002384 legitimately maps to two
    -- blogs). Volatile fields (updated_at) are deliberately excluded so an
    -- identical re-push never churns hashes.
    IF EXISTS (SELECT 1 FROM woo.app_flag WHERE name = 'pim_projection' AND enabled) THEN
        UPDATE _base b
        SET payload            = b.payload            || jsonb_build_object('pim', e.pim),
            structural_payload = b.structural_payload || jsonb_build_object('pim', e.pim)
        FROM (
            SELECT DISTINCT ON (store, sku) store, sku, pim
            FROM (
                SELECT COALESCE(NULLIF(btrim(p.fdm4_store), ''), bm.fdm4_store) AS store,
                       upper(btrim(p.sku_parent))                               AS sku,
                       p.blog_id                                                AS blog_id,
                       jsonb_build_object(
                           'name',              NULLIF(p.name, ''),
                           'description',       NULLIF(p.description, ''),
                           'short_description', NULLIF(p.short_description, ''),
                           'images',            COALESCE(p.payload -> 'parent' -> 'images', '[]'::jsonb),
                           'payload_md5',       p.payload_md5
                       )                                                        AS pim
                FROM pim.product_state p
                LEFT JOIN woo.store_blog_map bm ON bm.blog_id = p.blog_id
            ) src
            WHERE store IS NOT NULL AND sku <> ''
            ORDER BY store, sku, blog_id
        ) e
        WHERE b.kind = 'parent'
          AND b.fdm4_store = e.store
          AND upper(btrim(b.sku)) = e.sku;
    END IF;

    -- Price rules (woo.price_rule via woo.eval_price_rules - the SAME function
    -- the app preview uses): applied AFTER the tier fallback and BEFORE hashing,
    -- so rule prices flow through stockprice_hash / the engine fast path
    -- natively. `price` carries the rule-applied value (what the Woo engine
    -- reads); `base_price` always carries the pre-rule value (what the app
    -- preview evaluates from, so preview can never double-apply a live rule).
    -- FAST PATH: with no active in-window rule, skip the per-row evaluator
    -- entirely (~600k lateral calls cost minutes) - that branch is
    -- byte-identical to the pre-rules pipeline.
    IF EXISTS (SELECT 1 FROM woo.price_rule
                WHERE active
                  AND (effective_from  IS NULL OR current_date >= effective_from)
                  AND (effective_until IS NULL OR current_date <= effective_until)) THEN
        -- Materialize only rows that can match at least one active rule's
        -- targeting. The procedural evaluator still owns ordering, stacking,
        -- floors, and price math; this relation only avoids invoking it for
        -- rows which cannot possibly reach a rule.
        CREATE TEMP TABLE _rule_candidate ON COMMIT DROP AS
        SELECT b.fdm4_store, b.catalog_id, b.sku
        FROM _base b
        WHERE b.price IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM woo.price_rule pr
              WHERE pr.active
                AND (pr.effective_from IS NULL
                     OR current_date >= pr.effective_from)
                AND (pr.effective_until IS NULL
                     OR current_date <= pr.effective_until)
                AND (
                    (
                        COALESCE(cardinality(pr.stores), 0) = 0
                        AND COALESCE(cardinality(pr.store_tiers), 0) = 0
                    )
                    OR b.fdm4_store = ANY(COALESCE(pr.stores, '{}'))
                    OR EXISTS (
                        SELECT 1
                        FROM woo.store_pricing_tier spt
                        WHERE spt.fdm4_store = b.fdm4_store
                          AND spt.tier_name = ANY(
                              COALESCE(pr.store_tiers, '{}')
                          )
                    )
                )
                AND (
                    COALESCE(cardinality(pr.styles), 0) = 0
                    OR upper(btrim(b.style_code)) = ANY(pr.styles)
                )
                AND (
                    COALESCE(cardinality(pr.brands), 0) = 0
                    OR b.brand = ANY(pr.brands)
                )
                AND (
                    COALESCE(cardinality(pr.categories), 0) = 0
                    OR b.category = ANY(pr.categories)
                )
          );
        CREATE UNIQUE INDEX ON _rule_candidate (fdm4_store, catalog_id, sku);
        ANALYZE _rule_candidate;

        CREATE TEMP TABLE _rule_result ON COMMIT DROP AS
        SELECT b.fdm4_store, b.catalog_id, b.sku,
               rp.final_price AS rule_price
        FROM _base b
        JOIN _rule_candidate c
          USING (fdm4_store, catalog_id, sku)
        LEFT JOIN LATERAL woo.eval_price_rules(
            b.fdm4_store, b.style_code, b.brand, b.category,
            b.price, b.price_levels, b.def_cost
        ) rp ON true;
        CREATE UNIQUE INDEX ON _rule_result (fdm4_store, catalog_id, sku);

        CREATE TEMP TABLE _next ON COMMIT DROP AS
        SELECT
            fdm4_store, catalog_id, sku, kind, style_code, parent_sku, name, status,
            color_code, color, size_code, size,
            COALESCE(rule_price, price) AS price,
            price AS base_price,
            stock,
            mill_code, brand,
            category, item_name, origin_country, harmonization, item_status, web_active,
            ean_code, def_cost, weight, street_date, size_group,
            vendor_number, vendor_name, design_id, design_name, price_levels,
            CASE WHEN rule_price IS NOT NULL
                 THEN jsonb_set(payload, '{price}', to_jsonb(rule_price)) ELSE payload END AS payload,
            md5(structural_payload::text) AS structural_hash,
            md5((CASE WHEN rule_price IS NOT NULL
                      THEN jsonb_set(stockprice_payload, '{price}', to_jsonb(rule_price))
                      ELSE stockprice_payload END)::text) AS stockprice_hash,
            md5((CASE WHEN rule_price IS NOT NULL
                      THEN jsonb_set(payload, '{price}', to_jsonb(rule_price))
                      ELSE payload END)::text)            AS content_hash
        FROM (
            SELECT b.*, rr.rule_price
            FROM _base b
            LEFT JOIN _rule_result rr
              USING (fdm4_store, catalog_id, sku)
        ) u;
    ELSE
        CREATE TEMP TABLE _next ON COMMIT DROP AS
        SELECT
            fdm4_store, catalog_id, sku, kind, style_code, parent_sku, name, status,
            color_code, color, size_code, size, price, price AS base_price, stock,
            mill_code, brand,
            category, item_name, origin_country, harmonization, item_status, web_active,
            ean_code, def_cost, weight, street_date, size_group,
            vendor_number, vendor_name, design_id, design_name, price_levels,
            payload,
            md5(structural_payload::text) AS structural_hash,
            md5(stockprice_payload::text) AS stockprice_hash,
            md5(payload::text)            AS content_hash
        FROM _base;
    END IF;

    -- Upsert present rows. Bump row_version + changed_at ONLY when content
    -- actually differs (nextval in the unmatched CASE branch is not evaluated).
    INSERT INTO woo.store_product_state AS s (
        fdm4_store, catalog_id, sku, kind, style_code, parent_sku, name, status,
        color_code, color, size_code, size, price, base_price, stock, payload,
        mill_code, brand,
        category, item_name, origin_country, harmonization, item_status, web_active,
        ean_code, def_cost, weight, street_date, size_group,
        vendor_number, vendor_name, design_id, design_name, price_levels,
        structural_hash, stockprice_hash, content_hash,
        is_active, row_version, changed_at, refreshed_at
    )
    SELECT
        n.fdm4_store, n.catalog_id, n.sku, n.kind, n.style_code, n.parent_sku, n.name, n.status,
        n.color_code, n.color, n.size_code, n.size, n.price, n.base_price, n.stock, n.payload,
        n.mill_code, n.brand,
        n.category, n.item_name, n.origin_country, n.harmonization, n.item_status, n.web_active,
        n.ean_code, n.def_cost, n.weight, n.street_date, n.size_group,
        n.vendor_number, n.vendor_name, n.design_id, n.design_name, n.price_levels,
        n.structural_hash, n.stockprice_hash, n.content_hash,
        true, nextval('woo.state_version_seq'), now(), now()
    FROM _next n
    ON CONFLICT (fdm4_store, catalog_id, sku) DO UPDATE SET
        kind = EXCLUDED.kind, style_code = EXCLUDED.style_code, parent_sku = EXCLUDED.parent_sku,
        name = EXCLUDED.name, status = EXCLUDED.status,
        color_code = EXCLUDED.color_code, color = EXCLUDED.color,
        size_code = EXCLUDED.size_code, size = EXCLUDED.size,
        price = EXCLUDED.price, base_price = EXCLUDED.base_price,
        stock = EXCLUDED.stock, payload = EXCLUDED.payload,
        mill_code = EXCLUDED.mill_code, brand = EXCLUDED.brand,
        category = EXCLUDED.category, item_name = EXCLUDED.item_name,
        origin_country = EXCLUDED.origin_country, harmonization = EXCLUDED.harmonization,
        item_status = EXCLUDED.item_status, web_active = EXCLUDED.web_active,
        ean_code = EXCLUDED.ean_code, def_cost = EXCLUDED.def_cost, weight = EXCLUDED.weight,
        street_date = EXCLUDED.street_date, size_group = EXCLUDED.size_group,
        vendor_number = EXCLUDED.vendor_number, vendor_name = EXCLUDED.vendor_name,
        design_id = EXCLUDED.design_id, design_name = EXCLUDED.design_name,
        price_levels = EXCLUDED.price_levels,
        structural_hash = EXCLUDED.structural_hash, stockprice_hash = EXCLUDED.stockprice_hash,
        content_hash = EXCLUDED.content_hash,
        is_active = true,
        refreshed_at = now(),
        row_version = CASE WHEN s.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                           THEN nextval('woo.state_version_seq') ELSE s.row_version END,
        changed_at  = CASE WHEN s.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                           THEN now() ELSE s.changed_at END;

    -- Tombstone rows that were present and are now gone (bump version so the
    -- delta carries the removal). Keeps the row so the Woo engine can deactivate.
    UPDATE woo.store_product_state s
       SET is_active   = false,
           row_version = nextval('woo.state_version_seq'),
           changed_at  = now()
     WHERE s.is_active = true
       AND NOT EXISTS (
           SELECT 1 FROM _next n
            WHERE n.fdm4_store = s.fdm4_store
              AND n.catalog_id = s.catalog_id
              AND n.sku        = s.sku
       );

    -- Catalogs-per-store summary from the projected desired set, including a
    -- configured virtual catalog even when it currently projects zero parents.
    DELETE FROM woo.store_catalog;
    INSERT INTO woo.store_catalog (fdm4_store, catalog_id, products, suggested)
    SELECT fdm4_store, catalog_id, products, (rn = 1)
    FROM (
        SELECT fdm4_store, catalog_id, products,
               ROW_NUMBER() OVER (
                   PARTITION BY fdm4_store
                   ORDER BY clone_rank ASC, products DESC, catalog_id ASC
               ) AS rn
        FROM (
            SELECT n.fdm4_store, n.catalog_id,
                   count(DISTINCT n.sku) FILTER (WHERE n.kind = 'parent')
                       AS products,
                   -- clone/demo catalogs sort last so the "real" one is suggested
                   CASE WHEN n.catalog_id ~* '(_0?1|_woo(_1)?|demowebstore|_1)$'
                        THEN 1 ELSE 0 END AS clone_rank
            FROM _next n
            GROUP BY n.fdm4_store, n.catalog_id

            UNION ALL

            SELECT vcs.fdm4_store, vcs.catalog_id, 0::bigint AS products,
                   CASE WHEN vcs.catalog_id ~* '(_0?1|_woo(_1)?|demowebstore|_1)$'
                        THEN 1 ELSE 0 END AS clone_rank
            FROM woo.virtual_catalog_store vcs
            WHERE NOT EXISTS (
                SELECT 1 FROM _next n
                WHERE n.fdm4_store = vcs.fdm4_store
                  AND n.catalog_id = vcs.catalog_id
            )
        ) g
    ) r;

    SELECT count(*) INTO total FROM woo.store_product_state WHERE is_active;
    RETURN total;
END;
$$;

GRANT EXECUTE ON FUNCTION woo.refresh_product_state() TO etl_writer;
