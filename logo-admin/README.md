# Arborwear Logo Admin

Standalone FastAPI/Jinja administration for the warehouse-owned `logo.*`
overlay. The application runs beside PostgreSQL on the warehouse host, validates
operators with their normal WordPress account passwords, and writes an audit
identity (`updated_by`) with every assignment change.

The customer storefront does not call this application. WordPress continues to
read the warehouse through Phase A and projects `logo.assignment` into the ACF
`product_logos` JSON.

## Security model

- PostgreSQL role `logo_admin` can read `fdm4.*` and `woo.*` and can write only
  the reviewed Warehouse Operations surfaces: assignments/settings, logo
  names and lookup overrides, color/bulk state, the session registry, pricing
  tiers/rules, and sync exclusions. Audit/report tables remain append-only;
  `image_import` permits update solely to repair a missing mirrored asset.
- Operators log in with their normal WordPress username and password
  (validated via wp_authenticate() through POST /arb/v1/logo-admin/login, so
  Wordfence brute-force protection and lockouts apply), then discards the
  password. Only the returned identity is stored in the signed app session.
- The browser receives a signed, HTTP-only session cookie. Its lifetime is
  clamped to eight hours and every state-changing request requires a CSRF token.
- Sync requests use a separate WordPress service account from the server-side
  environment. Operator credentials are never reused for automation.
- Uploaded files are renamed to random identifiers and limited to PNG, JPEG,
  GIF, and WebP. SVG and executable content are rejected.
- The in-app assistant is disabled by default and has a second, fail-closed
  operator allowlist. `AGENT_ENABLED=true` alone does not expose it: the signed
  session's WordPress `user_login` must also appear in `AGENT_ALLOWED_USERS`.
  Assistant routes independently enforce the same check and return 404 to an
  authenticated operator who is not allowed.

## Required environment

Python 3.10+ and PostgreSQL are expected on the warehouse host. Runtime Python
dependencies are listed in `requirements.txt`.

For a future authorized local test run, install the separate test dependencies
and the pinned Playwright Chromium runtime before invoking the browser suite:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium
```

Chromium is test-only and is not installed or launched by application startup.

Create `/etc/arb-logo-admin.env`, owned by root and mode `0600`:

```ini
DATABASE_DSN="dbname=arb_warehouse user=logo_admin password=REPLACE_ME host=/var/run/postgresql"
SESSION_SECRET=REPLACE_WITH_AT_LEAST_32_RANDOM_BYTES
SESSION_COOKIE_SECURE=true
SESSION_TTL_SECONDS=28800

WP_AUTH_URL=https://arb-dev.arborwear.com/wp-json/arb/v1/logo-admin/auth
WP_SYNC_URL=https://arb-dev.arborwear.com/wp-json/arb/v1/logo-admin/sync
WP_SYNC_USER=logo-sync-service
WP_SYNC_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
WP_HTTP_TIMEOUT=20
WP_SYNC_TIMEOUT=360

UPLOAD_DIR=/var/lib/arb-logo-admin/uploads
MEDIA_BASE=https://media.arborwear.com/images/logos/warehouse/
FDM4_ART_BASE=https://media.arborwear.com/images/logos/
MAX_UPLOAD_BYTES=10485760

# MCP legacy imports are restricted to this non-public, app-owned directory.
MCP_IMPORT_DIR=/var/lib/arb-logo-admin/imports

# In-app assistant. Both feature flags and the operator allowlist fail closed.
AGENT_ENABLED=false
AGENT_WRITES_ENABLED=false
AGENT_ALLOWED_USERS=
# Required after the complete migration chain installs the reviewed function.
AGENT_REPULL_FUNCTION_SHA256=
OPENAI_API_KEY=
OPENAI_MODEL=
AGENT_DAILY_TOKEN_CAP=100000
AGENT_MONTHLY_TOKEN_CAP=2000000
AGENT_REQUESTS_PER_MINUTE=10
AGENT_MAX_INPUT_CHARS=8000
AGENT_MAX_OUTPUT_TOKENS=2048
AGENT_MAX_TOOL_CALLS=12
AGENT_MAX_TOOL_RESULT_BYTES=100000
AGENT_MAX_TURN_REPLAY_BYTES=1000000
AGENT_MAX_CONCURRENT_TURNS=4
AGENT_MAX_CHANGE_SET_ITEMS=50
AGENT_TURN_TIMEOUT_SECONDS=90
AGENT_CHAT_RETENTION_DAYS=30

