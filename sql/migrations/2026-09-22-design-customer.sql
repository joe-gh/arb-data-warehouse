-- Which FDM4 customers may use a design. FDM4 attaches customers at two
-- levels: the design itself (dec_design.cust_number) and each artwork inside
-- it (customer_art.cust_number = primary owner; customer_art_cust = the "Art
-- Customers" list, primary + secondaries). Lewis's left-chest design 3816 is
-- filed under the stock account 003101 while its art 72.2 lists Lewis 001114
-- as a customer, so the design-level rule alone refused it (2026-09-21).
--
-- Materialized, not a view: infra/load_dump.py DROPs and re-CREATEs every
-- fdm4.* table each hour and a dependent view would block the drop. The
-- loader calls logo.refresh_design_customer() right after the raw swap, so the
-- table is never more than one load stale. Read by the app
-- (design_resolver.design_available_to_store) and by WordPress
-- (arb_logo_design_map rebuild) so both sides give the same answer.
BEGIN;

CREATE TABLE IF NOT EXISTS logo.design_customer (
    design_id     text        NOT NULL CHECK (btrim(design_id) <> ''),
    cust_number   text        NOT NULL CHECK (btrim(cust_number) <> ''),
    via           text        NOT NULL CHECK (via IN ('design', 'art_primary', 'art_customer')),
    refreshed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (design_id, cust_number, via)
);
CREATE INDEX IF NOT EXISTS design_customer_cust_idx ON logo.design_customer (cust_number, design_id);

COMMENT ON TABLE logo.design_customer IS
    'Per design: every FDM4 customer allowed to use it (design owner + owners/customers of its artworks). Rebuilt by logo.refresh_design_customer() after each fdm4 load.';

CREATE OR REPLACE FUNCTION logo.refresh_design_customer() RETURNS integer AS $fn$
DECLARE
    n integer;
BEGIN
    -- Whole-table replace inside the caller's transaction: readers see the
    -- old set or the new set, never a partial one.
    DELETE FROM logo.design_customer;
    INSERT INTO logo.design_customer (design_id, cust_number, via)
    SELECT btrim(d.design_id), btrim(d.cust_number), 'design'
      FROM fdm4.dec_design d
     WHERE NULLIF(btrim(d.design_id), '') IS NOT NULL
       AND NULLIF(btrim(d.cust_number), '') IS NOT NULL
    UNION
    SELECT btrim(p.design_id), btrim(ca.cust_number), 'art_primary'
      FROM fdm4.design_pool p
      JOIN fdm4.customer_art ca
        ON btrim(ca.art_id) = btrim(p.art_id)
       AND btrim(ca.art_version_id) = COALESCE(NULLIF(btrim(p.art_version_id), ''), '1')
     WHERE NULLIF(btrim(p.design_id), '') IS NOT NULL
       AND NULLIF(btrim(ca.cust_number), '') IS NOT NULL
    UNION
    SELECT btrim(p.design_id), btrim(cac.cust_number), 'art_customer'
      FROM fdm4.design_pool p
      JOIN fdm4.customer_art_cust cac
        ON btrim(cac.art_id) = btrim(p.art_id)
       AND btrim(cac.art_version_id) = COALESCE(NULLIF(btrim(p.art_version_id), ''), '1')
     WHERE NULLIF(btrim(p.design_id), '') IS NOT NULL
       AND NULLIF(btrim(cac.cust_number), '') IS NOT NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$fn$ LANGUAGE plpgsql;

REVOKE EXECUTE ON FUNCTION logo.refresh_design_customer() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION logo.refresh_design_customer() TO etl_writer;

GRANT SELECT ON logo.design_customer TO woo_reader, insights_reader, logo_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON logo.design_customer TO etl_writer;

-- Populate now when the source tables already exist (production after the
-- extractor change); a harness without them is populated by tests/sql/seed.sql.
DO $do$
BEGIN
    IF to_regclass('fdm4.customer_art') IS NOT NULL
       AND to_regclass('fdm4.customer_art_cust') IS NOT NULL THEN
        PERFORM logo.refresh_design_customer();
    END IF;
END
$do$;

COMMIT;
