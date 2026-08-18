-- Price-rules engine (ARB_PRICING_RULES_PLAN.md, decisions resolved 2026-07-31):
-- first-match-wins by priority (per-rule stackable opt-in), NO rounding ever
-- (exact values, 4-dp end-to-end), above-MSRP allowed (preview flags), rules run
-- AFTER the tier fallback (they see the filled base price), scheduling in v1.
BEGIN;

-- Exact pricing end-to-end: widen the projected price to 4 decimal places.
-- Existing 2-dp values are unchanged; hashes only move when a rule actually
-- produces extra precision. (Woo side is value-preserving: engine fmt_price
-- never rounds.)
ALTER TABLE woo.store_product_state ALTER COLUMN price TYPE numeric(12,4);

CREATE TABLE IF NOT EXISTS woo.price_rule (
    rule_id       bigserial PRIMARY KEY,
    name          text NOT NULL,
    active        boolean NOT NULL DEFAULT false,
    priority      integer NOT NULL DEFAULT 100,     -- lower runs first
    stackable     boolean NOT NULL DEFAULT false,   -- chain stops after a non-stackable rule applies
    -- Targeting (AND-ed; empty/NULL dimension = unconstrained).
    -- Store match: stores[] and store_tiers[] BOTH empty = all stores, else
    -- store matches if in stores[] OR its assigned tier is in store_tiers[].
    stores        text[],
    store_tiers   text[],
    styles        text[],          -- stored UPPER-cased
    brands        text[],
    categories    text[],
    effect_type   text NOT NULL CHECK (effect_type IN
                    ('percent','flat','set_price','price_level','margin_over_cost')),
    effect_value  numeric(12,4),
    price_level_key text,          -- for effect_type='price_level' (msrp|corp1|corp2|corp3|wholesale|employee|base)
    floor_price   numeric(12,4),
    effective_from date,
    effective_until date,
    note          text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS price_rule_active_idx ON woo.price_rule (active, priority);

CREATE TABLE IF NOT EXISTS woo.price_rule_audit (
    id       bigserial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    op       text NOT NULL,
    rule_id  bigint,
    actor    text NOT NULL DEFAULT '',
    row_data jsonb
);

CREATE OR REPLACE FUNCTION woo.audit_price_rule_row() RETURNS trigger AS $fn$
BEGIN
    INSERT INTO woo.price_rule_audit (op, rule_id, actor, row_data)
    VALUES (TG_OP, COALESCE(NEW.rule_id, OLD.rule_id),
            COALESCE(NULLIF(current_setting('logo.actor', true), ''), current_user),
            CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END);
    RETURN COALESCE(NEW, OLD);
END $fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS price_rule_audit ON woo.price_rule;
CREATE TRIGGER price_rule_audit AFTER INSERT OR UPDATE OR DELETE ON woo.price_rule
    FOR EACH ROW EXECUTE FUNCTION woo.audit_price_rule_row();

GRANT SELECT ON woo.price_rule, woo.price_rule_audit TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.price_rule TO etl_writer, logo_admin;
GRANT SELECT, INSERT ON woo.price_rule_audit TO etl_writer, logo_admin;
GRANT USAGE ON SEQUENCE woo.price_rule_rule_id_seq TO etl_writer, logo_admin;
GRANT USAGE ON SEQUENCE woo.price_rule_audit_id_seq TO etl_writer, logo_admin;

-- THE single source of price-rule math. Used by BOTH the hourly transform and
-- the app preview (preview == reality by construction). Returns NULL final_price
-- when no rule applies (caller keeps the base). p_extra_active lets the preview
-- evaluate a draft/inactive rule as-if-active; p_ignore simulates deactivation.
CREATE OR REPLACE FUNCTION woo.eval_price_rules(
    p_store text, p_style text, p_brand text, p_category text,
    p_base numeric, p_levels jsonb, p_cost numeric,
    p_as_of date DEFAULT current_date,
    p_extra_active bigint[] DEFAULT NULL,
    p_ignore bigint[] DEFAULT NULL
) RETURNS TABLE (final_price numeric, applied_rule_ids bigint[])
LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    r record;
    v numeric;
    ids bigint[] := '{}';
BEGIN
    IF p_base IS NULL THEN
        RETURN QUERY SELECT NULL::numeric, ids; RETURN;
    END IF;
    v := p_base;
    FOR r IN
        SELECT * FROM woo.price_rule pr
         WHERE (pr.active OR (p_extra_active IS NOT NULL AND pr.rule_id = ANY(p_extra_active)))
           AND (p_ignore IS NULL OR NOT (pr.rule_id = ANY(p_ignore)))
           AND (pr.effective_from  IS NULL OR p_as_of >= pr.effective_from)
           AND (pr.effective_until IS NULL OR p_as_of <= pr.effective_until)
           AND ( (COALESCE(cardinality(pr.stores), 0) = 0 AND COALESCE(cardinality(pr.store_tiers), 0) = 0)
                 OR p_store = ANY(COALESCE(pr.stores, '{}'))
                 OR EXISTS (SELECT 1 FROM woo.store_pricing_tier spt
                             WHERE spt.fdm4_store = p_store
                               AND spt.tier_name = ANY(COALESCE(pr.store_tiers, '{}'))) )
           AND (COALESCE(cardinality(pr.styles), 0) = 0     OR upper(btrim(p_style)) = ANY(pr.styles))
           AND (COALESCE(cardinality(pr.brands), 0) = 0     OR p_brand = ANY(pr.brands))
           AND (COALESCE(cardinality(pr.categories), 0) = 0 OR p_category = ANY(pr.categories))
         ORDER BY pr.priority, pr.rule_id
    LOOP
        v := CASE r.effect_type
               WHEN 'percent'          THEN v * (1 + r.effect_value / 100.0)
               WHEN 'flat'             THEN v + r.effect_value
               WHEN 'set_price'        THEN r.effect_value
               WHEN 'price_level'      THEN COALESCE((p_levels ->> r.price_level_key)::numeric, v)
               WHEN 'margin_over_cost' THEN CASE WHEN p_cost IS NOT NULL AND p_cost > 0
                                                 THEN p_cost * r.effect_value ELSE v END
             END;
        IF v IS NULL THEN v := p_base; END IF;
        IF r.floor_price IS NOT NULL AND v < r.floor_price THEN v := r.floor_price; END IF;
        IF v < 0 THEN v := 0; END IF;
        ids := ids || r.rule_id;
        EXIT WHEN NOT r.stackable;
    END LOOP;
    IF cardinality(ids) = 0 THEN
        RETURN QUERY SELECT NULL::numeric, ids;
    ELSE
        RETURN QUERY SELECT round(v, 4), ids;
    END IF;
END $fn$;

GRANT EXECUTE ON FUNCTION woo.eval_price_rules(text,text,text,text,numeric,jsonb,numeric,date,bigint[],bigint[])
    TO woo_reader, insights_reader, etl_writer, logo_admin;

COMMIT;