# Category editor (ships dark; page-scoped WordPress targets - the rest of
# the app keeps using WP_SYNC_URL). Base URLs point at the categories broker
# namespace. CATMGR_APPLY_USERS is the fail-closed allowlist for Apply /
# restore / freeze; everyone else can edit drafts and snapshots.
CATMGR_ENABLED=false
CATMGR_DEV_URL=https://arb-dev.arborwear.com/wp-json/arb/v1/logo-admin/categories
CATMGR_DEV_USER=logo-sync-service
CATMGR_DEV_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
CATMGR_PROD_URL=
CATMGR_PROD_USER=
CATMGR_PROD_APP_PASSWORD=
CATMGR_WP_TIMEOUT=120
CATMGR_APPLY_USERS=
# Optional: who can SEE the category editor at all (nav + API). Empty = everyone
# with an app login; set to a few logins to keep it out of the team's way.
CATMGR_VIEW_USERS=

# Private spreadsheet staging. Files expire and are never publicly served.
AGENT_UPLOAD_DIR=/var/lib/arb-logo-admin/agent-uploads
AGENT_MAX_SPREADSHEET_BYTES=5242880
AGENT_MAX_SPREADSHEET_ROWS=500
AGENT_MAX_SPREADSHEET_COLUMNS=40
AGENT_MAX_CELL_CHARS=2000
AGENT_MAX_XLSX_ENTRIES=200
AGENT_MAX_XLSX_UNCOMPRESSED_BYTES=52428800
```

`SESSION_SECRET` must be unpredictable. Rotating it immediately invalidates all
app sessions. To revoke one operator without rotating the global secret, run
`UPDATE logo.admin_session SET revoked_at=now() WHERE user_login='...' AND
revoked_at IS NULL;` as the database owner; subsequent requests fail the
server-side registry check immediately. `MEDIA_BASE` must be an absolute
`http://` or `https://` URL ending
with `/`. It points at the PUBLIC host that serves the images - in this
deployment the media box (`https://media.arborwear.com/images/logos/warehouse/`),
fed by the additive publisher `infra/publish-logo-media.sh` (rsync
`--ignore-existing`, never deletes, writes only into its own subdirectory -
the media box holds the only copy of legacy assets and is never overwritten).
Phase A recognizes absolute `image_url` values and uses them unchanged.
`FDM4_ART_BASE` is used only for read-only design/scheme previews from FDM4's
relative PREVIEW/THUMB paths. NOTE: FDM4's art binaries are NOT web-hosted and
are NOT in the warehouse DB (fdm4.cust_art_file carries paths only) - previews
for FDM4-only designs will 404 until an art export from FDM4 exists; the
warehouse-owned `image_url` is what the storefront uses either way.

Create `MCP_IMPORT_DIR` as `arb-logo-admin:arb-logo-admin` mode `0750`. The
root-owned `mcp-run.sh` reads the protected environment and immediately drops
to that account; legacy-import paths outside this directory, symlinks, and
non-regular files are rejected.

`AGENT_ALLOWED_USERS` is a comma-separated list of WordPress `user_login`
values. Values are trimmed and compared case-insensitively. Its default is
empty, which means nobody can see or call the assistant even if
`AGENT_ENABLED=true`. During the initial pilot, set it only to Joseph's actual
WordPress login. `AGENT_WRITES_ENABLED` remains false until the complete write,
retention, load, failure, and rollback gates in `docs/agent-release-runbook.md`
have passed. OpenAI settings are required only when `AGENT_ENABLED=true`.
`AGENT_MAX_TURN_REPLAY_BYTES` is a cumulative per-turn persistence bound and
must be at least `AGENT_MAX_TOOL_RESULT_BYTES`. The 500-row spreadsheet cap is
processed as one bounded batch; ordinary chat change sets retain their separate
`AGENT_MAX_CHANGE_SET_ITEMS` cap.

The repository-owned nginx artifact is
`deploy/nginx/agent-chat.conf`. A future deployment may install/include that
reviewed file in the application server block; this local implementation does
not modify nginx.

### Single-operator pilot

Enable the assistant for exactly one WordPress login, reads first, writes only
after the write contract passes. Every step runs on the warehouse box and is
idempotent, so a partially completed pass can simply be repeated.

