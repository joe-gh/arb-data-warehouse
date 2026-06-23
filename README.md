# arb-data-warehouse

Arborwear data warehouse: the **FDM4 (Progress OpenEdge) → PostgreSQL → WooCommerce**
product-sync pipeline, plus the infrastructure and SQL that runs it. It runs on a
dedicated EC2 instance (`fdm4-warehouse`) and feeds the WooCommerce multisite's
product catalog. The same warehouse is intended to host future Insights/reporting.

## What this is

Replaces FDM4's WooCommerce REST product feed with a **reconcile-don't-replay** pipeline:

1. **Pull** — `pull_fdm4.py` streams FDM4 `PUB` tables to CSV over JDBC.
2. **Load** — `infra/load_dump.py` loads the CSVs into Postgres schema `fdm4.*`
   (raw, all-TEXT) and rebuilds the Woo-facing desired-state.
3. **Transform** — `sql/woo_transform.sql` (`woo.refresh_product_state()`) turns the
   raw mirror into `woo.store_product_state`, keyed by `(fdm4_store, catalog_id, sku)`.
4. **Reconcile** — a WordPress plugin (`arb-product-sync`, in the main WordPress repo)
   reads the desired-state and reconciles WooCommerce, touching only what changed.

The two halves coordinate through `woo.sync_control` (`sql/sync_control.sql`): the
warehouse records each pull's version; WordPress gates its reconcile on a new
successful pull.

See `docs/ARB_ODBC_PIPELINE_AND_PROTECTIONS.md` for the full architecture, diagrams,
and protection mechanisms.

## Layout

| Path | Purpose |
|------|---------|
| `pull_fdm4.py` | Stage 1 — JDBC pull of FDM4 `PUB` tables → `dump/*.csv` |
| `explore.py` | JDBC connection + exploration harness (shared by the puller) |
| `product_pull.py` | Ad-hoc single-product pull helper |
| `infra/load_dump.py` | Stage 2 — load CSVs → `fdm4.*` + call the transform |
| `infra/run_sync.sh` | Orchestrates pull → load → transform on the box; writes `woo.sync_control` |
| `infra/provision.sh` | Provision the EC2 warehouse + EIP + security group |
| `infra/cloud-init.sh` | First-boot setup (Postgres + dependencies) |
| `infra/backup-plan.json` | AWS Backup plan (GFS: daily-7d / weekly-28d / monthly-180d) |
| `infra/crontab.md` | The live cron schedules (warehouse + production) |
| `sql/woo_transform.sql` | Stage 3 — desired-state transform |
| `sql/sync_control.sql` | Coordination table DDL + grants |
| `sql/diagnostics/` | Ad-hoc verification / diagnostic queries |
| `docs/` | Architecture + operations reference |

## Prerequisites (not in the repo)

- **JDBC driver** — the DataDirect OpenEdge driver (`openedge.jar`). Licensed vendor
  binary, gitignored. Place it in the repo root (or point the harness at it) before
  running the puller.
- **Credentials** — an env file (e.g. `fdm4.env`) with the FDM4 connection details,
  referenced via the `ARB_DBTEST_ENV` environment variable. Gitignored — never commit
  credentials. See `explore.py` (`load_env()`) for the expected keys.
- **Python** — mirror the venv used on the box (`/opt/fdm4-extractor/venv`); deps
  include `jaydebeapi` and `psycopg2`.

## Running

Full pipeline on the warehouse box (pull → load → transform):

```bash
sudo bash infra/run_sync.sh
```

Just the pull (to CSV):

```bash
ARB_DBTEST_ENV=./fdm4.env python pull_fdm4.py
```

(Re)apply the transform definition:

```bash
sudo -u postgres psql -d arb_warehouse -f sql/woo_transform.sql
```

## Deployment & operations

- The box runs the pipeline from `/opt/fdm4-extractor`.
- `infra/crontab.md` is the source of truth for the schedules (hourly pull, gated
  reconcile, reaper, prune). They are installed by hand per host.
- Backups are handled by AWS Backup using `infra/backup-plan.json` (the instance is
  tagged `backup=daily`). The warehouse data is fully derived from FDM4, so a restore
  loses at most the time since the last snapshot — the next pull rebuilds current state.
