-- Reconcile repository DDL with the 2026-08-01 live logo/woo schema snapshot.
-- These objects predated a canonical CREATE statement in the repository.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'logo.assignment'::regclass
           AND conname = 'assignment_option_row_check'
    ) THEN
        ALTER TABLE logo.assignment
            ADD CONSTRAINT assignment_option_row_check CHECK (option_row >= 1);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS logo.default_cost (
    logo_code       text          NOT NULL,
    color_scheme_id text          NOT NULL,
    cost            numeric(12,2) NOT NULL,
    source          text          NOT NULL DEFAULT 'vn-reference',
    locked          boolean       NOT NULL DEFAULT false,
    updated_by      text          NOT NULL DEFAULT 'vn-import-20260731',
    updated_at      timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (logo_code, color_scheme_id)
);

CREATE TABLE IF NOT EXISTS logo.design_ipc (
    seq             bigserial,
    logo_code       text NOT NULL,
    color_scheme_id text NOT NULL,
    location        text NOT NULL DEFAULT '',
    design_id       text NOT NULL,
    art_id          text NOT NULL DEFAULT '',
    synthetic_ipc   text NOT NULL,
    source          text NOT NULL DEFAULT 'fdm4-design-map-20260731',
    PRIMARY KEY (logo_code, color_scheme_id, location)
);

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

COMMENT ON TABLE logo.display_name IS
    'Customer-facing logo names per (design_id,color_scheme_id). Seeded from FDM4 design_pool + filename parse; editable in Warehouse Ops; re-pullable from FDM4 via logo.repull_display_name().';

CREATE OR REPLACE FUNCTION logo.audit_display_name_row() RETURNS trigger AS $$
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
    VALUES (v_actor, 'logo_name_'||v_verb, '', coalesce(v_row->>'design_id',''), coalesce(v_row->>'color_scheme_id',''), NULL, NULL,
        jsonb_strip_nulls(jsonb_build_object('changes', v_changes,
            'old', CASE WHEN TG_OP='DELETE' THEN v_old END,
            'new', CASE WHEN TG_OP='INSERT' THEN v_new END)));
    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS logo_display_name_audit ON logo.display_name;
CREATE TRIGGER logo_display_name_audit
    AFTER INSERT OR UPDATE OR DELETE ON logo.display_name
    FOR EACH ROW EXECUTE FUNCTION logo.audit_display_name_row();

GRANT SELECT ON logo.default_cost, logo.design_ipc TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON logo.default_cost, logo.design_ipc TO logo_admin, etl_writer;
GRANT SELECT ON logo.display_name TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON logo.display_name TO logo_admin, etl_writer;

COMMIT;
