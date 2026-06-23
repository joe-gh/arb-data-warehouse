\echo =====================================================================
\echo TEST 1 (raw FDM4 check) - 400340 @ public-web per-color price
\echo =====================================================================
SELECT sc.description AS color,
       col->>'colorCode'  AS color_code,
       col->>'customPrice' AS color_price,
       d.detail_value::jsonb #>> '{product,0,customPrice}' AS product_price
FROM fdm4.catalog_product_detail d
CROSS JOIN LATERAL jsonb_array_elements(d.detail_value::jsonb #> '{product,0,color}') col
LEFT JOIN fdm4."style-color" sc
       ON sc."style-code" = '400340' AND sc."color-code" = col->>'colorCode'
WHERE d.product_id = '400340' AND d.catalog_id = 'S_002384_public-web' AND d.detail_type = 'storeData'
ORDER BY color_price NULLS LAST, color;

\echo =====================================================================
\echo TEST 2 - 706607 @ marine solutions: per-color stock + active flag
\echo =====================================================================
SELECT color,
       count(*) AS variations,
       sum(stock) AS total_stock,
       min(price) AS price,
       string_agg(DISTINCT payload->>'active', ',') AS active_flags
FROM woo.store_product_state
WHERE style_code = '706607' AND catalog_id = 'S_001167_marinesolutions' AND kind = 'variation'
GROUP BY color
ORDER BY color;

\echo -- raw storeData color set + instockcolors for 706607 @ S_001167 --
SELECT detail_type, left(detail_value, 900) AS value
FROM fdm4.catalog_product_detail
WHERE product_id = '706607' AND catalog_id = 'S_001167_marinesolutions'
  AND detail_type IN ('storeData', 'instockcolors', 'colorcount');
