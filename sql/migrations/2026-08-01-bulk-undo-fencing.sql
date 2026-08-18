-- Store the exact state written by a bulk batch so undo can refuse to clobber
-- an assignment changed after the batch.
BEGIN;

ALTER TABLE logo.bulk_batch_row
    ADD COLUMN IF NOT EXISTS after_row jsonb;

COMMIT;
