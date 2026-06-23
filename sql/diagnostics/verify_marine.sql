\echo == storeData: product[0] keys (what attributes exist at product level) ==
SELECT jsonb_object_keys(detail_value::jsonb #> '{product,0}') AS product_keys
FROM fdm4.catalog_product_detail
WHERE product_id='706607' AND catalog_id='S_001167_marinesolutions' AND detail_type='storeData';

\echo == storeData: FULL per-color objects (every flag FDM4 stores per color) ==
SELECT jsonb_pretty(col) AS color_entry
FROM fdm4.catalog_product_detail d
CROSS JOIN LATERAL jsonb_array_elements(d.detail_value::jsonb #> '{product,0,color}') col
WHERE d.product_id='706607' AND d.catalog_id='S_001167_marinesolutions' AND d.detail_type='storeData';

\echo == woo layer: per-color stock + price + active ==
SELECT color, count(*) AS vars, sum(stock) AS total_stock, min(price) AS price,
       string_agg(DISTINCT payload->>'active', ',') AS active_flags
FROM woo.store_product_state
WHERE style_code='706607' AND catalog_id='S_001167_marinesolutions' AND kind='variation'
GROUP BY color ORDER BY total_stock DESC, color;

\echo == FDM4 instockcolors / colorcount for this product+store ==
SELECT detail_type, detail_value
FROM fdm4.catalog_product_detail
WHERE product_id='706607' AND catalog_id='S_001167_marinesolutions'
  AND detail_type IN ('instockcolors','colorcount','sizecount');

\echo == item-level availability columns that exist (for a deeper follow-up) ==
SELECT string_agg(column_name, ', ' ORDER BY column_name) AS candidate_flag_columns
FROM information_schema.columns
WHERE table_schema='fdm4' AND table_name='item'
  AND column_name ~* '(activ|status|web|disc|hold|enabl|avail|show|visib|season|delet|inactiv|sell|flag|live|publish)';