1. Pick an idle window. The role script's GRANTs take exclusive table locks:

   ```bash
   sudo -u postgres psql -d arb_warehouse -c "SELECT pid, state, now()-query_start AS age, left(query,80) AS q FROM pg_stat_activity WHERE datname='arb_warehouse' AND state<>'idle' ORDER BY query_start;"
   ```

   Proceed only when no `run_sync`/transform/reconcile backends are active and
   the next hourly pull is more than ~15 minutes away.
2. Apply schema, role, and preflight exactly per "Complete migrations and
   write preflight" below: every `sql/migrations/*.sql` in `LC_ALL=C sort`
   order as the database owner (already-applied files are no-ops), then
   `sql/logo_admin_role.sql`, then `sql/diagnostics/agent-write-preflight.sql`
   through the `logo_admin` DSN. Expected: the preflight prints no `ERROR`.
3. Compute the repull function pin. The role script, the preflight, and the
   runtime contract all hash `pg_get_functiondef()` (the complete `CREATE OR
   REPLACE FUNCTION` text), not `prosrc`, so compute it the same way:

   ```bash
   sudo -u postgres psql -d arb_warehouse -At -c "SELECT encode(sha256(convert_to(pg_get_functiondef(p.oid),'UTF8')),'hex') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='logo' AND p.proname='repull_display_name';"
   ```
4. nginx SSE location, uploads directory, and the retention timer. Include
   `deploy/nginx/agent-chat.conf` in the logo-admin server block
   (`/etc/nginx/sites-available/embroidery.conf`, dated `.bak` first),
   `sudo nginx -t`, reload. Then:

   ```bash
   sudo install -d -o arb-logo-admin -g arb-logo-admin -m 0700 /var/lib/arb-logo-admin/agent-uploads
   sudo install -o root -g root -m 0644 /opt/arb-logo-admin/agent-maintenance.service /etc/systemd/system/
   sudo install -o root -g root -m 0644 /opt/arb-logo-admin/agent-maintenance.timer /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now agent-maintenance.timer
   ```
5. Environment, reads first (`sudoedit /etc/arb-logo-admin.env`):

   ```ini
   AGENT_ENABLED=true
   AGENT_WRITES_ENABLED=false
   AGENT_ALLOWED_USERS=<pilot operator's WordPress user_login>
   AGENT_REPULL_FUNCTION_SHA256=<value from step 3>
   OPENAI_API_KEY="<key>"
   OPENAI_MODEL=<model chosen for the pilot - required, no default>
   ```

   Then `sudo systemctl restart arb-logo-admin && sudo journalctl -u
   arb-logo-admin -n 50 --no-pager`. Expected: a clean start
   (`validate_registry` runs; no `ConfigurationError`).
6. Verify the gate. Pilot login: `#assistant-toggle` is visible and "list
   stores" streams an answer. Any other login: no toggle, and
   `curl -sS -o /dev/null -w '%{http_code}\n' -b '<their cookie>'
   https://embroidery.arborwear.com/api/agent/sessions` returns `404`.
7. Writes. Set `AGENT_WRITES_ENABLED=true` and restart. Startup now runs
   `validate_write_database_contract` and fails closed on any drift (read the
   journal). Pilot test: stage a `save_assignment` through chat, review the
   card, apply, verify in the editor and `logo.audit_log`, undo, verify the
   exact restore. Reads remain the fallback (`AGENT_WRITES_ENABLED=false`) if
   anything is off.

### Complete migrations and write preflight

Every release applies the complete immutable migration directory in filename
(date) order, then reapplies the generated least-privilege role. Never maintain
a hand-selected migration list: Warehouse Operations features share schema and
contract dependencies.

1. `sql/logo_schema.sql` for a blank database only.
2. Every `sql/migrations/*.sql`, sorted bytewise by filename, as the database
   owner.
3. `sql/logo_admin_role.sql` as the database owner.
4. `sql/diagnostics/agent-write-preflight.sql` through the `logo_admin` DSN.

When `logo.repull_display_name(text,boolean)` exists (the complete migration
chain installs it), pass its independently reviewed SHA-256 to both SQL files.
The diagnostic must run through the
deployed application DSN with PostgreSQL read-only mode forced; its terminal
assertion rejects the wrong role or a writable transaction:

