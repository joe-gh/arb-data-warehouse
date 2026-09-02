-- ============================================================================
-- Durable, staff-editable logo merchandising overlay.
--
-- Apply as a database owner:
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -d arb_warehouse -f logo_schema.sql
--
-- Safe to reapply. Woo and Insights can read the overlay; the extractor role
-- can seed and maintain assignments/settings and append import diagnostics.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS logo;
GRANT USAGE ON SCHEMA logo TO woo_reader, insights_reader, etl_writer;

CREATE TABLE IF NOT EXISTS logo.assignment (
    fdm4_store         text          NOT NULL,
    product_style      text          NOT NULL,
    garment_color_code text          NOT NULL,
    position           smallint      NOT NULL DEFAULT 1
                                   CONSTRAINT logo_assignment_position_check
                                   CHECK (position BETWEEN 1 AND 3),
    design_id          text          NOT NULL,
    logo_code          text          NOT NULL DEFAULT '',
    color_scheme_id    text          NOT NULL DEFAULT '',
    location           text          NOT NULL DEFAULT '',
    optional           boolean       NOT NULL DEFAULT false,
    background         text          NOT NULL DEFAULT '',
    cost_override      numeric(12,2),
    sort_order         integer       NOT NULL DEFAULT 0,
    -- Storefront logo image. Warehouse-owned (FDM4 never holds the image):
    -- seeded from the media-server sheet URLs; Phase B admin manages
    -- imports/uploads for new logos.
    image_url          text          NOT NULL DEFAULT '',
    active             boolean       NOT NULL DEFAULT true,
    updated_by         text          NOT NULL DEFAULT 'seed',
    updated_at         timestamptz   NOT NULL DEFAULT now(),
    -- One (store, style, color) may carry any number of selectable logo rows;
    -- the customer picks ONE row at checkout. position 1-3 = slots within a row.
    option_row         integer       NOT NULL DEFAULT 1
                                   CONSTRAINT logo_assignment_option_row_check
                                   CHECK (option_row BETWEEN 1 AND 999),
    name_override      text,
    CONSTRAINT assignment_option_row_check CHECK (option_row >= 1),
    PRIMARY KEY (fdm4_store, product_style, garment_color_code, option_row, position)
);

-- Column reconcile for installations that predate later columns.
ALTER TABLE logo.assignment
    ADD COLUMN IF NOT EXISTS sort_order integer NOT NULL DEFAULT 0;
ALTER TABLE logo.assignment
    ADD COLUMN IF NOT EXISTS image_url text NOT NULL DEFAULT '';
ALTER TABLE logo.assignment
    ADD COLUMN IF NOT EXISTS option_row integer NOT NULL DEFAULT 1;
ALTER TABLE logo.assignment
    ADD COLUMN IF NOT EXISTS name_override text;

-- ADD COLUMN IF NOT EXISTS does not add an inline constraint when the column
-- already exists. Reconcile named constraints explicitly for upgraded installs.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'logo.assignment'::regclass
           AND conname = 'logo_assignment_position_check'
    ) THEN
        ALTER TABLE logo.assignment
            ADD CONSTRAINT logo_assignment_position_check
            CHECK (position BETWEEN 1 AND 3);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'logo.assignment'::regclass
           AND conname = 'logo_assignment_option_row_check'
    ) THEN
        ALTER TABLE logo.assignment
            ADD CONSTRAINT logo_assignment_option_row_check
            CHECK (option_row BETWEEN 1 AND 999);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS logo_assign_store_style
    ON logo.assignment (fdm4_store, product_style);
CREATE INDEX IF NOT EXISTS logo_assign_design
    ON logo.assignment (design_id);

GRANT SELECT ON logo.assignment TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.assignment TO etl_writer;

CREATE TABLE IF NOT EXISTS logo.store_settings (
    fdm4_store  text          NOT NULL PRIMARY KEY,
    enabled     boolean       NOT NULL DEFAULT true,
    allows_none boolean       NOT NULL DEFAULT false,
    updated_by  text          NOT NULL DEFAULT '',
    updated_at  timestamptz   NOT NULL DEFAULT now()
);

-- Column reconcile for installations that predate the audit column.
ALTER TABLE logo.store_settings
    ADD COLUMN IF NOT EXISTS updated_by text NOT NULL DEFAULT '';

