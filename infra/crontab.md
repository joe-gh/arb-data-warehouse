# Cron schedules

Source of truth for the live cron jobs that drive the pipeline. These are **not**
auto-applied — install them by hand (`crontab -e` for the correct user on each host).

## Warehouse box (EC2 `fdm4-warehouse`) — root crontab

```cron
# Hourly live FDM4 pull -> load -> transform (~7.5 min).
# flock = no overlap; timeout = hard cap.
0 * * * *   /usr/bin/flock -n /tmp/fdm4-pull.lock /usr/bin/timeout 3300 /bin/bash /opt/fdm4-extractor/run_sync.sh >> /opt/fdm4-extractor/run_sync.log 2>&1

# Reaper: kill any pull/load wedged past 3300s.
*/10 * * * * ps -eo pid,etimes,args | awk '/[r]un_sync.sh|[p]ull_fdm4|[l]oad_dump/ && $2>3300 {print $1}' | xargs -r kill -9

# Prune: fail stale 'running' control rows >6h + delete control rows >30d.
17 5 * * *  sudo -u postgres psql -d arb_warehouse -tAc "UPDATE woo.sync_control SET status='failed', error=COALESCE(error,'stale running') WHERE status='running' AND started_at < now()-interval '6 hours'; DELETE FROM woo.sync_control WHERE requested_at < now()-interval '30 days'" >/dev/null 2>&1
```

## Production WordPress box — `www-data` crontab

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
