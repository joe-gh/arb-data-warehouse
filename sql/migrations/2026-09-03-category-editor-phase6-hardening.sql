-- Category editor hardening (2026-09-03).
--
-- previous_slug: when a store extra's slug is changed in the draft, the live
-- term still carries the old slug. Recording it lets the planner converge that
-- term IN PLACE (term_id kept, redirect on blog 1) instead of creating a new
-- term beside the old one.
ALTER TABLE catmgr.node_store_override
    ADD COLUMN IF NOT EXISTS previous_slug text
        CHECK (previous_slug IS NULL OR previous_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$');
