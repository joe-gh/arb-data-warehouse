-- ============================================================================
-- Least-privilege role for the WP-side PIM mirror (arb-pim-mirror.php).
--
-- Writes ONLY pim.*; no read or write anywhere else. Run as the owner of
-- arb_warehouse AFTER sql/pim_schema.sql. No embedded password; set one with:
--     \password pim_writer
-- Then add a scram-sha-256 pg_hba/PgBouncer entry mirroring woo_reader's, and
-- define ARB_WH_PG_PIM_USER / ARB_WH_PG_PIM_PASS in the PROD wp-config only
-- (per-box, never cloned - the mirror stays inert everywhere else).
-- ============================================================================

BEGIN;

DO $role$
BEGIN
    IF NOT EXISTS ( SELECT 1 FROM pg_roles WHERE rolname = 'pim_writer' ) THEN
        CREATE ROLE pim_writer LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$role$;

ALTER ROLE pim_writer LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
    NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8;

-- Strip any pre-existing memberships so the grants below are the complete
-- authority (a membership would still permit SET ROLE despite NOINHERIT).
DO $memberships$
DECLARE
    granted_role text;
BEGIN
    FOR granted_role IN
        SELECT role_name.rolname
          FROM pg_auth_members membership
          JOIN pg_roles member_role ON member_role.oid = membership.member
          JOIN pg_roles role_name ON role_name.oid = membership.roleid
         WHERE member_role.rolname = 'pim_writer'
    LOOP
        EXECUTE format( 'REVOKE %I FROM pim_writer', granted_role );
    END LOOP;
END
$memberships$;

ALTER ROLE pim_writer SET statement_timeout = '15s';
ALTER ROLE pim_writer SET lock_timeout = '5s';
ALTER ROLE pim_writer SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE pim_writer SET search_path = pim, pg_catalog;

GRANT CONNECT ON DATABASE arb_warehouse TO pim_writer;
REVOKE CREATE, TEMPORARY ON DATABASE arb_warehouse FROM pim_writer;

REVOKE ALL PRIVILEGES ON SCHEMA pim FROM pim_writer;
GRANT USAGE ON SCHEMA pim TO pim_writer;

-- The only durable writes available to the mirror.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA pim FROM pim_writer;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA pim FROM pim_writer;
GRANT SELECT, INSERT ON TABLE pim.ingest_event TO pim_writer;
GRANT SELECT, INSERT, UPDATE ON TABLE pim.product_state TO pim_writer;
GRANT USAGE, SELECT ON SEQUENCE pim.ingest_event_id_seq TO pim_writer;

-- Explicitly NO access to fdm4.*, woo.*, or logo.* - the mirror only records
-- what Sales Layer pushed; joins happen in the projection as woo_reader/owner.

COMMIT;
