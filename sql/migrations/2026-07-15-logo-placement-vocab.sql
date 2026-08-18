-- Canonical placement vocabulary for the Logo Admin placement picker.
--
-- Seeded from FDM4's OWN naming: the distinct `location` values the live
-- FDM4 design-mapping feed pushed into WordPress (arb_fdm4_design_map,
-- captured 2026-07-15). This is "the valid list in FDM4" - assignments should
-- use these names so per-placement design resolution keys match. Free text is
-- still allowed at write time; this table only drives the dropdown.
--
-- To refresh after FDM4 adds placements, re-run the distinct-location query
-- against arb_fdm4_design_map on WordPress and INSERT ... ON CONFLICT DO
-- NOTHING here (or add rows manually).
BEGIN;

CREATE TABLE IF NOT EXISTS logo.placement_vocab (
    name       text          NOT NULL PRIMARY KEY,
    source     text          NOT NULL DEFAULT 'fdm4-feed',
    active     boolean       NOT NULL DEFAULT true,
    created_at timestamptz   NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON logo.placement_vocab TO logo_admin, etl_writer;
GRANT SELECT ON logo.placement_vocab TO woo_reader, insights_reader;

INSERT INTO logo.placement_vocab (name) VALUES
    ('LEFT CHEST'),
    ('LEFT CHEST SWITCH OK'),
    ('RIGHT CHEST'),
    ('RIGHT CHEST SWITCH OK'),
    ('LEFT CHEST ABOVE POCKET'),
    ('RIGHT CHEST ABOVE POCKET'),
    ('BICEP LEFT SLEEVE'),
    ('BICEP RIGHT SLEEVE'),
    ('LEFT SHORT SLEEVE'),
    ('RIGHT SHORT SLEEVE'),
    ('FULL BACK'),
    ('BRIM HAT - CENTER FRONT'),
    ('ON THE POCKET'),
    ('CENTER CHEST'),
    ('CENTER FULL BACK'),
    ('KNIT CAP - CENTER FRONT'),
    ('BRIM HAT - FRONT LEFT'),
    ('DUFFLE BAG FRONT'),
    ('FULL CENTER FRONT'),
    ('TOTE BAG FRONT'),
    ('BRIM HAT - FRONT RIGHT'),
    ('BRIM HAT - CENTER BACK'),
    ('CENTER BACK NECK'),
    ('RIGHT LONG SLEEVE'),
    ('DUFFLE BAG BACK'),
    ('BACKPACK BAG FRONT'),
    ('LEFT LONG SLEEVE'),
    ('FRONT LEFT THIGH'),
    ('RIGHT THIGH (OK Switch LEFT)'),
    ('*SEE PROOF*'),
    ('BLANKET FRONT CORNER'),
    ('BRIM FLEX FIT HAT - CENTER BACK'),
    ('LEFT THIGH (OK Switch RIGHT)'),
    ('BACK RIGHT CALF'),
    ('HT BRIM HAT CENTER FRONT'),
    ('HIVIZ REFLECTIVE FULL BACK')
ON CONFLICT (name) DO NOTHING;

COMMIT;
