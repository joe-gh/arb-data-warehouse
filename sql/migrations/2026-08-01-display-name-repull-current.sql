-- Reassert the live per-store repull contract after the two historical
-- 2026-07-29 migrations, whose same-day filename order otherwise leaves the
-- older two-column implementation installed on a blank ordered migration run.
BEGIN;

CREATE TABLE IF NOT EXISTS logo.display_name (
    design_id         text        NOT NULL,
    color_scheme_id   text        NOT NULL,
    name              text        NOT NULL,
    source            text        NOT NULL DEFAULT 'manual',
    locked            boolean     NOT NULL DEFAULT false,
    uses              integer     NOT NULL DEFAULT 0,
    fdm4_description  text,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text,
    fdm4_store        text        NOT NULL DEFAULT '',
    PRIMARY KEY (design_id, color_scheme_id, fdm4_store)
);

CREATE OR REPLACE FUNCTION logo.repull_display_name(
    p_design_id text,
    p_force boolean DEFAULT false
) RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    v_id    text := btrim(p_design_id);
    v_art   text;
    v_cust  text;
    v_name  text;
    v_refs  text[];
    n integer := 0;
BEGIN
    SELECT array_agg(DISTINCT substring(a.fdm4_store from 3)) INTO v_refs
      FROM logo.assignment a
     WHERE btrim(a.design_id) = v_id AND a.active;
    IF v_refs IS NULL THEN RETURN 0; END IF;

    SELECT btrim(cd.cust_number) INTO v_cust
      FROM fdm4.dec_design cd
     WHERE btrim(cd.design_id) = v_id
     LIMIT 1;

    IF v_cust IS NOT NULL AND v_cust = ANY(v_refs) THEN
        SELECT btrim(dp.art_id) INTO v_art
          FROM fdm4.design_pool dp
         WHERE btrim(dp.design_id) = v_id
           AND NULLIF(btrim(dp.art_id), '') <> ''
         ORDER BY dp.design_pool_num
         LIMIT 1;
        IF v_art IS NULL THEN RETURN 0; END IF;
        SELECT r.description INTO v_name
          FROM logo.art_record r
         WHERE r.art_id = v_art
           AND NULLIF(btrim(r.description), '') <> ''
         ORDER BY (btrim(r.customer) = v_cust) DESC,
                  r.art_version NULLS LAST, r.seq
         LIMIT 1;
    ELSIF EXISTS (
        SELECT 1 FROM logo.art_record r
         WHERE r.art_id = v_id AND btrim(r.customer) = ANY(v_refs)
    ) THEN
        SELECT r.description INTO v_name
          FROM logo.art_record r
         WHERE r.art_id = v_id
           AND NULLIF(btrim(r.description), '') <> ''
         ORDER BY (btrim(r.customer) = ANY(v_refs)) DESC,
                  r.art_version NULLS LAST, r.seq
         LIMIT 1;
    ELSE
        RETURN 0;
    END IF;

    IF v_name IS NULL OR btrim(v_name) = '' THEN RETURN 0; END IF;

    WITH schemes AS (
        SELECT DISTINCT upper(btrim(a.color_scheme_id)) AS scheme
          FROM logo.assignment a
         WHERE btrim(a.design_id) = v_id
           AND NULLIF(btrim(a.color_scheme_id), '') IS NOT NULL
        UNION
        SELECT color_scheme_id FROM logo.display_name
         WHERE design_id = v_id AND fdm4_store = ''
    )
    INSERT INTO logo.display_name (
        design_id, color_scheme_id, fdm4_store, name, source, locked,
        uses, fdm4_description, updated_by
    )
    SELECT v_id, s.scheme, '', btrim(v_name), 'art_record', false,
           COALESCE((
               SELECT uses FROM logo.display_name d
                WHERE d.design_id = v_id
                  AND d.color_scheme_id = s.scheme
                  AND d.fdm4_store = ''
           ), 0),
           btrim(v_name),
           COALESCE(NULLIF(current_setting('logo.actor', true), ''), 'repull')
      FROM schemes s
    ON CONFLICT (design_id, color_scheme_id, fdm4_store) DO UPDATE
       SET name = EXCLUDED.name,
           source = EXCLUDED.source,
           fdm4_description = EXCLUDED.fdm4_description,
           updated_at = now(),
           updated_by = EXCLUDED.updated_by
     WHERE (NOT logo.display_name.locked OR p_force)
       AND logo.display_name.name IS DISTINCT FROM EXCLUDED.name;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

REVOKE ALL ON FUNCTION logo.repull_display_name(text, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION logo.repull_display_name(text, boolean) TO logo_admin;

COMMIT;