GRANT SELECT ON logo.store_settings TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.store_settings TO etl_writer;

-- Legacy image mirror map: source URL -> locally stored filename. Makes the
-- image import idempotent/re-runnable (an already-imported URL is reused, not
-- re-downloaded) and records provenance for every mirrored asset.
CREATE TABLE IF NOT EXISTS logo.image_import (
    source_url  text          NOT NULL PRIMARY KEY,
    filename    text          NOT NULL,
    bytes       bigint        NOT NULL DEFAULT 0,
    imported_at timestamptz   NOT NULL DEFAULT now(),
    imported_by text          NOT NULL DEFAULT ''
);
GRANT SELECT ON logo.image_import TO woo_reader, insights_reader;
GRANT SELECT, INSERT ON logo.image_import TO etl_writer;

CREATE TABLE IF NOT EXISTS logo.import_report (
    id            bigserial     PRIMARY KEY,
    imported_at   timestamptz   NOT NULL DEFAULT now(),
    fdm4_store    text,
    product_style text,
    product_color text,
    logo_code     text,
    reason        text          NOT NULL,
    detail        text
);

COMMENT ON COLUMN logo.import_report.reason IS
    'Validation/import reason code (for example: no_store, no_style, no_color_code, no_design, no_art, ambiguous_design, ambiguous_color, orphaned_companion, conflicting_location, duplicate_row, invalid_value, invalid_integer, invalid_boolean, invalid_cost, invalid_image_url, invalid_csv, database_error, image_import_failed).';

GRANT SELECT ON logo.import_report TO woo_reader, insights_reader;
GRANT SELECT, INSERT ON logo.import_report TO etl_writer;
GRANT USAGE, SELECT ON SEQUENCE logo.import_report_id_seq TO etl_writer;


-- ============================================================================
-- Append-only audit history (see migrations/2026-07-15-logo-audit-log.sql)
-- ============================================================================

CREATE TABLE IF NOT EXISTS logo.audit_log (
    id                 bigserial     PRIMARY KEY,
    at                 timestamptz   NOT NULL DEFAULT now(),
    actor              text          NOT NULL DEFAULT '',
    action             text          NOT NULL,
    fdm4_store         text          NOT NULL DEFAULT '',
    product_style      text          NOT NULL DEFAULT '',
    garment_color_code text          NOT NULL DEFAULT '',
    option_row         integer,
    position           integer,
    detail             jsonb
);

CREATE INDEX IF NOT EXISTS logo_audit_store_idx ON logo.audit_log (fdm4_store, id DESC);
CREATE INDEX IF NOT EXISTS logo_audit_at_idx    ON logo.audit_log (at DESC);

-- Immutable from every non-superuser role: INSERT + SELECT only.
GRANT SELECT, INSERT ON logo.audit_log TO logo_admin, etl_writer;
GRANT SELECT ON logo.audit_log TO woo_reader, insights_reader;
GRANT USAGE, SELECT ON SEQUENCE logo.audit_log_id_seq TO logo_admin, etl_writer;
REVOKE UPDATE, DELETE, TRUNCATE ON logo.audit_log FROM logo_admin, etl_writer, woo_reader, insights_reader;

