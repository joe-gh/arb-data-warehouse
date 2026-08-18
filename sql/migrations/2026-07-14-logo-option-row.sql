-- Adds the option_row dimension: one (store, style, color) may carry any
-- number of selectable logo rows; position 1-3 are slots WITHIN a row.
-- Existing rows become option_row 1.
BEGIN;

ALTER TABLE logo.assignment
    ADD COLUMN IF NOT EXISTS option_row integer NOT NULL DEFAULT 1
        CHECK (option_row >= 1);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
         WHERE c.conrelid = 'logo.assignment'::regclass
           AND c.contype = 'p'
           AND (SELECT count(*) FROM unnest(c.conkey)) = 5
    ) THEN
        ALTER TABLE logo.assignment DROP CONSTRAINT assignment_pkey;
        ALTER TABLE logo.assignment
            ADD PRIMARY KEY (fdm4_store, product_style, garment_color_code, option_row, position);
    END IF;
END $$;

COMMIT;
