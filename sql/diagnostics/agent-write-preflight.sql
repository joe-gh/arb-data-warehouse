\pset pager off
\set ON_ERROR_STOP on
-- Pass -v repull_function_sha256=... when the optional reviewed function is
-- installed; its definition_sha256 value is printed in the inventory below.
\if :{?repull_function_sha256}
\else
\set repull_function_sha256 ''
\endif

SELECT current_database(), current_user, version();

-- The application role must not acquire authority through flags or SET ROLE.
SELECT role.rolname,
       role.rolcanlogin,
       role.rolinherit,
       role.rolsuper,
       role.rolcreatedb,
       role.rolcreaterole,
       role.rolreplication,
       role.rolbypassrls,
       role.rolconnlimit,
       role.rolconfig,
       role.rolcanlogin
           AND NOT role.rolinherit
           AND NOT role.rolsuper
           AND NOT role.rolcreatedb
           AND NOT role.rolcreaterole
           AND NOT role.rolreplication
           AND NOT role.rolbypassrls AS safe_role_flags
 FROM pg_roles AS role
 WHERE role.rolname = 'logo_admin';

SELECT database.datname AS database_name,
       setting.setconfig
  FROM pg_db_role_setting AS setting
  JOIN pg_roles AS role ON role.oid = setting.setrole
  JOIN pg_database AS database ON database.oid = setting.setdatabase
 WHERE role.rolname = 'logo_admin'
   AND database.datname = current_database();

SELECT granted_role.rolname AS granted_role,
       member_role.rolname AS member_role,
       membership.admin_option
  FROM pg_auth_members AS membership
  JOIN pg_roles AS granted_role
    ON granted_role.oid = membership.roleid
  JOIN pg_roles AS member_role
    ON member_role.oid = membership.member
 WHERE member_role.rolname = 'logo_admin'
 ORDER BY granted_role.rolname;

SELECT count(*) = 0 AS no_role_memberships
  FROM pg_auth_members AS membership
  JOIN pg_roles AS member_role
    ON member_role.oid = membership.member
 WHERE member_role.rolname = 'logo_admin';

SELECT has_database_privilege(
           'logo_admin', current_database(), 'CONNECT'
       ) AS connect_allowed,
       NOT has_database_privilege(
           'logo_admin', current_database(), 'CREATE'
       ) AS create_denied,
       NOT has_database_privilege(
           'logo_admin', current_database(), 'TEMPORARY'
       ) AS temporary_denied;

WITH required(schema_name, usage_expected, create_expected) AS (
    VALUES
        ('logo', true, false),
        ('woo', true, false),
        ('fdm4', true, false),
        ('public', NULL::boolean, false)
)
SELECT schema_name,
       usage_expected,
       has_schema_privilege(
           'logo_admin', schema_name, 'USAGE'
       ) AS usage_actual,
       usage_expected IS NULL
           OR has_schema_privilege(
               'logo_admin', schema_name, 'USAGE'
           ) = usage_expected AS usage_passes,
       create_expected,
       has_schema_privilege(
           'logo_admin', schema_name, 'CREATE'
       ) AS create_actual,
       has_schema_privilege(
           'logo_admin', schema_name, 'CREATE'
       ) = create_expected AS create_passes
  FROM required
 ORDER BY schema_name;

SELECT namespace.nspname AS schema_name,
       has_schema_privilege(
           'logo_admin', namespace.oid, 'CREATE'
       ) AS create_actual,
       NOT has_schema_privilege(
           'logo_admin', namespace.oid, 'CREATE'
       ) AS create_denied
  FROM pg_namespace AS namespace
 WHERE namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
 ORDER BY namespace.nspname;

SELECT 'schema' AS object_kind,
       namespace.nspname AS schema_name,
       namespace.nspname AS object_name
  FROM pg_namespace AS namespace
 WHERE namespace.nspowner = (
       SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
   )
   AND namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
UNION ALL
SELECT 'relation', namespace.nspname, relation.relname
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
 WHERE relation.relowner = (
       SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
   )
   AND namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
UNION ALL
SELECT 'function', namespace.nspname,
       procedure.oid::regprocedure::text
  FROM pg_proc AS procedure
  JOIN pg_namespace AS namespace
    ON namespace.oid = procedure.pronamespace
 WHERE procedure.proowner = (
       SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
   )
   AND namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
ORDER BY 1, 2, 3;

WITH writable_table(schema_name, table_name) AS (
    VALUES
        ('logo', 'assignment'),
        ('logo', 'store_settings'),
        ('logo', 'placement_vocab'),
        ('logo', 'color_class'),
        ('logo', 'bulk_batch'),
        ('logo', 'bulk_batch_row'),
        ('logo', 'style_color_order'),
        ('logo', 'default_cost'),
        ('logo', 'design_ipc'),
        ('logo', 'admin_session'),
        ('logo', 'image_import'),
        ('logo', 'import_report'),
        ('logo', 'display_name'),
        ('logo', 'audit_log'),
        ('woo', 'price_rule'),
        ('woo', 'price_rule_audit'),
        ('woo', 'pricing_tier'),
        ('woo', 'store_pricing_tier'),
        ('woo', 'sync_exclusion'),
        ('woo', 'store_mix_store'),
        ('woo', 'store_mix_item'),
        ('woo', 'store_mix_audit'),
        ('woo', 'feed_consumer'),
        ('woo', 'app_flag'),
        ('woo', 'brand_stock_rule'),
        ('woo', 'stock_override'),
        ('woo', 'virtual_catalog_store'),
        ('logo', 'assignment_tombstone'),
        ('catmgr', 'snapshot'),
        ('catmgr', 'wp_term'),
        ('catmgr', 'wp_term_product'),
        ('catmgr', 'audit_log'),
        ('catmgr', 'node'),
        ('catmgr', 'node_store_override'),
        ('catmgr', 'slug_map'),
        ('catmgr', 'assignment_rule'),
        ('catmgr', 'product_assignment'),
        ('catmgr', 'uncategorized_ack'),
        ('catmgr', 'run'),
        ('catmgr', 'run_job'),
        ('catmgr', 'job_snapshot'),
        ('catmgr', 'redirect'),
        ('logo', 'agent_chat_session'),
        ('logo', 'agent_chat_message'),
        ('logo', 'agent_change_set'),
        ('logo', 'agent_change_set_item'),
        ('logo', 'agent_spreadsheet_job'),
        ('logo', 'agent_usage_daily'),
        ('logo', 'agent_usage_monthly'),
        ('logo', 'agent_rate_window'),
        ('logo', 'agent_quota_reservation'),
        ('logo', 'agent_action_journal')
)
SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       relation.relkind,
       relation.relpersistence,
       relation.relispartition,
       -- Ownership rule (deliberately relaxed, temporary): the owner passes
       -- when it is the database owner (pg_database.datdba) OR a superuser
       -- (pg_roles.rolsuper). On production arb_warehouse is owned by
       -- etl_writer while every object is owned by postgres. Arbitrary roles
       -- still fail. Every ownership check in this file applies this rule.
       owner.rolname AS owner_name,
       owner.rolsuper AS owner_is_superuser,
       database_owner.rolname AS expected_owner,
       (
           relation.relowner = database.datdba
           OR owner.rolsuper
       ) AS owner_passes,
       relation.relrowsecurity,
       relation.relforcerowsecurity
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN pg_roles AS owner ON owner.oid = relation.relowner
  JOIN pg_database AS database ON database.datname = current_database()
  JOIN pg_roles AS database_owner ON database_owner.oid = database.datdba
  JOIN writable_table
    ON writable_table.schema_name = namespace.nspname
   AND writable_table.table_name = relation.relname
 ORDER BY 1, 2;

WITH writable_table(schema_name, table_name) AS (
    VALUES
        ('logo', 'assignment'), ('logo', 'store_settings'),
        ('logo', 'placement_vocab'), ('logo', 'image_import'),
        ('logo', 'import_report'), ('logo', 'display_name'),
        ('logo', 'audit_log'), ('logo', 'color_class'),
        ('logo', 'bulk_batch'), ('logo', 'bulk_batch_row'),
        ('logo', 'style_color_order'),
        ('logo', 'default_cost'), ('logo', 'design_ipc'),
        ('logo', 'admin_session'), ('woo', 'price_rule'),
        ('woo', 'price_rule_audit'), ('woo', 'pricing_tier'),
        ('woo', 'store_pricing_tier'), ('woo', 'sync_exclusion'),
        ('woo', 'store_mix_store'), ('woo', 'store_mix_item'),
        ('woo', 'store_mix_audit'), ('woo', 'feed_consumer'),
        ('woo', 'app_flag'), ('woo', 'brand_stock_rule'),
        ('woo', 'stock_override'), ('woo', 'virtual_catalog_store'),
        ('logo', 'assignment_tombstone'),
        ('catmgr', 'snapshot'), ('catmgr', 'wp_term'),
        ('catmgr', 'wp_term_product'), ('catmgr', 'audit_log'),
        ('catmgr', 'node'), ('catmgr', 'node_store_override'),
        ('catmgr', 'slug_map'), ('catmgr', 'assignment_rule'),
        ('catmgr', 'product_assignment'), ('catmgr', 'uncategorized_ack'),
        ('catmgr', 'run'), ('catmgr', 'run_job'),
        ('catmgr', 'job_snapshot'), ('catmgr', 'redirect'),
        ('logo', 'agent_chat_session'), ('logo', 'agent_chat_message'),
        ('logo', 'agent_change_set'), ('logo', 'agent_change_set_item'),
        ('logo', 'agent_spreadsheet_job'), ('logo', 'agent_usage_daily'),
        ('logo', 'agent_usage_monthly'), ('logo', 'agent_rate_window'),
        ('logo', 'agent_quota_reservation'),
        ('logo', 'agent_action_journal')
)
SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       trigger.tgname,
       trigger.tgtype::integer,
       trigger.tgenabled,
       trigger.tgnargs,
       trigger.tgqual,
       trigger.tgconstraint,
       trigger.tgfoid::regprocedure AS trigger_function,
       pg_get_triggerdef(trigger.oid, true) AS definition
  FROM pg_trigger AS trigger
  JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN writable_table
    ON writable_table.schema_name = namespace.nspname
   AND writable_table.table_name = relation.relname
 WHERE NOT trigger.tgisinternal
 ORDER BY 1, 2, 3;

WITH writable_table(schema_name, table_name) AS (
    VALUES
        ('logo', 'assignment'), ('logo', 'store_settings'),
        ('logo', 'placement_vocab'), ('logo', 'image_import'),
        ('logo', 'import_report'), ('logo', 'display_name'),
        ('logo', 'audit_log'), ('logo', 'color_class'),
        ('logo', 'bulk_batch'), ('logo', 'bulk_batch_row'),
        ('logo', 'style_color_order'),
        ('logo', 'default_cost'), ('logo', 'design_ipc'),
        ('logo', 'admin_session'), ('woo', 'price_rule'),
        ('woo', 'price_rule_audit'), ('woo', 'pricing_tier'),
        ('woo', 'store_pricing_tier'), ('woo', 'sync_exclusion'),
        ('woo', 'store_mix_store'), ('woo', 'store_mix_item'),
        ('woo', 'store_mix_audit'), ('woo', 'feed_consumer'),
        ('woo', 'app_flag'), ('woo', 'brand_stock_rule'),
        ('woo', 'stock_override'), ('woo', 'virtual_catalog_store'),
        ('logo', 'assignment_tombstone'),
        ('catmgr', 'snapshot'), ('catmgr', 'wp_term'),
        ('catmgr', 'wp_term_product'), ('catmgr', 'audit_log'),
        ('catmgr', 'node'), ('catmgr', 'node_store_override'),
        ('catmgr', 'slug_map'), ('catmgr', 'assignment_rule'),
        ('catmgr', 'product_assignment'), ('catmgr', 'uncategorized_ack'),
        ('catmgr', 'run'), ('catmgr', 'run_job'),
        ('catmgr', 'job_snapshot'), ('catmgr', 'redirect'),
        ('logo', 'agent_chat_session'), ('logo', 'agent_chat_message'),
        ('logo', 'agent_change_set'), ('logo', 'agent_change_set_item'),
        ('logo', 'agent_spreadsheet_job'), ('logo', 'agent_usage_daily'),
        ('logo', 'agent_usage_monthly'), ('logo', 'agent_rate_window'),
        ('logo', 'agent_quota_reservation'),
        ('logo', 'agent_action_journal')
)
SELECT rules.schemaname, rules.tablename, rules.rulename, rules.definition
  FROM pg_rules AS rules
  JOIN writable_table
    ON writable_table.schema_name = rules.schemaname
   AND writable_table.table_name = rules.tablename
 ORDER BY 1, 2, 3;

