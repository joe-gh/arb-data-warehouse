-- Server-side registry for signed Warehouse Operations browser sessions.
-- The cookie remains signed and eight-hour bounded; a missing/revoked row is
-- now invalid immediately.
BEGIN;

CREATE TABLE IF NOT EXISTS logo.admin_session (
    session_hash text        NOT NULL PRIMARY KEY,
    user_login   text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    CONSTRAINT admin_session_hash_check
        CHECK (session_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS admin_session_user_active_idx
    ON logo.admin_session (user_login, expires_at)
    WHERE revoked_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON logo.admin_session TO logo_admin;

COMMIT;
