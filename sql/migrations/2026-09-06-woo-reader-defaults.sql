-- woo_reader: make the read-only/timeout safeguards role defaults.
--
-- WordPress (arb-product-sync, class-arb-warehouse-db.php) opens its warehouse
-- connection through PgBouncer in transaction pooling mode and then issues
-- session-level `SET default_transaction_read_only = on` and `SET
-- statement_timeout`. Under transaction pooling that session belongs to
-- whichever server connection served that transaction, so the very next query
-- can land on a different server with neither setting. Role defaults are
-- applied by the server at connect time for every backend woo_reader ever
-- uses, so the guarantee no longer depends on which pooled server answers.
--
-- The WordPress write path (control_write) issues SET TRANSACTION READ WRITE
-- inside its own transaction, which overrides the default for that
-- transaction only - so it keeps working.
--
-- Role-level, not database-level: woo_reader exists cluster-wide and reads
-- only this database. Idempotent (ALTER ROLE ... SET replaces the value).
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'woo_reader') THEN
        RAISE EXCEPTION
            'woo_reader does not exist; create the reader role before applying '
            'its connection defaults';
    END IF;
END
$$;

ALTER ROLE woo_reader SET statement_timeout = '30s';
ALTER ROLE woo_reader SET default_transaction_read_only = on;

COMMIT;