```bash
set +x
PGOPTIONS='-c default_transaction_read_only=on' \
psql "$DATABASE_DSN" -X -v ON_ERROR_STOP=1 \
  -v repull_function_sha256="${AGENT_REPULL_FUNCTION_SHA256:?reviewed hash required}" \
  -f /opt/arb-data-warehouse/sql/diagnostics/agent-write-preflight.sql
```

Do not run the diagnostic as `postgres`, print the DSN, or enable writes when
the final assertion fails.

## Deployment runbook

### 1. Deploy the database schema and least-privilege role

For a blank database, apply the base schema first. For both blank installs and
upgrades, apply every migration in date order before the role script because
the role validates and grants the complete current surface:

```bash
cd /opt/arb-data-warehouse
sudo -u postgres psql -v ON_ERROR_STOP=1 -d arb_warehouse -f sql/logo_schema.sql
while IFS= read -r migration; do
  sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d arb_warehouse -f "$migration" || exit
done < <(find sql/migrations -maxdepth 1 -type f -name '*.sql' -print | LC_ALL=C sort)
sudo -u postgres psql -X -v ON_ERROR_STOP=1 \
  -v repull_function_sha256="${AGENT_REPULL_FUNCTION_SHA256:?reviewed hash required}" \
  -d arb_warehouse -f sql/logo_admin_role.sql
sudo -u postgres psql -d arb_warehouse
```

At the `psql` prompt, set a unique SCRAM password without placing it in shell
history:

```text
\password logo_admin
\q
```

The role script revokes public execution of functions in `woo`/`fdm4` (including
the mutating `woo.refresh_product_state()` security-definer function) and
restores that refresh function to `etl_writer`. This closes indirect write paths;
grant any future read-only function explicitly to the callers that need it.
It also revokes the legacy `PUBLIC` create grant on the `public` schema because
PostgreSQL cannot deny that inherited privilege to only `logo_admin`; confirm no
unmanaged process relies on creating objects there before applying the script.

If objects in `fdm4` or `woo` are created by a database owner other than the
account that ran the role script, repeat the equivalent `ALTER DEFAULT
PRIVILEGES ... GRANT SELECT ... TO logo_admin` as that owner.

Verify least privilege manually:

```bash
psql "dbname=arb_warehouse user=logo_admin host=/var/run/postgresql"
```

The following should succeed/fail as shown:

```sql
SELECT count(*) FROM woo.store_product_state;                    -- succeeds
SELECT count(*) FROM fdm4.dec_design;                            -- succeeds
BEGIN;
UPDATE logo.store_settings SET updated_at = now() WHERE false;   -- succeeds
ROLLBACK;
UPDATE woo.store_product_state SET refreshed_at = now() WHERE false; -- permission denied
SELECT woo.refresh_product_state();                              -- permission denied
```

### 2. Configure local PostgreSQL authentication

Direct Unix-socket PostgreSQL is preferred. Ensure PostgreSQL uses SCRAM and put
this rule before any broader local `peer`/`trust` rule in `pg_hba.conf`:

```text
local   arb_warehouse   logo_admin   scram-sha-256
```

Reload PostgreSQL after editing. Never expose PostgreSQL port 5432 in the EC2
security group.

PgBouncer is not required for this local application. If it is deliberately
used, add `logo_admin` through the existing SCRAM `auth_file`/`auth_query`, map
`arb_warehouse`, retain transaction pooling, and point `DATABASE_DSN` at the
local PgBouncer socket/port. Do not add a public listener or a broad database
write role.

### 3. Deploy the WordPress broker

Deploy these WP files before starting the app:

- `wp-content/plugins/arb-admin/arb-logo-admin-api.php`
- the accompanying `arb-admin.php` include
- the already-built Phase A v3 product-sync and design-map changes

Confirm application passwords are enabled on the target environment for the
dedicated sync service account:

```bash
wp eval 'var_export(function_exists("wp_is_application_passwords_available") && wp_is_application_passwords_available());' --url=https://arb-dev.arborwear.com
```

Create the sync application password over HTTPS:

1. Operators need no setup - they sign in with their normal WordPress password.
2. Run the one-time setup command on the WordPress side (creates the
   super-admin `logo-sync-service` user + application password and prints the
   env lines):

   ```bash
   wp arb-logo-admin service-setup            # first run
   wp arb-logo-admin service-setup --regenerate   # rotate later
   ```
