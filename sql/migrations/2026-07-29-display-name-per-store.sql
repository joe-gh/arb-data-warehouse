-- Per-store logo display names. logo.display_name gains fdm4_store:
--   ''            = the global default row (all pre-existing rows)
--   'S_xxxxxx'    = a store-specific override that beats the global row for
--                   that store (WP design_art() resolves store-first).
-- The Logo Names tab lists only the (design, scheme) pairs the selected
-- store's active assignments use, and edits write store-scoped rows.
-- logo.repull_display_name keeps its (text, boolean) signature (contract-
-- asserted) and now refreshes ONLY the global row - hand-set store rows are
-- never touched by a re-pull.
BEGIN;

ALTER TABLE logo.display_name
    ADD COLUMN IF NOT EXISTS fdm4_store text NOT NULL DEFAULT '';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
         WHERE c.conname = 'display_name_pkey'
           AND c.conrelid = 'logo.display_name'::regclass
           AND array_length(c.conkey, 1) = 3
    ) THEN
        ALTER TABLE logo.display_name DROP CONSTRAINT display_name_pkey;
        ALTER TABLE logo.display_name
            ADD CONSTRAINT display_name_pkey
            PRIMARY KEY (design_id, color_scheme_id, fdm4_store);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION logo.repull_display_name(p_design_id text, p_force boolean DEFAULT false)
 RETURNS integer
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_id    text := btrim(p_design_id);
    v_art   text;
    v_cust  text;
    v_name  text;
    v_refs  text[];
    n integer := 0;
BEGIN
    -- Stores that actually reference this number in active assignments.
    SELECT array_agg(DISTINCT substring(a.fdm4_store from 3)) INTO v_refs
    FROM logo.assignment a WHERE btrim(a.design_id)=v_id AND a.active;
    IF v_refs IS NULL THEN RETURN 0; END IF;

    -- The number can be a real DESIGN id or a stale ART id (legacy seed): arbitrate
    -- by ownership. Design interpretation only counts when a referencing store owns
    -- the design; otherwise, if a referencing store owns the ART of this number in
    -- the curated art records, the number is a stale art id and the art name wins.
    SELECT btrim(cd.cust_number) INTO v_cust FROM fdm4.dec_design cd WHERE btrim(cd.design_id)=v_id LIMIT 1;

    IF v_cust IS NOT NULL AND v_cust = ANY(v_refs) THEN
        -- DESIGN interpretation: resolve the design's art, name from art_record.
        SELECT btrim(dp.art_id) INTO v_art FROM fdm4.design_pool dp
         WHERE btrim(dp.design_id)=v_id AND nullif(btrim(dp.art_id),'')<>'' ORDER BY dp.design_pool_num LIMIT 1;
        IF v_art IS NULL THEN RETURN 0; END IF;
        SELECT r.description INTO v_name FROM logo.art_record r
         WHERE r.art_id=v_art AND nullif(btrim(r.description),'')<>''
         ORDER BY (btrim(r.customer)=v_cust) DESC, r.art_version NULLS LAST, r.seq LIMIT 1;
    ELSIF EXISTS (SELECT 1 FROM logo.art_record r WHERE r.art_id=v_id AND btrim(r.customer)=ANY(v_refs)) THEN
        -- ART interpretation: the number IS the art; name from the owning store's record.
        SELECT r.description INTO v_name FROM logo.art_record r
         WHERE r.art_id=v_id AND nullif(btrim(r.description),'')<>''
         ORDER BY (btrim(r.customer)=ANY(v_refs)) DESC, r.art_version NULLS LAST, r.seq LIMIT 1;
    ELSE
        RETURN 0;   -- foreign design with no art claim: no name (falls back to code)
    END IF;

    IF v_name IS NULL OR btrim(v_name)='' THEN RETURN 0; END IF;

    WITH schemes AS (
        SELECT DISTINCT upper(btrim(a.color_scheme_id)) AS scheme FROM logo.assignment a
         WHERE btrim(a.design_id)=v_id AND nullif(btrim(a.color_scheme_id),'') IS NOT NULL
        UNION SELECT color_scheme_id FROM logo.display_name WHERE design_id=v_id AND fdm4_store=''
    )
    INSERT INTO logo.display_name (design_id, color_scheme_id, fdm4_store, name, source, locked, uses, fdm4_description, updated_by)
    SELECT v_id, s.scheme, '', btrim(v_name), 'art_record', false,
           coalesce((SELECT uses FROM logo.display_name d WHERE d.design_id=v_id AND d.color_scheme_id=s.scheme AND d.fdm4_store=''),0),
           btrim(v_name), coalesce(nullif(current_setting('logo.actor',true),''),'repull')
    FROM schemes s
    ON CONFLICT (design_id,color_scheme_id,fdm4_store) DO UPDATE
       SET name=EXCLUDED.name, source=EXCLUDED.source, fdm4_description=EXCLUDED.fdm4_description,
           updated_at=now(), updated_by=EXCLUDED.updated_by
       WHERE (NOT logo.display_name.locked OR p_force) AND logo.display_name.name IS DISTINCT FROM EXCLUDED.name;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$function$;

COMMIT;
