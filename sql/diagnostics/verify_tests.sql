\echo =====================================================================
\echo TEST 1 - style 400340 pricing by color on PUBLIC WEB
\echo expect: Cardinal Red = 50, other colors = 90
\echo =====================================================================
\echo -- catalogs that carry 400340 (confirm the public-web id) --
SELECT DISTINCT fdm4_store, catalog_id
FROM woo.store_product_state
WHERE style_code = '400340'
ORDER BY catalog_id;
\echo -- per-color effective price on public web --
SELECT catalog_id, color, min(price) AS price, count(*) AS variations
FROM woo.store_product_state
WHERE style_code = '400340' AND kind = 'variation' AND catalog_id ~* 'public'
GROUP BY catalog_id, color
ORDER BY catalog_id, price, color;

\echo =====================================================================
\echo TEST 2 - style 706607 color availability on MARINE SOLUTIONS
\echo expect: only 2 colors
\echo =====================================================================
\echo -- catalogs that carry 706607 --
SELECT DISTINCT fdm4_store, catalog_id
FROM woo.store_product_state
WHERE style_code = '706607'
ORDER BY catalog_id;
\echo -- colors on marine solutions --
SELECT catalog_id,
       count(DISTINCT color) AS distinct_colors,
       string_agg(DISTINCT color, ', ' ORDER BY color) AS colors
FROM woo.store_product_state
WHERE style_code = '706607' AND kind = 'variation' AND catalog_id ~* 'marine'
GROUP BY catalog_id;
