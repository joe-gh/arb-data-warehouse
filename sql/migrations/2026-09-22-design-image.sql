-- Per-store logo image for one FDM4 design + color scheme (Maria, 2026-09-21).
-- Same key shape as logo.display_name but never a global row: a Lewis fix
-- must never land on Davey, which shares design ids upstream. Setting a row
-- also rewrites image_url on the store's matching logo.assignment rows (see
-- mutations.set_design_image); this table is the default for rows added
-- later and the read-side fallback for rows with no image of their own.
BEGIN;

CREATE TABLE IF NOT EXISTS logo.design_image (
    design_id        text        NOT NULL CHECK (btrim(design_id) <> ''),
    color_scheme_id  text        NOT NULL CHECK (btrim(color_scheme_id) <> ''),
    fdm4_store       text        NOT NULL CHECK (btrim(fdm4_store) <> ''),
    image_url        text        NOT NULL CHECK (btrim(image_url) <> ''),
    source           text        NOT NULL CHECK (source IN ('art', 'store_row', 'upload', 'link')),
    locked           boolean     NOT NULL DEFAULT true,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text,
    PRIMARY KEY (design_id, color_scheme_id, fdm4_store)
);

COMMENT ON TABLE logo.design_image IS
    'Operator-set storefront image per (design, color scheme, store). Wins over FDM4 art; rows carry a materialized copy in logo.assignment.image_url.';

CREATE OR REPLACE FUNCTION logo.audit_design_image_row() RETURNS trigger AS $$
DECLARE
    v_actor   text := left(coalesce(nullif(current_setting('logo.actor', true), ''), current_user::text), 100);
    v_old     jsonb := CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END;
    v_new     jsonb := CASE WHEN TG_OP IN ('UPDATE','INSERT') THEN to_jsonb(NEW) END;
    v_row     jsonb := coalesce(v_new, v_old);
    v_changes jsonb;
    v_verb    text := CASE TG_OP WHEN 'INSERT' THEN 'created' WHEN 'UPDATE' THEN 'updated' ELSE 'deleted' END;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF (v_old - 'updated_at' - 'updated_by') = (v_new - 'updated_at' - 'updated_by') THEN RETURN NEW; END IF;
        SELECT jsonb_object_agg(o.key, jsonb_build_object('from',o.value,'to',n.value))
          INTO v_changes FROM jsonb_each(v_old) o JOIN jsonb_each(v_new) n USING(key)
         WHERE o.value IS DISTINCT FROM n.value AND o.key NOT IN ('updated_at','updated_by');
    END IF;
    INSERT INTO logo.audit_log (actor, action, fdm4_store, product_style, garment_color_code, option_row, position, detail)
    VALUES (v_actor, 'logo_image_'||v_verb, coalesce(v_row->>'fdm4_store',''), coalesce(v_row->>'design_id',''), coalesce(v_row->>'color_scheme_id',''), NULL, NULL,
        jsonb_strip_nulls(jsonb_build_object('changes', v_changes,
            'old', CASE WHEN TG_OP='DELETE' THEN v_old END,
            'new', CASE WHEN TG_OP='INSERT' THEN v_new END)));
    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS logo_design_image_audit ON logo.design_image;
CREATE TRIGGER logo_design_image_audit
    AFTER INSERT OR UPDATE OR DELETE ON logo.design_image
    FOR EACH ROW EXECUTE FUNCTION logo.audit_design_image_row();

REVOKE EXECUTE ON FUNCTION logo.audit_design_image_row() FROM PUBLIC;

-- /feed/logos and the WordPress reconcile fall back to this image for rows
-- with no image of their own; re-stamp those rows so caught-up consumers see
-- the change (same mechanism as display_name_feed_bump).
CREATE OR REPLACE FUNCTION logo.design_image_feed_bump() RETURNS trigger AS $fn$
DECLARE
    r logo.design_image%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    UPDATE logo.assignment a
       SET updated_at = a.updated_at
     WHERE a.fdm4_store = r.fdm4_store
       AND a.design_id IN (r.design_id, btrim(r.design_id))
       AND upper(btrim(a.color_scheme_id)) = upper(btrim(r.color_scheme_id))
       AND COALESCE(btrim(a.image_url), '') = '';
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS design_image_feed_bump ON logo.design_image;
CREATE TRIGGER design_image_feed_bump
    AFTER INSERT OR UPDATE OF image_url OR DELETE ON logo.design_image
    FOR EACH ROW EXECUTE FUNCTION logo.design_image_feed_bump();

REVOKE EXECUTE ON FUNCTION logo.design_image_feed_bump() FROM PUBLIC;

GRANT SELECT ON logo.design_image TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.design_image TO logo_admin, etl_writer;

COMMIT;
