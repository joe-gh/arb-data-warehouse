INSERT INTO woo.store_catalog (fdm4_store, catalog_id, products, suggested)
VALUES
    ('S_TEST', 'S_TEST_catalog', 6, true),
    ('S_EMPTY', 'S_EMPTY_catalog', 3, true);

INSERT INTO woo.store_product_state (
    fdm4_store, catalog_id, sku, kind, style_code, parent_sku,
    name, status, color_code, color, size_code, size, price, stock,
    payload, content_hash, is_active
) VALUES
    ('S_TEST', 'S_TEST_catalog', 'STYLE-1', 'parent', 'STYLE-1', NULL,
     'Style One', 'publish', NULL, NULL, NULL, NULL, 10, 1,
     '{}'::jsonb, 'parent-1', true),
    ('S_TEST', 'S_TEST_catalog', 'STYLE-1-RED', 'variation', 'STYLE-1', 'STYLE-1',
     'Style One Red', 'publish', 'RED', 'Red', 'M', 'Medium', 10, 1,
     '{}'::jsonb, 'variation-1', true),
    ('S_TEST', 'S_TEST_catalog', 'STYLE-1-BLU', 'variation', 'STYLE-1', 'STYLE-1',
     'Style One Blue', 'publish', 'BLU', 'Blue', 'M', 'Medium', 10, 1,
     '{}'::jsonb, 'variation-2', true),
    ('S_TEST', 'S_TEST_catalog', 'STYLE-1-GRN', 'variation', 'STYLE-1', 'STYLE-1',
     'Style One Green', 'publish', 'GRN', 'Green', 'M', 'Medium', 10, 1,
     '{}'::jsonb, 'variation-3', true),
    ('S_TEST', 'S_TEST_catalog', 'STYLE-2', 'parent', 'STYLE-2', NULL,
     'Style Two', 'publish', NULL, NULL, NULL, NULL, 12, 1,
     '{}'::jsonb, 'parent-2', true),
    ('S_TEST', 'S_TEST_catalog', 'STYLE-2-RED', 'variation', 'STYLE-2', 'STYLE-2',
     'Style Two Red', 'publish', 'RED', 'Red', 'M', 'Medium', 12, 1,
     '{}'::jsonb, 'variation-4', true);

INSERT INTO fdm4.dec_design (
    design_id, description, web_description, methods_used,
    design_categ_id, cust_number
) VALUES
    ('DESIGN-1', 'Test logo', 'Test Logo', 'EMB', 'LOGO', 'TEST'),
    ('DESIGN-2', 'Second logo', 'Second Logo', 'EMB', 'LOGO', 'TEST'),
    -- Deliberate namespace collision: DESIGN-1 maps to art ART-9001,
    -- which is also another customer's design ID. Resolvers must follow
    -- design_pool and must never emit this colliding art ID as the design.
    ('ART-9001', 'Colliding design', 'Wrong customer logo', 'EMB', 'LOGO', 'OTHER'),
    ('B9H-TEST-DESIGN', 'B9H test logo', NULL, NULL, NULL, NULL);

INSERT INTO fdm4.design_pool (design_pool_num, design_id, art_id)
VALUES ('9001', 'DESIGN-1', 'ART-9001');

INSERT INTO fdm4.cust_art_file (
    art_id, color_scheme_id, resource_type, target_web_path, target_filename
) VALUES
    ('ART-9001', 'SCHEME-1', 'PREVIEW', 'test/design-1.png', 'C1_SCHEME-1.png'),
    ('DESIGN-2', 'SCHEME-2', 'PREVIEW', 'test/design-2.png', 'C2_SCHEME-2.png'),
    ('B9H-TEST-DESIGN', 'WH', 'PREVIEW', 'logos/B9H_WH.png', 'B9H_WH.png');