CREATE OR REPLACE FUNCTION logo.audit_row() RETURNS trigger AS $$
DECLARE
    v_actor   text := left(coalesce(nullif(current_setting('logo.actor', true), ''), current_user::text), 100);
    v_old     jsonb := CASE WHEN TG_OP IN ('UPDATE', 'DELETE') THEN to_jsonb(OLD) END;
    v_new     jsonb := CASE WHEN TG_OP IN ('UPDATE', 'INSERT') THEN to_jsonb(NEW) END;
    v_row     jsonb := coalesce(v_new, v_old);
    v_changes jsonb;
    v_verb    text := CASE TG_OP WHEN 'INSERT' THEN 'created' WHEN 'UPDATE' THEN 'updated' ELSE 'deleted' END;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Skip no-op writes (idempotent re-imports re-stamp updated_at/by on
        -- identical data; that is not a change worth a history line).
        IF (v_old - 'updated_at' - 'updated_by') = (v_new - 'updated_at' - 'updated_by') THEN
            RETURN NEW;
        END IF;
        SELECT jsonb_object_agg(o.key, jsonb_build_object('from', o.value, 'to', n.value))
          INTO v_changes
          FROM jsonb_each(v_old) o
          JOIN jsonb_each(v_new) n USING (key)
         WHERE o.value IS DISTINCT FROM n.value
           AND o.key NOT IN ('updated_at', 'updated_by');
    END IF;

    INSERT INTO logo.audit_log (actor, action, fdm4_store, product_style, garment_color_code, option_row, position, detail)
    VALUES (
        v_actor,
        CASE WHEN TG_TABLE_NAME = 'assignment' THEN 'assignment_' WHEN TG_TABLE_NAME = 'color_class' THEN 'color_class_' ELSE 'store_settings_' END || v_verb,
        coalesce(v_row->>'fdm4_store', ''),
        coalesce(v_row->>'product_style', ''),
        coalesce(v_row->>'garment_color_code', ''),
        (v_row->>'option_row')::integer,
        (v_row->>'position')::integer,
        jsonb_strip_nulls(jsonb_build_object(
            'changes', v_changes,
            'old', CASE WHEN TG_OP = 'DELETE' THEN v_old END,
            'new', CASE WHEN TG_OP = 'INSERT' THEN v_new END
        ))
    );
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS logo_assignment_audit ON logo.assignment;
CREATE TRIGGER logo_assignment_audit
    AFTER INSERT OR UPDATE OR DELETE ON logo.assignment
    FOR EACH ROW EXECUTE FUNCTION logo.audit_row();

DROP TRIGGER IF EXISTS logo_store_settings_audit ON logo.store_settings;
CREATE TRIGGER logo_store_settings_audit
    AFTER INSERT OR UPDATE OR DELETE ON logo.store_settings
    FOR EACH ROW EXECUTE FUNCTION logo.audit_row();


-- Customer-facing design/scheme names. The physical column order matches the
-- 2026-08-01 live schema snapshot so a blank install and an upgraded install
-- converge before additive fix migrations extend the schema.
CREATE TABLE IF NOT EXISTS logo.display_name (
    design_id         text        NOT NULL,
    color_scheme_id   text        NOT NULL,
    name              text        NOT NULL,
    source            text        NOT NULL DEFAULT 'manual',
    locked            boolean     NOT NULL DEFAULT false,
    uses              integer     NOT NULL DEFAULT 0,
    fdm4_description  text,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text,
    fdm4_store        text        NOT NULL DEFAULT '',
    PRIMARY KEY (design_id, color_scheme_id, fdm4_store)
);

COMMENT ON TABLE logo.display_name IS
    'Customer-facing logo names per (design_id,color_scheme_id). Seeded from FDM4 design_pool + filename parse; editable in Warehouse Ops; re-pullable from FDM4 via logo.repull_display_name().';

CREATE OR REPLACE FUNCTION logo.audit_display_name_row() RETURNS trigger AS $$
DECLARE
    v_actor   text := left(coalesce(nullif(current_setting('logo.actor', true), ''), current_user::text), 100);
    v_old     jsonb := CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN to_jsonb(OLD) END;
    v_new     jsonb := CASE WHEN TG_OP IN ('UPDATE','INSERT') THEN to_jsonb(NEW) END;
    v_row     jsonb := coalesce(v_new, v_old);
    v_changes jsonb;
    v_verb    text := CASE TG_OP WHEN 'INSERT' THEN 'created' WHEN 'UPDATE' THEN 'updated' ELSE 'deleted' END;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF (v_old - 'updated_at' - 'updated_by') = (v_new - 'updated_at' - 'updated_by') THEN RETURN NEW; END IF;
        SELECT jsonb_object_agg(o.key, jsonb_build_object('from',o.value,'to',n.value))
          INTO v_changes FROM jsonb_each(v_old) o JOIN jsonb_each(v_new) n USING(key)
         WHERE o.value IS DISTINCT FROM n.value AND o.key NOT IN ('updated_at','updated_by');
    END IF;
    INSERT INTO logo.audit_log (actor, action, fdm4_store, product_style, garment_color_code, option_row, position, detail)
    VALUES (v_actor, 'logo_name_'||v_verb, '', coalesce(v_row->>'design_id',''), coalesce(v_row->>'color_scheme_id',''), NULL, NULL,
        jsonb_strip_nulls(jsonb_build_object('changes', v_changes,
            'old', CASE WHEN TG_OP='DELETE' THEN v_old END,
            'new', CASE WHEN TG_OP='INSERT' THEN v_new END)));
    RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS logo_display_name_audit ON logo.display_name;
