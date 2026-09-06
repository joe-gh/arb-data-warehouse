-- Product mix: refuse, at the data layer, to commit an empty list for an
-- active list-mode store.
--
-- The app checks "did that leave zero items?" after its DELETE and rolls back
-- if so, but under READ COMMITTED two concurrent removals of the two last
-- distinct styles each still see the other's uncommitted row, both count one
-- remaining item, and both commit - leaving the list empty. The transform's
-- EXISTS guard then skips the filter entirely for that store, so the next
-- hourly refresh projects the FULL FDM4 assortment back onto the store.
--
-- The app now takes a per-store row lock before every mix write (mix_service.
-- lock_store), which serializes those two removals. This trigger is the
-- second line: any path that empties an active list-mode store's item list -
-- app, MCP, assistant apply, hand-run SQL - fails at COMMIT.
--
-- DEFERRABLE INITIALLY DEFERRED so the legitimate empty-then-refill flows
-- (enable/switch-to-list seeding, import mode='reset') still work: the list is
-- only required to be non-empty at commit time, not between statements.
BEGIN;

CREATE OR REPLACE FUNCTION woo.mix_items_nonempty() RETURNS trigger AS $fn$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM woo.store_mix_store m
         WHERE m.fdm4_store = OLD.fdm4_store
           AND m.active
           AND m.mode = 'list'
    ) AND NOT EXISTS (
        SELECT 1 FROM woo.store_mix_item i
         WHERE i.fdm4_store = OLD.fdm4_store
    ) THEN
        RAISE EXCEPTION
            'product mix for % is empty while the store is an active custom '
            'list; an empty list makes the transform fall back to the full '
            'FDM4 assortment. Disable the override instead',
            OLD.fdm4_store
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $fn$ LANGUAGE plpgsql;
-- Trigger functions are invoked by the trigger machinery, not by callers, so
-- no role needs EXECUTE (the woo.audit_* trigger functions are the same).
-- PUBLIC EXECUTE would also put it in the app role's pinned callable
-- inventory.
REVOKE EXECUTE ON FUNCTION woo.mix_items_nonempty() FROM PUBLIC;

DROP TRIGGER IF EXISTS store_mix_item_nonempty ON woo.store_mix_item;
CREATE CONSTRAINT TRIGGER store_mix_item_nonempty
    AFTER DELETE ON woo.store_mix_item
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION woo.mix_items_nonempty();

COMMIT;