INSERT INTO logo.display_name (
    design_id, color_scheme_id, name, source, locked, uses,
    fdm4_description, updated_by, fdm4_store
) VALUES
    ('DESIGN-1', 'SCHEME-1', 'Global test logo', 'seed', false, 2,
     'Test Logo', 'seed', ''),
    ('DESIGN-1', 'SCHEME-1', 'Store test logo', 'manual', true, 1,
     'Test Logo', 'seed', 'S_TEST'),
    ('DESIGN-1', 'SCHEME-1', 'Other store logo', 'manual', true, 1,
     'Test Logo', 'seed', 'S_OTHER');

INSERT INTO logo.placement_vocab (name, active)
VALUES ('Left Chest', true), ('Right Chest', true)
ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active;

INSERT INTO logo.assignment (
    fdm4_store, product_style, garment_color_code, option_row, position,
    design_id, logo_code, color_scheme_id, location, optional,
    background, cost_override, sort_order, image_url, name_override, active,
    updated_by
) VALUES
    ('S_TEST', 'STYLE-1', 'RED', 1, 1, 'DESIGN-1', 'C1', 'SCHEME-1',
     'Left Chest', false, '', NULL, 0, '', 'Shopper-facing test name', true, 'seed'),
    ('S_TEST', 'STYLE-1', 'RED', 1, 2, 'DESIGN-2', 'C2', 'SCHEME-2',
     'Right Chest', false, '', NULL, 1, '', NULL, true, 'seed');

INSERT INTO logo.store_settings (
    fdm4_store, enabled, allows_none, updated_by
) VALUES ('S_TEST', true, false, 'seed');

INSERT INTO woo.pricing_tier (
    tier_name, price_levels_key, is_msrp, sort_order
) VALUES
    ('MSRP', 'msrp', true, 0),
    ('Corporate', 'corp', false, 1);

INSERT INTO woo.store_pricing_tier (fdm4_store, tier_name, note)
VALUES ('S_TEST', 'Corporate', 'fixture');

INSERT INTO woo.price_rule (
    name, active, priority, stackable, effect_type, effect_value, updated_by
) VALUES ('Fixture inactive rule', false, 100, false, 'percent', 5, 'seed');

INSERT INTO woo.sync_exclusion (
    fdm4_store, style_code, note, active, updated_by
) VALUES ('S_EMPTY', '', 'fixture disabled block', false, 'seed');

-- Product-mix fixtures: S_MIXED is a list-mode store where the operator kept
-- only MIX-1's RED channel (L excluded) and removed MIX-2 entirely; MIX-3 is
-- FDM4 drift (candidate only, not yet in state). S_ALLMODE follows FDM4.
INSERT INTO woo.store_catalog (fdm4_store, catalog_id, products, suggested)
VALUES
    ('S_MIXED', 'S_MIXED_catalog', 2, true),
    ('S_ALLMODE', 'S_ALLMODE_catalog', 1, true);

