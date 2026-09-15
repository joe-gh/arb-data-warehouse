-- push_pim.py (2026-09-15, later the same day): Sales Layer's product DELETE
-- detaches variants (prod_ref cleared, dropped to draft) instead of deleting
-- them, so the 14/15 Sept product removals left ~28k orphan variants behind.
-- New action variant_orphan_remove deletes variants that carry no product
-- reference; product_remove now deletes a product's variants first.
-- Idempotent rebuild of the CHECK with the full list.
BEGIN;

ALTER TABLE pim.push_change_row DROP CONSTRAINT IF EXISTS push_change_row_action_check;
ALTER TABLE pim.push_change_row ADD CONSTRAINT push_change_row_action_check
    CHECK (action IN ('color_fill', 'color_fix', 'variant_remove', 'variant_create',
                      'product_create', 'product_publish', 'product_draft',
                      'variant_publish', 'product_remove', 'style_fill',
                      'variant_orphan_remove'));

COMMIT;
