#!/usr/bin/env bash
# Notify registered feed consumers that a new product-state generation is
# published. Fired by run_sync.sh AFTER the success control row is finalized;
# fully best-effort - this script must NEVER fail or delay the pipeline
# (every step is guarded and it always exits 0).
#
# Usage: feed-ping.sh <refresh_version>
#
# The ping body carries NO secret ({"version": N, "generated_at": "..."}) -
# the database stores only sha256 token hashes, so the server cannot sign
# with a consumer's token. Consumers MUST treat the ping as an untrusted
# hint: on receipt, call back GET /feed/version with their own bearer token
# and pull /feed/products from their stored cursor. Delivery status is
# recorded on woo.feed_consumer (last_ping_at / last_ping_status) for the
# Health view; pull-only consumers (url = '') are skipped.
set -u

VER="${1:-}"
[[ "$VER" =~ ^[0-9]+$ ]] || exit 0
GENERATED_AT=$(date -u +%FT%TZ)
BODY=$(printf '{"version": %s, "generated_at": "%s"}' "$VER" "$GENERATED_AT")

psql_q() { sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d arb_warehouse -tAc "$1" 2>/dev/null; }

consumers=$(psql_q "SELECT name || E'\t' || url FROM woo.feed_consumer WHERE active AND url <> ''") || exit 0
[ -n "$consumers" ] || exit 0

while IFS=$'\t' read -r name url; do
  [ -n "$name" ] && [ -n "$url" ] || continue
  status=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' \
           -H 'Content-Type: application/json' \
           -X POST --data "$BODY" "$url" 2>/dev/null) || status="error"
  # Single-quote-safe: name comes from the PK we control; status is ours.
  psql_q "UPDATE woo.feed_consumer
             SET last_ping_at = now(),
                 last_ping_status = '$(printf '%s' "$status" | tr -cd '0-9a-zA-Z_-')'
           WHERE name = \$feedname\$${name}\$feedname\$" >/dev/null || true
done <<< "$consumers"

exit 0
