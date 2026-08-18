-- Resolve logo display names from the FDM4 "art records" export, keyed by ART id
-- (the logo) with the design's own customer preferred.
--
-- Background (confirmed with Arborwear - Josie/Melissa): in FDM4 the ART id
-- identifies the logo and the DESIGN id is that logo at a placement (a design's
-- dec_design description is "<art>-<placement>", e.g. 271 = "166-LC"). Art ids and
-- design ids are both shared across customers, and their numeric namespaces
-- collide. The art was originally made for its FIRST/primary customer, so the
-- authoritative name for a logo is that customer's description of the art.
--
-- The original repull matched design_pool by `art_id = v_id OR design_id = v_id`,
-- which - once logo.assignment.design_id was corrected off the seed's art-id
-- default - started pulling OTHER logos' art descriptions (Independence showed
-- "Kendall"/"SavATree"). design_pool descriptions are also sparse (many arts have
-- none), so blanks fell back to bare codes.
--
-- Fix: name each logo from logo.art_record (a curated FDM4 "art customers" export:
-- art_id -> description + the customer it was made for). For a design, resolve its
-- real art id (design_pool.design_id -> art_id), then take the art_record row for
-- this design's own customer if present, else the primary (first) row. This gives
-- ~99.9% coverage with clean, real names; shared/family arts (Davey, SavATree, ISA,
-- Lewis, DiGeronimo ...) correctly resolve to the parent account's name.
--
-- DATA LOAD (not in this migration - refresh whenever Arborwear re-exports):
--   the art_records export (xlsx) is flattened to a TSV with columns
--   art_id, art_version, art_full, status, description, method, location, notes,
--   created_by, customer  and loaded:
--     TRUNCATE logo.art_record;
--     \copy logo.art_record (art_id,art_version,art_full,status,description,method,
--            location,notes,created_by,customer)
--       FROM '<export>.tsv' WITH (FORMAT csv, DELIMITER E'\t', HEADER true);
-- After (re)loading, re-pull names:
--     SET logo.actor='display-name-artrecord';
--     DELETE FROM logo.display_name WHERE NOT locked AND source <> 'manual';
--     SELECT sum(logo.repull_display_name(design_id, false))
--       FROM (SELECT DISTINCT btrim(design_id) design_id
--               FROM logo.assignment WHERE active AND nullif(btrim(design_id),'')<>'') d;

BEGIN;

CREATE TABLE IF NOT EXISTS logo.art_record (
    seq         bigserial,
    art_id      text NOT NULL,
    art_version text,
    art_full    text,
    status      text,
    description text,
    method      text,
    location    text,
    notes       text,
    created_by  text,
    customer    text
);
CREATE INDEX IF NOT EXISTS idx_artrec_art      ON logo.art_record (art_id);
CREATE INDEX IF NOT EXISTS idx_artrec_art_cust ON logo.art_record (art_id, customer);
GRANT SELECT ON logo.art_record TO logo_admin;

DROP FUNCTION IF EXISTS logo.repull_display_name(text, boolean);

CREATE FUNCTION logo.repull_display_name(p_design_id text, p_force boolean DEFAULT false)
RETURNS integer AS $func$
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
    FROM logo.assignment a WHERE btrim(a.design_id) = v_id AND a.active;
    IF v_refs IS NULL THEN RETURN 0; END IF;

    -- The number can be a real DESIGN id or a stale ART id left by the legacy seed
    -- (assignment.design_id originally held art ids, and both numeric namespaces
    -- collide across customers). Arbitrate by OWNERSHIP: the design interpretation
    -- counts only when a referencing store owns the design; otherwise, if a
    -- referencing store owns the ART of this number in the curated art records,
    -- the number is a stale art id and the art's name wins. A foreign design with
    -- no art claim gets no name at all (the read path then falls back to the
    -- store's own logo code, never another customer's name). Example: Townsend's
    -- BTX carries stale art id 1728 ("Everett"), which collides with customer
    -- 040794's design 1728 (art 231, "Lewis Hat") - ownership arbitration names
    -- it Everett.
    SELECT btrim(cd.cust_number) INTO v_cust
    FROM fdm4.dec_design cd WHERE btrim(cd.design_id) = v_id LIMIT 1;

    IF v_cust IS NOT NULL AND v_cust = ANY(v_refs) THEN
        -- DESIGN interpretation: resolve the design's art, name from art_record.
        SELECT btrim(dp.art_id) INTO v_art
        FROM fdm4.design_pool dp
        WHERE btrim(dp.design_id) = v_id AND nullif(btrim(dp.art_id), '') <> ''
        ORDER BY dp.design_pool_num LIMIT 1;
        IF v_art IS NULL THEN RETURN 0; END IF;
        SELECT r.description INTO v_name
        FROM logo.art_record r
        WHERE r.art_id = v_art AND nullif(btrim(r.description), '') <> ''
        ORDER BY (btrim(r.customer) = v_cust) DESC, r.art_version NULLS LAST, r.seq
        LIMIT 1;
    ELSIF EXISTS ( SELECT 1 FROM logo.art_record r
                    WHERE r.art_id = v_id AND btrim(r.customer) = ANY(v_refs) ) THEN
        -- ART interpretation: the number IS the art; name from the owning store's record.
        SELECT r.description INTO v_name
        FROM logo.art_record r
        WHERE r.art_id = v_id AND nullif(btrim(r.description), '') <> ''
        ORDER BY (btrim(r.customer) = ANY(v_refs)) DESC, r.art_version NULLS LAST, r.seq
        LIMIT 1;
    ELSE
        RETURN 0;   -- foreign design, no art claim: no name (code fallback)
    END IF;

    IF v_name IS NULL OR btrim(v_name) = '' THEN RETURN 0; END IF;

    WITH schemes AS (
        SELECT DISTINCT upper(btrim(a.color_scheme_id)) AS scheme
        FROM logo.assignment a
        WHERE btrim(a.design_id) = v_id AND nullif(btrim(a.color_scheme_id), '') IS NOT NULL
        UNION
        SELECT color_scheme_id FROM logo.display_name WHERE design_id = v_id
    )
    INSERT INTO logo.display_name (design_id, color_scheme_id, name, source, locked, uses, fdm4_description, updated_by)
    SELECT v_id, s.scheme, btrim(v_name), 'art_record', false,
           coalesce((SELECT uses FROM logo.display_name d WHERE d.design_id = v_id AND d.color_scheme_id = s.scheme), 0),
           btrim(v_name), coalesce(nullif(current_setting('logo.actor', true), ''), 'repull')
    FROM schemes s
    ON CONFLICT (design_id, color_scheme_id) DO UPDATE
       SET name = EXCLUDED.name, source = EXCLUDED.source,
           fdm4_description = EXCLUDED.fdm4_description, updated_at = now(),
           updated_by = EXCLUDED.updated_by
       WHERE (NOT logo.display_name.locked OR p_force)
         AND logo.display_name.name IS DISTINCT FROM EXCLUDED.name;

    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$func$ LANGUAGE plpgsql;

COMMIT;
