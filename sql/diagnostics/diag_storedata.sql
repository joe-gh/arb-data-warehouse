SELECT pg_input_is_valid(detail_value, 'jsonb') AS dv_ok,
       length(detail_value) AS dv_len,
       left(detail_value, 600) AS dv_head
FROM fdm4.catalog_product_detail
WHERE detail_type = 'storeData'
ORDER BY length(detail_value) DESC
LIMIT 2;
\echo ===== is the JSON maybe in value_display instead? =====
SELECT pg_input_is_valid(value_display, 'jsonb') AS vd_ok,
       length(value_display) AS vd_len,
       left(value_display, 300) AS vd_head
FROM fdm4.catalog_product_detail
WHERE detail_type = 'storeData'
ORDER BY length(value_display) DESC
LIMIT 2;
