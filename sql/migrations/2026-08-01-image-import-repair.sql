-- Permit Warehouse Operations to repair a stale source-url mapping after the
-- corresponding mirrored file is lost or found inconsistent.
BEGIN;

GRANT UPDATE ON logo.image_import TO logo_admin;

COMMIT;
