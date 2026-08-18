-- Append-only audit log for logo.* changes, populated by row triggers so it
-- captures EVERY writer (Logo Admin app, seed, legacy import, manual psql) -
-- not just code paths that remember to log. The logo_admin role can INSERT
-- and SELECT but never UPDATE or DELETE, so history is immutable from the app.
--
-- Actor resolution: writers set the transaction-local GUC logo.actor
-- (set_config('logo.actor', <who>, true)); the trigger falls back to the
-- database role name when unset.
BEGIN;

CREATE TABLE IF NOT EXISTS logo.audit_log (
    id                 bigserial     PRIMARY KEY,
    at                 timestamptz   NOT NULL DEFAULT now(),
    actor              text          NOT NULL DEFAULT '',
    action             text          NOT NULL,
    fdm4_store         text          NOT NULL DEFAULT '',
    product_style      text          NOT NULL DEFAULT '',
    garment_color_code text          NOT NULL DEFAULT '',
    option_row         integer,
    position           integer,
    detail             jsonb
);

CREATE INDEX IF NOT EXISTS logo_audit_store_idx ON logo.audit_log (fdm4_store, id DESC);
CREATE INDEX IF NOT EXISTS logo_audit_at_idx    ON logo.audit_log (at DESC);

-- Immutable from every non-superuser role: INSERT + SELECT only.
GRANT SELECT, INSERT ON logo.audit_log TO logo_admin, etl_writer;
GRANT SELECT ON logo.audit_log TO woo_reader, insights_reader;
GRANT USAGE, SELECT ON SEQUENCE logo.audit_log_id_seq TO logo_admin, etl_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON logo.audit_log FROM logo_admin, etl_writer, woo_reader, insights_reader;

CREATE OR REPLACE FUNCTION logo.audit_row() RETURNS trigger AS $$
DECLARE
    v_actor   text := left(coalesce(nullif(current_setting('logo.actor', true), ''), current_user::text), 100);
    v_old     jsonb := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END;
    v_new     jsonb := CASE WHEN TG_OP IN ('UPDATE', 'INSERT') THEN to_jsonb(NEW) END;
    v_row     jsonb := coalesce(v_new, v_old);
    v_changes jsonb;
    v_verb    text := CASE TG_OP WHEN 'INSERT' THEN 'created' WHEN 'UPDATE' THEN 'updated' ELSE 'deleted' END;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Skip no-op writes (idempotent re-imports re-stamp updated_at/by on
        -- identical data; that is not a change worth a history line).
        IF (v_old - 'updated_at' - 'updated_by') = (v_new - 'updated_at' - 'updated_by') THEN
            RETURN NEW;
        END IF;
        SELECT jsonb_object_agg(o.key, jsonb_build_object('from', o.value, 'to', n.value))
          INTO v_changes
          FROM jsonb_each(v_old) o
          JOIN jsonb_each(v_new) n USING (key)
         WHERE o.value IS DISTINCT FROM n.value
           AND o.key NOT IN ('updated_at', 'updated_by');
    END IF;

    INSERT INTO logo.audit_log (actor, action, fdm4_store, product_style, garment_color_code, option_row, position, detail)
    VALUES (
        v_actor,
        CASE WHEN TG_TABLE_NAME = 'assignment' THEN 'assignment_' WHEN TG_TABLE_NAME = 'color_class' THEN 'color_class_' ELSE 'store_settings_' END || v_verb,
        coalesce(v_row->>'fdm4_store', ''),
        coalesce(v_row->>'product_style', ''),
        coalesce(v_row->>'garment_color_code', ''),
        (v_row->>'option_row')::integer,
        (v_row->>'position')::integer,
        jsonb_strip_nulls(jsonb_build_object(
            'changes', v_changes,
            'old', CASE WHEN TG_OP = 'DELETE' THEN v_old END,
            'new', CASE WHEN TG_OP = 'INSERT' THEN v_new END
        ))
    );
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS logo_assignment_audit ON logo.assignment;
CREATE TRIGGER logo_assignment_audit
    AFTER INSERT OR UPDATE OR DELETE ON logo.assignment
    FOR EACH ROW EXECUTE FUNCTION logo.audit_row();

DROP TRIGGER IF EXISTS logo_store_settings_audit ON logo.store_settings;
CREATE TRIGGER logo_store_settings_audit
    AFTER INSERT OR UPDATE OR DELETE ON logo.store_settings
    FOR EACH ROW EXECUTE FUNCTION logo.audit_row();

COMMIT;
