-- Category Editor Phase 5: apply runs, per-blog jobs, pre-apply snapshots,
-- and planned redirects.
--
-- A run freezes per-blog declarative plans into catmgr.run_job.payload; a
-- sequential worker converges each blog through the WP broker. job_snapshot
-- holds the blog's live state captured immediately before its job executed -
-- the emergency-restore source.
BEGIN;

CREATE TABLE IF NOT EXISTS catmgr.run (
    run_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    env                 text NOT NULL CHECK (env IN ('dev', 'prod')),
    target_blogs        integer[] NOT NULL,
    status              text NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'paused',
                                          'completed', 'failed', 'cancelled')),
    plan_totals         jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot_versions   jsonb NOT NULL DEFAULT '{}'::jsonb,
    stop_on_failure     boolean NOT NULL DEFAULT true,
    cancel_requested    boolean NOT NULL DEFAULT false,
    created_by          text NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    worker_heartbeat_at timestamptz
);
CREATE INDEX IF NOT EXISTS run_env_status ON catmgr.run (env, status);

CREATE TABLE IF NOT EXISTS catmgr.run_job (
    job_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     bigint NOT NULL REFERENCES catmgr.run (run_id) ON DELETE CASCADE,
    blog_id    integer NOT NULL,
    blog_path  text NOT NULL DEFAULT '',
    seq        integer NOT NULL,
    status     text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'done', 'failed',
                                 'skipped', 'cancelled')),
    payload    jsonb NOT NULL,
    progress   jsonb NOT NULL DEFAULT '{}'::jsonb,
    result     jsonb,
    attempt    integer NOT NULL DEFAULT 0,
    request_id text NOT NULL DEFAULT '',
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (run_id, blog_id)
);
CREATE INDEX IF NOT EXISTS run_job_run_seq ON catmgr.run_job (run_id, seq);

CREATE TABLE IF NOT EXISTS catmgr.job_snapshot (
    job_id   bigint PRIMARY KEY REFERENCES catmgr.run_job (job_id) ON DELETE CASCADE,
    payload  jsonb NOT NULL,
    taken_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS catmgr.redirect (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     bigint NOT NULL REFERENCES catmgr.run (run_id) ON DELETE CASCADE,
    blog_id    integer NOT NULL,
    old_path   text NOT NULL,
    new_path   text NOT NULL,
    status     text NOT NULL DEFAULT 'planned'
               CHECK (status IN ('planned', 'created', 'failed')),
    detail     text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS redirect_run ON catmgr.redirect (run_id);

GRANT SELECT ON catmgr.run, catmgr.run_job, catmgr.job_snapshot, catmgr.redirect
    TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON catmgr.run, catmgr.run_job, catmgr.job_snapshot, catmgr.redirect
    TO logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;

COMMIT;
