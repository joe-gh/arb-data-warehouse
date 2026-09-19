-- Per-colour price override (our side): force a Woo price on specific garment
-- colours of a style at a store when the FDM4/B3B catalog price is wrong and
-- read-only. woo.refresh_product_state() reads this as the TOP price
-- precedence (before storeData customPrice and the FDM4 level), so an override
-- survives every hourly reconcile. Staged/undoable through the Logo Admin
-- agent (set_color_prices / clear_color_prices). color_code is required — a
-- whole-style forced price is what price rules already do.

CREATE TABLE IF NOT EXISTS woo.color_price_override (
    fdm4_store  text          NOT NULL,
    style_code  text          NOT NULL,
    color_code  text          NOT NULL,
    -- Bounds (price >= 0, <= 10000) are enforced by the write tool
    -- (set_color_prices); the app's strict DB contract keeps this column
    -- constraint-free.
    price       numeric(10,2) NOT NULL,
    note        text          NOT NULL DEFAULT '',
    active      boolean       NOT NULL DEFAULT true,
    updated_by  text          NOT NULL DEFAULT '',
    updated_at  timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (fdm4_store, style_code, color_code)
);

GRANT SELECT ON woo.color_price_override TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.color_price_override TO etl_writer, logo_admin;