3. Put the printed `WP_SYNC_USER` / `WP_SYNC_APP_PASSWORD` lines in
   `/etc/arb-logo-admin.env`.

The WordPress broker routes are:

```text
POST /wp-json/arb/v1/logo-admin/login
GET  /wp-json/arb/v1/logo-admin/auth
POST /wp-json/arb/v1/logo-admin/sync
```

The category editor (arb-admin/arb-category-apply.php) adds, under
`/wp-json/arb/v1/logo-admin/categories/`: `blogs`, `export`, `status`,
`freeze`, `apply-terms`, `apply-memberships`, `finalize`, `restore` - all
app-password authenticated like sync, all listed in the Wordfence
app-password re-enable regexes.

The login route accepts an operator account password and returns identity only.
WordPress core performs application-password Basic authentication for the auth
and sync machine routes. The sync route resolves `fdm4_store` to an owned blog,
rebuilds `arb_logo_design_map`, and then calls the scoped Phase A reconcile.

### 4. Install the application service

```bash
sudo useradd --system --home /var/lib/arb-logo-admin --create-home --shell /usr/sbin/nologin arb-logo-admin
sudo install -d -o arb-logo-admin -g arb-logo-admin -m 0750 /var/lib/arb-logo-admin/uploads
sudo python3 -m venv /opt/arb-logo-admin-venv
sudo install -d -o arb-logo-admin -g arb-logo-admin -m 0700 /var/lib/arb-logo-admin/agent-uploads
sudo /opt/arb-logo-admin-venv/bin/pip install -r /opt/arb-logo-admin/requirements.txt
sudo install -o root -g root -m 0644 /opt/arb-logo-admin/logo-admin.service /etc/systemd/system/arb-logo-admin.service
sudo touch /etc/arb-logo-admin.env
sudo chown root:root /etc/arb-logo-admin.env
sudo chmod 0600 /etc/arb-logo-admin.env
sudoedit /etc/arb-logo-admin.env
sudo systemctl daemon-reload
sudo systemctl enable --now arb-logo-admin
sudo systemctl status arb-logo-admin
sudo journalctl -u arb-logo-admin -n 100 --no-pager
```

The service binds only to `127.0.0.1:8010`. Do not bind Uvicorn directly to a
public interface.

### 5. Put nginx/TLS in front

Terminate TLS at nginx and proxy the authenticated app to Uvicorn. Example:

```nginx
limit_req_zone $binary_remote_addr zone=logo_admin_login:10m rate=10r/m;

server {
    listen 443 ssl http2;
    server_name embroidery.arborwear.com;

    ssl_certificate     /etc/letsencrypt/live/embroidery.arborwear.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/embroidery.arborwear.com/privkey.pem;
    client_max_body_size 11m;

    # This location must be reachable by storefront customers if MEDIA_BASE
    # points at this hostname. Proxying lets the arb-logo-admin service retain
    # sole filesystem ownership; it intentionally has no admin-IP restriction.
    location ^~ /logo-media/ {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location = /login {
        limit_req zone=logo_admin_login burst=10 nodelay;
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Future deployment artifact lives in the repository at
    # logo-admin/deploy/nginx/agent-chat.conf. Install it at the exact path
    # below and include it inside this server block before `location /`.
    include /etc/nginx/snippets/arb-logo-admin-agent-chat.conf;

    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 420s;
    }
}
```

The assistant snippet disables proxy buffering, caching, and compression for
the authenticated POST SSE response so token events and 15-second heartbeats
reach the browser immediately. The checked-in snippet is a local deployment
artifact only; installing it and reloading nginx are deliberate future release
steps, not part of application startup.

Add HSTS only after the hostname and certificate are stable. The application
sets its own CSP and defensive response headers; keep nginx's MIME sniffing
protection on the public media location as shown.

### 6. Publish uploaded images

Uploaded images are first stored under `UPLOAD_DIR`. Choose one deployment model
explicitly:

- **Public nginx media path:** set `MEDIA_BASE` to this host's public
  `https://.../logo-media/` URL and allow storefront traffic to that location.
  The admin routes may still be restricted by an nginx IP allowlist, VPN, or a
  separate hostname, but the media path cannot inherit that restriction.
- **External media host (current deployment):** keep the admin EC2/security group
  private, set `MEDIA_BASE` to the public media prefix, and install
  `infra/publish-logo-media.sh` on its one-minute schedule. The publisher uses
  additive `rsync --ignore-existing` and never deletes or overwrites media.

