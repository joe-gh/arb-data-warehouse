-- Fix: canonical_key must allow MULTI-SEGMENT keys.
--
-- The original constraint assumed keys are always exactly products/<sku>/<file>,
-- but 656 of 245,923 published objects have a SKU containing a slash (e.g.
-- 'WU-DW/BRBL' -> products/WU-DW/BRBL/19394.jpg). Those objects are already in
-- the bucket and serve correctly; only the constraint was wrong, and it aborted
-- the blog-1 export.
--
-- The replacement keeps the security guarantees that matter (prefix pinned to
-- products/, no traversal, no backslashes, no empty segments, real filename)
-- while allowing any number of interior segments. The rendition writers already
-- derive sibling keys with rsplit/dirname, so multi-segment works unchanged.
BEGIN;

ALTER TABLE pim.media_rendition
    DROP CONSTRAINT IF EXISTS media_rendition_canonical_key_check;

ALTER TABLE pim.media_rendition
    ADD CONSTRAINT media_rendition_canonical_key_check
    CHECK (
        canonical_key ~ '^products/([^/\\]+/)+[^/\\]+$'
        AND canonical_key !~ '(^|/)\.\.?(/|$)'
    );

COMMIT;
