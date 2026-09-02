-- Category Editor foundations: the catmgr schema.
--
-- catmgr is the warehouse-owned home of the WooCommerce category editor
-- (ARB_CATEGORY_EDITOR_PLAN.md). This migration lays the snapshot layer:
-- per-environment, per-blog copies of the live product_cat state pulled
-- through the WP broker (arb-admin/arb-category-apply.php), re-importable at
-- any time. Imports are FULL-REPLACE per (env, blog) inside one transaction
-- and bump catmgr.snapshot.version so previews/plans built from an older
-- snapshot are detectably stale.
--
-- Audit here is EXPLICIT (application INSERTs, sync-intent style): no
-- triggers, so nothing extends the SHA-pinned logo.audit_row() machinery.
BEGIN;

CREATE SCHEMA IF NOT EXISTS catmgr;

-- One row per imported (environment, blog): the snapshot header.
CREATE TABLE IF NOT EXISTS catmgr.snapshot (
    env              text        NOT NULL CHECK (env IN ('dev', 'prod')),
    blog_id          integer     NOT NULL,
    version          bigint      NOT NULL DEFAULT 1,
    blog_path        text        NOT NULL DEFAULT '',
    imported_at      timestamptz NOT NULL DEFAULT now(),
    imported_by      text        NOT NULL DEFAULT '',
    term_count       integer     NOT NULL DEFAULT 0,
    membership_count integer     NOT NULL DEFAULT 0,
    PRIMARY KEY (env, blog_id)
);

-- The live product_cat terms of one blog at import time.
CREATE TABLE IF NOT EXISTS catmgr.wp_term (
    env              text    NOT NULL CHECK (env IN ('dev', 'prod')),
    blog_id          integer NOT NULL,
    term_id          bigint  NOT NULL,
    slug             text    NOT NULL,
    name             text    NOT NULL,
    parent_term_id   bigint  NOT NULL DEFAULT 0,
    description      text    NOT NULL DEFAULT '',
    count            integer NOT NULL DEFAULT 0,
    sort_order       integer NOT NULL DEFAULT 0,
    thumbnail_id     bigint  NOT NULL DEFAULT 0,
    name_locked      boolean NOT NULL DEFAULT false,
    snapshot_version bigint  NOT NULL,
    PRIMARY KEY (env, blog_id, term_id)
);
CREATE INDEX IF NOT EXISTS wp_term_env_blog_slug ON catmgr.wp_term (env, blog_id, slug);

-- Published-product memberships of those terms. sku = parent style code
-- (_sku -> product_style fallback, uppercased) - the cross-blog product
-- identity, joinable to woo.store_product_state.
CREATE TABLE IF NOT EXISTS catmgr.wp_term_product (
    env              text    NOT NULL CHECK (env IN ('dev', 'prod')),
    blog_id          integer NOT NULL,
    term_id          bigint  NOT NULL,
    product_id       bigint  NOT NULL,
    sku              text    NOT NULL DEFAULT '',
    snapshot_version bigint  NOT NULL,
    PRIMARY KEY (env, blog_id, term_id, product_id)
);
CREATE INDEX IF NOT EXISTS wp_term_product_env_blog_sku ON catmgr.wp_term_product (env, blog_id, sku);

-- Append-only feature audit. Explicit INSERTs from the application only.
CREATE TABLE IF NOT EXISTS catmgr.audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text        NOT NULL DEFAULT '',
    action     text        NOT NULL,
    entity     text        NOT NULL DEFAULT '',
    entity_key text        NOT NULL DEFAULT '',
    detail     jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS catmgr_audit_log_at ON catmgr.audit_log (at);

GRANT USAGE ON SCHEMA catmgr TO logo_admin, etl_writer, woo_reader, insights_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA catmgr TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON catmgr.snapshot, catmgr.wp_term, catmgr.wp_term_product TO logo_admin;
-- Append-only: no UPDATE/DELETE/TRUNCATE for the application role.
GRANT SELECT, INSERT ON catmgr.audit_log TO logo_admin;
REVOKE UPDATE, DELETE, TRUNCATE ON catmgr.audit_log FROM logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;

COMMIT;
