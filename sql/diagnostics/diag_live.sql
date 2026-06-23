\echo ===== columns of catalog_product_detail =====
\d fdm4.catalog_product_detail
\echo ===== detail_type distribution =====
SELECT detail_type, count(*) FROM fdm4.catalog_product_detail GROUP BY 1 ORDER BY 2 DESC LIMIT 25;
\echo ===== site_id format =====
SELECT count(*) AS total,
       count(*) FILTER (WHERE site_id ~ '^S_') AS s_prefixed,
       count(DISTINCT site_id) AS distinct_sites
FROM fdm4.catalog_product_detail;
\echo ===== sample site_id values =====
SELECT DISTINCT site_id FROM fdm4.catalog_product_detail ORDER BY 1 LIMIT 25;
\echo ===== storeData rows + json validity (whole table) =====
SELECT count(*) AS storedata_rows FROM fdm4.catalog_product_detail WHERE detail_type = 'storeData';
SELECT count(*) AS valid_json_rows FROM fdm4.catalog_product_detail WHERE pg_input_is_valid(detail_value, 'jsonb');
\echo ===== sample detail_value (first 300 chars) =====
SELECT detail_type, left(detail_value, 300) AS sample FROM fdm4.catalog_product_detail WHERE detail_value <> '' LIMIT 3;
\echo ===== the exact transform driver filter =====
SELECT count(*) AS transform_driver_rows
FROM fdm4.catalog_product_detail
WHERE detail_type = 'storeData' AND site_id ~ '^S_' AND pg_input_is_valid(detail_value, 'jsonb');
