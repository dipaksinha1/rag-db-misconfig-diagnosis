-- id: bought_together
-- component: product_mix
-- description: Products bought by recent buyers of a best-selling product (self-join on skewed data).
-- sensitive_to: default_statistics_target
-- repeat: 2
SELECT o2.product_id, count(*) AS together
FROM wl_orders o1
JOIN wl_orders o2 ON o2.customer_id = o1.customer_id
                 AND o2.order_date >= date '2023-01-01' + %(order_days)s - 30
WHERE o1.product_id = mod(%(i)s * 7919, %(products)s) + 1
  AND o1.order_date >= date '2023-01-01' + %(order_days)s - 3
  AND o2.product_id <> o1.product_id
GROUP BY o2.product_id
ORDER BY together DESC
LIMIT 20
