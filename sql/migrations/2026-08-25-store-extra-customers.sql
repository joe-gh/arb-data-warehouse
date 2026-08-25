-- Some stores' FDM4 designs live under a DIFFERENT customer number than the
-- webstore's own (FDM4 keeps a separate design account): Southview's store is
-- 035340 but its 66 designs belong to 033403; Lewis is 001114 with designs
-- under 003101. The app's design-ownership gate only accepted the store's own
-- derived customer, so every such design failed validation with a misleading
-- "no color scheme" error. extra_customers extends the gate per store.
BEGIN;

ALTER TABLE logo.store_settings
    ADD COLUMN IF NOT EXISTS extra_customers text[] NOT NULL DEFAULT '{}';

INSERT INTO logo.store_settings (fdm4_store, extra_customers, updated_by)
VALUES ('S_035340', ARRAY['033403'], 'migration-2026-08-25'),
       ('S_001114', ARRAY['003101'], 'migration-2026-08-25')
ON CONFLICT (fdm4_store) DO UPDATE
    SET extra_customers = EXCLUDED.extra_customers,
        updated_by      = EXCLUDED.updated_by,
        updated_at      = now();

COMMIT;
