-- PIM push service: staged change sets (infra/push_pim.py).
-- The diff engine writes proposed rows here; nothing touches the PIM until
-- rows are explicitly approved and the apply engine runs with
-- PIM_PUSH_ENABLED=1. Rows carry full before and after values for review.
BEGIN;

CREATE TABLE IF NOT EXISTS pim.push_change_set (
    set_id     bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT '',
    note       text NOT NULL DEFAULT '',
    status     text NOT NULL DEFAULT 'proposed'
               CHECK (status IN ('proposed','approved','applying','done','cancelled'))
);

CREATE TABLE IF NOT EXISTS pim.push_change_row (
    row_id     bigserial PRIMARY KEY,
    set_id     bigint NOT NULL REFERENCES pim.push_change_set(set_id) ON DELETE CASCADE,
    lane       text NOT NULL CHECK (lane IN ('a','b')),
    action     text NOT NULL CHECK (action IN
                 ('color_fill','color_fix','variant_remove','variant_create','product_create')),
    prod_ref   text NOT NULL DEFAULT '',
    frmt_ref   text NOT NULL DEFAULT '',
    style_code text NOT NULL DEFAULT '',
    before     jsonb,
    after      jsonb,
    status     text NOT NULL DEFAULT 'proposed'
               CHECK (status IN ('proposed','approved','rejected','applied','failed','skipped')),
    result     text NOT NULL DEFAULT '',
    applied_at timestamptz
);
CREATE INDEX IF NOT EXISTS push_change_row_set ON pim.push_change_row (set_id, status);
CREATE INDEX IF NOT EXISTS push_change_row_action ON pim.push_change_row (set_id, action);

GRANT SELECT ON pim.push_change_set, pim.push_change_row TO woo_reader, insights_reader, logo_admin;
GRANT SELECT, INSERT, UPDATE ON pim.push_change_set, pim.push_change_row TO etl_writer;
GRANT USAGE ON SEQUENCE pim.push_change_set_set_id_seq, pim.push_change_row_row_id_seq TO etl_writer;

COMMIT;
