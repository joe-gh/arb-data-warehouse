-- FDM4 art-customer tables arrive from the hourly extractor in production;
-- a harness cloned from an older schema source may lack them.
CREATE TABLE IF NOT EXISTS fdm4.customer_art (
    event_code text, license_status text, end_user_id text, width text, stitch_count text, square_dim text,
    method_id text, location_id text, last_used text, last_updated_by text, last_updated text, last_order_number text,
    last_bo_number text, height text, fabric text, description text, dec_web_active text, cust_version_id text,
    cust_number text, cust_art_id text, create_date text, created_by text, char_3 text, char_2 text, char_1 text,
    art_version_id text, art_status text, art_id text, art_categ_id text, active text, est_run_time text,
    archive_location text, archive_type text, review_date text, art_notes text, est_run_time_uom text,
    digitizing_status text, art_type text, cust_type text, web_active text, use_diameter_dims text, diameter text);
CREATE TABLE IF NOT EXISTS fdm4.customer_art_cust (
    char_3 text, char_2 text, char_1 text, cust_number text, art_version_id text, art_id text);
GRANT SELECT ON fdm4.customer_art, fdm4.customer_art_cust TO logo_admin;

TRUNCATE TABLE
    logo.agent_spreadsheet_job,
    logo.agent_action_journal,
    logo.agent_change_set_item,
    logo.agent_change_set,
    logo.agent_chat_message,
    logo.agent_chat_session,
    logo.agent_quota_reservation,
    logo.agent_rate_window,
    logo.agent_usage_daily,
    logo.agent_usage_monthly,
    logo.audit_log,
    logo.image_import,
    logo.import_report,
    logo.admin_session,
    logo.assignment,
    logo.store_settings,
    logo.display_name,
    logo.design_image,
    logo.default_cost,
    logo.design_ipc,
    logo.bulk_batch_row,
    logo.style_color_order,
    logo.bulk_batch,
    logo.color_class,
    logo.design_customer,
    woo.price_rule_audit,
    woo.price_rule,
    woo.sync_exclusion,
    woo.store_mix_audit,
    woo.feed_consumer,
    woo.store_mix_candidate,
    woo.store_mix_item,
    woo.store_mix_store,
    woo.store_pricing_tier,
    woo.pricing_tier,
    woo.store_product_state,
    woo.store_catalog,
    catmgr.snapshot,
    catmgr.wp_term,
    catmgr.wp_term_product,
    catmgr.wp_uncategorized_product,
    catmgr.audit_log,
    catmgr.node,
    catmgr.node_store_override,
    catmgr.slug_map,
    catmgr.assignment_rule,
    catmgr.product_assignment,
    catmgr.uncategorized_ack,
    catmgr.run,
    catmgr.run_job,
    catmgr.job_snapshot,
    catmgr.redirect
RESTART IDENTITY CASCADE;

DELETE FROM logo.placement_vocab
 WHERE name IN ('Left Chest', 'Right Chest');

DELETE FROM fdm4.design_pool
 WHERE btrim(design_id) IN ('DESIGN-1', 'DESIGN-2', 'ART-9001', 'DESIGN-3', 'DESIGN-4', 'DESIGN-5');
DELETE FROM fdm4.cust_art_file
 WHERE btrim(art_id) IN ('ART-9001', 'DESIGN-2', 'B9H-TEST-DESIGN', 'ART-3', 'ART-4', 'ART-5');
DELETE FROM fdm4.dec_design
 WHERE btrim(design_id) IN (
     'DESIGN-1', 'DESIGN-2', 'ART-9001', 'B9H-TEST-DESIGN', 'DESIGN-3', 'DESIGN-4', 'DESIGN-5'
 );
DELETE FROM fdm4.customer_art WHERE btrim(art_id) IN ('ART-3', 'ART-4', 'ART-5');
DELETE FROM fdm4.customer_art_cust WHERE btrim(art_id) IN ('ART-3', 'ART-4', 'ART-5');
