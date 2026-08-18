-- Garment-color light/dark classification and bulk-apply batch tracking.
--
-- color_class: AI or manually reviewed classification of each garment color
--   as "light" or "dark". Drives the single-store bulk-apply feature so the
--   UI can suggest appropriate logos for each garment color bucket.
--
-- bulk_batch / bulk_batch_row: undo-safe batch records for the bulk-apply
--   operation. bulk_batch captures intent (store, target filter, result count);
--   bulk_batch_row snapshots the before-state of every assignment row touched
--   so the apply can be fully reversed.
BEGIN;

CREATE TABLE IF NOT EXISTS logo.color_class (
  color_code text PRIMARY KEY,
  color_name text NOT NULL,
  light_dark text NOT NULL CHECK (light_dark IN ('light','dark')),
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
  PRIMARY KEY (batch_id, product_style, garment_color_code, option_row, position)
);

GRANT SELECT ON logo.color_class TO woo_reader, insights_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.color_class, logo.bulk_batch, logo.bulk_batch_row TO logo_admin, etl_writer;

COMMIT;
