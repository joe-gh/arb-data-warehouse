-- Per-product logo PLACEMENT geometry, mirrored from production WP.
-- Source: the `product_logo_placement` ACF meta on parent products -
-- {left, top, width, height, angle, note} percentages positioning the logo
-- overlay on the product photo (hand-tuned in the WP placement editor).
-- Written by the WP-side mirror hook + `wp arb placement-backfill` (both use
-- the pim_writer path, like pim.product_state). Rows exist only where real
-- geometry is set ({"set":false} products are absent = unset).
-- Served to consumers on /feed/products parent rows as payload.logo_placement.
BEGIN;

CREATE TABLE IF NOT EXISTS pim.product_placement (
    blog_id    integer     NOT NULL,
    sku_parent text        NOT NULL,
    fdm4_store text,
    env        text        NOT NULL DEFAULT '',
    placement  jsonb       NOT NULL,
    fallback   boolean     NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (blog_id, sku_parent)
);
CREATE INDEX IF NOT EXISTS product_placement_sku ON pim.product_placement (sku_parent);
CREATE INDEX IF NOT EXISTS product_placement_store ON pim.product_placement (fdm4_store);

GRANT SELECT ON pim.product_placement TO woo_reader, insights_reader, logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON pim.product_placement TO pim_writer;

COMMIT;
