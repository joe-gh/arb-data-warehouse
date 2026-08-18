-- Persistent reservations and accounting for agent cost/rate limits.
BEGIN;

CREATE TABLE logo.agent_usage_daily (
    user_login      text        NOT NULL,
    usage_day       date        NOT NULL,
    requests        integer     NOT NULL DEFAULT 0 CHECK (requests >= 0),
    reserved_tokens bigint      NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
    input_tokens    bigint      NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens   bigint      NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_login, usage_day)
);

CREATE TABLE logo.agent_usage_monthly (
    usage_month     date        PRIMARY KEY,
    requests        integer     NOT NULL DEFAULT 0 CHECK (requests >= 0),
    reserved_tokens bigint      NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
    input_tokens    bigint      NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens   bigint      NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (date_trunc('month', usage_month)::date = usage_month)
);

CREATE TABLE logo.agent_rate_window (
    user_login   text        NOT NULL,
    window_start timestamptz NOT NULL,
    requests     integer     NOT NULL DEFAULT 0 CHECK (requests >= 0),
    PRIMARY KEY (user_login, window_start)
);

-- One durable row per reservation makes reconciliation idempotent. Aggregate
-- counters remain fast cap checks; this journal prevents a retry from
-- subtracting tokens reserved by another concurrent request.
CREATE TABLE logo.agent_quota_reservation (
    id              uuid        PRIMARY KEY,
    user_login      text        NOT NULL,
    usage_day       date        NOT NULL,
    usage_month     date        NOT NULL,
    window_start    timestamptz NOT NULL,
    reserved_tokens bigint      NOT NULL CHECK (reserved_tokens > 0),
    status          text        NOT NULL DEFAULT 'reserved'
                                CHECK (status IN (
                                    'reserved', 'reconciled', 'retained'
                                )),
    input_tokens    bigint      NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens   bigint      NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    provider_started_at timestamptz,
    expires_at      timestamptz NOT NULL
                                DEFAULT (now() + interval '15 minutes'),
    finalized_at    timestamptz
);

CREATE INDEX agent_quota_reservation_owner_created_idx
    ON logo.agent_quota_reservation (user_login, created_at DESC);

CREATE INDEX agent_quota_reservation_stale_idx
    ON logo.agent_quota_reservation (expires_at, id)
    WHERE status = 'reserved';

GRANT SELECT, INSERT, UPDATE, DELETE
    ON logo.agent_usage_daily,
       logo.agent_usage_monthly,
       logo.agent_rate_window,
       logo.agent_quota_reservation
    TO logo_admin;

COMMIT;
