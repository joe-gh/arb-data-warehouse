-- Owner-scoped metadata for short-lived, privately stored spreadsheet jobs.
-- Requires 2026-07-17-agent-chat-state.sql and
-- 2026-07-17-agent-change-sets.sql.
BEGIN;

CREATE TABLE logo.agent_spreadsheet_job (
    id               uuid        PRIMARY KEY,
    session_id       uuid        NOT NULL,
    user_login       text        NOT NULL,
    storage_key      uuid        NOT NULL UNIQUE,
    change_set_id    uuid        UNIQUE,
    original_name    text        NOT NULL,
    media_type       text        NOT NULL,
    byte_size        bigint      NOT NULL CHECK (byte_size >= 0),
    sha256           text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    format_name      text        NOT NULL
                     CHECK (format_name IN ('csv', 'xlsx')),
    status           text        NOT NULL
                     CHECK (status IN (
                         'mapping_processing',
                         'mapping_pending',
                         'mapping_confirmed',
                         'staged',
                         'rejected',
                         'expired'
                     )),
    mapping_revision integer     NOT NULL DEFAULT 1
                     CHECK (mapping_revision >= 1),
    mapping_hash     text        NOT NULL
                     CHECK (mapping_hash ~ '^[0-9a-f]{64}$'),
    mapping          jsonb       NOT NULL,
    rejected_rows    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    UNIQUE (id, user_login),
    FOREIGN KEY (session_id, user_login)
        REFERENCES logo.agent_chat_session (id, user_login)
        ON DELETE RESTRICT,
    FOREIGN KEY (change_set_id, user_login)
        REFERENCES logo.agent_change_set (id, user_login)
        ON DELETE RESTRICT
);

CREATE INDEX agent_spreadsheet_owner_status_idx
    ON logo.agent_spreadsheet_job (
        user_login,
        status,
        created_at DESC
    );

GRANT SELECT, INSERT, UPDATE, DELETE
    ON logo.agent_spreadsheet_job
    TO logo_admin;

COMMIT;
