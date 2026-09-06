-- Retirement marker for the Sales Layer api2 landing tables.
--
-- infra/pull_pim.py only ever upserted, so a record deleted upstream stayed in
-- the mirror for ever even though the weekly `--full` run is documented as the
-- deletion reconciliation (infra/crontab.md). A stale variant row also hides a
-- legitimate re-create from push_pim's anti-joins.
--
-- retired_at is NULL for a live record and carries the time the first full pull
-- that no longer saw it ran. Nothing is deleted: a record that comes back is
-- simply set live again by the next pull that sees it, and the history of what
-- went missing stays readable.
--
-- Apply as postgres on the warehouse box BEFORE the next weekly full pull:
--   sudo -u postgres psql -d arb_warehouse -f 2026-09-06-pim-api-retire.sql

BEGIN;

ALTER TABLE pim.api_product ADD COLUMN IF NOT EXISTS retired_at timestamptz;
ALTER TABLE pim.api_variant ADD COLUMN IF NOT EXISTS retired_at timestamptz;
ALTER TABLE pim.api_image   ADD COLUMN IF NOT EXISTS retired_at timestamptz;

-- push_pim.py and the name backfill look up live variants by ref; the partial
-- index keeps that anti-join on the live set only.
CREATE INDEX IF NOT EXISTS api_variant_live_ref
    ON pim.api_variant (frmt_ref)
    WHERE retired_at IS NULL;

COMMIT;
