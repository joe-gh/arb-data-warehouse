-- Woo product presence for the PIM push (2026-09-09). WordPress pushes, once
-- an hour, every published parent product per store with its catalog
-- visibility and the SKUs of its published variations (wp arb
-- pim-presence-push). push_pim.py reads it to decide product visibility in
-- the PIM: live on a store that is not one of the all-products stores ->
-- visible, otherwise draft. Full replace per env, so the writer needs DELETE.
-- The change-row action list (a CHECK constraint) gains product_draft.
BEGIN;

CREATE TABLE IF NOT EXISTS pim.woo_presence (
    env          text        NOT NULL,
    blog_id      integer     NOT NULL,
    parent_sku   text        NOT NULL,
    visible      boolean     NOT NULL DEFAULT true,
    upcs         text[]      NOT NULL DEFAULT '{}',
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (env, blog_id, parent_sku)
);
CREATE INDEX IF NOT EXISTS pim_woo_presence_sku  ON pim.woo_presence (parent_sku);
CREATE INDEX IF NOT EXISTS pim_woo_presence_upcs ON pim.woo_presence USING gin (upcs);

GRANT SELECT, INSERT, DELETE ON TABLE pim.woo_presence TO pim_writer;
GRANT SELECT ON TABLE pim.woo_presence TO woo_reader, insights_reader;

ALTER TABLE pim.push_change_row DROP CONSTRAINT IF EXISTS push_change_row_action_check;
ALTER TABLE pim.push_change_row ADD CONSTRAINT push_change_row_action_check
    CHECK (action IN ('color_fill', 'color_fix', 'variant_remove', 'variant_create',
                      'product_create', 'product_publish', 'product_draft'));

COMMIT;
