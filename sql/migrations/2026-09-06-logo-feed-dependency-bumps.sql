-- Advance the logo feed cursor when a served dependency changes (audit F-16).
--
-- /feed/logos does not serve logo.assignment alone: logo_name comes from
-- logo.display_name (per-store row, then the global row) and price comes from
-- logo.default_cost. Editing either table changed what the feed serves without
-- changing logo.assignment.row_version, so a consumer that had already paged
-- to the ceiling kept the old name or the old cost forever.
--
-- Fix: an AFTER trigger on each dependency touches the assignments whose
-- served value can have changed. The touch is a no-op UPDATE (updated_at set
-- to its own value) purely so the BEFORE trigger from
-- 2026-08-03-logo-feed-versioning.sql re-stamps row_version; no business
-- column moves. Assignments that override the value locally (name_override,
-- cost_override) are excluded: nothing served for them changed.
--
-- Consequences, accepted deliberately:
--   * logo_assignment_audit writes one 'assignment_updated' row per bumped
--     assignment, whose only change is row_version.
--   * The undo kernel compares state with row_version stripped
--     (snapshots.TRIGGER_MANAGED_COLUMNS), and updated_at/updated_by do not
--     move here, so a bump can never turn into a drift refusal.
--   * A repull or bulk rename touching many names now touches the matching
--     assignments as well; the work is proportional to the assignments that
--     actually serve those names.
--
-- Match semantics mirror the joins /feed/logos uses, deliberately as a
-- superset so a bump can never be missed. design_id stays a plain column in
-- the predicate (logo_assign_design indexes it; an expression there once cost
-- hours in the transform), with the normalization on the trigger row's side.
BEGIN;

CREATE OR REPLACE FUNCTION logo.display_name_feed_bump() RETURNS trigger AS $fn$
DECLARE
    r logo.display_name%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    UPDATE logo.assignment a
       SET updated_at = a.updated_at
     WHERE a.design_id IN (r.design_id, btrim(r.design_id))
       AND upper(btrim(a.color_scheme_id)) = upper(btrim(r.color_scheme_id))
       -- A global row ('') can be the served name for any store; a per-store
       -- row only affects its own store.
       AND (r.fdm4_store = '' OR a.fdm4_store = r.fdm4_store)
       AND COALESCE(a.name_override, '') = '';
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION logo.default_cost_feed_bump() RETURNS trigger AS $fn$
DECLARE
    r logo.default_cost%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    UPDATE logo.assignment a
       SET updated_at = a.updated_at
     WHERE upper(btrim(a.logo_code)) = upper(btrim(r.logo_code))
       AND upper(btrim(a.color_scheme_id)) = upper(btrim(r.color_scheme_id))
       AND a.cost_override IS NULL;
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS display_name_feed_bump ON logo.display_name;
CREATE TRIGGER display_name_feed_bump
    AFTER INSERT OR UPDATE OF name OR DELETE ON logo.display_name
    FOR EACH ROW EXECUTE FUNCTION logo.display_name_feed_bump();

DROP TRIGGER IF EXISTS default_cost_feed_bump ON logo.default_cost;
CREATE TRIGGER default_cost_feed_bump
    AFTER INSERT OR UPDATE OF cost OR DELETE ON logo.default_cost
    FOR EACH ROW EXECUTE FUNCTION logo.default_cost_feed_bump();

-- The triggers run with the writer's own privileges, and every writer of
-- logo.display_name / logo.default_cost already updates logo.assignment.
REVOKE EXECUTE ON FUNCTION logo.display_name_feed_bump() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION logo.default_cost_feed_bump() FROM PUBLIC;

COMMIT;
