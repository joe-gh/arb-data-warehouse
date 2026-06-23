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
-- CATALOG-AWARE: a store (site_id) can host several catalogs (catalog_id) — a
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
    -- Freshly-computed desired set into a temp table (same extraction as before,
    -- now also computing the structural / stock-price / content hashes).
    CREATE TEMP TABLE _next ON COMMIT DROP AS
    SELECT
        fdm4_store, catalog_id, sku, kind, style_code, parent_sku, name, status,
        color_code, color, size_code, size, price, stock, payload,
        md5(structural_payload::text) AS structural_hash,
        md5(stockprice_payload::text) AS stockprice_hash,
        md5(payload::text)            AS content_hash
    FROM (
        -- Parents: one per (store, catalog, product) from the per-store storeData JSON.
        SELECT
            d.fdm4_store, d.catalog_id, d.sku, 'parent'::text AS kind, d.style_code,
            NULL::text AS parent_sku, d.name, d.status,
            NULL::text AS color_code, NULL::text AS color, NULL::text AS size_code, NULL::text AS size,
            NULLIF(d.price_text, '')::numeric AS price, NULL::numeric AS stock,
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
                d.detail_value::jsonb #>> '{product,0,customPrice}' AS price_text
            FROM fdm4.catalog_product_detail d
            JOIN fdm4.style s ON s."style-code" = d.product_id
            WHERE d.detail_type = 'storeData'
              AND d.site_id ~ '^S_'
              AND pg_input_is_valid(d.detail_value, 'jsonb')
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
                    WHEN COALESCE(NULLIF(sd.color_price, ''), NULLIF(sd.prod_price, ''), '0')::numeric > 0
                        THEN COALESCE(NULLIF(sd.color_price, ''), sd.prod_price)
                    ELSE NULLIF(i."retail-price", '')
                END            AS price_text,
                bal.stock      AS stock,
                i.active       AS active
            FROM (
                SELECT d.site_id AS fdm4_store, d.catalog_id, d.product_id,
                       d.detail_value::jsonb #>> '{product,0,customPrice}' AS prod_price,
                       col ->> 'colorCode'                                  AS color_code,
                       col ->> 'customPrice'                                AS color_price
                FROM fdm4.catalog_product_detail d
                CROSS JOIN LATERAL jsonb_array_elements(d.detail_value::jsonb #> '{product,0,color}') AS col
                WHERE d.detail_type = 'storeData'
                  AND d.site_id ~ '^S_'
                  AND pg_input_is_valid(d.detail_value, 'jsonb')
            ) sd
            JOIN fdm4.item i
              ON i."style-code" = sd.product_id
             AND i."color-code" = sd.color_code
            LEFT JOIN fdm4."style-color" sc
              ON sc."style-code" = i."style-code" AND sc."color-code" = i."color-code"
            LEFT JOIN fdm4."style-size" ss
              ON ss."style-code" = i."style-code" AND ss."size-code" = i."size-code"
            LEFT JOIN (
                SELECT "item-number", SUM(NULLIF("inv-bal", '')::numeric) AS stock
                FROM fdm4."item-balance" GROUP BY "item-number"
            ) bal ON bal."item-number" = i."item-number"
            WHERE i."upc-code" IS NOT NULL AND i."upc-code" <> ''   -- skip items with no barcode
            -- Deterministic pick among duplicate (store,catalog,upc) rows (e.g. dup
            -- colour entries in storeData) so payload / content_hash / row_version is
            -- stable run-to-run for change-tracking.
            ORDER BY sd.fdm4_store, sd.catalog_id, i."upc-code",
                     CASE
                         WHEN COALESCE(NULLIF(sd.color_price, ''), NULLIF(sd.prod_price, ''), '0')::numeric > 0
                             THEN COALESCE(NULLIF(sd.color_price, ''), sd.prod_price)
                         ELSE NULLIF(i."retail-price", '')
                     END, i."color-code", i."size-code"
        ) v
    ) u;

    -- Upsert present rows. Bump row_version + changed_at ONLY when content
    -- actually differs (nextval in the unmatched CASE branch is not evaluated).
    INSERT INTO woo.store_product_state AS s (
        fdm4_store, catalog_id, sku, kind, style_code, parent_sku, name, status,
        color_code, color, size_code, size, price, stock, payload,
        structural_hash, stockprice_hash, content_hash,
        is_active, row_version, changed_at, refreshed_at
    )
    SELECT
        n.fdm4_store, n.catalog_id, n.sku, n.kind, n.style_code, n.parent_sku, n.name, n.status,
        n.color_code, n.color, n.size_code, n.size, n.price, n.stock, n.payload,
        n.structural_hash, n.stockprice_hash, n.content_hash,
        true, nextval('woo.state_version_seq'), now(), now()
    FROM _next n
    ON CONFLICT (fdm4_store, catalog_id, sku) DO UPDATE SET
        kind = EXCLUDED.kind, style_code = EXCLUDED.style_code, parent_sku = EXCLUDED.parent_sku,
        name = EXCLUDED.name, status = EXCLUDED.status,
        color_code = EXCLUDED.color_code, color = EXCLUDED.color,
        size_code = EXCLUDED.size_code, size = EXCLUDED.size,
        price = EXCLUDED.price, stock = EXCLUDED.stock, payload = EXCLUDED.payload,
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

    -- Catalogs-per-store summary + suggested primary (non-clone name, most products).
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
            SELECT d.site_id AS fdm4_store, d.catalog_id,
                   count(DISTINCT d.product_id) AS products,
                   -- clone/demo catalogs sort last so the "real" one is suggested
                   CASE WHEN d.catalog_id ~* '(_0?1|_woo(_1)?|demowebstore|_1)$' THEN 1 ELSE 0 END AS clone_rank
            FROM fdm4.catalog_product_detail d
            WHERE d.detail_type = 'storeData' AND d.site_id ~ '^S_'
            GROUP BY d.site_id, d.catalog_id
        ) g
    ) r;

    SELECT count(*) INTO total FROM woo.store_product_state WHERE is_active;
    RETURN total;
END;
$$;

GRANT EXECUTE ON FUNCTION woo.refresh_product_state() TO etl_writer;
