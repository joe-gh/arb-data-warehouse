-- ============================================================================
-- Least-privilege role for the WP-side PIM mirror (arb-pim-mirror.php).
--
-- Writes ONLY pim.*; no read or write anywhere else. Run as the owner of
-- arb_warehouse AFTER sql/pim_schema.sql. No embedded password; set one with:
--     \password pim_writer
-- Then add a scram-sha-256 pg_hba/PgBouncer entry mirroring woo_reader's, and
-- define ARB_WH_PG_PIM_USER / ARB_WH_PG_PIM_PASS in the PROD wp-config only
-- (per-box, never cloned - the mirror stays inert everywhere else).
--
-- Reapplying this file is a full reset of pim_writer's authority, so the grant
-- list below has to be complete. The manifest check runs before the first
-- REVOKE and stops the run when pim holds a table this file does not name.
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

-- Fail closed on an unlisted table. This file revokes everything in pim and
-- then re-grants only what is named below, so a table added by a later
-- migration silently loses its grants when the policy is reapplied (that is
-- how media_object / product_placement / media_rendition lost the CRUD the
-- WordPress placement mirror needs). Anything in pim that is not in this
-- manifest stops the run instead: decide its grant, add it here, rerun.
DO $manifest$
DECLARE
    unlisted text;
BEGIN
    SELECT string_agg(relation.relname, ', ' ORDER BY relation.relname)
      INTO unlisted
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
     WHERE namespace.nspname = 'pim'
       AND relation.relkind IN ('r', 'p')
       AND relation.relname <> ALL (ARRAY[
           'ingest_event', 'product_state', 'media_object',
           'product_placement', 'media_rendition', 'api_product',
           'api_variant', 'api_image', 'api_pull_state',
           'push_change_set', 'push_change_row'
       ]);
    IF unlisted IS NOT NULL THEN
        RAISE EXCEPTION
            'pim tables are missing from the pim_writer privilege manifest: %',
            unlisted;
    END IF;
END
$manifest$;

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
-- The live WordPress placement mirror connects as pim_writer and maintains
-- these three (media objects, per-product placements, derived renditions);
-- the 2026-08-02 / 08-03 / 08-04 migrations granted them and this file used
-- to revoke them straight back on the next run. Their keys are natural, so no
-- sequence grants are needed.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE pim.media_object TO pim_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE pim.product_placement TO pim_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE pim.media_rendition TO pim_writer;

-- Explicitly NO access to fdm4.*, woo.*, or logo.* - the mirror only records
-- what Sales Layer pushed; joins happen in the projection as woo_reader/owner.

COMMIT;
