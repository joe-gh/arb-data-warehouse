-- Manually curated Woo category trees, imported from a production blog's
-- product_cat taxonomy (see infra/import-curated-categories.sh). Deliberately
-- separate from everything FDM4: FDM4 only knows a flat ~16-value category
-- vocabulary, while the retail site carries a hand-curated 3-level tree.
-- Import is full-replace per blog, so re-running always reflects current
-- curation; imported_at stamps the snapshot.
BEGIN;

CREATE SCHEMA IF NOT EXISTS curated;

CREATE TABLE IF NOT EXISTS curated.category (
    blog_id        integer NOT NULL,
    term_id        bigint  NOT NULL,
    slug           text    NOT NULL,
    name           text    NOT NULL,
    parent_term_id bigint  NOT NULL DEFAULT 0,   -- 0 = top level
    depth          integer NOT NULL DEFAULT 0,   -- 0 = top level
    path           text    NOT NULL DEFAULT '',  -- "Men's > Pants > Work Pants"
    sort_order     integer NOT NULL DEFAULT 0,   -- WP term_order when present
    product_count  integer NOT NULL DEFAULT 0,   -- published products (direct)
    imported_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (blog_id, term_id)
);

CREATE TABLE IF NOT EXISTS curated.category_product (
    blog_id     integer NOT NULL,
    term_id     bigint  NOT NULL,
    sku         text    NOT NULL,      -- parent product SKU / style code
    product_id  bigint  NOT NULL,      -- WP post id, for traceability
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (blog_id, term_id, product_id)
);
CREATE INDEX IF NOT EXISTS category_product_sku ON curated.category_product (blog_id, sku);

GRANT USAGE ON SCHEMA curated TO woo_reader, insights_reader, logo_admin, etl_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA curated TO woo_reader, insights_reader, logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA curated TO etl_writer;

COMMIT;
