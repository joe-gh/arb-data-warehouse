-- Operator-facing store names. woo.store_blog_map.blog_name carries the
-- WordPress site title of the store's blog so the Warehouse Ops pickers read
-- "Payne Tompkins" instead of a capitalized catalog slug ("Paynetompkins").
-- Seeded from PROD (sql/seeds/2026-09-03-store-blog-names.sql); refresh the
-- same way when a store is renamed in WordPress (logo-admin/README.md,
-- "Store display names").
BEGIN;

ALTER TABLE woo.store_blog_map
    ADD COLUMN IF NOT EXISTS blog_name text NOT NULL DEFAULT '';

COMMIT;