If the EC2 security group allows HTTPS only from office/VPN addresses, uploaded
images on the same hostname will not render for customers. Existing absolute
media-server URLs remain valid and do not require this decision.

Do not delete uploaded files automatically when an assignment is removed; a
file may be shared by multiple assignments and is retained for audit/rollback.

### 7. Validate on dev

1. Log in with an operator's normal WordPress account password; confirm invalid
   credentials receive the same generic error and lockout protections apply.
2. Select a mapped store and style. Confirm the grid contains active warehouse
   garment colors and positions 1-3.
3. Exercise add, inline edit, soft disable, reactivate, hard delete, store
   settings, apply-to-all-colors, and copy-from-style.
4. Export CSV, re-import it, and confirm invalid rows appear in the import-report
   viewer instead of disappearing. Formula-leading text is apostrophe-escaped in
   exports and restored on import.
5. Test an existing absolute image URL and one uploaded image from an external,
   logged-out browser.
6. Click Sync Style. Confirm design-map and product reconcile statistics appear.
   A non-owned store must be refused before any WordPress mutation; migrate
   ownership explicitly, then retry.
7. Validate the storefront by garment color and place a test order. Confirm the
   order receives FDM4 art/design/color-scheme/location metadata.
8. Repeat with Sync Store only after the scoped pilot is correct.

### 8. Production rollout and rollback

- Back up `logo.assignment`, `logo.store_settings`, `logo.import_report`, and
  `UPLOAD_DIR` before the first production edit.
- Point the app at production WordPress only after dev validation.
- Add stores to `arb_logo_reconcile_blogs` one at a time; sync requests are
  rejected until that is done.
- Keep the legacy CSV writer available until each pilot passes storefront and
  order-stamping checks.
- To stop the admin immediately: `systemctl stop arb-logo-admin`.
- To stop projection for a store, remove its blog from the ownership option and
  stop scheduled/on-demand syncs; warehouse data remains intact.
- Rotate the service application password, database password, and session
  secret after any suspected exposure.

## Product feed (machine consumers)

External systems (Emblem ingest, a future Shopify adapter) consume the product
projection as a versioned delta feed served by this app:

- `GET /feed/version` - current generation: `{version, refreshed_at, active_rows}`.
- `GET /feed/products?since_version=N&limit=M` (`M` ≤ 5000) - state rows with
  `row_version > N` ordered by `row_version`, INCLUDING `is_active=false`
  tombstones; page with the returned `next_since_version` until it is `null`.
  Consumers own their cursor; replaying from 0 is always safe.
- `GET /feed/stores` - store inventory with blog ids/paths.

Auth is a per-consumer bearer token (`Authorization: Bearer <token>`) checked
against `woo.feed_consumer` - sha256 at rest, constant-time compare, no
session/CSRF. nginx exposes `/feed/` WITHOUT the operator IP allowlist
(`deploy/nginx/embroidery-feed-location.conf`); the token is the gate.
Registration/rotation is psql-only for now - the exact procedure is documented
in `sql/migrations/2026-08-02-feed-consumers.sql`.

After every successful pipeline refresh, `infra/feed-ping.sh` (fired by
`run_sync.sh`, fully non-fatal) POSTs `{"version", "generated_at"}` to each
active consumer's webhook `url`. The ping carries no secret and must be
treated as an untrusted hint: on receipt the consumer calls back
`GET /feed/version` with its own token and pulls from its stored cursor.
Delivery telemetry lands on `woo.feed_consumer`
(`last_ping_at/status`, `last_pull_at/version`).

## Operations

- Application logs: `journalctl -u arb-logo-admin`.
- Uploaded-file backup: `/var/lib/arb-logo-admin/uploads`.
- Private assistant spreadsheet staging: `/var/lib/arb-logo-admin/agent-uploads`
  (mode `0700`, short-lived, never exposed through `/logo-media`).
- Assistant release and rollback: `docs/agent-release-runbook.md`.
- Assignment and store-setting changes are audit-attributed through `updated_by`
  and `updated_at`; import-report details include the operator login. A hard
  delete cannot retain the deleted row's audit fields.
- Import failures are retained in `logo.import_report` and shown in the app.
- The application has no frontend build step and no CDN runtime dependency.
