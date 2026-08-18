-- Catalog scoping for logo assignments.
--
-- FDM4 shares customer codes (fdm4_store) across webstores in a way our
-- platform does not: an FDM4 CATALOG is effectively our store. S_002384 serves
-- two webstores (public-web on blog 1, demowebstore on blog 46), and the
-- sheet-mirror imported the demo webstore's sheet at store grain - so catalog-
-- pinned consumers (Emblem) saw the demo logos on public-web.
--
-- Model: catalog_id NULL = the assignment applies to EVERY catalog/webstore of
-- the store (the correct meaning for all 75 single-catalog stores); an explicit
-- catalog_id scopes the row to that one webstore. The backfill below tags the
-- demo webstore's imported rows; the UPDATE fires assignment_feed_stamp, so
-- every touched row re-versions above the current ceiling and catalog-aware
-- consumers converge on their next pull.
BEGIN;

ALTER TABLE logo.assignment ADD COLUMN IF NOT EXISTS catalog_id text;
CREATE INDEX IF NOT EXISTS assignment_catalog ON logo.assignment (catalog_id)
    WHERE catalog_id IS NOT NULL;

-- The S_002384 assignment set came verbatim from /Demo/demo-webstore.csv
-- (import batch sheet-mirror-20260730): it belongs to the demo webstore.
UPDATE logo.assignment
   SET catalog_id = 'S_002384_demowebstore'
 WHERE fdm4_store = 'S_002384'
   AND catalog_id IS NULL;

COMMIT;
