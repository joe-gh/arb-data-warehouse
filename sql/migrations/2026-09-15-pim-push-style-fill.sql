-- push_pim.py (2026-09-15): FDM4 is the single source of truth. Two additions:
--   style_fill      a PIM product with no style number whose reference (or a
--                   variant UPC) FDM4 knows gets FDM4's style code
--   product_remove  now also fires for products none of whose identifiers
--                   FDM4 knows (reason recorded in the row's `before`)
-- Only style_fill is a new action value; idempotent rebuild of the CHECK.
BEGIN;

ALTER TABLE pim.push_change_row DROP CONSTRAINT IF EXISTS push_change_row_action_check;
ALTER TABLE pim.push_change_row ADD CONSTRAINT push_change_row_action_check
    CHECK (action IN ('color_fill', 'color_fix', 'variant_remove', 'variant_create',
                      'product_create', 'product_publish', 'product_draft',
                      'variant_publish', 'product_remove', 'style_fill'));

COMMIT;