CREATE TRIGGER logo_display_name_audit
    AFTER INSERT OR UPDATE OR DELETE ON logo.display_name
    FOR EACH ROW EXECUTE FUNCTION logo.audit_display_name_row();

GRANT SELECT ON logo.display_name TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.display_name TO logo_admin, etl_writer;


-- ============================================================================
-- Canonical FDM4 placement vocabulary (see migrations/2026-07-15-logo-placement-vocab.sql)
-- ============================================================================

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


-- ============================================================================
-- Garment-color classification and bulk-apply batch tracking
-- (see migrations/2026-07-23-bulk-apply-logos.sql)
-- ============================================================================

CREATE TABLE IF NOT EXISTS logo.color_class (
  color_code text PRIMARY KEY,
  color_name text NOT NULL,
  light_dark text NOT NULL CHECK (light_dark IN ('light','dark','both')),
  source     text NOT NULL DEFAULT 'ai' CHECK (source IN ('ai','manual')),
  confidence numeric,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text NOT NULL DEFAULT ''
);

DROP TRIGGER IF EXISTS logo_color_class_audit ON logo.color_class;
CREATE TRIGGER logo_color_class_audit
  AFTER INSERT OR UPDATE OR DELETE ON logo.color_class
  FOR EACH ROW EXECUTE FUNCTION logo.audit_row();

CREATE TABLE IF NOT EXISTS logo.bulk_batch (
  batch_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fdm4_store   text NOT NULL,
  logo_code    text,
  color_scheme text,
  placement    text,
  target       jsonb,
  applied      int NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   text NOT NULL DEFAULT '',
  undone_at    timestamptz
);

CREATE TABLE IF NOT EXISTS logo.bulk_batch_row (
  batch_id           bigint NOT NULL REFERENCES logo.bulk_batch(batch_id) ON DELETE CASCADE,
  fdm4_store         text NOT NULL,
  product_style      text NOT NULL,
  garment_color_code text NOT NULL,
  option_row         int  NOT NULL,
  position           int  NOT NULL,
  before_row         jsonb,
  after_row          jsonb,
  PRIMARY KEY (batch_id, product_style, garment_color_code, option_row, position)
);

