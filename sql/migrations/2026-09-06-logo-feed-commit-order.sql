-- Make logo feed version allocation match commit order (audit F-15).
--
-- The two feed-versioning triggers from 2026-08-03 call nextval() wherever the
-- writing transaction happens to reach them, so two concurrent editor writes
-- can allocate 101 and 102 and commit 102 first. A consumer that pages while
-- 101 is still uncommitted advances its cursor past 102 and never sees 101
-- again: the row is published below a cursor that has already moved on.
--
-- Serializing allocation on one transaction-scoped advisory lock makes
-- allocation order equal commit order: the holder of the lock keeps it until
-- it commits or rolls back, so a lower version can never become visible after
-- a higher one. The lock is held only for the tail of each writing
-- transaction, and every writer of logo.assignment takes it in the same order,
-- so it adds no deadlock risk.
--
-- Bodies are identical to sql/migrations/2026-08-03-logo-feed-versioning.sql
-- apart from the added PERFORM; keep the two files in step.
BEGIN;

CREATE OR REPLACE FUNCTION logo.assignment_feed_stamp() RETURNS trigger AS $fn$
BEGIN
    -- Allocation order must equal commit order (see header): hold the lock
    -- from nextval() until this transaction ends.
    PERFORM pg_advisory_xact_lock(hashtext('logo.assignment_version_seq'));
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
    -- Allocation order must equal commit order (see header): hold the lock
    -- from nextval() until this transaction ends.
    PERFORM pg_advisory_xact_lock(hashtext('logo.assignment_version_seq'));
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

COMMIT;
