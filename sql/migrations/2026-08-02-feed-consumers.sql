-- Versioned product feed: registered machine consumers (Emblem, a future
-- Shopify adapter, ...) pull woo.store_product_state deltas over the
-- token-authenticated /feed/* endpoints served by logo-admin; the pipeline
-- pings each consumer's webhook after every successful refresh
-- (infra/feed-ping.sh, fired by infra/run_sync.sh) so consumers pull
-- immediately instead of polling.
--
-- Registering a consumer (no UI yet - psql as the database owner):
--   1. Generate a token CLIENT-side and hand it to the consumer over a
--      secure channel; the database stores only its sha256:
--        TOKEN=$(openssl rand -hex 32); echo "token: $TOKEN"
--        HASH=$(printf '%s' "$TOKEN" | sha256sum | cut -d' ' -f1)
--   2. INSERT INTO woo.feed_consumer (name, url, token_hash, note, created_by)
--      VALUES ('emblem', 'https://consumer.example/hook', '<HASH>',
--              'Emblem ingest', 'joseph');
--      -- url may be '' for pull-only consumers (no ping).
--   3. Consumer calls:  GET /feed/version  with
--      Authorization: Bearer <TOKEN>   to verify, then pages
--      GET /feed/products?since_version=0 keeping its own cursor.
-- Revoke by UPDATE ... SET active=false (or rotate token_hash).
BEGIN;

CREATE TABLE IF NOT EXISTS woo.feed_consumer (
    name             text PRIMARY KEY,
    url              text NOT NULL DEFAULT '',   -- ping webhook; '' = pull-only
    token_hash       text NOT NULL,              -- sha256 hex of the bearer token
    active           boolean NOT NULL DEFAULT true,
    note             text NOT NULL DEFAULT '',
    last_ping_at     timestamptz,
    last_ping_status text NOT NULL DEFAULT '',
    last_pull_at     timestamptz,
    last_pull_version bigint,
    created_by       text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- Keyset pagination for /feed/products (WHERE row_version > N ORDER BY
-- row_version LIMIT M). The existing sps_storecat_ver index leads with
-- fdm4_store, so the feed needs a bare row_version index.
CREATE INDEX IF NOT EXISTS sps_row_version
    ON woo.store_product_state (row_version);

GRANT SELECT ON woo.feed_consumer TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON woo.feed_consumer TO logo_admin;

COMMIT;
