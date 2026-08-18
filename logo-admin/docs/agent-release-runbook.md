# In-App Assistant Release and Rollback Runbook

This runbook is for a future, explicitly approved deployment. Creating these
artifacts does not authorize a deployment, migration, service restart, or nginx
reload.

## Fixed deployment paths

```text
Application:        /opt/arb-logo-admin
Python environment: /opt/arb-logo-admin-venv
Environment file:   /etc/arb-logo-admin.env
Private uploads:    /var/lib/arb-logo-admin/agent-uploads
nginx snippet:      /etc/nginx/snippets/arb-logo-admin-agent-chat.conf
```

The SQL migrations remain in the warehouse repository under
`/opt/arb-data-warehouse/sql/migrations`. The web service itself runs only from
the application path listed above, not from a nested warehouse-repository
checkout.

## Release invariants

- WordPress retains the existing filterable `manage_options` login gate.
- `AGENT_ENABLED`, `AGENT_WRITES_ENABLED`, and the per-user allowlist are
  independent gates. All default to denying access.
- `AGENT_ALLOWED_USERS` contains WordPress `user_login` values. Empty means
  nobody. The initial pilot contains Joseph's login only.
- Every agent route returns 404 to an authenticated user outside that allowlist.
- The model cannot call apply, discard, mapping-confirm, or undo.
- Reads are bounded. Writes are limited to the reviewed transactional registry.
- Every OpenAI request uses `store=false`; provider and application caps remain
  active before the first call.
- Production writes stay disabled until the final gate below is signed off.

## 1. Pre-deployment evidence

1. Confirm the filenames and reviewed checksums of every immutable
   `sql/migrations/*.sql` file in bytewise date order. The application release
   never uses a hand-selected subset of the migration directory.
2. Run the complete pytest suite against the disposable `arb_warehouse_test_*`
   database using distinct application and reset DSNs.
3. Run `sql/diagnostics/agent-write-preflight.sql` through the deployed
   `logo_admin` application DSN with a forced read-only transaction and the
   independently reviewed repull-function hash:

   ```bash
   set +x
   PGOPTIONS='-c default_transaction_read_only=on' \
   psql "$DATABASE_DSN" -X -v ON_ERROR_STOP=1 \
     -v repull_function_sha256="${AGENT_REPULL_FUNCTION_SHA256:?reviewed hash required}" \
     -f /opt/arb-data-warehouse/sql/diagnostics/agent-write-preflight.sql \
     > /var/tmp/agent-write-preflight.txt
   ```

   Never run this diagnostic as `postgres` or print the DSN. Its terminal
   assertion must pass; review every schema signature, trigger, rule/RLS
   policy, constraint/index, function body, role setting/membership, and
   effective grant. Any mismatch blocks writes.
4. Confirm the provider-side monthly budget limit and application token/rate
   caps.
5. Confirm no model tool schema contains sync, upload, import, mirror, export,
   apply, discard, mapping-confirm, undo, or redo.
6. Confirm load/failure tests cover provider failures, browser disconnects,
   database rollback, process recovery, and the configured maximum change set.

## 2. Deploy inert code and schema

Keep these values in `/etc/arb-logo-admin.env`:

```ini
AGENT_ENABLED=false
AGENT_WRITES_ENABLED=false
AGENT_ALLOWED_USERS=
```

Apply the reviewed migrations before starting code that references their
tables. Install dependencies from `/opt/arb-logo-admin/requirements.txt` into
`/opt/arb-logo-admin-venv`. Install, but do not yet enable, the maintenance
service/timer and the checked-in nginx snippet.

After the service restarts, smoke-test the existing dashboard operations:

- WordPress login/logout and CSRF failure behavior;
- logo reads and existing direct edits;
- pricing reads and existing direct edits;
- activity and import-report views;
- existing MCP process behavior.

No assistant entry point or API should be visible while the feature is off.

## 3. Enable Joseph-only read access

Configure the actual WordPress login, not a display name:

```ini
AGENT_ENABLED=true
AGENT_WRITES_ENABLED=false
AGENT_ALLOWED_USERS=joseph-login
OPENAI_API_KEY=replace-at-deploy-time
OPENAI_MODEL=approved-model-id
```

Install `deploy/nginx/agent-chat.conf` at
`/etc/nginx/snippets/arb-logo-admin-agent-chat.conf`, include it inside the
application server block before `location /`, validate nginx configuration,
then reload it through the normal controlled deployment process.

Evidence required:

1. Joseph sees the assistant drawer and can stream a bounded read.
2. Another authenticated administrator receives the unchanged dashboard with
   no assistant DOM and receives 404 for every agent endpoint.
3. An empty allowlist denies Joseph as well.
4. SSE token events and 15-second heartbeats arrive without proxy buffering.
5. Session ownership returns 404 across users.
6. Quota reservations occur before provider-client construction.
7. Logs contain metadata only, never prompts, tool arguments/results,
   spreadsheets, previews, secrets, or provider payloads.

## 4. Validate staging with writes still disabled

Against a disposable or staging database, exercise:

- dependent cumulative commands;
- same-row cumulative edits;
- failed candidate staging;
- stale revision and preview hash;
- hard-delete acknowledgement;
- atomic rollback when a later command fails;
- exact undo and intervening-edit conflict;
- known and inferred spreadsheet mapping with both human confirmations.

The production feature flag remains `AGENT_WRITES_ENABLED=false` throughout.

## 5. Enable the limited write pilot

Only after named human sign-off on all prior evidence:

```ini
AGENT_ENABLED=true
AGENT_WRITES_ENABLED=true
AGENT_ALLOWED_USERS=joseph-login
```

Verify, in order:

1. save one assignment and inspect its cumulative preview;
2. apply the exact displayed revision/hash;
3. undo and compare every business-table column;
4. stage a multi-command dependent batch;
5. reject a stale apply after an independent edit;
6. require the extra destructive checkbox for a hard delete;
7. reject undo after an intervening edit without overwriting it;
8. upload a bounded spreadsheet, confirm its mapping, then separately confirm
   its change set;
9. confirm the hourly maintenance timer removes expired private files/state.

Do not add more allowed users until this pilot is explicitly approved.

## Immediate rollback

Set both flags false first:

```ini
AGENT_WRITES_ENABLED=false
AGENT_ENABLED=false
AGENT_ALLOWED_USERS=
```

Restart only the application service through the controlled operations process
and verify it is active. The existing dashboard and MCP service must continue.
Do not down-migrate additive agent tables during an incident. Preserve pending
change sets and journals for investigation; disabling the feature prevents web
access and model calls.

If streaming itself is the incident source, remove the nginx snippet include
only after the application flags are false and nginx configuration validates.

## Retention operations

The future maintenance timer runs hourly. Before enabling production writes,
confirm:

- `agent-maintenance.timer` is scheduled and persistent;
- `/var/lib/arb-logo-admin/agent-uploads` is owned by `arb-logo-admin` and mode
  `0700`;
- dry-run counts match expected fixture/production state;
- cleanup logs include counts, ages, UUIDs, and statuses only;
- applied journals are retained for 400 days;
- spreadsheet files/jobs expire after one hour.

## Final gate record

Record the reviewer, date, environment, migration checksums, preflight output,
test result location, provider budget, load-test result, rollback rehearsal, and
the exact `AGENT_ALLOWED_USERS` value. Missing evidence means writes remain off.
