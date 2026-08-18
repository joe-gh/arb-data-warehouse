-- Allow 'both' as a garment color classification: colors that should receive
-- a logo whether the bulk-apply targets light OR dark garments (bulk-apply
-- matches light_dark IN (target, 'both')).
BEGIN;

ALTER TABLE logo.color_class DROP CONSTRAINT color_class_light_dark_check;
ALTER TABLE logo.color_class
    ADD CONSTRAINT color_class_light_dark_check
    CHECK (light_dark IN ('light', 'dark', 'both'));

COMMIT;