WITH writable_table(schema_name, table_name) AS (
    VALUES
        ('logo', 'assignment'), ('logo', 'store_settings'),
        ('logo', 'placement_vocab'), ('logo', 'image_import'),
        ('logo', 'import_report'), ('logo', 'display_name'),
        ('logo', 'audit_log'), ('logo', 'color_class'),
        ('logo', 'bulk_batch'), ('logo', 'bulk_batch_row'),
        ('logo', 'style_color_order'),
        ('logo', 'default_cost'), ('logo', 'design_ipc'),
        ('logo', 'admin_session'), ('woo', 'price_rule'),
        ('woo', 'price_rule_audit'), ('woo', 'pricing_tier'),
        ('woo', 'store_pricing_tier'), ('woo', 'sync_exclusion'),
        ('woo', 'store_mix_store'), ('woo', 'store_mix_item'),
        ('woo', 'store_mix_audit'), ('woo', 'feed_consumer'),
        ('woo', 'app_flag'), ('woo', 'brand_stock_rule'),
        ('woo', 'stock_override'), ('woo', 'virtual_catalog_store'),
        ('logo', 'assignment_tombstone'),
        ('catmgr', 'snapshot'), ('catmgr', 'wp_term'),
        ('catmgr', 'wp_term_product'), ('catmgr', 'audit_log'),
        ('catmgr', 'node'), ('catmgr', 'node_store_override'),
        ('catmgr', 'slug_map'), ('catmgr', 'assignment_rule'),
        ('catmgr', 'product_assignment'), ('catmgr', 'uncategorized_ack'),
        ('catmgr', 'run'), ('catmgr', 'run_job'),
        ('catmgr', 'job_snapshot'), ('catmgr', 'redirect'),
        ('logo', 'agent_chat_session'), ('logo', 'agent_chat_message'),
        ('logo', 'agent_change_set'), ('logo', 'agent_change_set_item'),
        ('logo', 'agent_spreadsheet_job'), ('logo', 'agent_usage_daily'),
        ('logo', 'agent_usage_monthly'), ('logo', 'agent_rate_window'),
        ('logo', 'agent_quota_reservation'),
        ('logo', 'agent_action_journal')
)
SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       policy.polname,
       policy.polcmd,
       policy.polpermissive,
       pg_get_expr(policy.polqual, policy.polrelid, true) AS using_expression,
       pg_get_expr(
           policy.polwithcheck, policy.polrelid, true
       ) AS check_expression
  FROM pg_policy AS policy
  JOIN pg_class AS relation ON relation.oid = policy.polrelid
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN writable_table
    ON writable_table.schema_name = namespace.nspname
   AND writable_table.table_name = relation.relname
 ORDER BY 1, 2, 3;

SELECT source_namespace.nspname AS source_schema,
       source.relname AS source_table,
       constraint_row.conname,
       constraint_row.contype,
       target_namespace.nspname AS target_schema,
       target.relname AS target_table,
       constraint_row.confupdtype,
       constraint_row.confdeltype,
       constraint_row.confmatchtype,
       constraint_row.condeferrable,
       constraint_row.condeferred,
       constraint_row.convalidated,
       constraint_row.connoinherit,
       pg_get_constraintdef(constraint_row.oid, true) AS definition
  FROM pg_constraint AS constraint_row
  JOIN pg_class AS source ON source.oid = constraint_row.conrelid
  JOIN pg_namespace AS source_namespace
    ON source_namespace.oid = source.relnamespace
  LEFT JOIN pg_class AS target ON target.oid = constraint_row.confrelid
  LEFT JOIN pg_namespace AS target_namespace
    ON target_namespace.oid = target.relnamespace
 WHERE (
           (source_namespace.nspname, source.relname) IN (
               ('logo', 'assignment'),
               ('logo', 'store_settings'),
               ('woo', 'store_pricing_tier')
           )
           AND constraint_row.contype IN ('p', 'c', 'f', 'u', 'x')
       )
    OR (
           (target_namespace.nspname, target.relname) IN (
               ('logo', 'assignment'),
               ('logo', 'store_settings'),
               ('woo', 'store_pricing_tier')
           )
           AND constraint_row.contype = 'f'
       )
 ORDER BY 1, 2, 3;

SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       index_class.relname AS index_name,
       pg_get_indexdef(index_row.indexrelid) AS definition
  FROM pg_index AS index_row
  JOIN pg_class AS relation ON relation.oid = index_row.indrelid
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid
 WHERE (namespace.nspname, relation.relname) IN (
       ('logo', 'assignment'),
       ('logo', 'store_settings'),
       ('woo', 'store_pricing_tier')
   )
   AND index_row.indisunique
   AND NOT EXISTS (
       SELECT 1
         FROM pg_constraint AS constraint_row
        WHERE constraint_row.conindid = index_row.indexrelid
   )
 ORDER BY 1, 2, 3;

SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       attribute.attname AS column_name,
       attribute.attnum::integer AS ordinal_position,
       format_type(attribute.atttypid, attribute.atttypmod) AS formatted_type,
       NOT attribute.attnotnull AS nullable,
       attribute.attgenerated,
       attribute.attidentity,
       pg_collation.collname AS collation_name,
       pg_get_expr(
           default_row.adbin, default_row.adrelid, true
       ) AS default_expression
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
   AND attribute.attnum > 0
   AND NOT attribute.attisdropped
  LEFT JOIN pg_collation
    ON pg_collation.oid = attribute.attcollation
  LEFT JOIN pg_attrdef AS default_row
    ON default_row.adrelid = relation.oid
   AND default_row.adnum = attribute.attnum
 WHERE namespace.nspname = 'logo'
   AND relation.relname LIKE 'agent\_%' ESCAPE '\'
 ORDER BY 1, 2, attribute.attnum;

SELECT source_namespace.nspname AS source_schema,
       source.relname AS source_table,
       constraint_row.conname,
       constraint_row.contype,
       target_namespace.nspname AS target_schema,
       target.relname AS target_table,
       constraint_row.condeferrable,
       constraint_row.condeferred,
       constraint_row.convalidated,
       constraint_row.connoinherit,
       pg_get_constraintdef(constraint_row.oid, true) AS definition
  FROM pg_constraint AS constraint_row
  JOIN pg_class AS source ON source.oid = constraint_row.conrelid
  JOIN pg_namespace AS source_namespace
    ON source_namespace.oid = source.relnamespace
  LEFT JOIN pg_class AS target ON target.oid = constraint_row.confrelid
  LEFT JOIN pg_namespace AS target_namespace
    ON target_namespace.oid = target.relnamespace
 WHERE (
           source_namespace.nspname = 'logo'
           AND source.relname LIKE 'agent\_%' ESCAPE '\'
           AND constraint_row.contype IN ('p', 'u', 'c', 'f', 'x')
       )
    OR (
           target_namespace.nspname = 'logo'
           AND target.relname LIKE 'agent\_%' ESCAPE '\'
           AND constraint_row.contype = 'f'
       )
 ORDER BY 1, 2, 3;

SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       index_class.relname AS index_name,
       index_row.indisunique,
       index_row.indisprimary,
       index_row.indisvalid,
       index_row.indisready,
       index_row.indislive,
       index_row.indnullsnotdistinct,
       pg_get_indexdef(index_row.indexrelid) AS definition,
       pg_get_expr(index_row.indpred, index_row.indrelid, true) AS predicate
  FROM pg_index AS index_row
  JOIN pg_class AS relation ON relation.oid = index_row.indrelid
  JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
  JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid
 WHERE namespace.nspname = 'logo'
   AND relation.relname LIKE 'agent\_%' ESCAPE '\'
 ORDER BY 1, 2, 3;

-- Inventory every callable object in the writable schemas, not merely the
-- known names. Any unexplained SECURITY DEFINER path blocks release.
SELECT procedure.oid::regprocedure AS signature,
       owner.rolname AS owner,
       procedure.prokind,
       language.lanname AS language,
       procedure.prosecdef AS security_definer,
       procedure.proleakproof AS leakproof,
       procedure.provolatile AS volatility,
       procedure.proconfig AS fixed_settings,
       has_function_privilege(
           'logo_admin', procedure.oid, 'EXECUTE'
       ) AS logo_admin_execute,
       has_function_privilege(
           'public', procedure.oid, 'EXECUTE'
       ) AS public_execute,
       pg_get_function_result(procedure.oid) AS result_type,
       encode(sha256(convert_to(
           procedure.prosrc, 'UTF8'
       )), 'hex') AS source_sha256,
       encode(sha256(convert_to(
           pg_get_functiondef(procedure.oid), 'UTF8'
       )), 'hex') AS definition_sha256,
       pg_get_functiondef(procedure.oid) AS definition
  FROM pg_proc AS procedure
  JOIN pg_namespace AS namespace
    ON namespace.oid = procedure.pronamespace
  JOIN pg_roles AS owner ON owner.oid = procedure.proowner
  JOIN pg_language AS language ON language.oid = procedure.prolang
 WHERE namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
   AND procedure.prokind IN ('f', 'p')
 ORDER BY 1;

-- Raw ACL expansion makes PUBLIC and WITH GRANT OPTION visible during review.
SELECT procedure.oid::regprocedure AS signature,
       CASE
           WHEN privilege.grantee = 0 THEN 'PUBLIC'
           ELSE pg_get_userbyid(privilege.grantee)
       END AS grantee,
       pg_get_userbyid(privilege.grantor) AS grantor,
       privilege.privilege_type,
       privilege.is_grantable
  FROM pg_proc AS procedure
  JOIN pg_namespace AS namespace
    ON namespace.oid = procedure.pronamespace
 CROSS JOIN LATERAL aclexplode(coalesce(
     procedure.proacl,
     acldefault('f', procedure.proowner)
 )) AS privilege
 WHERE namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
 ORDER BY 1, 2, 4;

-- Direct grants are useful evidence, but the effective matrix below is the
-- release decision because PUBLIC also contributes authority.
SELECT table_schema, table_name, grantee, privilege_type, is_grantable
  FROM information_schema.role_table_grants
 WHERE grantee IN ('logo_admin', 'PUBLIC')
   AND table_schema <> 'information_schema'
   AND table_schema !~ '^pg_'
 ORDER BY 1, 2, 3, 4;

SELECT namespace.nspname AS schema_name,
       relation.relname AS table_name,
       attribute.attname AS column_name,
       CASE
           WHEN privilege.grantee = 0 THEN 'PUBLIC'
           ELSE pg_get_userbyid(privilege.grantee)
       END AS grantee,
       privilege.privilege_type,
       privilege.is_grantable
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  JOIN pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
   AND attribute.attnum > 0
   AND NOT attribute.attisdropped
 CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
 WHERE namespace.nspname <> 'information_schema'
   AND namespace.nspname !~ '^pg_'
   AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
   AND (
       privilege.grantee = 0
       OR privilege.grantee = (
           SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
       )
   )
 ORDER BY 1, 2, attribute.attnum, 5;

WITH table_policy(schema_name, table_name, policy) AS (
    VALUES
        ('logo', 'assignment', 'crud'),
        ('logo', 'art_record', 'read'),
        ('logo', 'store_settings', 'crud'),
        ('logo', 'placement_vocab', 'crud'),
        ('logo', 'color_class', 'crud'),
        ('logo', 'bulk_batch', 'crud'),
        ('logo', 'bulk_batch_row', 'crud'),
        ('logo', 'style_color_order', 'crud'),
        ('logo', 'default_cost', 'crud'),
        ('logo', 'design_ipc', 'crud'),
        ('logo', 'admin_session', 'crud'),
        ('logo', 'image_import', 'cru'),
        ('logo', 'import_report', 'append'),
        ('logo', 'display_name', 'crud'),
        ('logo', 'audit_log', 'append'),
        ('woo', 'price_rule', 'crud'),
        ('woo', 'price_rule_audit', 'append'),
        ('woo', 'pricing_tier', 'crud'),
        ('woo', 'store_pricing_tier', 'crud'),
        ('woo', 'sync_exclusion', 'crud'),
        ('woo', 'store_mix_store', 'crud'),
        ('woo', 'store_mix_item', 'crud'),
        ('woo', 'store_mix_audit', 'append'),
        ('woo', 'feed_consumer', 'crud'),
        ('woo', 'app_flag', 'crud'),
        ('woo', 'brand_stock_rule', 'crud'),
        ('woo', 'stock_override', 'crud'),
        ('woo', 'virtual_catalog_store', 'crud'),
        ('logo', 'assignment_tombstone', 'crud'),
        ('catmgr', 'snapshot', 'crud'),
        ('catmgr', 'wp_term', 'crud'),
        ('catmgr', 'wp_term_product', 'crud'),
        ('catmgr', 'node', 'crud'),
        ('catmgr', 'node_store_override', 'crud'),
        ('catmgr', 'slug_map', 'crud'),
        ('catmgr', 'assignment_rule', 'crud'),
        ('catmgr', 'product_assignment', 'crud'),
        ('catmgr', 'uncategorized_ack', 'crud'),
        ('catmgr', 'run', 'crud'),
        ('catmgr', 'run_job', 'crud'),
        ('catmgr', 'job_snapshot', 'crud'),
        ('catmgr', 'redirect', 'crud'),
        ('catmgr', 'audit_log', 'append'),
        ('logo', 'agent_chat_session', 'crud'),
        ('logo', 'agent_chat_message', 'crud'),
        ('logo', 'agent_change_set', 'crud'),
        ('logo', 'agent_change_set_item', 'crud'),
        ('logo', 'agent_spreadsheet_job', 'crud'),
        ('logo', 'agent_usage_daily', 'crud'),
        ('logo', 'agent_usage_monthly', 'crud'),
        ('logo', 'agent_rate_window', 'crud'),
        ('logo', 'agent_quota_reservation', 'crud'),
        ('logo', 'agent_action_journal', 'append')
), table_privilege(privilege_name) AS (
    VALUES
        ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
        ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
), inventory AS (
    SELECT format(
               '%I.%I', namespace.nspname, relation.relname
           ) AS table_name,
           privilege_name,
           CASE
               WHEN policy = 'crud'
                AND privilege_name IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
                   THEN true
               WHEN policy = 'cru'
                AND privilege_name IN ('SELECT', 'INSERT', 'UPDATE')
                   THEN true
               WHEN policy = 'append'
                AND privilege_name IN ('SELECT', 'INSERT')
                   THEN true
               WHEN policy = 'read' AND privilege_name = 'SELECT'
                   THEN true
               -- Unlisted relations: warehouse reads (woo/fdm4) plus the
               -- read-only pim/curated surfaces sql/logo_admin_role.sql grants.
               WHEN policy IS NULL THEN
                   (
                       namespace.nspname IN ('woo', 'fdm4')
                       OR namespace.nspname IN ('pim', 'curated')
                   )
                   AND privilege_name = 'SELECT'
               ELSE false
           END AS expected,
           has_table_privilege(
               'logo_admin', relation.oid, privilege_name
           ) AS actual,
           has_table_privilege(
               'public', relation.oid, privilege_name
           ) AS public_actual
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
     CROSS JOIN table_privilege
      LEFT JOIN table_policy
        ON table_policy.schema_name = namespace.nspname
       AND table_policy.table_name = relation.relname
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
)
SELECT table_name,
       privilege_name,
       expected,
       actual,
       public_actual,
       actual = expected AND NOT public_actual AS passes
  FROM inventory
 ORDER BY table_name, privilege_name;

