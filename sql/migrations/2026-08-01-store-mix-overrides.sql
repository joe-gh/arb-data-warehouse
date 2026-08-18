-- Product Mix Overrides (ARB_PRODUCT_MIX_PLAN.md, decisions resolved 2026-08-01):
-- opt-in per-store control of WHICH products project into Woo. v1 is
-- remove-only (trims what FDM4 offers; additive mix is v2).
--   mode='all'  : follow FDM4 completely - NO filtering; new FDM4 products
--                 flow in automatically (Square / S_015883 runs like this).
--   mode='list' : project ONLY styles present in woo.store_mix_item, with
--                 optional color-channel restriction (colors[]; NULL = all)
--                 and per-color size exclusions (size_excludes jsonb:
--                 {"<COLOR_CODE>": ["<SIZE_CODE>", ...]}).
-- Stores absent from woo.store_mix_store are 100% FDM4-driven and untouched.
-- Conventions: style codes, color codes (colors[] entries and size_excludes
-- keys) and size codes (size_excludes values) are stored upper(btrim())'d by
-- the app; the transform uppercases its side of every comparison.
-- Managed in the Warehouse Ops "Product Mix" tab.
BEGIN;

CREATE TABLE IF NOT EXISTS woo.store_mix_store (
    fdm4_store  text PRIMARY KEY,
    mode        text NOT NULL DEFAULT 'list' CHECK (mode IN ('all','list')),
    active      boolean NOT NULL DEFAULT true,
    note        text NOT NULL DEFAULT '',
    created_by  text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    imported_at timestamptz
);

CREATE TABLE IF NOT EXISTS woo.store_mix_item (
    fdm4_store    text NOT NULL,
    style_code    text NOT NULL,
    colors        text[],                   -- NULL = all color channels
    size_excludes jsonb,                    -- {"COLOR": ["SIZE", ...]} within included colors
    source        text NOT NULL DEFAULT 'manual' CHECK (source IN ('import','manual')),
    added_by      text NOT NULL DEFAULT '',
    added_at      timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL DEFAULT '',
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fdm4_store, style_code)
);

-- Transform-maintained drift set: what FDM4 would CURRENTLY give each active
-- override store (rebuilt every refresh from _base BEFORE filtering). Powers
-- the "new in FDM4, not in your mix" badge and merge re-imports - once the
-- filter is live, excluded styles tombstone out of store_product_state, so
-- drift is not queryable from state.
CREATE TABLE IF NOT EXISTS woo.store_mix_candidate (
    fdm4_store text NOT NULL,
    style_code text NOT NULL,
    colors     text[],
    PRIMARY KEY (fdm4_store, style_code)
);

CREATE TABLE IF NOT EXISTS woo.store_mix_audit (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    op         text NOT NULL,
    tbl        text NOT NULL,
    fdm4_store text,
    style_code text,
    actor      text NOT NULL DEFAULT '',
    row_data   jsonb
);

-- Shared audit trigger for both mix tables. Field access goes through
-- to_jsonb() because store_mix_store has no style_code column and plpgsql
-- NEW.<missing column> raises at runtime.
CREATE OR REPLACE FUNCTION woo.audit_store_mix_row() RETURNS trigger AS $fn$
DECLARE
    rowj jsonb;
BEGIN
    rowj := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
    INSERT INTO woo.store_mix_audit (op, tbl, fdm4_store, style_code, actor, row_data)
    VALUES (TG_OP, TG_TABLE_NAME,
            rowj ->> 'fdm4_store',
            rowj ->> 'style_code',
            COALESCE(NULLIF(current_setting('logo.actor', true), ''), current_user),
            rowj);
    RETURN COALESCE(NEW, OLD);
END $fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS store_mix_store_audit ON woo.store_mix_store;
CREATE TRIGGER store_mix_store_audit
    AFTER INSERT OR UPDATE OR DELETE ON woo.store_mix_store
    FOR EACH ROW EXECUTE FUNCTION woo.audit_store_mix_row();

DROP TRIGGER IF EXISTS store_mix_item_audit ON woo.store_mix_item;
CREATE TRIGGER store_mix_item_audit
    AFTER INSERT OR UPDATE OR DELETE ON woo.store_mix_item
    FOR EACH ROW EXECUTE FUNCTION woo.audit_store_mix_row();

-- Grants mirror the price-rules / sync-exclusion precedent. The transform
-- runs as the database owner, so store_mix_candidate needs no writer grant;
-- the app reads it for the drift badge.
GRANT SELECT ON woo.store_mix_store, woo.store_mix_item,
               woo.store_mix_candidate, woo.store_mix_audit
    TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.store_mix_store, woo.store_mix_item
    TO etl_writer, logo_admin;
GRANT SELECT ON woo.store_mix_candidate TO etl_writer, logo_admin;
GRANT SELECT, INSERT ON woo.store_mix_audit TO etl_writer, logo_admin;
GRANT USAGE ON SEQUENCE woo.store_mix_audit_id_seq TO etl_writer, logo_admin;

COMMIT;
