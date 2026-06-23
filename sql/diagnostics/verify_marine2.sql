\echo == 706607 per-color item flags + stock (marine's 5 catalog colors) ==
SELECT sc.description AS color,
       i."color-code" AS code,
       count(*) AS items,
       string_agg(DISTINCT COALESCE(NULLIF(i.active,''),'·'), ',')              AS active,
       string_agg(DISTINCT COALESCE(NULLIF(i."web-active",''),'·'), ',')        AS web_active,
       string_agg(DISTINCT COALESCE(NULLIF(i."item-status",''),'·'), ',')       AS item_status,
       string_agg(DISTINCT COALESCE(NULLIF(i."warehouse-status",''),'·'), ',')  AS wh_status,
       string_agg(DISTINCT COALESCE(NULLIF(i.presell,''),'·'), ',')             AS presell,
       COALESCE(SUM(bal.stock), 0) AS stock
FROM fdm4.item i
LEFT JOIN fdm4."style-color" sc
       ON sc."style-code" = i."style-code" AND sc."color-code" = i."color-code"
LEFT JOIN (
    SELECT "item-number", SUM(NULLIF("inv-bal",'')::numeric) AS stock
    FROM fdm4."item-balance" GROUP BY "item-number"
) bal ON bal."item-number" = i."item-number"
WHERE i."style-code" = '706607'
  AND i."color-code" IN ('0002','0007','0022','0023','0026')
GROUP BY sc.description, i."color-code"
ORDER BY web_active DESC, color;
