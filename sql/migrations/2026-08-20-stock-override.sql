-- Per-style fake-inventory overrides (Warehouse Ops "Fake Inventory" page).
--
-- The WP sync engine automatically fakes stock at 99,999 for third-party
-- styles (non-Arborwear mill). This table is the manual override layer on
-- top: mode 'fake' forces a style to fake inventory, mode 'real' forces real
-- inventory. It replaces the legacy WP site options arb_tp_real_styles /
-- arb_tp_fake_styles (both empty at migration time on prod and dev, so this
-- table intentionally starts empty and nothing changes behavior).
--
-- The WP-side reader (AVNPH::style_is_third_party) consults this table first
-- and falls back to the site options if the warehouse is unreachable.
--
-- Apply as postgres on the warehouse box:
--   sudo -u postgres psql -d arb_warehouse -f 2026-08-20-stock-override.sql

BEGIN;

CREATE TABLE IF NOT EXISTS woo.stock_override (
    style_code text PRIMARY KEY,
    mode       text NOT NULL CHECK (mode IN ('fake', 'real')),
    note       text NOT NULL DEFAULT '',
    active     boolean NOT NULL DEFAULT true,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON woo.stock_override TO logo_admin;
GRANT SELECT ON woo.stock_override TO woo_reader;

COMMIT;
