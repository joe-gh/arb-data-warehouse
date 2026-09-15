-- push_pim.py (2026-09-14): the PIM carries only what the Woo stores carry,
-- everything visible. Two new change-row actions:
--   variant_publish  a draft variant under a live product is switched to visible
--   product_remove   a product live on no counting store is deleted from the PIM
-- product_draft is no longer proposed (removal replaces it) but stays admitted
-- so historical rows keep validating. The action list is a CHECK constraint
-- (2026-08-21-pim-push-change-sets.sql); idempotent rebuild with the full list.
BEGIN;

ALTER TABLE pim.push_change_row DROP CONSTRAINT IF EXISTS push_change_row_action_check;
ALTER TABLE pim.push_change_row ADD CONSTRAINT push_change_row_action_check
    CHECK (action IN ('color_fill', 'color_fix', 'variant_remove', 'variant_create',
                      'product_create', 'product_publish', 'product_draft',
                      'variant_publish', 'product_remove'));

COMMIT;
