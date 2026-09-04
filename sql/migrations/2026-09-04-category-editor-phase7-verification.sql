-- Category editor phase 7 (2026-09-04): live-state fences, parked-term
-- lineage, uncategorized products, worker claim tokens, richer run outcomes.
--
-- wp_term.parked_from   the ORIGINAL slug of a term the broker parked on a
--                       catmgrtmp-<id> temp slug (from its _arb_catmgr_parked
--                       term meta), so a re-plan keeps the term's disposition
--                       (merge destination, in-place update) instead of
--                       inferring a plain delete from the slug prefix.
-- snapshot.fingerprint  sha256 of the normalized export (terms, memberships,
--                       uncategorized products). A plan carries it and the
--                       worker compares it with the LIVE export captured right
--                       before the first mutation: any WordPress change since
--                       the snapshot refuses the apply instead of being
--                       silently overwritten.
-- wp_uncategorized_product  products with no product_cat at all: they were
--                       invisible to rules/assignments because the export
--                       started from term_relationships.
-- run_job.worker_token  the app worker that claimed the job; a stale worker
--                       (reclaimed after a restart) can no longer overwrite a
--                       newer worker's progress.
-- run.status            completed_with_skips / completed_unverified make a
--                       partial or unverified migration visible.
BEGIN;

ALTER TABLE catmgr.wp_term
    ADD COLUMN IF NOT EXISTS parked_from text NOT NULL DEFAULT '';
ALTER TABLE catmgr.snapshot
    ADD COLUMN IF NOT EXISTS fingerprint text NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS catmgr.wp_uncategorized_product (
    env              text    NOT NULL CHECK (env IN ('dev', 'prod')),
    blog_id          integer NOT NULL,
    product_id       bigint  NOT NULL,
    sku              text    NOT NULL DEFAULT '',
    snapshot_version bigint  NOT NULL,
    PRIMARY KEY (env, blog_id, product_id)
);
CREATE INDEX IF NOT EXISTS wp_uncategorized_product_sku
    ON catmgr.wp_uncategorized_product (env, blog_id, sku);

ALTER TABLE catmgr.run_job
    ADD COLUMN IF NOT EXISTS worker_token text NOT NULL DEFAULT '';

ALTER TABLE catmgr.run DROP CONSTRAINT IF EXISTS run_status_check;
ALTER TABLE catmgr.run ADD CONSTRAINT run_status_check CHECK (status IN (
    'queued', 'running', 'paused', 'completed', 'completed_with_skips',
    'completed_unverified', 'failed', 'cancelled'));

GRANT SELECT ON catmgr.wp_uncategorized_product TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON catmgr.wp_uncategorized_product TO logo_admin;

COMMIT;