-- Editor-only display order of garment colors inside one style's logo grid.
-- Global per style (a garment's colors are the same in every store). Colors
-- without a row sort after the ordered ones, alphabetically by name. This
-- never reaches /feed or WordPress: storefront color order is WooCommerce
-- attribute order, not a logo concern. Mirror of
-- migrations/2026-09-02-style-color-order.sql for blank installs.
CREATE TABLE IF NOT EXISTS logo.style_color_order (
    product_style       text        NOT NULL CHECK (btrim(product_style) <> ''),
    garment_color_code  text        NOT NULL CHECK (btrim(garment_color_code) <> ''),
    sort_order          integer     NOT NULL CHECK (sort_order >= 0),
    updated_by          text        NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product_style, garment_color_code)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE logo.style_color_order TO logo_admin;

GRANT SELECT ON logo.color_class TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.color_class, logo.bulk_batch, logo.bulk_batch_row TO logo_admin, etl_writer;


-- ---------------------------------------------------------------------------
-- Category editor (catmgr): snapshot layer. Mirror of
-- migrations/2026-09-01-category-editor-foundations.sql for blank installs.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS catmgr;

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
GRANT SELECT, INSERT ON catmgr.audit_log TO logo_admin;
REVOKE UPDATE, DELETE, TRUNCATE ON catmgr.audit_log FROM logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;

-- ---------------------------------------------------------------------------
-- Category editor (catmgr): draft tree + store overlays. Mirror of
-- migrations/2026-09-01-category-editor-phase2-draft.sql.
-- ---------------------------------------------------------------------------


CREATE TABLE IF NOT EXISTS catmgr.node (
    node_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_id   bigint REFERENCES catmgr.node (node_id) ON DELETE RESTRICT,
    name        text    NOT NULL CHECK (btrim(name) <> ''),
    slug        text    NOT NULL UNIQUE
                CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    sort_order  integer NOT NULL DEFAULT 0,
    description text    NOT NULL DEFAULT '',
    updated_by  text    NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS node_parent ON catmgr.node (parent_id);

CREATE TABLE IF NOT EXISTS catmgr.node_store_override (
    override_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    blog_id             integer NOT NULL,
    blog_path           text    NOT NULL DEFAULT '',
    kind                text    NOT NULL
                        CHECK (kind IN ('extra_node', 'rename', 'exclude')),
    node_id             bigint REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    name                text,
    slug                text CHECK (slug IS NULL OR slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    parent_node_id      bigint REFERENCES catmgr.node (node_id) ON DELETE SET NULL,
    include_descendants boolean NOT NULL DEFAULT true,
    sort_order          integer NOT NULL DEFAULT 0,
    updated_by          text    NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT override_shape CHECK (
        (kind = 'extra_node' AND node_id IS NULL
             AND name IS NOT NULL AND btrim(name) <> ''
             AND slug IS NOT NULL)
        OR (kind = 'rename' AND node_id IS NOT NULL
             AND name IS NOT NULL AND btrim(name) <> ''
             AND slug IS NULL AND parent_node_id IS NULL)
        OR (kind = 'exclude' AND node_id IS NOT NULL
             AND name IS NULL AND slug IS NULL AND parent_node_id IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS override_rename_once
    ON catmgr.node_store_override (blog_id, node_id) WHERE kind = 'rename';
CREATE UNIQUE INDEX IF NOT EXISTS override_exclude_once
    ON catmgr.node_store_override (blog_id, node_id) WHERE kind = 'exclude';
CREATE UNIQUE INDEX IF NOT EXISTS override_extra_slug_once
    ON catmgr.node_store_override (blog_id, slug) WHERE kind = 'extra_node';
CREATE INDEX IF NOT EXISTS override_blog ON catmgr.node_store_override (blog_id);

GRANT SELECT ON catmgr.node, catmgr.node_store_override
    TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON catmgr.node, catmgr.node_store_override TO logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;


-- ---------------------------------------------------------------------------
-- Category editor (catmgr): slug map, rules, product assignments. Mirror of
-- migrations/2026-09-01-category-editor-phase3-mapping.sql.
-- ---------------------------------------------------------------------------


CREATE TABLE IF NOT EXISTS catmgr.slug_map (
    old_slug       text PRIMARY KEY,
    action         text NOT NULL
                   CHECK (action IN ('map', 'delete', 'store_custom')),
    target_node_id bigint REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    is_primary     boolean NOT NULL DEFAULT false,
    override_id    bigint REFERENCES catmgr.node_store_override (override_id)
                   ON DELETE CASCADE,
    note           text NOT NULL DEFAULT '',
    updated_by     text NOT NULL DEFAULT '',
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT slug_map_shape CHECK (
        (action = 'map' AND target_node_id IS NOT NULL AND override_id IS NULL)
        OR (action = 'delete' AND target_node_id IS NULL
            AND override_id IS NULL AND NOT is_primary)
        OR (action = 'store_custom' AND target_node_id IS NULL
            AND NOT is_primary)
    )
);
-- Exactly one primary (in-place survivor) per target node.
CREATE UNIQUE INDEX IF NOT EXISTS slug_map_one_primary
    ON catmgr.slug_map (target_node_id) WHERE is_primary;
CREATE INDEX IF NOT EXISTS slug_map_target ON catmgr.slug_map (target_node_id);

CREATE TABLE IF NOT EXISTS catmgr.assignment_rule (
    rule_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id    bigint NOT NULL REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    kind       text   NOT NULL DEFAULT 'filter' CHECK (kind IN ('filter')),
    spec       jsonb  NOT NULL,
    priority   integer NOT NULL DEFAULT 0,
    note       text    NOT NULL DEFAULT '',
    updated_by text    NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assignment_rule_node ON catmgr.assignment_rule (node_id);

CREATE TABLE IF NOT EXISTS catmgr.product_assignment (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id  bigint NOT NULL REFERENCES catmgr.node (node_id) ON DELETE CASCADE,
    sku      text   NOT NULL CHECK (btrim(sku) <> ''),
    mode     text   NOT NULL CHECK (mode IN ('add', 'remove')),
    source   text   NOT NULL CHECK (source IN ('manual', 'csv', 'ai', 'rule')),
    note     text   NOT NULL DEFAULT '',
    added_by text   NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (node_id, sku, mode)
);
CREATE INDEX IF NOT EXISTS product_assignment_sku ON catmgr.product_assignment (sku);

GRANT SELECT ON catmgr.slug_map, catmgr.assignment_rule, catmgr.product_assignment
    TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON catmgr.slug_map, catmgr.assignment_rule, catmgr.product_assignment
    TO logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;


-- ---------------------------------------------------------------------------
-- Category editor (catmgr): uncategorized acknowledgements. Mirror of
-- migrations/2026-09-01-category-editor-phase4-planner.sql.
-- ---------------------------------------------------------------------------


CREATE TABLE IF NOT EXISTS catmgr.uncategorized_ack (
    sku      text PRIMARY KEY CHECK (btrim(sku) <> ''),
    note     text NOT NULL DEFAULT '',
    added_by text NOT NULL DEFAULT '',
    added_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON catmgr.uncategorized_ack TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON catmgr.uncategorized_ack TO logo_admin;


-- ---------------------------------------------------------------------------
-- Category editor (catmgr): runs, jobs, snapshots, redirects. Mirror of
-- migrations/2026-09-01-category-editor-phase5-runs.sql.
-- ---------------------------------------------------------------------------


CREATE TABLE IF NOT EXISTS catmgr.run (
    run_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    env                 text NOT NULL CHECK (env IN ('dev', 'prod')),
    target_blogs        integer[] NOT NULL,
    status              text NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'paused',
                                          'completed', 'failed', 'cancelled')),
    plan_totals         jsonb NOT NULL DEFAULT '{}'::jsonb,
    snapshot_versions   jsonb NOT NULL DEFAULT '{}'::jsonb,
    stop_on_failure     boolean NOT NULL DEFAULT true,
    cancel_requested    boolean NOT NULL DEFAULT false,
    created_by          text NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    worker_heartbeat_at timestamptz
);
CREATE INDEX IF NOT EXISTS run_env_status ON catmgr.run (env, status);

CREATE TABLE IF NOT EXISTS catmgr.run_job (
    job_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     bigint NOT NULL REFERENCES catmgr.run (run_id) ON DELETE CASCADE,
    blog_id    integer NOT NULL,
    blog_path  text NOT NULL DEFAULT '',
    seq        integer NOT NULL,
    status     text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'done', 'failed',
                                 'skipped', 'cancelled')),
    payload    jsonb NOT NULL,
    progress   jsonb NOT NULL DEFAULT '{}'::jsonb,
    result     jsonb,
    attempt    integer NOT NULL DEFAULT 0,
    request_id text NOT NULL DEFAULT '',
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (run_id, blog_id)
);
CREATE INDEX IF NOT EXISTS run_job_run_seq ON catmgr.run_job (run_id, seq);

CREATE TABLE IF NOT EXISTS catmgr.job_snapshot (
    job_id   bigint PRIMARY KEY REFERENCES catmgr.run_job (job_id) ON DELETE CASCADE,
    payload  jsonb NOT NULL,
    taken_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS catmgr.redirect (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     bigint NOT NULL REFERENCES catmgr.run (run_id) ON DELETE CASCADE,
    blog_id    integer NOT NULL,
    old_path   text NOT NULL,
    new_path   text NOT NULL,
    status     text NOT NULL DEFAULT 'planned'
               CHECK (status IN ('planned', 'created', 'failed')),
    detail     text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS redirect_run ON catmgr.redirect (run_id);

GRANT SELECT ON catmgr.run, catmgr.run_job, catmgr.job_snapshot, catmgr.redirect
    TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON catmgr.run, catmgr.run_job, catmgr.job_snapshot, catmgr.redirect
    TO logo_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA catmgr TO logo_admin;


COMMIT;
