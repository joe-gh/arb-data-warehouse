-- push_pim.py gains a product_publish action (2026-09-09): products the tool
-- created as drafts in the PIM are switched to visible, and new products are
-- created visible from now on. The change-row action list is a CHECK
-- constraint (2026-08-21-pim-push-change-sets.sql), so the new value has to be
-- admitted here. Idempotent: the constraint is rebuilt with the full list.
BEGIN;

ALTER TABLE pim.push_change_row DROP CONSTRAINT IF EXISTS push_change_row_action_check;
ALTER TABLE pim.push_change_row ADD CONSTRAINT push_change_row_action_check
    CHECK (action IN ('color_fill', 'color_fix', 'variant_remove', 'variant_create',
                      'product_create', 'product_publish'));

COMMIT;
