SELECT count(*) AS woo_state_rows FROM woo.store_product_state;
SELECT kind, count(*) FROM woo.store_product_state GROUP BY kind ORDER BY kind;
SELECT count(DISTINCT fdm4_store) AS stores, count(DISTINCT catalog_id) AS catalogs FROM woo.store_product_state;
\echo ===== reconstruct style 400240 across stores (price levels) =====
SELECT fdm4_store, catalog_id,
       count(*) FILTER (WHERE kind='variation') AS variations,
       min(price) AS min_price, max(price) AS max_price
FROM woo.store_product_state
WHERE style_code = '400240'
GROUP BY fdm4_store, catalog_id
ORDER BY fdm4_store
LIMIT 15;
\echo ===== sample 400240 variation rows =====
SELECT fdm4_store, sku, color, size, price, stock
FROM woo.store_product_state
WHERE style_code = '400240' AND kind = 'variation'
ORDER BY fdm4_store, sku
LIMIT 10;
