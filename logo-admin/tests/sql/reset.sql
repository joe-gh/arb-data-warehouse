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
    logo.default_cost,
    logo.design_ipc,
    logo.bulk_batch_row,
    logo.style_color_order,
    logo.bulk_batch,
    logo.color_class,
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
 WHERE btrim(design_id) IN ('DESIGN-1', 'DESIGN-2', 'ART-9001');
DELETE FROM fdm4.cust_art_file
 WHERE btrim(art_id) IN ('ART-9001', 'DESIGN-2', 'B9H-TEST-DESIGN');
DELETE FROM fdm4.dec_design
 WHERE btrim(design_id) IN (
     'DESIGN-1', 'DESIGN-2', 'ART-9001', 'B9H-TEST-DESIGN'
 );
