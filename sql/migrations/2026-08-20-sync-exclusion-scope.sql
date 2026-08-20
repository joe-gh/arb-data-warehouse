-- Pricing-scoped store sync blocks.
--
-- scope 'full'    : the whole-store block behaves as before (store skipped).
-- scope 'pricing' : the sync runs normally for the store (creates, stock,
--                   status, deactivations) but never writes a price to an
--                   existing variation. New variations still get their
--                   initial FDM4 price. Per-style rows ignore scope (a style
--                   freeze is already total).
--
-- Deploy order matters: apply this BEFORE the engine that reads the column
-- ships anywhere, because the engine's exclusion read fails open.
--
-- Apply as postgres on the warehouse box:
--   sudo -u postgres psql -d arb_warehouse -f 2026-08-20-sync-exclusion-scope.sql

BEGIN;

ALTER TABLE woo.sync_exclusion
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'full';

ALTER TABLE woo.sync_exclusion
    DROP CONSTRAINT IF EXISTS sync_exclusion_scope_check;
ALTER TABLE woo.sync_exclusion
    ADD CONSTRAINT sync_exclusion_scope_check CHECK (scope IN ('full', 'pricing'));

COMMIT;
