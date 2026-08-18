-- Pending, confirmation-bound agent mutations and append-only apply/undo
-- journal. Requires 2026-07-17-agent-chat-state.sql.
BEGIN;

CREATE TABLE logo.agent_change_set (
    id                   uuid        PRIMARY KEY,
    session_id           uuid        NOT NULL,
    user_login           text        NOT NULL,
    origin               text        NOT NULL DEFAULT 'chat'
                         CHECK (origin IN ('chat', 'spreadsheet')),
    status               text        NOT NULL DEFAULT 'pending'
                         CHECK (status IN (
                             'pending', 'applied', 'discarded', 'undone'
                         )),
    revision             integer     NOT NULL DEFAULT 0 CHECK (revision >= 0),
    preview_hash         text,
    preview_diff         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    affected_scopes      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    contains_hard_delete boolean     NOT NULL DEFAULT false,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    expires_at           timestamptz NOT NULL,
    applied_at           timestamptz,
    undone_at            timestamptz,
    UNIQUE (id, user_login),
    FOREIGN KEY (session_id, user_login)
        REFERENCES logo.agent_chat_session (id, user_login)
        ON DELETE RESTRICT,
    CHECK (preview_hash IS NULL OR preview_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX agent_change_set_owner_status_idx
    ON logo.agent_change_set (user_login, status, updated_at DESC);

CREATE TABLE logo.agent_change_set_item (
    id            uuid        PRIMARY KEY,
    change_set_id uuid        NOT NULL,
    user_login    text        NOT NULL,
    call_id       text        NOT NULL,
    tool_name     text        NOT NULL,
    arguments     jsonb       NOT NULL
                  CHECK (jsonb_typeof(arguments) = 'object'),
    sort_order    integer     NOT NULL CHECK (sort_order >= 0),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (change_set_id, call_id),
    UNIQUE (change_set_id, sort_order),
    FOREIGN KEY (change_set_id, user_login)
        REFERENCES logo.agent_change_set (id, user_login)
        ON DELETE CASCADE
);

CREATE TABLE logo.agent_action_journal (
    id            uuid        PRIMARY KEY,
    change_set_id uuid        NOT NULL,
    user_login    text        NOT NULL,
    event_type    text        NOT NULL
                  CHECK (event_type IN ('apply', 'undo')),
    actor         text        NOT NULL,
    preview_hash  text        NOT NULL
                  CHECK (preview_hash ~ '^[0-9a-f]{64}$'),
    before_state  jsonb       NOT NULL,
    after_state   jsonb       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (change_set_id, event_type),
    FOREIGN KEY (change_set_id, user_login)
        REFERENCES logo.agent_change_set (id, user_login)
        ON DELETE RESTRICT
);

CREATE INDEX agent_action_journal_owner_idx
    ON logo.agent_action_journal (user_login, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON logo.agent_change_set, logo.agent_change_set_item
    TO logo_admin;

-- Defensive and explicit: history is append-only even if a previous role or
-- default-privilege rule granted broader table access.
REVOKE UPDATE, DELETE, TRUNCATE
    ON logo.agent_action_journal
    FROM logo_admin;
GRANT SELECT, INSERT
    ON logo.agent_action_journal
    TO logo_admin;

-- Exact restore may delete a settings row that did not exist before apply;
-- pricing undo likewise recreates or removes one store assignment.
GRANT SELECT, INSERT, UPDATE, DELETE
    ON logo.store_settings, woo.store_pricing_tier
    TO logo_admin;

-- Direct journal privileges remain append-only. Retention is the sole delete
-- path and can remove only complete applied/undone histories older than the
-- fixed 400-day policy. The migration owner, not logo_admin, owns this
-- narrowly scoped function. Eligible change-sets are locked so cleanup cannot
-- race an undo.
CREATE OR REPLACE FUNCTION logo.prune_agent_history()
RETURNS TABLE(journals_deleted bigint, change_sets_deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, logo
AS $$
DECLARE
    retention_cutoff timestamptz := now() - interval '400 days';
    eligible_ids uuid[];
BEGIN
    SELECT coalesce(array_agg(eligible.id), '{}'::uuid[])
      INTO eligible_ids
      FROM (
          SELECT cs.id
            FROM logo.agent_change_set AS cs
           WHERE cs.status IN ('applied', 'undone')
             AND cs.updated_at < retention_cutoff
             AND EXISTS (
                 SELECT 1
                   FROM logo.agent_action_journal AS required_apply
                  WHERE required_apply.change_set_id = cs.id
                    AND required_apply.user_login = cs.user_login
                    AND required_apply.event_type = 'apply'
             )
             AND (
                 cs.status = 'applied'
                 OR EXISTS (
                     SELECT 1
                       FROM logo.agent_action_journal AS required_undo
                      WHERE required_undo.change_set_id = cs.id
                        AND required_undo.user_login = cs.user_login
                        AND required_undo.event_type = 'undo'
                 )
             )
             AND NOT EXISTS (
                 SELECT 1
                   FROM logo.agent_action_journal AS recent_event
                  WHERE recent_event.change_set_id = cs.id
                    AND recent_event.user_login = cs.user_login
                    AND recent_event.created_at >= retention_cutoff
             )
           FOR UPDATE OF cs SKIP LOCKED
      ) AS eligible;

    DELETE FROM logo.agent_action_journal
     WHERE change_set_id = ANY (eligible_ids);
    GET DIAGNOSTICS journals_deleted = ROW_COUNT;

    DELETE FROM logo.agent_change_set
     WHERE id = ANY (eligible_ids);
    GET DIAGNOSTICS change_sets_deleted = ROW_COUNT;
    RETURN NEXT;
END;
$$;

REVOKE ALL ON FUNCTION logo.prune_agent_history() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION logo.prune_agent_history() TO logo_admin;

COMMIT;
