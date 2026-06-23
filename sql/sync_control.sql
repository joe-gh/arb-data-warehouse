-- ============================================================================
-- woo.sync_control — cross-box coordination + status for the FDM4 → Postgres → Woo
-- pipeline, so WP can KNOW the pull's status (and trigger on-demand runs) over the
-- Postgres connection it already has — no SSH, no HTTP service on the DB box.
--
--   * The box (run_sync.sh, role etl_writer/postgres) writes pull run status.
--   * WP (role woo_reader) reads status to gate its Woo sync, and INSERTs request
--     rows for on-demand pulls; a lightweight cron poller on the box acts on them.
--
-- The warehouse is SHARED by dev + prod (both connect as woo_reader). So this is ONE
-- table; the `env` column separates per-env Woo operations. The FDM4 pull is 'global'
-- (a single refresh serves both envs — same FDM4 source).
--
-- Apply:  sudo -u postgres psql -d arb_warehouse -f sync_control.sql   (idempotent)
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS woo;

CREATE TABLE IF NOT EXISTS woo.sync_control (
    id              bigserial   PRIMARY KEY,
    op              text        NOT NULL,                     -- 'pull' | 'woo-full' | 'woo-delta' | 'targeted'
    env             text        NOT NULL DEFAULT 'global',    -- 'global' (shared pull) | 'dev' | 'prod'
    status          text        NOT NULL DEFAULT 'requested', -- requested | running | success | failed | canceled
    requested_by    text,                                     -- 'cron' | 'wp:prod' | 'admin:joseph' ...
    payload         jsonb,                                    -- targeted runs: {"stores":[...],"products":[...]}
    requested_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    rows_loaded     bigint,
    refresh_version bigint,                                   -- max woo.state_version_seq after a pull
    note            text,
    error           text
);

CREATE INDEX IF NOT EXISTS sync_control_op_status  ON woo.sync_control (op, status, requested_at DESC);
CREATE INDEX IF NOT EXISTS sync_control_env_status ON woo.sync_control (env, status, requested_at DESC);

-- WP (woo_reader): read status, request runs, update its own requests. (Write access
-- is scoped to THIS table only — woo_reader stays read-only on the data tables.)
GRANT USAGE ON SCHEMA woo TO woo_reader;
GRANT SELECT, INSERT, UPDATE ON woo.sync_control TO woo_reader;
GRANT USAGE, SELECT ON SEQUENCE woo.sync_control_id_seq TO woo_reader;

-- Box writers (the pull + the poller).
GRANT SELECT, INSERT, UPDATE ON woo.sync_control TO etl_writer;
GRANT USAGE, SELECT ON SEQUENCE woo.sync_control_id_seq TO etl_writer;
