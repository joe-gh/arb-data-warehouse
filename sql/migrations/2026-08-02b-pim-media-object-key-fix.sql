-- Fix: s3_key must NOT be unique. The dedupe design maps MANY source URLs
-- (the same image uploaded to N multisite blogs) onto ONE canonical S3 object;
-- the unique index made the publisher's mapping flush fail on the first
-- cross-site duplicate. Keep a plain index for lookups.
BEGIN;
DROP INDEX IF EXISTS pim.media_object_key;
CREATE INDEX IF NOT EXISTS media_object_key ON pim.media_object (s3_key);
COMMIT;
