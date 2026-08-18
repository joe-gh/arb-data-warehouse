-- Price-rule hardening (post-audit 2026-07-31). Four changes:
--
--  1) store_product_state.base_price = the PRE-RULE price. The `price` column
--     stays the rule-applied projection (it is what the Woo sync engine reads
--     via fetch_desired), so it cannot change meaning; the app's rule preview
--     switches its base from `price` to `base_price` so previewing while a
--     rule is already baked in can no longer double-apply that rule.
--     Backfill base_price = price is exact today (zero active rules) and is
--     self-healing regardless: the next hourly refresh rewrites both columns.
--
--  2) price_rule.last_previewed_at = server-side preview gate. The preview
--     endpoint stamps it (only when the rule was not edited mid-preview);
--     material edits clear it; the new toggle endpoint refuses to activate a
--     rule whose stamp is missing. Previously the "preview required before
--     activating" promise was enforced only in the browser.
--
--  3) eval_price_rules: a rule that matches targeting but whose effect cannot
--     resolve a value for the row (price_level key absent from price_levels,
--     margin_over_cost with NULL/<=0 cost, NULL effect_value) is now SKIPPED
--     entirely -- it no longer consumes the first-match slot, no longer resets
--     an already-stacked value back to base, and no longer rewrites the price
--     representation (pure hash churn) without changing the value.
--
--  4) audit trigger: preview stamps (updates that change ONLY
--     last_previewed_at) are not audited; every other write still is.
--
-- After applying this migration, re-apply sql/woo_transform.sql -- the
-- refresh_product_state() there now writes base_price.
BEGIN;

ALTER TABLE woo.store_product_state ADD COLUMN IF NOT EXISTS base_price numeric(12,4);
UPDATE woo.store_product_state SET base_price = price WHERE base_price IS NULL;

ALTER TABLE woo.price_rule ADD COLUMN IF NOT EXISTS last_previewed_at timestamptz;

-- Preview stamps are routine reads, not policy changes: keep them out of the
-- audit trail so it stays a signal, not a log of every preview click.
CREATE OR REPLACE FUNCTION woo.audit_price_rule_row() RETURNS trigger AS $fn$
BEGIN
    IF TG_OP = 'UPDATE'
       AND to_jsonb(NEW) - 'last_previewed_at' = to_jsonb(OLD) - 'last_previewed_at' THEN
        RETURN NEW;
    END IF;
    INSERT INTO woo.price_rule_audit (op, rule_id, actor, row_data)
    VALUES (TG_OP, COALESCE(NEW.rule_id, OLD.rule_id),
            COALESCE(NULLIF(current_setting('logo.actor', true), ''), current_user),
            CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END);
    RETURN COALESCE(NEW, OLD);
END $fn$ LANGUAGE plpgsql;

-- THE single source of price-rule math (transform + preview). Identical to the
-- 2026-07-31-price-rules.sql body except: an unresolvable effect skips the
-- rule (CONTINUE) instead of pass-through-and-claim.
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
        -- A matching rule whose effect cannot produce a value for THIS row is
        -- skipped outright: no slot consumed, no price touched.
        resolved := CASE r.effect_type
               WHEN 'percent'          THEN CASE WHEN r.effect_value IS NOT NULL
                                                 THEN v * (1 + r.effect_value / 100.0) END
               WHEN 'flat'             THEN CASE WHEN r.effect_value IS NOT NULL
                                                 THEN v + r.effect_value END
               WHEN 'set_price'        THEN r.effect_value
               WHEN 'price_level'      THEN (p_levels ->> r.price_level_key)::numeric
               WHEN 'margin_over_cost' THEN CASE WHEN r.effect_value IS NOT NULL
                                                  AND p_cost IS NOT NULL AND p_cost > 0
                                                 THEN p_cost * r.effect_value END
             END;
        CONTINUE WHEN resolved IS NULL;
        v := resolved;
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
