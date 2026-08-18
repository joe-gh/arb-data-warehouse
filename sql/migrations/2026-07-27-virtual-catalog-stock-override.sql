-- Per-store stock override for virtual-catalog stores: when set, every
-- synthetic variation projects this stock instead of the live item-balance
-- availability. Used for the Square store (S_015883), whose Woo products
-- exist only to feed the Square POS catalog and must always look purchasable
-- (81% of the projection has no real stock). NULL = real stock, unchanged.
--
-- This migration only adds the (NULL) column. Activating an override is a
-- deliberate UPDATE run separately once the updated transform is deployed:
--
--   UPDATE woo.virtual_catalog_store SET stock_override = 9999
--    WHERE fdm4_store = 'S_015883';
BEGIN;

ALTER TABLE woo.virtual_catalog_store
    ADD COLUMN IF NOT EXISTS stock_override numeric;

COMMIT;
