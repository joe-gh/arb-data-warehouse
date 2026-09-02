-- ============================================================================
-- Canonical least-privilege policy for Warehouse Operations.
--
-- Generated from audit-report/live-grants-20260801.txt and reconciled against
-- audit-report/live-schema-20260801.sql. Regenerate by taking a fresh schema
-- dump plus \dp/routine ACL snapshot, updating the validation and grant lists
-- below, and running the disposable role/preflight tests before review.
--
-- Run as a superuser (or the database owner, where it also owns the objects)
-- AFTER sql/logo_schema.sql, sql/woo_transform.sql,
-- and every sql/migrations/*.sql file in lexical order. Every fail-closed
-- validation runs before the first REVOKE, so a stale contract cannot strip a
-- working deployment. The transaction makes the revoke/regrant phase atomic.
-- ============================================================================

\set ON_ERROR_STOP on
\if :{?repull_function_sha256}
\else
\set repull_function_sha256 ''
\endif

BEGIN;

DO $create_role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'logo_admin') THEN
        CREATE ROLE logo_admin LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END
$create_role$;

-- --------------------------------------------------------------------------
-- Phase 1: validation only. No REVOKE statement may appear above the end of
-- this phase. Checks use column names, never physical ordinals.
-- --------------------------------------------------------------------------
DO $validate_live_contract$
DECLARE
    missing_relations text;
    assignment_contract_ok boolean;
    writable_contract_ok boolean;
    trigger_contract_ok boolean;
    rule_contract_ok boolean;
    audit_contract_ok boolean;
    prune_contract_ok boolean;
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_database AS database
          JOIN pg_roles AS owner_role ON owner_role.oid = database.datdba
         WHERE owner_role.rolname = 'logo_admin'
           AND database.datname = current_database()
    ) THEN
        RAISE EXCEPTION
            'logo_admin owns the database; owner authority cannot be revoked';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM pg_namespace AS namespace
          JOIN pg_roles AS owner_role ON owner_role.oid = namespace.nspowner
         WHERE owner_role.rolname = 'logo_admin'
           AND namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
        UNION ALL
        SELECT 1
          FROM pg_class AS relation
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_roles AS owner_role ON owner_role.oid = relation.relowner
         WHERE owner_role.rolname = 'logo_admin'
           AND namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
        UNION ALL
        SELECT 1
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace
            ON namespace.oid = procedure.pronamespace
          JOIN pg_roles AS owner_role ON owner_role.oid = procedure.proowner
         WHERE owner_role.rolname = 'logo_admin'
           AND namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
    ) THEN
        RAISE EXCEPTION
            'logo_admin owns durable objects; ownership authority cannot be revoked';
    END IF;

    SELECT string_agg(required.name, ', ' ORDER BY required.name)
      INTO missing_relations
      FROM unnest(ARRAY[
          'logo.assignment',
          'logo.art_record',
          'logo.audit_log',
          'logo.bulk_batch',
          'logo.bulk_batch_row',
          'logo.style_color_order',
          'logo.color_class',
          'logo.default_cost',
          'logo.design_ipc',
          'logo.display_name',
          'logo.image_import',
          'logo.import_report',
          'logo.placement_vocab',
          'logo.store_settings',
          'logo.admin_session',
          'logo.agent_chat_session',
          'logo.agent_chat_message',
          'logo.agent_change_set',
          'logo.agent_change_set_item',
          'logo.agent_spreadsheet_job',
          'logo.agent_usage_daily',
          'logo.agent_usage_monthly',
          'logo.agent_rate_window',
          'logo.agent_quota_reservation',
          'logo.agent_action_journal',
          'logo.assignment_tombstone',
          'woo.price_rule',
          'woo.price_rule_audit',
          'woo.pricing_tier',
          'woo.store_catalog',
          'woo.store_pricing_tier',
          'woo.store_product_state',
          'woo.sync_control',
          'woo.sync_exclusion',
          'woo.virtual_catalog_store',
          'woo.app_flag',
          'woo.brand_stock_rule',
          'woo.feed_consumer',
          'woo.stock_override',
          'woo.store_blog_map',
          'woo.store_mix_audit',
          'woo.store_mix_candidate',
          'woo.store_mix_item',
          'woo.store_mix_store',
          'pim.product_state',
          'curated.category',
          'curated.category_product',
          'catmgr.snapshot',
          'catmgr.wp_term',
          'catmgr.wp_term_product',
          'catmgr.audit_log',
          'catmgr.node',
          'catmgr.node_store_override',
          'catmgr.slug_map',
          'catmgr.assignment_rule',
          'catmgr.product_assignment',
          'catmgr.uncategorized_ack',
          'catmgr.run',
          'catmgr.run_job',
          'catmgr.job_snapshot',
          'catmgr.redirect'
      ]::text[]) AS required(name)
     WHERE to_regclass(required.name) IS NULL;
    IF missing_relations IS NOT NULL THEN
        RAISE EXCEPTION 'required Warehouse Operations relations are absent: %',
            missing_relations;
    END IF;

    WITH expected(ordinal_position, column_name, formatted_type, nullable) AS (
        VALUES
            (1, 'fdm4_store', 'text', false),
            (2, 'product_style', 'text', false),
            (3, 'garment_color_code', 'text', false),
            (4, 'position', 'smallint', false),
            (5, 'design_id', 'text', false),
            (6, 'logo_code', 'text', false),
            (7, 'color_scheme_id', 'text', false),
            (8, 'location', 'text', false),
            (9, 'optional', 'boolean', false),
            (10, 'background', 'text', false),
            (11, 'cost_override', 'numeric(12,2)', true),
            (12, 'sort_order', 'integer', false),
            (13, 'image_url', 'text', false),
            (14, 'active', 'boolean', false),
            (15, 'updated_by', 'text', false),
            (16, 'updated_at', 'timestamp with time zone', false),
            (17, 'option_row', 'integer', false),
            (18, 'name_override', 'text', true),
            (19, 'row_version', 'bigint', false),
            (20, 'catalog_id', 'text', true)
    ), actual AS (
        SELECT attribute.attnum::integer AS ordinal_position,
               attribute.attname AS column_name,
               format_type(attribute.atttypid, attribute.atttypmod)
                   AS formatted_type,
               NOT attribute.attnotnull AS nullable
          FROM pg_attribute AS attribute
         WHERE attribute.attrelid = 'logo.assignment'::regclass
           AND attribute.attnum > 0
           AND NOT attribute.attisdropped
    )
    SELECT (SELECT count(*) FROM actual) = (SELECT count(*) FROM expected)
       AND NOT EXISTS (
           SELECT ordinal_position, column_name, formatted_type, nullable
             FROM expected
           EXCEPT
           SELECT ordinal_position, column_name, formatted_type, nullable
             FROM actual
       )
      INTO assignment_contract_ok;
    IF assignment_contract_ok IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'logo.assignment must match the live 20-column name/type/null contract';
    END IF;

    WITH writable(name) AS (
        SELECT unnest(ARRAY[
            'logo.assignment', 'logo.audit_log', 'logo.bulk_batch',
            'logo.bulk_batch_row', 'logo.style_color_order',
            'logo.color_class', 'logo.default_cost',
            'logo.design_ipc', 'logo.display_name', 'logo.image_import',
            'logo.import_report', 'logo.placement_vocab', 'logo.store_settings',
            'logo.admin_session', 'logo.agent_chat_session',
            'logo.agent_chat_message', 'logo.agent_change_set',
            'logo.agent_change_set_item', 'logo.agent_spreadsheet_job',
            'logo.agent_usage_daily', 'logo.agent_usage_monthly',
            'logo.agent_rate_window', 'logo.agent_quota_reservation',
            'logo.agent_action_journal', 'logo.assignment_tombstone',
            'woo.price_rule', 'woo.price_rule_audit',
            'woo.pricing_tier', 'woo.store_pricing_tier', 'woo.sync_exclusion',
            'woo.app_flag', 'woo.brand_stock_rule', 'woo.feed_consumer',
            'woo.stock_override', 'woo.store_mix_audit',
            'woo.store_mix_item', 'woo.store_mix_store',
            'woo.virtual_catalog_store',
            'catmgr.snapshot', 'catmgr.wp_term', 'catmgr.wp_term_product',
            'catmgr.audit_log', 'catmgr.node', 'catmgr.node_store_override',
            'catmgr.slug_map', 'catmgr.assignment_rule',
            'catmgr.product_assignment', 'catmgr.uncategorized_ack',
            'catmgr.run', 'catmgr.run_job', 'catmgr.job_snapshot',
            'catmgr.redirect'
        ]::text[])
    )
    -- Ownership gate, deliberately relaxed (temporary): on production the
    -- arb_warehouse database is owned by etl_writer while every table and
    -- function in logo/woo/fdm4/catmgr/curated/pim is owned by postgres (a
    -- superuser), so "owned by the database owner" can never hold there and
    -- this script could not be applied. Until the database owner is changed,
    -- an object owner is acceptable when it is the database owner
    -- (pg_database.datdba) OR a superuser (pg_roles.rolsuper). Arbitrary
    -- roles are still rejected. The same rule is applied by every ownership
    -- check below, by sql/diagnostics/agent-write-preflight.sql, and by
    -- logo-admin/database_contract.py.
    SELECT bool_and(
               relation.relkind = 'r'
               AND relation.relpersistence = 'p'
               AND NOT relation.relispartition
               AND NOT relation.relrowsecurity
               AND NOT relation.relforcerowsecurity
               AND (
                   relation.relowner = database.datdba
                   OR relation_owner.rolsuper
               )
           )
      INTO writable_contract_ok
      FROM writable
      JOIN pg_class AS relation ON relation.oid = to_regclass(writable.name)
      JOIN pg_roles AS relation_owner ON relation_owner.oid = relation.relowner
      JOIN pg_database AS database ON database.datname = current_database();
    IF writable_contract_ok IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'a writable relation is not an ordinary permanent non-RLS table owned by the database owner or a superuser';
    END IF;

    WITH expected(
        table_name, trigger_name, trigger_type, trigger_enabled,
        function_schema, function_name
    ) AS (
        VALUES
            ('logo.assignment', 'logo_assignment_audit', 29, 'O',
             'logo', 'audit_row'),
            ('logo.assignment', 'assignment_feed_stamp', 23, 'O',
             'logo', 'assignment_feed_stamp'),
            ('logo.assignment', 'assignment_feed_tombstone', 9, 'O',
             'logo', 'assignment_feed_tombstone'),
            ('logo.store_settings', 'logo_store_settings_audit', 29, 'O',
             'logo', 'audit_row'),
            ('logo.color_class', 'logo_color_class_audit', 29, 'O',
             'logo', 'audit_row'),
            ('logo.display_name', 'logo_display_name_audit', 29, 'O',
             'logo', 'audit_display_name_row'),
            ('woo.price_rule', 'price_rule_audit', 29, 'O',
             'woo', 'audit_price_rule_row'),
            ('woo.store_mix_store', 'store_mix_store_audit', 29, 'O',
             'woo', 'audit_store_mix_row'),
            ('woo.store_mix_item', 'store_mix_item_audit', 29, 'O',
             'woo', 'audit_store_mix_row')
    ), actual AS (
        SELECT format('%I.%I', namespace.nspname, relation.relname),
               trigger.tgname,
               trigger.tgtype::integer,
               trigger.tgenabled::text,
               function_namespace.nspname,
               procedure.proname
          FROM pg_trigger AS trigger
          JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          JOIN pg_proc AS procedure ON procedure.oid = trigger.tgfoid
          JOIN pg_namespace AS function_namespace
            ON function_namespace.oid = procedure.pronamespace
         WHERE NOT trigger.tgisinternal
           AND format('%I.%I', namespace.nspname, relation.relname) IN (
               SELECT table_name FROM expected
           )
    )
    SELECT NOT EXISTS (SELECT * FROM expected EXCEPT SELECT * FROM actual)
       AND NOT EXISTS (SELECT * FROM actual EXCEPT SELECT * FROM expected)
      INTO trigger_contract_ok
    ;
    IF trigger_contract_ok IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'required audit-trigger inventory has drifted';
    END IF;

    SELECT NOT EXISTS (
               SELECT 1
                 FROM pg_rewrite AS rewrite
                 JOIN pg_class AS relation ON relation.oid = rewrite.ev_class
                WHERE relation.oid = ANY (ARRAY[
                    'logo.assignment'::regclass,
                    'logo.store_settings'::regclass,
                    'logo.color_class'::regclass,
                    'logo.display_name'::regclass,
                    'woo.price_rule'::regclass
                ])
                  AND rewrite.rulename <> '_RETURN'
           )
       AND NOT EXISTS (
               SELECT 1
                 FROM pg_policy AS policy
                WHERE policy.polrelid = ANY (ARRAY[
                    'logo.assignment'::regclass,
                    'logo.store_settings'::regclass,
                    'logo.color_class'::regclass,
                    'logo.display_name'::regclass,
                    'woo.price_rule'::regclass
                ])
           )
      INTO rule_contract_ok;
    IF rule_contract_ok IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'writable trigger/rule/RLS semantics have drifted';
    END IF;

    SELECT EXISTS (
               SELECT 1
                 FROM pg_proc AS procedure
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = procedure.pronamespace
                 JOIN pg_language AS language
                   ON language.oid = procedure.prolang
                 JOIN pg_roles AS procedure_owner
                   ON procedure_owner.oid = procedure.proowner
                 JOIN pg_database AS database
                   ON database.datname = current_database()
                WHERE procedure.oid = 'logo.audit_row()'::regprocedure
                  AND namespace.nspname = 'logo'
                  AND (
                      procedure.proowner = database.datdba
                      OR procedure_owner.rolsuper
                  )
                  AND procedure.prokind = 'f'
                  AND NOT procedure.prosecdef
                  AND language.lanname = 'plpgsql'
                  AND pg_get_function_result(procedure.oid) = 'trigger'
                  AND encode(sha256(convert_to(
                      procedure.prosrc, 'UTF8'
                  )), 'hex') =
                      '0ffa5f09bd205a694dfe288347074a85195458d1eb3ae74577d4723343d7e58b'
           )
      INTO audit_contract_ok;
    IF audit_contract_ok IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'logo.audit_row() contract has drifted';
    END IF;

    IF to_regprocedure(
        'woo.eval_price_rules(text,text,text,text,numeric,jsonb,numeric,date,bigint[],bigint[])'
    ) IS NULL THEN
        RAISE EXCEPTION 'woo.eval_price_rules(...) is absent';
    END IF;
    IF to_regprocedure('logo.prune_agent_history()') IS NOT NULL
    THEN
        SELECT EXISTS (
           SELECT 1
             FROM pg_proc AS procedure
             JOIN pg_language AS language ON language.oid = procedure.prolang
             JOIN pg_roles AS procedure_owner
               ON procedure_owner.oid = procedure.proowner
             JOIN pg_database AS database
               ON database.datname = current_database()
            WHERE procedure.oid = to_regprocedure('logo.prune_agent_history()')
              AND (
                  procedure.proowner = database.datdba
                  OR procedure_owner.rolsuper
              )
              AND procedure.prokind = 'f'
              AND procedure.prosecdef
              AND language.lanname = 'plpgsql'
              AND procedure.proconfig = ARRAY[
                  'search_path=pg_catalog, logo'
              ]::text[]
              AND pg_get_function_result(procedure.oid) =
                  'TABLE(journals_deleted bigint, change_sets_deleted bigint)'
              AND encode(sha256(convert_to(
                  procedure.prosrc, 'UTF8'
              )), 'hex') =
                  '378f41091ba89926fda1364b2c99bd2901b8e01ddde9c8fa52f97b3f3f8c2269'
        ) INTO prune_contract_ok;
        IF prune_contract_ok IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'logo.prune_agent_history() contract has drifted';
        END IF;
    END IF;
END
$validate_live_contract$;

-- psql variables are intentionally evaluated outside a dollar-quoted block.
-- An installed legacy repull routine receives EXECUTE only when its complete
-- definition matches the independently reviewed deployment hash. The reviewed
-- implementation reads logo.display_name and fdm4.design_pool (plus the
-- warehouse-owned art records); a body change requires a newly reviewed hash.
WITH repull AS (
    SELECT procedure.oid,
           procedure.proowner,
           owner_role.rolsuper AS owner_is_superuser,
           procedure.prokind,
           procedure.prosecdef,
           procedure.proconfig,
           procedure.prolang,
           database.datdba,
           language.lanname,
           pg_get_function_result(procedure.oid) AS function_result,
           encode(sha256(convert_to(
               pg_get_functiondef(procedure.oid), 'UTF8'
           )), 'hex') AS actual_sha256,
           lower(:'repull_function_sha256') AS expected_repull_function_sha256
      FROM pg_proc AS procedure
      JOIN pg_language AS language ON language.oid = procedure.prolang
      JOIN pg_roles AS owner_role ON owner_role.oid = procedure.proowner
      JOIN pg_database AS database ON database.datname = current_database()
     WHERE procedure.oid =
           to_regprocedure('logo.repull_display_name(text,boolean)')
), repull_contract AS (
    SELECT to_regprocedure('logo.repull_display_name(text,boolean)') IS NULL
        OR EXISTS (
            SELECT 1 FROM repull
             WHERE (proowner = datdba OR owner_is_superuser)
               AND prokind = 'f'
               AND NOT prosecdef
               AND (proconfig IS NULL OR proconfig = ARRAY[
                   'search_path=pg_catalog, logo, fdm4'
               ]::text[])
               AND lanname = 'plpgsql'
               AND function_result = 'integer'
               AND length(expected_repull_function_sha256) = 64
               AND actual_sha256 = expected_repull_function_sha256
        ) AS passes
)
SELECT 1 / CASE WHEN passes THEN 1 ELSE 0 END AS repull_contract_assertion
  FROM repull_contract;

-- --------------------------------------------------------------------------
-- Phase 2: atomically replace logo_admin's direct authority, then restore the
-- complete reviewed live application surface plus additive fix tables.
-- --------------------------------------------------------------------------
ALTER ROLE logo_admin LOGIN NOINHERIT NOSUPERUSER NOCREATEDB
    NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12;
ALTER ROLE logo_admin SET statement_timeout = '30s';
ALTER ROLE logo_admin SET lock_timeout = '5s';
ALTER ROLE logo_admin SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE logo_admin SET search_path = logo, woo, fdm4, pg_catalog;

DO $reset_database_role_settings$
BEGIN
    EXECUTE format(
        'ALTER ROLE logo_admin IN DATABASE %I RESET ALL',
        current_database()
    );
END
$reset_database_role_settings$;

DO $memberships$
DECLARE
    granted_role text;
BEGIN
    FOR granted_role IN
        SELECT role_name.rolname
          FROM pg_auth_members membership
          JOIN pg_roles member_role ON member_role.oid = membership.member
          JOIN pg_roles role_name ON role_name.oid = membership.roleid
         WHERE member_role.rolname = 'logo_admin'
    LOOP
        EXECUTE format('REVOKE %I FROM logo_admin', granted_role);
    END LOOP;
END
$memberships$;

DO $database_policy$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO logo_admin',
        current_database()
    );
    EXECUTE format(
        'REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC, logo_admin',
        current_database()
    );
END
$database_policy$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

DO $clear_direct_authority$
DECLARE
    schema_name text;
    column_acl record;
BEGIN
    FOR schema_name IN
        SELECT namespace.nspname
          FROM pg_namespace AS namespace
         WHERE namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
         ORDER BY namespace.nspname
    LOOP
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON SCHEMA %I FROM logo_admin', schema_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM logo_admin',
            schema_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM logo_admin',
            schema_name
        );
        EXECUTE format(
            'REVOKE ALL PRIVILEGES ON ALL ROUTINES IN SCHEMA %I FROM logo_admin',
            schema_name
        );
    END LOOP;

    -- Table-level REVOKE does not remove legacy column ACLs. Remove every
    -- direct logo_admin column grant, including grants in unknown schemas.
    FOR column_acl IN
        SELECT namespace.nspname AS schema_name,
               relation.relname AS relation_name,
               attribute.attname AS column_name,
               string_agg(DISTINCT privilege.privilege_type, ', ')
                   AS privilege_list
          FROM pg_attribute AS attribute
          JOIN pg_class AS relation ON relation.oid = attribute.attrelid
          JOIN pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
         CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
          JOIN pg_roles AS grantee ON grantee.oid = privilege.grantee
         WHERE attribute.attnum > 0
           AND NOT attribute.attisdropped
           AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
           AND namespace.nspname <> 'information_schema'
           AND namespace.nspname !~ '^pg_'
           AND grantee.rolname = 'logo_admin'
         GROUP BY namespace.nspname, relation.relname, attribute.attname
    LOOP
        EXECUTE format(
            'REVOKE %s (%s) ON TABLE %I.%I FROM logo_admin',
            column_acl.privilege_list,
            quote_ident(column_acl.column_name),
            column_acl.schema_name,
            column_acl.relation_name
        );
    END LOOP;
END
$clear_direct_authority$;

REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA logo FROM PUBLIC;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA woo FROM PUBLIC;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA fdm4 FROM PUBLIC;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA pim FROM PUBLIC;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA curated FROM PUBLIC;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA catmgr FROM PUBLIC;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA logo, woo, fdm4, pim, curated, catmgr FROM PUBLIC;

GRANT USAGE ON SCHEMA logo, woo, fdm4, pim, curated, catmgr TO logo_admin;
GRANT SELECT ON ALL TABLES IN SCHEMA woo, fdm4 TO logo_admin;
-- pim and curated are read-only surfaces for the application; catmgr write
-- grants are explicit below.
GRANT SELECT ON ALL TABLES IN SCHEMA pim, curated TO logo_admin;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE logo.assignment TO logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    logo.bulk_batch,
    logo.bulk_batch_row,
    logo.style_color_order,
    logo.color_class,
    logo.default_cost,
    logo.design_ipc,
    logo.display_name,
    logo.placement_vocab,
    logo.admin_session,
    logo.agent_chat_session,
    logo.agent_chat_message,
    logo.agent_change_set,
    logo.agent_change_set_item,
    logo.agent_spreadsheet_job,
    logo.agent_usage_daily,
    logo.agent_usage_monthly,
    logo.agent_rate_window,
    logo.agent_quota_reservation
    TO logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE logo.store_settings TO logo_admin;
GRANT SELECT, INSERT ON TABLE
    logo.audit_log,
    logo.import_report,
    logo.agent_action_journal
    TO logo_admin;
GRANT SELECT, INSERT, UPDATE ON TABLE logo.image_import TO logo_admin;
GRANT SELECT ON TABLE logo.art_record TO logo_admin;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    woo.price_rule,
    woo.pricing_tier,
    woo.store_pricing_tier,
    woo.sync_exclusion,
    woo.app_flag,
    woo.brand_stock_rule,
    woo.feed_consumer,
    woo.stock_override,
    woo.store_mix_item,
    woo.store_mix_store,
    woo.virtual_catalog_store
    TO logo_admin;
GRANT SELECT, INSERT ON TABLE woo.price_rule_audit, woo.store_mix_audit TO logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE logo.assignment_tombstone TO logo_admin;

-- Category editor (catmgr): snapshots are app-owned; audit is append-only.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    catmgr.snapshot,
    catmgr.wp_term,
    catmgr.wp_term_product,
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
    TO logo_admin;
GRANT SELECT, INSERT ON TABLE catmgr.audit_log TO logo_admin;

GRANT SELECT, USAGE ON SEQUENCE
    logo.audit_log_id_seq,
    logo.import_report_id_seq
    TO logo_admin;
GRANT USAGE ON SEQUENCE
    woo.price_rule_rule_id_seq,
    woo.price_rule_audit_id_seq,
    woo.store_mix_audit_id_seq,
    logo.assignment_version_seq
    TO logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;

GRANT EXECUTE ON FUNCTION
    woo.eval_price_rules(
        text, text, text, text, numeric, jsonb, numeric,
        date, bigint[], bigint[]
    )
    TO woo_reader, insights_reader, etl_writer, logo_admin;
GRANT EXECUTE ON FUNCTION woo.refresh_product_state() TO etl_writer;

DO $optional_function_grants$
BEGIN
    IF to_regprocedure('logo.repull_display_name(text,boolean)') IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION
            logo.repull_display_name(text, boolean) TO logo_admin;
    END IF;
    IF to_regprocedure('logo.prune_agent_history()') IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION logo.prune_agent_history() TO logo_admin;
    END IF;
END
$optional_function_grants$;

ALTER DEFAULT PRIVILEGES IN SCHEMA woo, fdm4, pim, curated
    GRANT SELECT ON TABLES TO logo_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA logo, woo, fdm4, pim, curated, catmgr
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA logo, woo, fdm4, pim, curated, catmgr
    REVOKE EXECUTE ON ROUTINES FROM PUBLIC;

COMMIT;
