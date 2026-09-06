-- Resolve a design's artwork instead of guessing one (audit F-05, feed half).
--
-- An FDM4 design can carry several decorations, each with its own artwork and
-- its own placement (design 4706 "ARBGUARD- LC BLS" = art 203 on the left
-- chest plus art 55 on the bicep left sleeve). /feed/logos picked the artwork
-- with "SELECT art_id FROM fdm4.design_pool WHERE design_id = ... LIMIT 1" -
-- no ordering, no placement or logo-code match - and, when the design had no
-- pool row at all, emitted the DESIGN id in the art_id field. Design ids and
-- art ids share one number space across customers, so that could hand a
-- consumer another company's artwork number.
--
-- logo.art_pool() is the resolver, with the same precedence the WordPress
-- logo reconcile uses (arb-product-sync/includes/class-arb-logo-reconcile.php):
--   1. the pool row whose FDM4 placement code maps to the assignment's
--      placement name (loc_map below, FDM4's own code -> name mapping);
--   2. else the pool row whose art files carry the assignment's logo code
--      (fdm4.cust_art_file.source_path is "<CODE>_<scheme>...");
--   3. else every pool row, unmatched - the caller may use it only when the
--      design has exactly one artwork.
-- Ordering inside a tier is by design_pool_num, numerically (the reconcile
-- orders that column as text).
--
-- The caller decides: an art id is published when a tier-1 or tier-2 match
-- exists, or when the whole pool holds one distinct artwork. Anything else is
-- unresolved and the feed says so (art_unresolved) rather than guessing.
--
-- Shape notes:
--   * plpgsql, not a single SQL statement, so the placement tier can return
--     without ever reading fdm4.cust_art_file. The fdm4 tables are dropped and
--     recreated by every load and carry no indexes, so each tier is a
--     sequential scan; skipping tiers is the only bound available.
--   * every returning statement is row-bounded and deterministically ordered.
--   * plpgsql bodies are not parsed at creation time, so this file can be
--     applied to a database whose fdm4 tables are not loaded yet.
BEGIN;

CREATE OR REPLACE FUNCTION logo.art_pool(
    p_design_id text,
    p_location  text,
    p_logo_code text
) RETURNS TABLE (
    art_id     text,
    art_num    text,
    matched    boolean,
    match_rank smallint,
    num_sort   numeric
) AS $fn$
#variable_conflict use_column
DECLARE
    v_design   text := btrim(COALESCE(p_design_id, ''));
    v_location text := upper(btrim(COALESCE(p_location, '')));
    v_code     text := upper(btrim(COALESCE(p_logo_code, '')));
    v_arts     integer;
BEGIN
    IF v_design = '' THEN
        RETURN;
    END IF;

    -- Tier 1: the artwork FDM4 puts at this placement.
    IF v_location <> '' THEN
        RETURN QUERY
        WITH pool AS (
            SELECT btrim(dp.art_id) AS art_num,
                   btrim(dp.art_id)
                   || CASE
                          WHEN NULLIF(btrim(dp.art_version_id), '') IS NOT NULL
                          THEN '.' || btrim(dp.art_version_id)
                          ELSE ''
                      END AS art_id,
                   upper(btrim(dp.location_id)) AS lcode,
                   CASE
                       WHEN btrim(dp.design_pool_num) ~ '^[0-9]+$'
                       THEN btrim(dp.design_pool_num)::numeric
                   END AS num_sort
              FROM fdm4.design_pool dp
             WHERE btrim(dp.design_id) = v_design
               AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
        ), loc_map (lcode, name) AS (
            VALUES
                ('LC', 'LEFT CHEST'), ('LCS', 'LEFT CHEST SWITCH OK'),
                ('LCAP', 'LEFT CHEST ABOVE POCKET'), ('RC', 'RIGHT CHEST'),
                ('RCS', 'RIGHT CHEST SWITCH OK'),
                ('RCAP', 'RIGHT CHEST ABOVE POCKET'),
                ('BLS', 'BICEP LEFT SLEEVE'), ('BRS', 'BICEP RIGHT SLEEVE'),
                ('FB', 'FULL BACK'), ('RSS', 'RIGHT SHORT SLEEVE'),
                ('LSS', 'LEFT SHORT SLEEVE'), ('BHCF', 'BRIM HAT - CENTER FRONT'),
                ('CC', 'CENTER CHEST'), ('CFB', 'CENTER FULL BACK'),
                ('OP', 'ON THE POCKET'), ('NCCF', 'KNIT CAP - CENTER FRONT'),
                ('KCCF', 'KNIT CAP - CENTER FRONT'),
                ('BHFL', 'BRIM HAT - FRONT LEFT'), ('DBF', 'DUFFLE BAG FRONT'),
                ('FF', 'FULL CENTER FRONT'), ('BPF', 'BACKPACK BAG FRONT'),
                ('DBB', 'DUFFLE BAG BACK'), ('CBN', 'CENTER BACK NECK'),
                ('PROOF', '*SEE PROOF*'), ('BHFR', 'BRIM HAT - FRONT RIGHT'),
                ('RLS', 'RIGHT LONG SLEEVE'), ('LLS', 'LEFT LONG SLEEVE'),
                ('BHCB', 'BRIM HAT - CENTER BACK'), ('TBF', 'TOTE BAG FRONT'),
                ('FLT', 'FRONT LEFT THIGH'),
                ('RT', 'RIGHT THIGH (OK SWITCH LEFT)'),
                ('FFCB', 'BRIM FLEX FIT HAT - CENTER BACK'),
                ('HTBHCF', 'HT BRIM HAT CENTER FRONT'),
                ('LT', 'LEFT THIGH (OK SWITCH RIGHT)'),
                ('BRC', 'BACK RIGHT CALF'),
                ('HVSAFB', 'HIVIZ REFLECTIVE FULL BACK'),
                ('BFC', 'BLANKET FRONT CORNER'), ('CBF', 'COOLER BAG FRONT')
        )
        SELECT p.art_id, p.art_num, true, 0::smallint, p.num_sort
          FROM pool p
          JOIN loc_map m ON m.lcode = p.lcode
         WHERE m.name = v_location
         ORDER BY p.num_sort NULLS LAST, p.art_id
         LIMIT 200;
        IF FOUND THEN
            RETURN;
        END IF;
    END IF;

    SELECT count(DISTINCT btrim(dp.art_id))
      INTO v_arts
      FROM fdm4.design_pool dp
     WHERE btrim(dp.design_id) = v_design
       AND NULLIF(btrim(dp.art_id), '') IS NOT NULL;

    IF v_arts = 0 THEN
        RETURN;
    END IF;

    -- One artwork, or no logo code to match on: hand back the pool unmatched
    -- and let the caller decide. Reading fdm4.cust_art_file here would cost a
    -- full scan and could not change the answer.
    IF v_arts = 1 OR v_code = '' THEN
        RETURN QUERY
        WITH pool AS (
            SELECT btrim(dp.art_id) AS art_num,
                   btrim(dp.art_id)
                   || CASE
                          WHEN NULLIF(btrim(dp.art_version_id), '') IS NOT NULL
                          THEN '.' || btrim(dp.art_version_id)
                          ELSE ''
                      END AS art_id,
                   CASE
                       WHEN btrim(dp.design_pool_num) ~ '^[0-9]+$'
                       THEN btrim(dp.design_pool_num)::numeric
                   END AS num_sort
              FROM fdm4.design_pool dp
             WHERE btrim(dp.design_id) = v_design
               AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
        )
        SELECT p.art_id, p.art_num, false, 2::smallint, p.num_sort
          FROM pool p
         ORDER BY p.num_sort NULLS LAST, p.art_id
         LIMIT 200;
        RETURN;
    END IF;

    -- Tier 2: the artwork whose art files carry this logo code, else the whole
    -- pool unmatched.
    RETURN QUERY
    WITH pool AS (
        SELECT btrim(dp.art_id) AS art_num,
               btrim(dp.art_id)
               || CASE
                      WHEN NULLIF(btrim(dp.art_version_id), '') IS NOT NULL
                      THEN '.' || btrim(dp.art_version_id)
                      ELSE ''
                  END AS art_id,
               CASE
                   WHEN btrim(dp.design_pool_num) ~ '^[0-9]+$'
                   THEN btrim(dp.design_pool_num)::numeric
               END AS num_sort
          FROM fdm4.design_pool dp
         WHERE btrim(dp.design_id) = v_design
           AND NULLIF(btrim(dp.art_id), '') IS NOT NULL
    ), art_code AS (
        SELECT DISTINCT btrim(f.art_id) AS art_num
          FROM fdm4.cust_art_file f
         WHERE NULLIF(btrim(f.source_path), '') IS NOT NULL
           AND upper(split_part(btrim(f.source_path), '_', 1)) = v_code
    )
    SELECT p.art_id, p.art_num,
           (c.art_num IS NOT NULL),
           CASE WHEN c.art_num IS NOT NULL THEN 1 ELSE 2 END::smallint,
           p.num_sort
      FROM pool p
      LEFT JOIN art_code c ON c.art_num = p.art_num
     ORDER BY (c.art_num IS NULL), p.num_sort NULLS LAST, p.art_id
     LIMIT 200;
END;
$fn$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION logo.art_pool(text, text, text) IS
    'Candidate FDM4 artworks for one logo assignment, ranked by placement then logo code (match_rank 0/1) and finally unmatched (2). Callers publish an art id only on a match or a single-artwork design.';

REVOKE EXECUTE ON FUNCTION logo.art_pool(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION logo.art_pool(text, text, text)
    TO logo_admin, woo_reader, insights_reader, etl_writer;

COMMIT;
