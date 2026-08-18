-- Pricing-tier fallback: when a store's FDM4 catalog price is missing/0 (so the
-- transform would fall back to MSRP/retail) AND the store is configured to a
-- non-MSRP tier, use that tier's computed price from the price list instead.
-- FDM4's published price always wins; this only fills blanks. Empty
-- store_pricing_tier = feature inert.
BEGIN;

-- The available tiers, each mapped to a key in the computed price_levels jsonb
-- (base/corp1/corp2/corp3/wholesale/employee/msrp). is_msrp tiers are a no-op
-- (they equal the retail fallback), so a store on MSRP behaves as unconfigured.
CREATE TABLE IF NOT EXISTS woo.pricing_tier (
    tier_name        text    NOT NULL PRIMARY KEY,
    price_levels_key text    NOT NULL,
    is_msrp          boolean NOT NULL DEFAULT false,
    sort_order       integer NOT NULL DEFAULT 0
);

INSERT INTO woo.pricing_tier (tier_name, price_levels_key, is_msrp, sort_order) VALUES
    ('Level 1 (Corp 1)', 'corp1',     false, 1),
    ('Level 2 (Corp 2)', 'corp2',     false, 2),
    ('Level 3 (Corp 3)', 'corp3',     false, 3),
    ('Wholesale',        'wholesale', false, 4),
    ('Employee',         'employee',  false, 5),
    ('MSRP',             'msrp',      true,  6)
ON CONFLICT (tier_name) DO NOTHING;

-- Store -> tier assignment. Only stores listed here get the fallback.
CREATE TABLE IF NOT EXISTS woo.store_pricing_tier (
    fdm4_store text        NOT NULL PRIMARY KEY,
    tier_name  text        NOT NULL REFERENCES woo.pricing_tier(tier_name),
    note       text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Mariani: FDM4 never published its catalog pricing; fall back to Level 3.
INSERT INTO woo.store_pricing_tier (fdm4_store, tier_name, note) VALUES
    ('S_001165', 'Level 3 (Corp 3)', 'Mariani - FDM4 catalog price unpublished; fallback to Level 3')
ON CONFLICT (fdm4_store) DO NOTHING;

GRANT SELECT ON woo.pricing_tier, woo.store_pricing_tier TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.pricing_tier, woo.store_pricing_tier TO etl_writer;
-- The Warehouse Operations app (logo_admin role) manages these via its UI.
GRANT USAGE ON SCHEMA woo TO logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.pricing_tier, woo.store_pricing_tier TO logo_admin;

COMMIT;
