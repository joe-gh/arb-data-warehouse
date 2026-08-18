-- Per-row shopper-facing name override for logo assignments. When set (non-
-- empty), the WP logo reconcile shows this exact name on the site, overriding
-- the design display-name resolution entirely. NULL/'' = unset (normal
-- resolution: display_name per scheme -> design default -> FDM4 art
-- description -> logo code).
--
-- Nullable ON PURPOSE: bulk-apply undo restores rows via
-- jsonb_populate_record, and pre-existing batch snapshots lack this key
-- (NULL); a NOT NULL column would make old undos fail.
BEGIN;

ALTER TABLE logo.assignment
    ADD COLUMN IF NOT EXISTS name_override text;

COMMIT;
