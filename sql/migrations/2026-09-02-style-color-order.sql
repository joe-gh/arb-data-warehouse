-- Editor-only display order of garment colors inside one style's logo grid.
-- Global per style (a garment's colors are the same in every store). Colors
-- without a row sort after the ordered ones, alphabetically by name. This
-- never reaches /feed or WordPress: storefront color order is WooCommerce
-- attribute order, not a logo concern.
BEGIN;

CREATE TABLE IF NOT EXISTS logo.style_color_order (
    product_style       text        NOT NULL CHECK (btrim(product_style) <> ''),
    garment_color_code  text        NOT NULL CHECK (btrim(garment_color_code) <> ''),
    sort_order          integer     NOT NULL CHECK (sort_order >= 0),
    updated_by          text        NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product_style, garment_color_code)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE logo.style_color_order TO logo_admin;

COMMIT;
