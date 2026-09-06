-- Price rules: an exclusion list must not suppress a rule for rows whose
-- attribute is unknown.
--
-- `NOT (p_brand = ANY(pr.excl_brands))` is NULL when p_brand is NULL, so the
-- whole WHERE clause was NULL and the rule was skipped for every row with an
-- unresolved brand or category - the opposite of "exclude brand X". A NULL
-- attribute matches no exclusion entry, so it must read as "not excluded":
-- COALESCE(..., false) around each membership test. Applied to stores and
-- styles too, defence in depth (p_store is always supplied today, and p_style
-- goes through upper(btrim(...)) which is NULL for a NULL style).
--
-- Signature, ordering, stacking, basis, rounding, ceilings and floors are
-- unchanged; canonical body copied from
-- sql/migrations/2026-08-21-price-rule-exclusions-rounding.sql.
BEGIN;

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
    resolved numeric;
    start_from numeric;
    msrp numeric;
    ids bigint[] := '{}';
BEGIN
    IF p_base IS NULL THEN
        RETURN QUERY SELECT NULL::numeric, ids; RETURN;
    END IF;
    v := p_base;
    msrp := (p_levels ->> 'msrp')::numeric;
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
           AND (COALESCE(cardinality(pr.excl_stores), 0) = 0     OR NOT COALESCE(p_store = ANY(pr.excl_stores), false))
           AND (COALESCE(cardinality(pr.excl_styles), 0) = 0     OR NOT COALESCE(upper(btrim(p_style)) = ANY(pr.excl_styles), false))
           AND (COALESCE(cardinality(pr.excl_brands), 0) = 0     OR NOT COALESCE(p_brand = ANY(pr.excl_brands), false))
           AND (COALESCE(cardinality(pr.excl_categories), 0) = 0 OR NOT COALESCE(p_category = ANY(pr.excl_categories), false))
         ORDER BY pr.priority, pr.rule_id
    LOOP
        -- Percent and flat can run off a chosen price level instead of the
        -- running value; a row without that level skips the rule entirely.
        start_from := CASE
            WHEN r.basis IS NULL OR r.basis = 'current' THEN v
            ELSE (p_levels ->> r.basis)::numeric
        END;
        resolved := CASE r.effect_type
               WHEN 'percent'          THEN CASE WHEN r.effect_value IS NOT NULL AND start_from IS NOT NULL
                                                 THEN start_from * (1 + r.effect_value / 100.0) END
               WHEN 'flat'             THEN CASE WHEN r.effect_value IS NOT NULL AND start_from IS NOT NULL
                                                 THEN start_from + r.effect_value END
               WHEN 'set_price'        THEN r.effect_value
               WHEN 'price_level'      THEN (p_levels ->> r.price_level_key)::numeric
               WHEN 'margin_over_cost' THEN CASE WHEN r.effect_value IS NOT NULL
                                                  AND p_cost IS NOT NULL AND p_cost > 0
                                                 THEN p_cost * r.effect_value END
             END;
        CONTINUE WHEN resolved IS NULL;
        v := resolved;
        -- Cosmetic ending first, then the hard business bounds.
        v := CASE COALESCE(r.rounding, 'none')
               WHEN '99' THEN GREATEST(round(v, 0) - 0.01, 0)
               WHEN '95' THEN GREATEST(round(v, 0) - 0.05, 0)
               WHEN '00' THEN round(v, 0)
               ELSE v
             END;
        IF r.ceiling_price IS NOT NULL AND v > r.ceiling_price THEN v := r.ceiling_price; END IF;
        IF r.cap_at_msrp AND msrp IS NOT NULL AND v > msrp THEN v := msrp; END IF;
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

COMMIT;