WITH table_policy(schema_name, table_name, policy) AS (
    VALUES
        ('logo', 'assignment', 'crud'),
        ('logo', 'art_record', 'read'),
        ('logo', 'store_settings', 'crud'),
        ('logo', 'placement_vocab', 'crud'),
        ('logo', 'color_class', 'crud'),
        ('logo', 'bulk_batch', 'crud'),
        ('logo', 'bulk_batch_row', 'crud'),
        ('logo', 'style_color_order', 'crud'),
        ('logo', 'default_cost', 'crud'),
        ('logo', 'design_ipc', 'crud'),
        ('logo', 'admin_session', 'crud'),
        ('logo', 'image_import', 'cru'),
        ('logo', 'import_report', 'append'),
        ('logo', 'display_name', 'crud'),
        ('logo', 'audit_log', 'append'),
        ('woo', 'price_rule', 'crud'),
        ('woo', 'price_rule_audit', 'append'),
        ('woo', 'pricing_tier', 'crud'),
        ('woo', 'store_pricing_tier', 'crud'),
        ('woo', 'sync_exclusion', 'crud'),
        ('woo', 'store_mix_store', 'crud'),
        ('woo', 'store_mix_item', 'crud'),
        ('woo', 'store_mix_audit', 'append'),
        ('woo', 'feed_consumer', 'crud'),
        ('woo', 'app_flag', 'crud'),
        ('woo', 'brand_stock_rule', 'crud'),
        ('woo', 'stock_override', 'crud'),
        ('woo', 'virtual_catalog_store', 'crud'),
        ('logo', 'assignment_tombstone', 'crud'),
        ('catmgr', 'snapshot', 'crud'),
        ('catmgr', 'wp_term', 'crud'),
        ('catmgr', 'wp_term_product', 'crud'),
        ('catmgr', 'node', 'crud'),
        ('catmgr', 'node_store_override', 'crud'),
        ('catmgr', 'slug_map', 'crud'),
        ('catmgr', 'assignment_rule', 'crud'),
        ('catmgr', 'product_assignment', 'crud'),
        ('catmgr', 'uncategorized_ack', 'crud'),
        ('catmgr', 'run', 'crud'),
        ('catmgr', 'run_job', 'crud'),
        ('catmgr', 'job_snapshot', 'crud'),
        ('catmgr', 'redirect', 'crud'),
        ('catmgr', 'audit_log', 'append'),
        ('logo', 'agent_chat_session', 'crud'),
        ('logo', 'agent_chat_message', 'crud'),
        ('logo', 'agent_change_set', 'crud'),
        ('logo', 'agent_change_set_item', 'crud'),
        ('logo', 'agent_spreadsheet_job', 'crud'),
        ('logo', 'agent_usage_daily', 'crud'),
        ('logo', 'agent_usage_monthly', 'crud'),
        ('logo', 'agent_rate_window', 'crud'),
        ('logo', 'agent_quota_reservation', 'crud'),
        ('logo', 'agent_action_journal', 'append')
), column_privilege(privilege_name) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
), inventory AS (
    SELECT format(
               '%I.%I', namespace.nspname, relation.relname
           ) AS table_name,
           attribute.attname AS column_name,
           privilege_name,
           CASE
               WHEN policy = 'crud' THEN privilege_name IN (
                   'SELECT', 'INSERT', 'UPDATE'
               )
               WHEN policy = 'cru' THEN privilege_name IN (
                   'SELECT', 'INSERT', 'UPDATE'
               )
               WHEN policy = 'append' THEN privilege_name IN (
                   'SELECT', 'INSERT'
               )
               WHEN policy = 'read' THEN privilege_name = 'SELECT'
               ELSE (
                        namespace.nspname IN ('woo', 'fdm4')
                        OR namespace.nspname IN ('pim', 'curated')
                    )
                    AND privilege_name = 'SELECT'
           END AS expected,
           has_column_privilege(
               'logo_admin', relation.oid, attribute.attnum, privilege_name
           ) AS actual,
           has_column_privilege(
               'public', relation.oid, attribute.attnum, privilege_name
           ) AS public_actual
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      JOIN pg_attribute AS attribute
        ON attribute.attrelid = relation.oid
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
     CROSS JOIN column_privilege
      LEFT JOIN table_policy
        ON table_policy.schema_name = namespace.nspname
       AND table_policy.table_name = relation.relname
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
)
SELECT table_name,
       column_name,
       privilege_name,
       expected,
       actual,
       public_actual,
       actual = expected AND NOT public_actual AS passes
  FROM inventory
 ORDER BY table_name, column_name, privilege_name;

WITH sequence_policy(schema_name, sequence_name, policy) AS (
    VALUES
        ('logo', 'audit_log_id_seq', 'usage_select'),
        ('logo', 'import_report_id_seq', 'usage_select'),
        ('woo', 'price_rule_rule_id_seq', 'usage'),
        ('woo', 'price_rule_audit_id_seq', 'usage'),
        ('woo', 'store_mix_audit_id_seq', 'usage'),
        ('logo', 'assignment_version_seq', 'usage'),
        ('catmgr', 'assignment_rule_rule_id_seq', 'usage'),
        ('catmgr', 'audit_log_id_seq', 'usage'),
        ('catmgr', 'node_node_id_seq', 'usage'),
        ('catmgr', 'node_store_override_override_id_seq', 'usage'),
        ('catmgr', 'product_assignment_id_seq', 'usage'),
        ('catmgr', 'redirect_id_seq', 'usage'),
        ('catmgr', 'run_job_job_id_seq', 'usage'),
        ('catmgr', 'run_run_id_seq', 'usage')
), sequence_privilege(privilege_name) AS (
    VALUES ('USAGE'), ('SELECT'), ('UPDATE')
), inventory AS (
    SELECT format(
               '%I.%I', namespace.nspname, relation.relname
           ) AS sequence_name,
           privilege_name,
           CASE sequence_policy.policy
               WHEN 'usage_select' THEN privilege_name IN ('USAGE', 'SELECT')
               WHEN 'usage' THEN privilege_name = 'USAGE'
               ELSE false
           END AS expected,
           has_sequence_privilege(
               'logo_admin', relation.oid, privilege_name
           ) AS actual,
           has_sequence_privilege(
               'public', relation.oid, privilege_name
           ) AS public_actual
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
     CROSS JOIN sequence_privilege
      LEFT JOIN sequence_policy
        ON sequence_policy.schema_name = namespace.nspname
       AND sequence_policy.sequence_name = relation.relname
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND relation.relkind = 'S'
)
SELECT sequence_name,
       privilege_name,
       expected,
       actual,
       public_actual,
       actual = expected AND NOT public_actual AS passes
  FROM inventory
 ORDER BY sequence_name, privilege_name;

SELECT count(*) = 0 AS no_agent_sequences
  FROM pg_class AS relation
  JOIN pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
 WHERE namespace.nspname = 'logo'
   AND relation.relkind = 'S'
   AND relation.relname LIKE 'agent\_%' ESCAPE '\';

-- The only definer write path available to logo_admin is fixed, owner-bound,
-- non-grantable, and unavailable to PUBLIC.
SELECT procedure.oid::regprocedure AS signature,
       function_owner.rolname AS owner,
       function_owner.rolsuper AS owner_is_superuser,
       database_owner.rolname AS expected_owner,
       (
           procedure.proowner = database.datdba
           OR function_owner.rolsuper
       ) AS owner_passes,
       procedure.prokind = 'f' AS function_kind_passes,
       procedure.pronargs = 0 AS zero_arguments_passes,
       language.lanname AS language,
       procedure.prosecdef AS security_definer,
       procedure.proconfig AS fixed_settings,
       procedure.proconfig = ARRAY[
           'search_path=pg_catalog, logo'
       ]::text[] AS fixed_search_path_passes,
       pg_get_function_result(procedure.oid) AS result_type,
       has_function_privilege(
           'logo_admin', procedure.oid, 'EXECUTE'
       ) AS logo_admin_execute,
       NOT has_function_privilege(
           'public', procedure.oid, 'EXECUTE'
       ) AS public_execute_denied,
       NOT EXISTS (
           SELECT 1
             FROM aclexplode(coalesce(
                 procedure.proacl,
                 acldefault('f', procedure.proowner)
             )) AS privilege
            WHERE privilege.grantee = (
                SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
            )
              AND privilege.privilege_type = 'EXECUTE'
              AND privilege.is_grantable
       ) AS execute_not_grantable,
       pg_get_functiondef(procedure.oid) AS definition
  FROM pg_proc AS procedure
  JOIN pg_namespace AS namespace
    ON namespace.oid = procedure.pronamespace
  JOIN pg_roles AS function_owner
    ON function_owner.oid = procedure.proowner
  JOIN pg_language AS language
    ON language.oid = procedure.prolang
  JOIN pg_database AS database
    ON database.datname = current_database()
  JOIN pg_roles AS database_owner
    ON database_owner.oid = database.datdba
 WHERE procedure.oid = to_regprocedure('logo.prune_agent_history()')
   AND namespace.nspname = 'logo';

