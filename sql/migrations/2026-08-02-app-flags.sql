-- ============================================================================
-- Generic app flags for warehouse-side feature gates.
--
-- woo.app_flag holds named boolean switches read by the transform (and any
-- other warehouse-side consumer). An ABSENT row means OFF - this migration
-- deliberately inserts nothing, so every gated feature ships dark.
--
-- First consumer: 'pim_projection' (PIM phase 2). Enable procedure:
--   1. On PROD WP, turn the 'product_sync_pim_content' feature flag ON first
--      (arb-admin/arb-feature-flags.php or ARB_FLAG_PRODUCT_SYNC_PIM_CONTENT).
--      This is inert while no payload carries a 'pim' object.
--   2. On the warehouse:
--        INSERT INTO woo.app_flag (name, enabled, note, updated_by)
--        VALUES ('pim_projection', true, 'PIM phase 2 projection', '<who>')
--        ON CONFLICT (name) DO UPDATE SET enabled = true, updated_at = now();
--   3. The next hourly refresh adds 'pim' objects to enriched parent rows
--      (structural_hash version-bumps), and the engine applies the content in
--      the same reconcile pass. Verify: inspect a payload's pim object, run a
--      one-store dry-run, then let the gated reconcile execute.
--   Rollback: UPDATE woo.app_flag SET enabled = false WHERE name = 'pim_projection';
--   (next refresh removes the pim objects; hashes revert; engine flag may stay
--   on - it is inert without payload data.)
--
-- Deploy order (hard): apply this migration BEFORE re-applying
-- sql/woo_transform.sql - the transform probes woo.app_flag on every refresh.
--
-- Apply as a database owner:
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -d arb_warehouse \
--     -f 2026-08-02-app-flags.sql
-- Safe to reapply.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS woo.app_flag (
    name       text        NOT NULL PRIMARY KEY,
    enabled    boolean     NOT NULL DEFAULT false,
    note       text        NOT NULL DEFAULT '',
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON woo.app_flag TO logo_admin;
GRANT SELECT ON woo.app_flag TO woo_reader, insights_reader;

COMMIT;
