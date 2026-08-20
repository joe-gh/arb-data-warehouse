# Cron schedules

Source of truth for the live cron jobs that drive the pipeline. These are **not**
auto-applied - install them by hand (`crontab -e` for the correct user on each host).

## Warehouse box (EC2 `fdm4-warehouse`) - root crontab

```cron
# Hourly live FDM4 pull -> load -> transform (~7.5 min).
# flock = no overlap; timeout = hard cap.
0 * * * *   /usr/bin/flock -n /tmp/fdm4-pull.lock /usr/bin/timeout 3300 /bin/bash /opt/fdm4-extractor/run_sync.sh >> /opt/fdm4-extractor/run_sync.log 2>&1

# Reaper (reap.sh): kill any pull/load wedged past the 55m cap AND flip its
# dangling 'running' control row to 'failed'. The mark-failed step closes the
# gap where SIGKILL bypasses run_sync.sh's EXIT trap (which would otherwise leave
# the row 'running' until the daily prune). Runs every 10m so stale rows clear fast.
*/10 * * * * /bin/bash /opt/fdm4-extractor/reap.sh >/dev/null 2>&1

# Daily prune: backstop fail of any 'running' row >6h + delete control rows >30d.
# (reap.sh now handles the fast fail-stale path; this is the long-tail cleanup.)
17 5 * * *  sudo -u postgres psql -d arb_warehouse -tAc "UPDATE woo.sync_control SET status='failed', error=COALESCE(error,'stale running') WHERE status='running' AND started_at < now()-interval '6 hours'; DELETE FROM woo.sync_control WHERE requested_at < now()-interval '30 days'" >/dev/null 2>&1
```

`reap.sh` lives in this repo at `infra/reap.sh` (deployed to `/opt/fdm4-extractor/reap.sh`).

## Warehouse box - `ubuntu` crontab (media pipeline)

```cron
# :35 Publish new PIM originals to S3 (reads pim.product_state, subtracts pim.media_object).
35 * * * * /usr/bin/flock -n /tmp/media-publish.lock /usr/bin/timeout 3000 /usr/bin/python3 /opt/fdm4-extractor/publish-product-media.py --workers 2 >> /home/ubuntu/media-publish-cron.log 2>&1

# :55 Generate any pending renditions (WHERE generated=false; partial index makes idle runs free).
55 * * * * /usr/bin/flock -n /tmp/media-generate.lock /usr/bin/timeout 3000 nice -n 10 /usr/bin/python3 /opt/fdm4-extractor/generate-renditions.py --workers 4 >> /home/ubuntu/media-generate-cron.log 2>&1
```

The middle step of the hourly cascade (`:45` incremental rendition export) runs on
the **production WordPress box** `ubuntu` crontab - it needs WP-CLI and the blog
tables. Each stage self-heals if the previous one hasn't run yet
(miss → `_arb_cdn_unresolved` marker → weekly retry; empty pending set → no-op):

```cron
# :45 Export rendition definitions for NEW attachments only (no stamp, no marker).
45 * * * * /usr/bin/flock -n /home/ubuntu/.media-export.lock /usr/bin/timeout 3000 sudo -u www-data php -d max_execution_time=0 /usr/local/bin/wp --path=/var/www/arborwear arb media-rendition-export --blogs=all --incremental >> /home/ubuntu/media-export-cron.log 2>&1

# Sun 03:12 Retry unresolved attachments whose markers are older than 7 days.
12 3 * * 0 /usr/bin/flock -n /home/ubuntu/.media-export.lock /usr/bin/timeout 7200 sudo -u www-data php -d max_execution_time=0 /usr/local/bin/wp --path=/var/www/arborwear arb media-rendition-export --blogs=all --retry-unresolved >> /home/ubuntu/media-export-cron.log 2>&1
```

## Production WordPress box - `www-data` crontab

```cron
# Gated full reconcile every 15 min. flock = no overlap; timeout 14400 (4h) = reaper.
*/15 * * * * /usr/bin/flock -n /var/www/arborwear/wp-content/private-logs/.product-sync.lock /usr/bin/timeout 14400 /bin/bash /usr/local/bin/arb-hourly-reconcile.sh >> /var/www/arborwear/wp-content/private-logs/hourly-reconcile.log 2>&1
```

> `arb-hourly-reconcile.sh` and the `arb-product-sync` WordPress plugin live in the
> main WordPress repo, not here. The reconcile is **gated**: it runs only when a newer
> successful pull has landed (tracked in `woo.sync_control`).

## Log rotation

`/etc/logrotate.d/arb-sync` on both boxes: `weekly`, `rotate 4`, `compress`,
`copytruncate` on the run/reconcile logs.

## PIM (Sales Layer) content pull (root crontab, added 2026-08-20)

    25 * * * *  flock -n /tmp/pim-pull.lock timeout 1500 bash -c '. /opt/fdm4-extractor/pim.env; export PIM_API_KEY; sudo -E -u postgres /opt/fdm4-extractor/venv/bin/python /opt/fdm4-extractor/pull_pim.py' >> /opt/fdm4-extractor/pull_pim.log
    40 3 * * 0  same with --full (weekly deletion reconciliation)

Key lives in root-only /opt/fdm4-extractor/pim.env. Log rotated weekly via
/etc/logrotate.d/arb-pim-pull. Landing tables pim.api_* are isolated: nothing
downstream reads them until the projection wiring step.
