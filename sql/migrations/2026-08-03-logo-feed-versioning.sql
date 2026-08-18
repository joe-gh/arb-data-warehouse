-- Ask E (Emblem): versioning for logo.assignment so /feed/logos can keyset-
-- page changes with tombstones, mirroring /feed/products.
--
-- Design: trigger-based, zero app-code changes.
--   * BEFORE INSERT/UPDATE stamps row_version from logo.assignment_version_seq
--     (every change re-versions; soft-retires via active=false are naturally
--     versioned tombstones).
--   * AFTER DELETE writes logo.assignment_tombstone (hard deletes become
--     is_active=false feed rows); INSERT of the same key revives (tombstone
--     cleared by the stamp trigger).
--   * Backfill-above-ceiling contract: any bulk backfill MUST let the trigger
--     assign versions (or use nextval) - never renumber below the ceiling.
-- The feed's logo_version domain is independent of woo.state_version_seq.
BEGIN;

CREATE SEQUENCE IF NOT EXISTS logo.assignment_version_seq;

ALTER TABLE logo.assignment ADD COLUMN IF NOT EXISTS row_version bigint;
CREATE INDEX IF NOT EXISTS assignment_row_version ON logo.assignment (row_version);

CREATE TABLE IF NOT EXISTS logo.assignment_tombstone (
    fdm4_store         text        NOT NULL,
    product_style      text        NOT NULL,
    garment_color_code text        NOT NULL,
    option_row         integer     NOT NULL,
    position           smallint    NOT NULL,
    row_version        bigint      NOT NULL,
    deleted_at         timestamptz NOT NULL DEFAULT now(),
    deleted_by         text        NOT NULL DEFAULT '',
    PRIMARY KEY (fdm4_store, product_style, garment_color_code, option_row, position)
);
CREATE INDEX IF NOT EXISTS assignment_tombstone_version
    ON logo.assignment_tombstone (row_version);

CREATE OR REPLACE FUNCTION logo.assignment_feed_stamp() RETURNS trigger AS $fn$
BEGIN
    NEW.row_version := nextval('logo.assignment_version_seq');
    IF TG_OP = 'INSERT' THEN
        DELETE FROM logo.assignment_tombstone t
         WHERE t.fdm4_store = NEW.fdm4_store
           AND t.product_style = NEW.product_style
           AND t.garment_color_code = NEW.garment_color_code
           AND t.option_row = NEW.option_row
           AND t.position = NEW.position;
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION logo.assignment_feed_tombstone() RETURNS trigger AS $fn$
BEGIN
    INSERT INTO logo.assignment_tombstone
        (fdm4_store, product_style, garment_color_code, option_row, position,
         row_version, deleted_at, deleted_by)
    VALUES
        (OLD.fdm4_store, OLD.product_style, OLD.garment_color_code,
         OLD.option_row, OLD.position,
         nextval('logo.assignment_version_seq'), now(),
         COALESCE(current_setting('logo.actor', true), current_user))
    ON CONFLICT (fdm4_store, product_style, garment_color_code, option_row, position)
    DO UPDATE SET row_version = EXCLUDED.row_version,
                  deleted_at  = EXCLUDED.deleted_at,
                  deleted_by  = EXCLUDED.deleted_by;
    RETURN OLD;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS assignment_feed_stamp ON logo.assignment;
CREATE TRIGGER assignment_feed_stamp
    BEFORE INSERT OR UPDATE ON logo.assignment
    FOR EACH ROW EXECUTE FUNCTION logo.assignment_feed_stamp();

DROP TRIGGER IF EXISTS assignment_feed_tombstone ON logo.assignment;
CREATE TRIGGER assignment_feed_tombstone
    AFTER DELETE ON logo.assignment
    FOR EACH ROW EXECUTE FUNCTION logo.assignment_feed_tombstone();

-- Backfill existing rows (trigger assigns the version on this UPDATE).
UPDATE logo.assignment SET row_version = 0 WHERE row_version IS NULL;
ALTER TABLE logo.assignment ALTER COLUMN row_version SET NOT NULL;

-- Writers of logo.assignment run the triggers with their own privileges.
GRANT USAGE ON SEQUENCE logo.assignment_version_seq TO logo_admin, etl_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.assignment_tombstone TO logo_admin, etl_writer;
GRANT SELECT ON logo.assignment_tombstone TO woo_reader, insights_reader;

COMMIT;
