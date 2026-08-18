-- Harden logo.assignment after the option_row rollout.
--
-- The applied 2026-07-14 migration only counted primary-key columns and its
-- inline option_row CHECK could be skipped when the column already existed.
-- This migration verifies the exact key and adds explicit bounded constraints.
BEGIN;

DO $$
DECLARE
    current_key text[];
    primary_name text;
BEGIN
    SELECT c.conname,
           array_agg(a.attname ORDER BY key_column.ordinality)
      INTO primary_name, current_key
      FROM pg_constraint c
      CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
      JOIN pg_attribute a
        ON a.attrelid = c.conrelid
       AND a.attnum = key_column.attnum
     WHERE c.conrelid = 'logo.assignment'::regclass
       AND c.contype = 'p'
     GROUP BY c.conname;

    IF current_key IS DISTINCT FROM ARRAY[
        'fdm4_store',
        'product_style',
        'garment_color_code',
        'option_row',
        'position'
    ]::text[] THEN
        IF primary_name IS NOT NULL THEN
            EXECUTE format(
                'ALTER TABLE logo.assignment DROP CONSTRAINT %I',
                primary_name
            );
        END IF;
        ALTER TABLE logo.assignment
            ADD PRIMARY KEY (
                fdm4_store,
                product_style,
                garment_color_code,
                option_row,
                position
            );
    END IF;
END $$;

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

COMMIT;