-- Terminal assertion. ON_ERROR_STOP makes division-by-zero exit psql nonzero,
-- so this diagnostic cannot be mistaken for an approval when any invariant is
-- false. The detailed inventories above identify the failing contract.
WITH execution_contract AS (
    SELECT current_user = 'logo_admin'
           AND session_user = 'logo_admin'
           AND current_setting('transaction_read_only') = 'on' AS passes
), role_contract AS (
    SELECT count(*) = 1
           AND bool_and(
               role.rolcanlogin
               AND NOT role.rolinherit
               AND NOT role.rolsuper
               AND NOT role.rolcreatedb
               AND NOT role.rolcreaterole
               AND NOT role.rolreplication
               AND NOT role.rolbypassrls
               AND role.rolconnlimit = 12
               AND (
                   SELECT array_agg(
                              replace(setting, ' ', '')
                              ORDER BY replace(setting, ' ', '')
                          )
                     FROM unnest(role.rolconfig) AS setting
               ) = ARRAY[
                   'idle_in_transaction_session_timeout=30s',
                   'lock_timeout=5s',
                   'search_path=logo,woo,fdm4,pg_catalog',
                   'statement_timeout=30s'
               ]::text[]
               AND NOT EXISTS (
                   SELECT 1
                     FROM pg_db_role_setting AS setting
                     JOIN pg_database AS database
                       ON database.oid = setting.setdatabase
                    WHERE setting.setrole = role.oid
                      AND database.datname = current_database()
               )
           ) AS passes
      FROM pg_roles AS role
     WHERE role.rolname = 'logo_admin'
), membership_contract AS (
    SELECT count(*) = 0 AS passes
      FROM pg_auth_members AS membership
      JOIN pg_roles AS member_role
        ON member_role.oid = membership.member
     WHERE member_role.rolname = 'logo_admin'
), database_contract AS (
    SELECT has_database_privilege(
               'logo_admin', current_database(), 'CONNECT'
           )
           AND NOT has_database_privilege(
               'logo_admin', current_database(), 'CREATE'
           )
           AND NOT has_database_privilege(
               'logo_admin', current_database(), 'TEMPORARY'
           ) AS passes
), schema_policy(schema_name, usage_expected, create_expected) AS (
    VALUES
        ('logo', true, false),
        ('woo', true, false),
        ('fdm4', true, false),
        ('public', NULL::boolean, false)
), schema_contract AS (
    SELECT coalesce(bool_and(
               (usage_expected IS NULL OR has_schema_privilege(
                   'logo_admin', schema_name, 'USAGE'
               ) = usage_expected)
               AND has_schema_privilege(
                   'logo_admin', schema_name, 'CREATE'
               ) = create_expected
           ), false)
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_namespace AS namespace
                WHERE namespace.nspname <> 'information_schema'
                  AND namespace.nspname !~ '^pg_'
                  AND has_schema_privilege(
                      'logo_admin', namespace.oid, 'CREATE'
                  )
           ) AS passes
      FROM schema_policy
), ownership_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM pg_namespace AS namespace
                WHERE namespace.nspowner = (
                      SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
                  )
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname !~ '^pg_'
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                WHERE relation.relowner = (
                      SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
                  )
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname !~ '^pg_'
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_proc AS procedure
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = procedure.pronamespace
                WHERE procedure.proowner = (
                      SELECT oid FROM pg_roles WHERE rolname = 'logo_admin'
                  )
                  AND namespace.nspname <> 'information_schema'
                  AND namespace.nspname !~ '^pg_'
           ) AS passes
), table_policy(schema_name, table_name, policy, required) AS (
    VALUES
        ('logo', 'assignment', 'crud', true),
        ('logo', 'art_record', 'read', true),
        ('logo', 'store_settings', 'crud', true),
        ('logo', 'placement_vocab', 'crud', true),
        ('logo', 'color_class', 'crud', true),
        ('logo', 'bulk_batch', 'crud', true),
        ('logo', 'bulk_batch_row', 'crud', true),
        ('logo', 'style_color_order', 'crud', true),
        ('logo', 'default_cost', 'crud', true),
        ('logo', 'design_ipc', 'crud', true),
        ('logo', 'admin_session', 'crud', true),
        ('logo', 'image_import', 'cru', true),
        ('logo', 'import_report', 'append', true),
        ('logo', 'display_name', 'crud', true),
        ('logo', 'audit_log', 'append', true),
        ('woo', 'price_rule', 'crud', true),
        ('woo', 'price_rule_audit', 'append', true),
        ('woo', 'pricing_tier', 'crud', true),
        ('woo', 'store_pricing_tier', 'crud', true),
        ('woo', 'sync_exclusion', 'crud', true),
        ('woo', 'store_mix_store', 'crud', true),
        ('woo', 'store_mix_item', 'crud', true),
        ('woo', 'store_mix_audit', 'append', true),
        ('woo', 'feed_consumer', 'crud', true),
        ('woo', 'app_flag', 'crud', true),
        ('woo', 'brand_stock_rule', 'crud', true),
        ('woo', 'stock_override', 'crud', true),
        ('woo', 'virtual_catalog_store', 'crud', true),
        ('logo', 'assignment_tombstone', 'crud', true),
        ('catmgr', 'snapshot', 'crud', true),
        ('catmgr', 'wp_term', 'crud', true),
        ('catmgr', 'wp_term_product', 'crud', true),
        ('catmgr', 'node', 'crud', true),
        ('catmgr', 'node_store_override', 'crud', true),
        ('catmgr', 'slug_map', 'crud', true),
        ('catmgr', 'assignment_rule', 'crud', true),
        ('catmgr', 'product_assignment', 'crud', true),
        ('catmgr', 'uncategorized_ack', 'crud', true),
        ('catmgr', 'run', 'crud', true),
        ('catmgr', 'run_job', 'crud', true),
        ('catmgr', 'job_snapshot', 'crud', true),
        ('catmgr', 'redirect', 'crud', true),
        ('catmgr', 'audit_log', 'append', true),
        ('logo', 'agent_chat_session', 'crud', true),
        ('logo', 'agent_chat_message', 'crud', true),
        ('logo', 'agent_change_set', 'crud', true),
        ('logo', 'agent_change_set_item', 'crud', true),
        ('logo', 'agent_spreadsheet_job', 'crud', true),
        ('logo', 'agent_usage_daily', 'crud', true),
        ('logo', 'agent_usage_monthly', 'crud', true),
        ('logo', 'agent_rate_window', 'crud', true),
        ('logo', 'agent_quota_reservation', 'crud', true),
        ('logo', 'agent_action_journal', 'append', true)
), table_privilege(privilege_name) AS (
    VALUES
        ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
        ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
), table_inventory AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS table_name,
           policy,
           privilege_name,
           has_table_privilege(
               'logo_admin', relation.oid, privilege_name
           ) AS actual,
           has_table_privilege(
               'public', relation.oid, privilege_name
           ) AS public_actual
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
     CROSS JOIN table_privilege
      LEFT JOIN table_policy
        ON table_policy.schema_name = namespace.nspname
       AND table_policy.table_name = relation.relname
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
), table_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM table_policy
                WHERE required
                  AND to_regclass(format(
                      '%I.%I', schema_name, table_name
                  )) IS NULL
           )
           AND coalesce(bool_and(
               CASE
                   WHEN policy = 'crud' THEN actual = (
                       privilege_name IN (
                           'SELECT', 'INSERT', 'UPDATE', 'DELETE'
                       )
                   )
                   WHEN policy = 'cru' THEN actual = (
                       privilege_name IN ('SELECT', 'INSERT', 'UPDATE')
                   )
                   WHEN policy = 'append' THEN actual = (
                       privilege_name IN ('SELECT', 'INSERT')
                   )
                   WHEN policy = 'read' THEN actual = (
                       privilege_name = 'SELECT'
                   )
                   ELSE actual = (
                       (
                           schema_name IN ('woo', 'fdm4')
                           OR schema_name IN ('pim', 'curated')
                       )
                       AND privilege_name = 'SELECT'
                   )
               END
               AND NOT public_actual
           ), false)
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(coalesce(
                    relation.relacl,
                    acldefault('r', relation.relowner)
                )) AS privilege
                WHERE namespace.nspname <> 'information_schema'
                  AND namespace.nspname !~ '^pg_'
                  AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
                  AND privilege.grantee = (
                      SELECT oid
                        FROM pg_roles
                       WHERE rolname = 'logo_admin'
                  )
                  AND privilege.is_grantable
           ) AS passes
      FROM table_inventory
), writable_relation_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM table_policy
                 LEFT JOIN pg_namespace AS namespace
                   ON namespace.nspname = table_policy.schema_name
                 LEFT JOIN pg_class AS relation
                   ON relation.relnamespace = namespace.oid
                  AND relation.relname = table_policy.table_name
                WHERE (table_policy.required AND relation.oid IS NULL)
                   OR (
                       relation.oid IS NOT NULL
                       AND (
                           relation.relkind <> 'r'
                           OR relation.relpersistence <> 'p'
                           OR relation.relispartition
                           OR (
                               relation.relowner IS DISTINCT FROM (
                                   SELECT database.datdba
                                     FROM pg_database AS database
                                    WHERE database.datname = current_database()
                               )
                               AND NOT EXISTS (
                                   SELECT 1
                                     FROM pg_roles AS relation_owner
                                    WHERE relation_owner.oid = relation.relowner
                                      AND relation_owner.rolsuper
                               )
                           )
                       )
                   )
           ) AS passes
), column_privilege(privilege_name) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
), column_inventory AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS table_name,
           policy,
           attribute.attname AS column_name,
           privilege_name,
           has_column_privilege(
               'logo_admin', relation.oid, attribute.attnum, privilege_name
           ) AS actual,
           has_column_privilege(
               'public', relation.oid, attribute.attnum, privilege_name
           ) AS public_actual
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      JOIN pg_attribute AS attribute
        ON attribute.attrelid = relation.oid
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
     CROSS JOIN column_privilege
      LEFT JOIN table_policy
        ON table_policy.schema_name = namespace.nspname
       AND table_policy.table_name = relation.relname
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
), column_contract AS (
    SELECT coalesce(bool_and(
               CASE
                   WHEN policy = 'crud' THEN actual = (
                       privilege_name IN ('SELECT', 'INSERT', 'UPDATE')
                   )
                   WHEN policy = 'cru' THEN actual = (
                       privilege_name IN ('SELECT', 'INSERT', 'UPDATE')
                   )
                   WHEN policy = 'append' THEN actual = (
                       privilege_name IN ('SELECT', 'INSERT')
                   )
                   WHEN policy = 'read' THEN actual = (
                       privilege_name = 'SELECT'
                   )
                   ELSE actual = (
                       (
                           schema_name IN ('woo', 'fdm4')
                           OR schema_name IN ('pim', 'curated')
                       )
                       AND privilege_name = 'SELECT'
                   )
               END
               AND NOT public_actual
           ), false)
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                 JOIN pg_attribute AS attribute
                   ON attribute.attrelid = relation.oid
                  AND attribute.attnum > 0
                  AND NOT attribute.attisdropped
                CROSS JOIN LATERAL aclexplode(
                    attribute.attacl
                ) AS privilege
                WHERE namespace.nspname <> 'information_schema'
                  AND namespace.nspname !~ '^pg_'
                  AND relation.relkind IN ('r', 'p', 'v', 'f', 'm')
                  AND privilege.grantee = (
                      SELECT oid
                        FROM pg_roles
                       WHERE rolname = 'logo_admin'
                  )
                  AND privilege.is_grantable
           ) AS passes
      FROM column_inventory
), restore_column_policy(
    schema_name, table_name, column_name, formatted_type, nullable,
    default_signature
) AS (
    VALUES
        ('logo', 'assignment', 'fdm4_store', 'text', false, NULL),
        ('logo', 'assignment', 'product_style', 'text', false, NULL),
        ('logo', 'assignment', 'garment_color_code', 'text', false, NULL),
        ('logo', 'assignment', 'position', 'smallint', false, '1'),
        ('logo', 'assignment', 'option_row', 'integer', false, '1'),
        ('logo', 'assignment', 'design_id', 'text', false, NULL),
        ('logo', 'assignment', 'logo_code', 'text', false, ''''''),
        ('logo', 'assignment', 'color_scheme_id', 'text', false, ''''''),
        ('logo', 'assignment', 'location', 'text', false, ''''''),
        ('logo', 'assignment', 'optional', 'boolean', false, 'false'),
        ('logo', 'assignment', 'background', 'text', false, ''''''),
        ('logo', 'assignment', 'cost_override', 'numeric(12,2)', true, NULL),
        ('logo', 'assignment', 'sort_order', 'integer', false, '0'),
        ('logo', 'assignment', 'image_url', 'text', false, ''''''),
        ('logo', 'assignment', 'active', 'boolean', false, 'true'),
        ('logo', 'assignment', 'updated_by', 'text', false, '''seed'''),
        ('logo', 'assignment', 'updated_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'assignment', 'name_override', 'text', true, NULL),
        ('logo', 'assignment', 'row_version', 'bigint', false, NULL),
        ('logo', 'assignment', 'catalog_id', 'text', true, NULL),
        ('logo', 'store_settings', 'fdm4_store', 'text', false, NULL),
        ('logo', 'store_settings', 'enabled', 'boolean', false, 'true'),
        ('logo', 'store_settings', 'allows_none', 'boolean', false, 'false'),
        ('logo', 'store_settings', 'updated_by', 'text', false, ''''''),
        ('logo', 'store_settings', 'updated_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'store_settings', 'extra_customers', 'text[]', false, '''{}''[]'),
        ('woo', 'store_pricing_tier', 'fdm4_store', 'text', false, NULL),
        ('woo', 'store_pricing_tier', 'tier_name', 'text', false, NULL),
        ('woo', 'store_pricing_tier', 'note', 'text', false, ''''''),
        ('woo', 'store_pricing_tier', 'updated_at', 'timestamp with time zone', false, 'now')
), restore_column_order_policy(schema_name, table_name, column_names) AS (
    VALUES
        ('logo', 'assignment', ARRAY[
            'fdm4_store', 'product_style', 'garment_color_code', 'position',
            'design_id', 'logo_code', 'color_scheme_id',
            'location', 'optional', 'background', 'cost_override',
            'sort_order', 'image_url', 'active', 'updated_by', 'updated_at',
            'option_row', 'name_override', 'row_version', 'catalog_id'
        ]::text[]),
        ('logo', 'store_settings', ARRAY[
            'fdm4_store', 'enabled', 'allows_none', 'updated_by', 'updated_at',
            'extra_customers'
        ]::text[]),
        ('woo', 'store_pricing_tier', ARRAY[
            'fdm4_store', 'tier_name', 'note', 'updated_at'
        ]::text[])
), restore_column_inventory AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS table_name,
           attribute.attname AS column_name,
           attribute.attnum::integer AS ordinal_position,
           format_type(
               attribute.atttypid, attribute.atttypmod
           ) AS formatted_type,
           NOT attribute.attnotnull AS nullable,
           attribute.attgenerated AS generated_kind,
           attribute.attidentity AS identity_kind,
           pg_collation.collname AS collation_name,
           CASE WHEN default_row.oid IS NULL THEN NULL ELSE regexp_replace(
               regexp_replace(
                   lower(pg_get_expr(
                       default_row.adbin, default_row.adrelid, true
                   )),
                   '::(timestamp (with|without) time zone|character varying|smallint|integer|bigint|text|date|jsonb|interval|boolean|uuid|numeric)\M',
                   '', 'g'
               ),
               '[[:space:]()"]', '', 'g'
           ) END AS default_signature
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      JOIN pg_attribute AS attribute
        ON attribute.attrelid = relation.oid
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
      LEFT JOIN pg_collation
        ON pg_collation.oid = attribute.attcollation
      LEFT JOIN pg_attrdef AS default_row
        ON default_row.adrelid = relation.oid
       AND default_row.adnum = attribute.attnum
     WHERE (namespace.nspname, relation.relname) IN (
         ('logo', 'assignment'),
         ('logo', 'store_settings'),
         ('woo', 'store_pricing_tier')
     )
), restore_column_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM restore_column_policy AS expected
                 LEFT JOIN restore_column_inventory AS actual
                   ON actual.schema_name = expected.schema_name
                  AND actual.table_name = expected.table_name
                  AND actual.column_name = expected.column_name
                WHERE actual.column_name IS NULL
                   OR actual.formatted_type <> expected.formatted_type
                   OR actual.nullable <> expected.nullable
                   OR actual.generated_kind <> ''
                   OR actual.identity_kind <> ''
                   OR actual.collation_name IS DISTINCT FROM CASE
                       WHEN expected.formatted_type IN ('text', 'text[]')
                           THEN 'default'
                       ELSE NULL
                   END
                   OR actual.default_signature IS DISTINCT FROM
                      expected.default_signature
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM restore_column_inventory AS actual
                 LEFT JOIN restore_column_policy AS expected
                   ON expected.schema_name = actual.schema_name
                  AND expected.table_name = actual.table_name
                  AND expected.column_name = actual.column_name
                WHERE expected.column_name IS NULL
           ) AS passes
), restore_constraint_inventory AS (
    SELECT source_namespace.nspname AS source_schema,
           source.relname AS source_table,
           constraint_row.conname AS constraint_name,
           constraint_row.contype AS constraint_type,
           ARRAY(
               SELECT attribute.attname::text
                 FROM unnest(constraint_row.conkey)
                      WITH ORDINALITY AS key_column(
                          attnum, ordinal_position
                      )
                 JOIN pg_attribute AS attribute
                   ON attribute.attrelid = source.oid
                  AND attribute.attnum = key_column.attnum
                ORDER BY key_column.ordinal_position
           ) AS key_columns,
           target_namespace.nspname AS target_schema,
           target.relname AS target_table,
           ARRAY(
               SELECT attribute.attname::text
                 FROM unnest(constraint_row.confkey)
                      WITH ORDINALITY AS key_column(
                          attnum, ordinal_position
                      )
                 JOIN pg_attribute AS attribute
                   ON attribute.attrelid = target.oid
                  AND attribute.attnum = key_column.attnum
                ORDER BY key_column.ordinal_position
           ) AS referenced_columns,
           constraint_row.confupdtype AS update_action,
           constraint_row.confdeltype AS delete_action,
           constraint_row.confmatchtype AS match_type,
           constraint_row.condeferrable AS is_deferrable,
           constraint_row.condeferred AS initially_deferred,
           constraint_row.convalidated AS validated,
           constraint_row.connoinherit AS no_inherit,
           regexp_replace(
               replace(replace(replace(
                   lower(pg_get_expr(
                       constraint_row.conbin,
                       constraint_row.conrelid,
                       true
                   )),
                   '::smallint', ''
               ), '::integer', ''), '::bigint', ''),
               '[[:space:]()"]', '', 'g'
           ) AS check_expression
      FROM pg_constraint AS constraint_row
      JOIN pg_class AS source ON source.oid = constraint_row.conrelid
      JOIN pg_namespace AS source_namespace
        ON source_namespace.oid = source.relnamespace
      LEFT JOIN pg_class AS target ON target.oid = constraint_row.confrelid
      LEFT JOIN pg_namespace AS target_namespace
        ON target_namespace.oid = target.relnamespace
     WHERE (
               (source_namespace.nspname, source.relname) IN (
                   ('logo', 'assignment'),
                   ('logo', 'store_settings'),
                   ('woo', 'store_pricing_tier')
               )
               AND constraint_row.contype IN ('p', 'c', 'f', 'u', 'x')
           )
        OR (
               (target_namespace.nspname, target.relname) IN (
                   ('logo', 'assignment'),
                   ('logo', 'store_settings'),
                   ('woo', 'store_pricing_tier')
               )
               AND constraint_row.contype = 'f'
           )
), restore_constraint_contract AS (
    SELECT count(*) = 7
           AND bool_and(
               NOT is_deferrable
               AND NOT initially_deferred
               AND validated
               -- PostgreSQL marks index-backed and foreign-key constraints
               -- NO INHERIT; the flag is only a policy signal on CHECKs.
               AND (constraint_type <> 'c' OR NOT no_inherit)
               AND CASE
                   WHEN source_schema = 'logo'
                    AND source_table = 'assignment'
                    AND constraint_name = 'assignment_pkey'
                    AND constraint_type = 'p'
                       THEN key_columns = ARRAY[
                           'fdm4_store', 'product_style',
                           'garment_color_code', 'option_row', 'position'
                       ]::text[]
                   WHEN source_schema = 'logo'
                    AND source_table = 'store_settings'
                    AND constraint_name = 'store_settings_pkey'
                    AND constraint_type = 'p'
                       THEN key_columns = ARRAY['fdm4_store']::text[]
                   WHEN source_schema = 'woo'
                    AND source_table = 'store_pricing_tier'
                    AND constraint_name = 'store_pricing_tier_pkey'
                    AND constraint_type = 'p'
                       THEN key_columns = ARRAY['fdm4_store']::text[]
                   WHEN source_schema = 'logo'
                    AND source_table = 'assignment'
                    AND constraint_name =
                        'logo_assignment_position_check'
                    AND constraint_type = 'c'
                       THEN key_columns = ARRAY['position']::text[]
                           AND check_expression =
                               'position>=1andposition<=3'
                   WHEN source_schema = 'logo'
                    AND source_table = 'assignment'
                    AND constraint_name = 'assignment_option_row_check'
                    AND constraint_type = 'c'
                       THEN key_columns = ARRAY['option_row']::text[]
                           AND check_expression = 'option_row>=1'
                   WHEN source_schema = 'logo'
                    AND source_table = 'assignment'
                    AND constraint_name =
                        'logo_assignment_option_row_check'
                    AND constraint_type = 'c'
                       THEN key_columns = ARRAY['option_row']::text[]
                           AND check_expression =
                               'option_row>=1andoption_row<=999'
                   WHEN source_schema = 'woo'
                    AND source_table = 'store_pricing_tier'
                    AND constraint_name =
                        'store_pricing_tier_tier_name_fkey'
                    AND constraint_type = 'f'
                       THEN key_columns = ARRAY['tier_name']::text[]
                           AND target_schema = 'woo'
                           AND target_table = 'pricing_tier'
                           AND referenced_columns =
                               ARRAY['tier_name']::text[]
                           AND update_action = 'a'
                           AND delete_action = 'a'
                           AND match_type = 's'
                   ELSE false
               END
           ) AS passes
      FROM restore_constraint_inventory
), restore_unique_index_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM pg_index AS index_row
                 JOIN pg_class AS relation
                   ON relation.oid = index_row.indrelid
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                WHERE (namespace.nspname, relation.relname) IN (
                    ('logo', 'assignment'),
                    ('logo', 'store_settings'),
                    ('woo', 'store_pricing_tier')
                )
                  AND index_row.indisunique
                  AND NOT EXISTS (
                      SELECT 1
                        FROM pg_constraint AS constraint_row
                       WHERE constraint_row.conindid = index_row.indexrelid
                  )
           ) AS passes
), trigger_inventory AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS table_name,
           trigger.tgname AS trigger_name,
           trigger.tgtype::integer AS trigger_type,
           trigger.tgenabled AS enabled,
           function_namespace.nspname AS function_schema,
           function_row.proname AS function_name,
           oidvectortypes(function_row.proargtypes) AS argument_types,
           trigger.tgnargs AS argument_count,
           trigger.tgqual IS NULL AS no_when_clause,
           trigger.tgconstraint = 0 AS not_constraint_trigger
      FROM pg_trigger AS trigger
      JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      JOIN pg_proc AS function_row ON function_row.oid = trigger.tgfoid
      JOIN pg_namespace AS function_namespace
        ON function_namespace.oid = function_row.pronamespace
      JOIN table_policy
        ON table_policy.schema_name = namespace.nspname
       AND table_policy.table_name = relation.relname
     WHERE NOT trigger.tgisinternal
), trigger_contract AS (
    -- Mirrors EXPECTED_TRIGGERS in logo-admin/database_contract.py: the
    -- audit triggers (tgtype 29) plus the feed-versioning triggers on
    -- logo.assignment (stamp 23 = BEFORE INSERT OR UPDATE, tombstone 9 =
    -- AFTER DELETE).
    SELECT count(*) = 9
           AND count(*) FILTER (
               WHERE schema_name = 'logo'
                 AND table_name = 'assignment'
                 AND trigger_name = 'logo_assignment_audit'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'logo'
                 AND table_name = 'assignment'
                 AND trigger_name = 'assignment_feed_stamp'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'logo'
                 AND table_name = 'assignment'
                 AND trigger_name = 'assignment_feed_tombstone'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'logo'
                 AND table_name = 'store_settings'
                 AND trigger_name = 'logo_store_settings_audit'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'logo'
                 AND table_name = 'color_class'
                 AND trigger_name = 'logo_color_class_audit'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'logo'
                 AND table_name = 'display_name'
                 AND trigger_name = 'logo_display_name_audit'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'woo'
                 AND table_name = 'price_rule'
                 AND trigger_name = 'price_rule_audit'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'woo'
                 AND table_name = 'store_mix_store'
                 AND trigger_name = 'store_mix_store_audit'
           ) = 1
           AND count(*) FILTER (
               WHERE schema_name = 'woo'
                 AND table_name = 'store_mix_item'
                 AND trigger_name = 'store_mix_item_audit'
           ) = 1
           AND bool_and(
               enabled = 'O'
               AND CASE
                   WHEN schema_name = 'logo' AND table_name = 'assignment'
                    AND trigger_name = 'assignment_feed_stamp'
                       THEN trigger_type = 23
                            AND function_schema = 'logo'
                            AND function_name = 'assignment_feed_stamp'
                   WHEN schema_name = 'logo' AND table_name = 'assignment'
                    AND trigger_name = 'assignment_feed_tombstone'
                       THEN trigger_type = 9
                            AND function_schema = 'logo'
                            AND function_name = 'assignment_feed_tombstone'
                   WHEN schema_name = 'logo' AND table_name IN (
                       'assignment', 'store_settings', 'color_class'
                   )
                       THEN trigger_type = 29
                            AND function_schema = 'logo'
                            AND function_name = 'audit_row'
                   WHEN schema_name = 'logo' AND table_name = 'display_name'
                       THEN trigger_type = 29
                            AND function_schema = 'logo'
                            AND function_name = 'audit_display_name_row'
                   WHEN schema_name = 'woo' AND table_name = 'price_rule'
                       THEN trigger_type = 29
                            AND function_schema = 'woo'
                            AND function_name = 'audit_price_rule_row'
                   WHEN schema_name = 'woo' AND table_name IN (
                       'store_mix_store', 'store_mix_item'
                   )
                       THEN trigger_type = 29
                            AND function_schema = 'woo'
                            AND function_name = 'audit_store_mix_row'
                   ELSE false
               END
               AND argument_types = ''
               AND argument_count = 0
               AND no_when_clause
               AND not_constraint_trigger
           ) AS passes
      FROM trigger_inventory
), rule_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM pg_rewrite AS rewrite
                 JOIN pg_class AS relation
                   ON relation.oid = rewrite.ev_class
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                 JOIN table_policy
                   ON table_policy.schema_name = namespace.nspname
                  AND table_policy.table_name = relation.relname
                WHERE rewrite.rulename <> '_RETURN'
           ) AS passes
), rls_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                 JOIN table_policy
                   ON table_policy.schema_name = namespace.nspname
                  AND table_policy.table_name = relation.relname
                WHERE relation.relrowsecurity
                   OR relation.relforcerowsecurity
                   OR EXISTS (
                       SELECT 1
                         FROM pg_policy AS policy
                        WHERE policy.polrelid = relation.oid
                   )
           ) AS passes
), audit_function_contract AS (
    SELECT count(*) = 1
           AND bool_and(
               (
                   procedure.proowner = database.datdba
                   OR function_owner.rolsuper
               )
               AND procedure.prokind = 'f'
               AND procedure.pronargs = 0
               AND language.lanname = 'plpgsql'
               AND NOT procedure.prosecdef
               AND procedure.proconfig IS NULL
               AND pg_get_function_result(procedure.oid) = 'trigger'
               AND encode(sha256(convert_to(
                   procedure.prosrc, 'UTF8'
               )), 'hex') =
                   '0ffa5f09bd205a694dfe288347074a85195458d1eb3ae74577d4723343d7e58b'
               AND NOT has_function_privilege(
                   'logo_admin', procedure.oid, 'EXECUTE'
               )
               AND NOT has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               )
           ) AS passes
      FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace
        ON namespace.oid = procedure.pronamespace
      JOIN pg_language AS language ON language.oid = procedure.prolang
      JOIN pg_roles AS function_owner
        ON function_owner.oid = procedure.proowner
      JOIN pg_database AS database
        ON database.datname = current_database()
     WHERE procedure.oid = to_regprocedure('logo.audit_row()')
       AND namespace.nspname = 'logo'
), agent_table_policy AS (
    SELECT schema_name, table_name
      FROM table_policy
     WHERE schema_name = 'logo'
       AND table_name LIKE 'agent\_%' ESCAPE '\'
), agent_relation_contract AS (
    SELECT count(*) = 10
           AND bool_and(
               relation.relkind = 'r'
               AND relation.relpersistence = 'p'
               AND NOT relation.relispartition
               AND (
                   relation.relowner = database.datdba
                   OR relation_owner.rolsuper
               )
               AND access_method.amname = 'heap'
               AND relation.reloptions IS NULL
               AND relation.relreplident = 'd'
               AND relation.reltablespace = 0
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_class AS candidate
                 JOIN pg_namespace AS candidate_namespace
                   ON candidate_namespace.oid = candidate.relnamespace
                WHERE candidate_namespace.nspname = 'logo'
                  AND candidate.relname LIKE 'agent\_%' ESCAPE '\'
                  AND candidate.relkind IN ('r', 'p', 'v', 'f', 'm')
                  AND NOT EXISTS (
                      SELECT 1
                        FROM agent_table_policy
                       WHERE agent_table_policy.table_name = candidate.relname
                  )
           ) AS passes
      FROM agent_table_policy
      JOIN pg_namespace AS namespace
        ON namespace.nspname = agent_table_policy.schema_name
      JOIN pg_class AS relation
        ON relation.relnamespace = namespace.oid
       AND relation.relname = agent_table_policy.table_name
      JOIN pg_am AS access_method ON access_method.oid = relation.relam
      JOIN pg_roles AS relation_owner
        ON relation_owner.oid = relation.relowner
      JOIN pg_database AS database
        ON database.datname = current_database()
), agent_column_policy(
    schema_name, table_name, column_name, formatted_type, nullable,
    default_signature
) AS (
    VALUES
        ('logo', 'agent_chat_session', 'id', 'uuid', false, NULL),
        ('logo', 'agent_chat_session', 'user_login', 'text', false, NULL),
        ('logo', 'agent_chat_session', 'title', 'text', false, ''''''),
        ('logo', 'agent_chat_session', 'active_turn_id', 'uuid', true, NULL),
        ('logo', 'agent_chat_session', 'turn_lease_expires_at', 'timestamp with time zone', true, NULL),
        ('logo', 'agent_chat_session', 'created_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_chat_session', 'updated_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_chat_session', 'expires_at', 'timestamp with time zone', false, NULL),
        ('logo', 'agent_chat_message', 'id', 'uuid', false, NULL),
        ('logo', 'agent_chat_message', 'session_id', 'uuid', false, NULL),
        ('logo', 'agent_chat_message', 'user_login', 'text', false, NULL),
        ('logo', 'agent_chat_message', 'turn_id', 'uuid', false, NULL),
        ('logo', 'agent_chat_message', 'role', 'text', false, NULL),
        ('logo', 'agent_chat_message', 'status', 'text', false, NULL),
        ('logo', 'agent_chat_message', 'content', 'text', false, ''''''),
        ('logo', 'agent_chat_message', 'replay_items', 'jsonb', false, '''[]'''),
        ('logo', 'agent_chat_message', 'created_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_usage_daily', 'user_login', 'text', false, NULL),
        ('logo', 'agent_usage_daily', 'usage_day', 'date', false, NULL),
        ('logo', 'agent_usage_daily', 'requests', 'integer', false, '0'),
        ('logo', 'agent_usage_daily', 'reserved_tokens', 'bigint', false, '0'),
        ('logo', 'agent_usage_daily', 'input_tokens', 'bigint', false, '0'),
        ('logo', 'agent_usage_daily', 'output_tokens', 'bigint', false, '0'),
        ('logo', 'agent_usage_daily', 'updated_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_usage_monthly', 'usage_month', 'date', false, NULL),
        ('logo', 'agent_usage_monthly', 'requests', 'integer', false, '0'),
        ('logo', 'agent_usage_monthly', 'reserved_tokens', 'bigint', false, '0'),
        ('logo', 'agent_usage_monthly', 'input_tokens', 'bigint', false, '0'),
        ('logo', 'agent_usage_monthly', 'output_tokens', 'bigint', false, '0'),
        ('logo', 'agent_usage_monthly', 'updated_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_rate_window', 'user_login', 'text', false, NULL),
        ('logo', 'agent_rate_window', 'window_start', 'timestamp with time zone', false, NULL),
        ('logo', 'agent_rate_window', 'requests', 'integer', false, '0'),
        ('logo', 'agent_quota_reservation', 'id', 'uuid', false, NULL),
        ('logo', 'agent_quota_reservation', 'user_login', 'text', false, NULL),
        ('logo', 'agent_quota_reservation', 'usage_day', 'date', false, NULL),
        ('logo', 'agent_quota_reservation', 'usage_month', 'date', false, NULL),
        ('logo', 'agent_quota_reservation', 'window_start', 'timestamp with time zone', false, NULL),
        ('logo', 'agent_quota_reservation', 'reserved_tokens', 'bigint', false, NULL),
        ('logo', 'agent_quota_reservation', 'status', 'text', false, '''reserved'''),
        ('logo', 'agent_quota_reservation', 'input_tokens', 'bigint', false, '0'),
        ('logo', 'agent_quota_reservation', 'output_tokens', 'bigint', false, '0'),
        ('logo', 'agent_quota_reservation', 'created_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_quota_reservation', 'provider_started_at', 'timestamp with time zone', true, NULL),
        ('logo', 'agent_quota_reservation', 'expires_at', 'timestamp with time zone', false, 'now+''00:15:00''|now+''15minutes'''),
        ('logo', 'agent_quota_reservation', 'finalized_at', 'timestamp with time zone', true, NULL),
        ('logo', 'agent_change_set', 'id', 'uuid', false, NULL),
        ('logo', 'agent_change_set', 'session_id', 'uuid', false, NULL),
        ('logo', 'agent_change_set', 'user_login', 'text', false, NULL),
        ('logo', 'agent_change_set', 'origin', 'text', false, '''chat'''),
        ('logo', 'agent_change_set', 'status', 'text', false, '''pending'''),
        ('logo', 'agent_change_set', 'revision', 'integer', false, '0'),
        ('logo', 'agent_change_set', 'preview_hash', 'text', true, NULL),
        ('logo', 'agent_change_set', 'preview_diff', 'jsonb', false, '''{}'''),
        ('logo', 'agent_change_set', 'affected_scopes', 'jsonb', false, '''[]'''),
        ('logo', 'agent_change_set', 'contains_hard_delete', 'boolean', false, 'false'),
        ('logo', 'agent_change_set', 'created_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_change_set', 'updated_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_change_set', 'expires_at', 'timestamp with time zone', false, NULL),
        ('logo', 'agent_change_set', 'applied_at', 'timestamp with time zone', true, NULL),
        ('logo', 'agent_change_set', 'undone_at', 'timestamp with time zone', true, NULL),
        ('logo', 'agent_change_set_item', 'id', 'uuid', false, NULL),
        ('logo', 'agent_change_set_item', 'change_set_id', 'uuid', false, NULL),
        ('logo', 'agent_change_set_item', 'user_login', 'text', false, NULL),
        ('logo', 'agent_change_set_item', 'call_id', 'text', false, NULL),
        ('logo', 'agent_change_set_item', 'tool_name', 'text', false, NULL),
        ('logo', 'agent_change_set_item', 'arguments', 'jsonb', false, NULL),
        ('logo', 'agent_change_set_item', 'sort_order', 'integer', false, NULL),
        ('logo', 'agent_change_set_item', 'created_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_action_journal', 'id', 'uuid', false, NULL),
        ('logo', 'agent_action_journal', 'change_set_id', 'uuid', false, NULL),
        ('logo', 'agent_action_journal', 'user_login', 'text', false, NULL),
        ('logo', 'agent_action_journal', 'event_type', 'text', false, NULL),
        ('logo', 'agent_action_journal', 'actor', 'text', false, NULL),
        ('logo', 'agent_action_journal', 'preview_hash', 'text', false, NULL),
        ('logo', 'agent_action_journal', 'before_state', 'jsonb', false, NULL),
        ('logo', 'agent_action_journal', 'after_state', 'jsonb', false, NULL),
        ('logo', 'agent_action_journal', 'created_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_spreadsheet_job', 'id', 'uuid', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'session_id', 'uuid', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'user_login', 'text', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'storage_key', 'uuid', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'change_set_id', 'uuid', true, NULL),
        ('logo', 'agent_spreadsheet_job', 'original_name', 'text', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'media_type', 'text', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'byte_size', 'bigint', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'sha256', 'text', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'format_name', 'text', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'status', 'text', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'mapping_revision', 'integer', false, '1'),
        ('logo', 'agent_spreadsheet_job', 'mapping_hash', 'text', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'mapping', 'jsonb', false, NULL),
        ('logo', 'agent_spreadsheet_job', 'rejected_rows', 'jsonb', false, '''[]'''),
        ('logo', 'agent_spreadsheet_job', 'created_at', 'timestamp with time zone', false, 'now'),
        ('logo', 'agent_spreadsheet_job', 'expires_at', 'timestamp with time zone', false, NULL)
), agent_column_order_policy(schema_name, table_name, column_names) AS (
    VALUES
        ('logo', 'agent_chat_session', ARRAY[
            'id', 'user_login', 'title', 'active_turn_id',
            'turn_lease_expires_at', 'created_at', 'updated_at', 'expires_at'
        ]::text[]),
        ('logo', 'agent_chat_message', ARRAY[
            'id', 'session_id', 'user_login', 'turn_id', 'role', 'status',
            'content', 'replay_items', 'created_at'
        ]::text[]),
        ('logo', 'agent_usage_daily', ARRAY[
            'user_login', 'usage_day', 'requests', 'reserved_tokens',
            'input_tokens', 'output_tokens', 'updated_at'
        ]::text[]),
        ('logo', 'agent_usage_monthly', ARRAY[
            'usage_month', 'requests', 'reserved_tokens', 'input_tokens',
            'output_tokens', 'updated_at'
        ]::text[]),
        ('logo', 'agent_rate_window', ARRAY[
            'user_login', 'window_start', 'requests'
        ]::text[]),
        ('logo', 'agent_quota_reservation', ARRAY[
            'id', 'user_login', 'usage_day', 'usage_month', 'window_start',
            'reserved_tokens', 'status', 'input_tokens', 'output_tokens',
            'created_at', 'provider_started_at', 'expires_at', 'finalized_at'
        ]::text[]),
        ('logo', 'agent_change_set', ARRAY[
            'id', 'session_id', 'user_login', 'origin', 'status', 'revision',
            'preview_hash', 'preview_diff', 'affected_scopes',
            'contains_hard_delete', 'created_at', 'updated_at', 'expires_at',
            'applied_at', 'undone_at'
        ]::text[]),
        ('logo', 'agent_change_set_item', ARRAY[
            'id', 'change_set_id', 'user_login', 'call_id', 'tool_name',
            'arguments', 'sort_order', 'created_at'
        ]::text[]),
        ('logo', 'agent_action_journal', ARRAY[
            'id', 'change_set_id', 'user_login', 'event_type', 'actor',
            'preview_hash', 'before_state', 'after_state', 'created_at'
        ]::text[]),
        ('logo', 'agent_spreadsheet_job', ARRAY[
            'id', 'session_id', 'user_login', 'storage_key', 'change_set_id',
            'original_name', 'media_type', 'byte_size', 'sha256',
            'format_name', 'status', 'mapping_revision', 'mapping_hash',
            'mapping', 'rejected_rows', 'created_at', 'expires_at'
        ]::text[])
), agent_column_inventory AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS table_name,
           attribute.attname AS column_name,
           attribute.attnum::integer AS ordinal_position,
           format_type(attribute.atttypid, attribute.atttypmod)
               AS formatted_type,
           NOT attribute.attnotnull AS nullable,
           attribute.attgenerated AS generated_kind,
           attribute.attidentity AS identity_kind,
           pg_collation.collname AS collation_name,
           CASE WHEN default_row.oid IS NULL THEN NULL ELSE regexp_replace(
               regexp_replace(
                   lower(pg_get_expr(
                       default_row.adbin, default_row.adrelid, true
                   )),
                   '::(timestamp (with|without) time zone|character varying|smallint|integer|bigint|text|date|jsonb|interval|boolean|uuid|numeric)\M',
                   '', 'g'
               ),
               '[[:space:]()"]', '', 'g'
           ) END AS default_signature
      FROM agent_table_policy
      JOIN pg_namespace AS namespace
        ON namespace.nspname = agent_table_policy.schema_name
      JOIN pg_class AS relation
        ON relation.relnamespace = namespace.oid
       AND relation.relname = agent_table_policy.table_name
      JOIN pg_attribute AS attribute
        ON attribute.attrelid = relation.oid
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
      LEFT JOIN pg_collation
        ON pg_collation.oid = attribute.attcollation
      LEFT JOIN pg_attrdef AS default_row
        ON default_row.adrelid = relation.oid
       AND default_row.adnum = attribute.attnum
), agent_column_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM agent_column_policy AS expected
                 LEFT JOIN agent_column_inventory AS actual
                   ON actual.schema_name = expected.schema_name
                  AND actual.table_name = expected.table_name
                  AND actual.column_name = expected.column_name
                 LEFT JOIN agent_column_order_policy AS expected_order
                   ON expected_order.schema_name = expected.schema_name
                  AND expected_order.table_name = expected.table_name
                WHERE expected_order.table_name IS NULL
                   OR actual.column_name IS NULL
                   OR actual.ordinal_position <> array_position(
                       expected_order.column_names, expected.column_name
                   )
                   OR actual.formatted_type <> expected.formatted_type
                   OR actual.nullable <> expected.nullable
                   OR actual.generated_kind <> ''
                   OR actual.identity_kind <> ''
                   OR actual.collation_name IS DISTINCT FROM CASE
                       WHEN expected.formatted_type = 'text' THEN 'default'
                       ELSE NULL
                   END
                   OR (
                       expected.default_signature IS NULL
                       AND actual.default_signature IS NOT NULL
                   )
                   OR (
                       expected.default_signature IS NOT NULL
                       AND (
                           actual.default_signature IS NULL
                           OR NOT actual.default_signature = ANY (
                               string_to_array(
                                   expected.default_signature, '|'
                               )
                           )
                       )
                   )
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM agent_column_inventory AS actual
                 LEFT JOIN agent_column_policy AS expected
                   ON expected.schema_name = actual.schema_name
                  AND expected.table_name = actual.table_name
                  AND expected.column_name = actual.column_name
                WHERE expected.column_name IS NULL
           ) AS passes
), agent_constraint_inventory AS (
    SELECT format(
               '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s',
               format(
                   '%I.%I', source_namespace.nspname, source.relname
               ),
               constraint_row.conname,
               constraint_row.contype,
               array_to_string(ARRAY(
                   SELECT attribute.attname::text
                     FROM unnest(constraint_row.conkey)
                          WITH ORDINALITY AS key_column(
                              attnum, ordinal_position
                          )
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = source.oid
                      AND attribute.attnum = key_column.attnum
                    ORDER BY key_column.ordinal_position
               ), ','),
               CASE WHEN constraint_row.contype = 'f' THEN format(
                   '%I.%I', target_namespace.nspname, target.relname
               ) ELSE '' END,
               CASE WHEN constraint_row.contype = 'f' THEN array_to_string(
                   ARRAY(
                       SELECT attribute.attname::text
                         FROM unnest(constraint_row.confkey)
                              WITH ORDINALITY AS key_column(
                                  attnum, ordinal_position
                              )
                         JOIN pg_attribute AS attribute
                           ON attribute.attrelid = target.oid
                          AND attribute.attnum = key_column.attnum
                        ORDER BY key_column.ordinal_position
                   ), ','
               ) ELSE '' END,
               CASE WHEN constraint_row.contype = 'f'
                   THEN constraint_row.confupdtype ELSE '' END,
               CASE WHEN constraint_row.contype = 'f'
                   THEN constraint_row.confdeltype ELSE '' END,
               CASE WHEN constraint_row.contype = 'f'
                   THEN constraint_row.confmatchtype ELSE '' END,
               CASE WHEN constraint_row.contype = 'c' THEN regexp_replace(
                   regexp_replace(
                       lower(pg_get_expr(
                           constraint_row.conbin,
                           constraint_row.conrelid,
                           true
                       )),
                       '::(timestamp (with|without) time zone|character varying|smallint|integer|bigint|text|date|jsonb|interval|boolean|uuid|numeric)\M',
                       '', 'g'
                   ),
                   '[[:space:]()"]', '', 'g'
               ) ELSE '' END
           ) AS signature,
           constraint_row.condeferrable AS is_deferrable,
           constraint_row.condeferred AS initially_deferred,
           constraint_row.convalidated AS validated,
           constraint_row.connoinherit AS no_inherit
      FROM pg_constraint AS constraint_row
      JOIN pg_class AS source ON source.oid = constraint_row.conrelid
      JOIN pg_namespace AS source_namespace
        ON source_namespace.oid = source.relnamespace
      LEFT JOIN pg_class AS target ON target.oid = constraint_row.confrelid
      LEFT JOIN pg_namespace AS target_namespace
        ON target_namespace.oid = target.relnamespace
     WHERE (
               EXISTS (
                   SELECT 1
                     FROM agent_table_policy
                    WHERE agent_table_policy.schema_name =
                          source_namespace.nspname
                      AND agent_table_policy.table_name = source.relname
               )
               AND constraint_row.contype IN ('p', 'u', 'c', 'f', 'x')
           )
        OR (
               constraint_row.contype = 'f'
               AND EXISTS (
                   SELECT 1
                     FROM agent_table_policy
                    WHERE agent_table_policy.schema_name =
                          target_namespace.nspname
                      AND agent_table_policy.table_name = target.relname
               )
           )
), agent_constraint_contract AS (
    SELECT array_agg(signature ORDER BY signature) = ARRAY[
        'logo.agent_action_journal|agent_action_journal_change_set_id_event_type_key|u|change_set_id,event_type||||||',
        'logo.agent_action_journal|agent_action_journal_change_set_id_user_login_fkey|f|change_set_id,user_login|logo.agent_change_set|id,user_login|a|r|s|',
        'logo.agent_action_journal|agent_action_journal_event_type_check|c|event_type||||||event_type=anyarray[''apply'',''undo'']',
        'logo.agent_action_journal|agent_action_journal_pkey|p|id||||||',
        'logo.agent_action_journal|agent_action_journal_preview_hash_check|c|preview_hash||||||preview_hash~''^[0-9a-f]{64}$''',
        'logo.agent_change_set_item|agent_change_set_item_arguments_check|c|arguments||||||jsonb_typeofarguments=''object''',
        'logo.agent_change_set_item|agent_change_set_item_change_set_id_call_id_key|u|change_set_id,call_id||||||',
        'logo.agent_change_set_item|agent_change_set_item_change_set_id_sort_order_key|u|change_set_id,sort_order||||||',
        'logo.agent_change_set_item|agent_change_set_item_change_set_id_user_login_fkey|f|change_set_id,user_login|logo.agent_change_set|id,user_login|a|c|s|',
        'logo.agent_change_set_item|agent_change_set_item_pkey|p|id||||||',
        'logo.agent_change_set_item|agent_change_set_item_sort_order_check|c|sort_order||||||sort_order>=0',
        'logo.agent_change_set|agent_change_set_id_user_login_key|u|id,user_login||||||',
        'logo.agent_change_set|agent_change_set_origin_check|c|origin||||||origin=anyarray[''chat'',''spreadsheet'']',
        'logo.agent_change_set|agent_change_set_pkey|p|id||||||',
        'logo.agent_change_set|agent_change_set_preview_hash_check|c|preview_hash||||||preview_hashisnullorpreview_hash~''^[0-9a-f]{64}$''',
        'logo.agent_change_set|agent_change_set_revision_check|c|revision||||||revision>=0',
        'logo.agent_change_set|agent_change_set_session_id_user_login_fkey|f|session_id,user_login|logo.agent_chat_session|id,user_login|a|r|s|',
        'logo.agent_change_set|agent_change_set_status_check|c|status||||||status=anyarray[''pending'',''applied'',''discarded'',''undone'']',
        'logo.agent_chat_message|agent_chat_message_pkey|p|id||||||',
        'logo.agent_chat_message|agent_chat_message_replay_items_check|c|replay_items||||||jsonb_typeofreplay_items=''array''',
        'logo.agent_chat_message|agent_chat_message_role_check|c|role||||||role=anyarray[''user'',''assistant'']',
        'logo.agent_chat_message|agent_chat_message_session_id_turn_id_role_key|u|session_id,turn_id,role||||||',
        'logo.agent_chat_message|agent_chat_message_session_id_user_login_fkey|f|session_id,user_login|logo.agent_chat_session|id,user_login|a|c|s|',
        'logo.agent_chat_message|agent_chat_message_status_check|c|status||||||status=anyarray[''complete'',''failed'',''cancelled'']',
        'logo.agent_chat_session|agent_chat_session_id_user_login_key|u|id,user_login||||||',
        'logo.agent_chat_session|agent_chat_session_pkey|p|id||||||',
        'logo.agent_quota_reservation|agent_quota_reservation_input_tokens_check|c|input_tokens||||||input_tokens>=0',
        'logo.agent_quota_reservation|agent_quota_reservation_output_tokens_check|c|output_tokens||||||output_tokens>=0',
        'logo.agent_quota_reservation|agent_quota_reservation_pkey|p|id||||||',
        'logo.agent_quota_reservation|agent_quota_reservation_reserved_tokens_check|c|reserved_tokens||||||reserved_tokens>0',
        'logo.agent_quota_reservation|agent_quota_reservation_status_check|c|status||||||status=anyarray[''reserved'',''reconciled'',''retained'']',
        'logo.agent_rate_window|agent_rate_window_pkey|p|user_login,window_start||||||',
        'logo.agent_rate_window|agent_rate_window_requests_check|c|requests||||||requests>=0',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_byte_size_check|c|byte_size||||||byte_size>=0',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_change_set_id_key|u|change_set_id||||||',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_change_set_id_user_login_fkey|f|change_set_id,user_login|logo.agent_change_set|id,user_login|a|r|s|',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_format_name_check|c|format_name||||||format_name=anyarray[''csv'',''xlsx'']',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_id_user_login_key|u|id,user_login||||||',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_mapping_hash_check|c|mapping_hash||||||mapping_hash~''^[0-9a-f]{64}$''',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_mapping_revision_check|c|mapping_revision||||||mapping_revision>=1',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_pkey|p|id||||||',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_session_id_user_login_fkey|f|session_id,user_login|logo.agent_chat_session|id,user_login|a|r|s|',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_sha256_check|c|sha256||||||sha256~''^[0-9a-f]{64}$''',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_status_check|c|status||||||status=anyarray[''mapping_processing'',''mapping_pending'',''mapping_confirmed'',''staged'',''rejected'',''expired'']',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_storage_key_key|u|storage_key||||||',
        'logo.agent_usage_daily|agent_usage_daily_input_tokens_check|c|input_tokens||||||input_tokens>=0',
        'logo.agent_usage_daily|agent_usage_daily_output_tokens_check|c|output_tokens||||||output_tokens>=0',
        'logo.agent_usage_daily|agent_usage_daily_pkey|p|user_login,usage_day||||||',
        'logo.agent_usage_daily|agent_usage_daily_requests_check|c|requests||||||requests>=0',
        'logo.agent_usage_daily|agent_usage_daily_reserved_tokens_check|c|reserved_tokens||||||reserved_tokens>=0',
        'logo.agent_usage_monthly|agent_usage_monthly_input_tokens_check|c|input_tokens||||||input_tokens>=0',
        'logo.agent_usage_monthly|agent_usage_monthly_output_tokens_check|c|output_tokens||||||output_tokens>=0',
        'logo.agent_usage_monthly|agent_usage_monthly_pkey|p|usage_month||||||',
        'logo.agent_usage_monthly|agent_usage_monthly_requests_check|c|requests||||||requests>=0',
        'logo.agent_usage_monthly|agent_usage_monthly_reserved_tokens_check|c|reserved_tokens||||||reserved_tokens>=0',
        'logo.agent_usage_monthly|agent_usage_monthly_usage_month_check|c|usage_month||||||date_trunc''month'',usage_month=usage_month'
    ]::text[]
           AND bool_and(
               NOT is_deferrable
               AND NOT initially_deferred
               AND validated
               -- PostgreSQL marks index-backed and foreign-key constraints
               -- NO INHERIT; the flag is only a policy signal on CHECKs.
               AND (split_part(signature, '|', 3) <> 'c' OR NOT no_inherit)
           ) AS passes
      FROM agent_constraint_inventory
), agent_index_inventory AS (
    SELECT format(
               '%s|%s|%s|%s|%s|%s|%s',
               format('%I.%I', namespace.nspname, relation.relname),
               index_class.relname,
               CASE WHEN index_row.indisunique THEN 'true' ELSE 'false' END,
               CASE WHEN index_row.indisprimary THEN 'true' ELSE 'false' END,
               array_to_string(ARRAY(
                   SELECT attribute.attname::text
                     FROM unnest(index_row.indkey::smallint[])
                          WITH ORDINALITY AS key_column(
                              attnum, ordinal_position
                          )
                     JOIN pg_attribute AS attribute
                       ON attribute.attrelid = relation.oid
                      AND attribute.attnum = key_column.attnum
                    WHERE key_column.ordinal_position <=
                          index_row.indnkeyatts
                    ORDER BY key_column.ordinal_position
               ), ','),
               array_to_string(index_row.indoption::smallint[], ','),
               CASE WHEN index_row.indpred IS NULL THEN '' ELSE
                   regexp_replace(
                       regexp_replace(
                           lower(pg_get_expr(
                               index_row.indpred,
                               index_row.indrelid,
                               true
                           )),
                           '::(timestamp (with|without) time zone|character varying|smallint|integer|bigint|text|date|jsonb|interval|boolean|uuid|numeric)\M',
                           '', 'g'
                       ),
                       '[[:space:]()"]', '', 'g'
                   )
               END
           ) AS signature,
           index_method.amname AS access_method,
           index_row.indisvalid AS is_valid,
           index_row.indisready AS is_ready,
           index_row.indislive AS is_live,
           index_row.indisclustered AS is_clustered,
           index_row.indisreplident AS is_replica_identity,
           index_row.indnullsnotdistinct AS nulls_not_distinct,
           index_row.indexprs IS NOT NULL AS has_expressions,
           index_row.indnkeyatts AS key_attribute_count,
           index_row.indnatts AS attribute_count,
           index_class.reltablespace AS tablespace_oid
      FROM pg_index AS index_row
      JOIN pg_class AS relation ON relation.oid = index_row.indrelid
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      JOIN pg_class AS index_class
        ON index_class.oid = index_row.indexrelid
      JOIN pg_am AS index_method ON index_method.oid = index_class.relam
      JOIN agent_table_policy
        ON agent_table_policy.schema_name = namespace.nspname
       AND agent_table_policy.table_name = relation.relname
), agent_index_contract AS (
    SELECT array_agg(signature ORDER BY signature) = ARRAY[
        'logo.agent_action_journal|agent_action_journal_change_set_id_event_type_key|true|false|change_set_id,event_type|0,0|',
        'logo.agent_action_journal|agent_action_journal_owner_idx|false|false|user_login,created_at|0,3|',
        'logo.agent_action_journal|agent_action_journal_pkey|true|true|id|0|',
        'logo.agent_change_set_item|agent_change_set_item_change_set_id_call_id_key|true|false|change_set_id,call_id|0,0|',
        'logo.agent_change_set_item|agent_change_set_item_change_set_id_sort_order_key|true|false|change_set_id,sort_order|0,0|',
        'logo.agent_change_set_item|agent_change_set_item_pkey|true|true|id|0|',
        'logo.agent_change_set|agent_change_set_id_user_login_key|true|false|id,user_login|0,0|',
        'logo.agent_change_set|agent_change_set_owner_status_idx|false|false|user_login,status,updated_at|0,0,3|',
        'logo.agent_change_set|agent_change_set_pkey|true|true|id|0|',
        'logo.agent_chat_message|agent_chat_message_owner_session_idx|false|false|user_login,session_id,created_at,id|0,0,3,3|',
        'logo.agent_chat_message|agent_chat_message_pkey|true|true|id|0|',
        'logo.agent_chat_message|agent_chat_message_session_id_turn_id_role_key|true|false|session_id,turn_id,role|0,0,0|',
        'logo.agent_chat_session|agent_chat_session_id_user_login_key|true|false|id,user_login|0,0|',
        'logo.agent_chat_session|agent_chat_session_owner_updated_idx|false|false|user_login,updated_at|0,3|',
        'logo.agent_chat_session|agent_chat_session_pkey|true|true|id|0|',
        'logo.agent_quota_reservation|agent_quota_reservation_owner_created_idx|false|false|user_login,created_at|0,3|',
        'logo.agent_quota_reservation|agent_quota_reservation_pkey|true|true|id|0|',
        'logo.agent_quota_reservation|agent_quota_reservation_stale_idx|false|false|expires_at,id|0,0|status=''reserved''',
        'logo.agent_rate_window|agent_rate_window_pkey|true|true|user_login,window_start|0,0|',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_change_set_id_key|true|false|change_set_id|0|',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_id_user_login_key|true|false|id,user_login|0,0|',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_pkey|true|true|id|0|',
        'logo.agent_spreadsheet_job|agent_spreadsheet_job_storage_key_key|true|false|storage_key|0|',
        'logo.agent_spreadsheet_job|agent_spreadsheet_owner_status_idx|false|false|user_login,status,created_at|0,0,3|',
        'logo.agent_usage_daily|agent_usage_daily_pkey|true|true|user_login,usage_day|0,0|',
        'logo.agent_usage_monthly|agent_usage_monthly_pkey|true|true|usage_month|0|'
    ]::text[]
           AND bool_and(
               access_method = 'btree'
               AND is_valid
               AND is_ready
               AND is_live
               AND NOT is_clustered
               AND NOT is_replica_identity
               AND NOT nulls_not_distinct
               AND NOT has_expressions
               AND key_attribute_count = attribute_count
               AND tablespace_oid = 0
           ) AS passes
      FROM agent_index_inventory
), sequence_policy(schema_name, sequence_name, policy) AS (
    VALUES
        ('logo', 'audit_log_id_seq', 'usage_select'),
        ('logo', 'import_report_id_seq', 'usage_select'),
        ('woo', 'price_rule_rule_id_seq', 'usage'),
        ('woo', 'price_rule_audit_id_seq', 'usage'),
        ('woo', 'store_mix_audit_id_seq', 'usage'),
        ('logo', 'assignment_version_seq', 'usage'),
        ('catmgr', 'assignment_rule_rule_id_seq', 'usage'),
        ('catmgr', 'audit_log_id_seq', 'usage'),
        ('catmgr', 'node_node_id_seq', 'usage'),
        ('catmgr', 'node_store_override_override_id_seq', 'usage'),
        ('catmgr', 'product_assignment_id_seq', 'usage'),
        ('catmgr', 'redirect_id_seq', 'usage'),
        ('catmgr', 'run_job_job_id_seq', 'usage'),
        ('catmgr', 'run_run_id_seq', 'usage')
), sequence_privilege(privilege_name) AS (
    VALUES ('USAGE'), ('SELECT'), ('UPDATE')
), sequence_inventory AS (
    SELECT namespace.nspname AS schema_name,
           relation.relname AS sequence_name,
           sequence_policy.sequence_name IS NOT NULL AS allowlisted,
           sequence_policy.policy,
           privilege_name,
           has_sequence_privilege(
               'logo_admin', relation.oid, privilege_name
           ) AS actual,
           has_sequence_privilege(
               'public', relation.oid, privilege_name
           ) AS public_actual
      FROM pg_class AS relation
      JOIN pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
     CROSS JOIN sequence_privilege
      LEFT JOIN sequence_policy
        ON sequence_policy.schema_name = namespace.nspname
       AND sequence_policy.sequence_name = relation.relname
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND relation.relkind = 'S'
), sequence_contract AS (
    SELECT NOT EXISTS (
               SELECT 1
                 FROM sequence_policy
                WHERE to_regclass(format(
                    '%I.%I', schema_name, sequence_name
                )) IS NULL
           )
           AND coalesce(bool_and(
               actual = CASE policy
                   WHEN 'usage_select' THEN privilege_name IN ('USAGE', 'SELECT')
                   WHEN 'usage' THEN privilege_name = 'USAGE'
                   ELSE false
               END
               AND NOT public_actual
           ), false)
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(coalesce(
                    relation.relacl,
                    acldefault('s', relation.relowner)
                )) AS privilege
                WHERE namespace.nspname <> 'information_schema'
                  AND namespace.nspname !~ '^pg_'
                  AND relation.relkind = 'S'
                  AND privilege.grantee = (
                      SELECT oid
                        FROM pg_roles
                       WHERE rolname = 'logo_admin'
                  )
                  AND privilege.is_grantable
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM pg_class AS relation
                 JOIN pg_namespace AS namespace
                   ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'logo'
                  AND relation.relkind = 'S'
                  AND relation.relname LIKE 'agent\_%' ESCAPE '\'
           ) AS passes
      FROM sequence_inventory
), callable_inventory AS (
    SELECT namespace.nspname AS schema_name,
           procedure.proname AS routine_name,
           oidvectortypes(procedure.proargtypes) AS argument_types,
           procedure.prokind AS routine_kind
      FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace
        ON namespace.oid = procedure.pronamespace
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND procedure.prokind IN ('f', 'p')
       AND has_function_privilege(
           'logo_admin', procedure.oid, 'EXECUTE'
       )
), callable_contract AS (
    SELECT EXISTS (
               SELECT 1
                 FROM callable_inventory
                WHERE schema_name = 'logo'
                  AND routine_name = 'prune_agent_history'
                  AND argument_types = ''
                  AND routine_kind = 'f'
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM callable_inventory
                WHERE NOT (
                    routine_kind = 'f'
                    AND (
                        (
                            schema_name = 'logo'
                            AND
                            routine_name = 'prune_agent_history'
                            AND argument_types = ''
                        )
                        OR (
                            schema_name = 'logo'
                            AND
                            routine_name = 'repull_display_name'
                            AND argument_types = 'text, boolean'
                        )
                        OR (
                            schema_name = 'woo'
                            AND routine_name = 'eval_price_rules'
                            AND argument_types =
                                'text, text, text, text, numeric, jsonb, numeric, date, bigint[], bigint[]'
                        )
                    )
                )
           ) AS passes
), definer_inventory AS (
    SELECT namespace.nspname AS schema_name,
           procedure.proname AS function_name,
           oidvectortypes(procedure.proargtypes) AS argument_types,
           (
               procedure.proowner = database.datdba
               OR function_owner.rolsuper
           ) AS owner_passes,
           NOT has_function_privilege(
               'public', procedure.oid, 'EXECUTE'
           ) AS public_denied,
           NOT EXISTS (
               SELECT 1
                 FROM aclexplode(coalesce(
                     procedure.proacl,
                     acldefault('f', procedure.proowner)
                 )) AS privilege
                WHERE privilege.grantee = (
                    SELECT oid
                      FROM pg_roles
                     WHERE rolname = 'logo_admin'
                )
                  AND privilege.privilege_type = 'EXECUTE'
                  AND privilege.is_grantable
           ) AS not_grantable
      FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace
        ON namespace.oid = procedure.pronamespace
      JOIN pg_roles AS function_owner
        ON function_owner.oid = procedure.proowner
      JOIN pg_database AS database
        ON database.datname = current_database()
     WHERE namespace.nspname <> 'information_schema'
       AND namespace.nspname !~ '^pg_'
       AND procedure.prosecdef
       AND has_function_privilege(
           'logo_admin', procedure.oid, 'EXECUTE'
       )
), definer_contract AS (
    SELECT EXISTS (
               SELECT 1
                 FROM definer_inventory
                WHERE schema_name = 'logo'
                  AND function_name = 'prune_agent_history'
                  AND argument_types = ''
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM definer_inventory
                WHERE NOT (
                    schema_name = 'logo'
                    AND function_name = 'prune_agent_history'
                    AND argument_types = ''
                )
           )
           AND coalesce(bool_and(
               owner_passes AND public_denied AND not_grantable
           ), false) AS passes
      FROM definer_inventory
), prune_contract AS (
    SELECT count(*) = 1
           AND bool_and(
               (
                   procedure.proowner = database.datdba
                   OR function_owner.rolsuper
               )
               AND procedure.prokind = 'f'
               AND procedure.pronargs = 0
               AND language.lanname = 'plpgsql'
               AND procedure.prosecdef
               AND procedure.proconfig = ARRAY[
                   'search_path=pg_catalog, logo'
               ]::text[]
               AND pg_get_function_result(procedure.oid) =
                   'TABLE(journals_deleted bigint, change_sets_deleted bigint)'
               AND encode(sha256(convert_to(
                   procedure.prosrc, 'UTF8'
               )), 'hex') =
                   '378f41091ba89926fda1364b2c99bd2901b8e01ddde9c8fa52f97b3f3f8c2269'
               AND has_function_privilege(
                   'logo_admin', procedure.oid, 'EXECUTE'
               )
               AND NOT has_function_privilege(
                   'public', procedure.oid, 'EXECUTE'
               )
           ) AS passes
      FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace
        ON namespace.oid = procedure.pronamespace
      JOIN pg_language AS language
        ON language.oid = procedure.prolang
      JOIN pg_roles AS function_owner
        ON function_owner.oid = procedure.proowner
      JOIN pg_database AS database
        ON database.datname = current_database()
     WHERE procedure.oid = to_regprocedure('logo.prune_agent_history()')
       AND namespace.nspname = 'logo'
), repull_contract AS (
    SELECT count(*) = 0
           OR (
               count(*) = 1
               AND bool_and(
                   oidvectortypes(procedure.proargtypes) = 'text, boolean'
                   AND (
                       procedure.proowner = database.datdba
                       OR function_owner.rolsuper
                   )
                   AND procedure.prokind = 'f'
                   AND language.lanname = 'plpgsql'
                   AND NOT procedure.prosecdef
                   AND (
                       procedure.proconfig IS NULL
                       OR procedure.proconfig = ARRAY[
                           'search_path=pg_catalog, logo, fdm4'
                       ]::text[]
                   )
                   AND pg_get_function_result(procedure.oid) = 'integer'
                   AND length(:'repull_function_sha256') = 64
                   AND encode(sha256(convert_to(
                       pg_get_functiondef(procedure.oid), 'UTF8'
                   )), 'hex') = lower(:'repull_function_sha256')
                   AND has_function_privilege(
                       'logo_admin', procedure.oid, 'EXECUTE'
                   )
                   AND NOT has_function_privilege(
                       'public', procedure.oid, 'EXECUTE'
                   )
                   AND position(
                       'logo.display_name' IN lower(
                           pg_get_functiondef(procedure.oid)
                       )
                   ) > 0
                   AND position(
                       'fdm4.design_pool' IN lower(
                           pg_get_functiondef(procedure.oid)
                       )
                   ) > 0
                   AND lower(pg_get_functiondef(procedure.oid))
                       !~ '\mexecute\M'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM aclexplode(coalesce(
                             procedure.proacl,
                             acldefault('f', procedure.proowner)
                         )) AS privilege
                        WHERE privilege.grantee = (
                            SELECT oid
                              FROM pg_roles
                             WHERE rolname = 'logo_admin'
                        )
                          AND privilege.privilege_type = 'EXECUTE'
                          AND privilege.is_grantable
                   )
               )
           ) AS passes
      FROM pg_proc AS procedure
      JOIN pg_namespace AS namespace
        ON namespace.oid = procedure.pronamespace
      JOIN pg_language AS language
        ON language.oid = procedure.prolang
      JOIN pg_roles AS function_owner
        ON function_owner.oid = procedure.proowner
      JOIN pg_database AS database
        ON database.datname = current_database()
     WHERE namespace.nspname = 'logo'
       AND procedure.proname = 'repull_display_name'
)
SELECT 1 / CASE
    WHEN coalesce((SELECT passes FROM execution_contract), false)
     AND coalesce((SELECT passes FROM role_contract), false)
     AND coalesce((SELECT passes FROM membership_contract), false)
     AND coalesce((SELECT passes FROM database_contract), false)
     AND coalesce((SELECT passes FROM schema_contract), false)
     AND coalesce((SELECT passes FROM ownership_contract), false)
     AND coalesce((SELECT passes FROM table_contract), false)
     AND coalesce((SELECT passes FROM writable_relation_contract), false)
     AND coalesce((SELECT passes FROM column_contract), false)
     AND coalesce((SELECT passes FROM restore_column_contract), false)
     AND coalesce((SELECT passes FROM restore_constraint_contract), false)
     AND coalesce((SELECT passes FROM restore_unique_index_contract), false)
     AND coalesce((SELECT passes FROM trigger_contract), false)
     AND coalesce((SELECT passes FROM rule_contract), false)
     AND coalesce((SELECT passes FROM rls_contract), false)
     AND coalesce((SELECT passes FROM audit_function_contract), false)
     AND coalesce((SELECT passes FROM agent_relation_contract), false)
     AND coalesce((SELECT passes FROM agent_column_contract), false)
     AND coalesce((SELECT passes FROM agent_constraint_contract), false)
     AND coalesce((SELECT passes FROM agent_index_contract), false)
     AND coalesce((SELECT passes FROM sequence_contract), false)
     AND coalesce((SELECT passes FROM callable_contract), false)
     AND coalesce((SELECT passes FROM definer_contract), false)
     AND coalesce((SELECT passes FROM prune_contract), false)
     AND coalesce((SELECT passes FROM repull_contract), false)
    THEN 1 ELSE 0
END AS preflight_assertion;
