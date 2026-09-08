-- Analyst read-only login and two credential tables pulled out of the
-- analyst role (2026-09-08).
--
-- 1. sergey_ro is the read-only login handed to the developer who reviews
--    warehouse data. It is a member of insights_reader (every SELECT the
--    analyst role gets, it gets) and holds no grants of its own. Its session
--    defaults make mistakes cheap: read-only transactions, a 120 s statement
--    timeout, a 60 s idle-in-transaction timeout, at most 5 connections. The
--    password is set out of band (ALTER ROLE ... PASSWORD) and kept in
--    /root/.sergey_ro.pw on the warehouse box; PgBouncer's userlist.txt
--    carries the SCRAM verifier so the login works on :6432.
-- 2. Two tables the analyst role could read hold credentials: catmgr.run_job
--    (worker_token, the secret the WordPress job callbacks present) and
--    woo.feed_consumer (token_hash of each feed client). Neither is
--    analytics data, so SELECT is revoked from insights_reader outright.
--    logo_schema.sql carries the same revoke after its blanket catmgr grant
--    so a fresh bootstrap matches. woo_reader (the WordPress engine) and
--    logo_admin (the app) keep their access.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sergey_ro') THEN
        CREATE ROLE sergey_ro LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
            CONNECTION LIMIT 5;
    END IF;
END $$;

GRANT insights_reader TO sergey_ro;
ALTER ROLE sergey_ro CONNECTION LIMIT 5;
ALTER ROLE sergey_ro SET default_transaction_read_only = on;
ALTER ROLE sergey_ro SET statement_timeout = '120s';
ALTER ROLE sergey_ro SET idle_in_transaction_session_timeout = '60s';

REVOKE ALL ON TABLE catmgr.run_job FROM insights_reader;
REVOKE ALL ON TABLE woo.feed_consumer FROM insights_reader;

COMMIT;