INSERT INTO woo.store_product_state (
    fdm4_store, catalog_id, sku, kind, style_code, parent_sku,
    name, status, color_code, color, size_code, size, price, stock,
    payload, content_hash, is_active
) VALUES
    ('S_MIXED', 'S_MIXED_catalog', 'MIX-1', 'parent', 'MIX-1', NULL,
     'Mixed One', 'publish', NULL, NULL, NULL, NULL, 20, 1,
     '{}'::jsonb, 'mix-parent-1', true),
    ('S_MIXED', 'S_MIXED_catalog', 'MIX-1-RED-M', 'variation', 'MIX-1', 'MIX-1',
     'Mixed One Red M', 'publish', 'RED', 'Red', 'M', 'Medium', 20, 1,
     '{}'::jsonb, 'mix-var-1', true),
    ('S_MIXED', 'S_MIXED_catalog', 'MIX-1-RED-L', 'variation', 'MIX-1', 'MIX-1',
     'Mixed One Red L', 'publish', 'RED', 'Red', 'L', 'Large', 20, 1,
     '{}'::jsonb, 'mix-var-2', false),
    ('S_MIXED', 'S_MIXED_catalog', 'MIX-2', 'parent', 'MIX-2', NULL,
     'Mixed Two', 'publish', NULL, NULL, NULL, NULL, 22, 1,
     '{}'::jsonb, 'mix-parent-2', false),
    ('S_MIXED', 'S_MIXED_catalog', 'MIX-2-BLU-M', 'variation', 'MIX-2', 'MIX-2',
     'Mixed Two Blue M', 'publish', 'BLU', 'Blue', 'M', 'Medium', 22, 1,
     '{}'::jsonb, 'mix-var-3', false),
    ('S_ALLMODE', 'S_ALLMODE_catalog', 'ALL-1', 'parent', 'ALL-1', NULL,
     'All One', 'publish', NULL, NULL, NULL, NULL, 30, 1,
     '{}'::jsonb, 'all-parent-1', true),
    ('S_ALLMODE', 'S_ALLMODE_catalog', 'ALL-1-GRN-M', 'variation', 'ALL-1', 'ALL-1',
     'All One Green M', 'publish', 'GRN', 'Green', 'M', 'Medium', 30, 1,
     '{}'::jsonb, 'all-var-1', true);

INSERT INTO woo.store_mix_store (
    fdm4_store, mode, active, note, created_by, updated_by, imported_at
) VALUES
    ('S_MIXED', 'list', true, 'fixture list-mode store', 'seed', 'seed', now()),
    ('S_ALLMODE', 'all', true, 'fixture all-mode store', 'seed', 'seed', NULL);

INSERT INTO woo.store_mix_item (
    fdm4_store, style_code, colors, size_excludes, source, added_by, updated_by
) VALUES
    ('S_MIXED', 'MIX-1', ARRAY['RED'], '{"RED": ["L"]}'::jsonb,
     'import', 'seed', 'seed');

INSERT INTO woo.store_mix_candidate (fdm4_store, style_code, colors)
VALUES
    ('S_MIXED', 'MIX-1', ARRAY['RED']),
    ('S_MIXED', 'MIX-2', ARRAY['BLU']),
    ('S_MIXED', 'MIX-3', ARRAY['GRN']);

-- Feed consumers: 'feedtest' authenticates with bearer token
-- 'feed-test-token'; 'feedoff' is registered but inactive and its token
-- ('feed-off-token') must be rejected.
INSERT INTO woo.feed_consumer (name, url, token_hash, active, note, created_by) VALUES
    ('feedtest', '', '3b09e69913044e103bf6d0eb809f5cf04e797f04991d0355a7b675ab78e979cc', true,  'integration test consumer', 'seed'),
    ('feedoff',  '', '3a0e11d32db575e37683e6cf16a03a237e405634f91ad13481938f30db2e9e94', false, 'inactive test consumer',    'seed');

-- Feed-only store: one live row and one tombstone (is_active=false) so the
-- feed suite can assert consumers receive retirements. S_FEEDDEAD has no
-- store_catalog row on purpose - it must not surface in operator store lists.
INSERT INTO woo.store_product_state (
    fdm4_store, catalog_id, sku, kind, style_code, parent_sku,
    name, status, color_code, color, size_code, size, price, stock,
    payload, content_hash, is_active
) VALUES
    ('S_FEEDDEAD', 'S_FEEDDEAD_catalog', 'FEED-1', 'parent', 'FEED-1', NULL,
     'Feed Live', 'publish', NULL, NULL, NULL, NULL, 12, 5,
     '{"price": "12"}'::jsonb, 'feed-live-1', true),
    ('S_FEEDDEAD', 'S_FEEDDEAD_catalog', 'FEED-2', 'parent', 'FEED-2', NULL,
     'Feed Gone', 'publish', NULL, NULL, NULL, NULL, 12, 0,
     '{"price": "12"}'::jsonb, 'feed-gone-1', false);
