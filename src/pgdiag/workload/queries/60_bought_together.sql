-- id: bought_together
-- component: product_affinity
-- description: What else the last 90 days' buyers of a best-selling product bought in the last 120 days (skewed self-join).
-- sensitive_to: default_statistics_target
-- repeat: 5
SELECT o2.product_id, count(*) AS together
FROM wl_orders o1
JOIN wl_orders o2 ON o2.customer_id = o1.customer_id
                 AND o2.ordered_at >= timestamp '2023-01-01' + %(order_days)s * interval '1 day'
                                      - interval '120 days'
WHERE o1.product_id = mod(%(i)s * 3917, %(products)s) + 1
  AND o1.ordered_at >= timestamp '2023-01-01' + %(order_days)s * interval '1 day'
                       - interval '90 days'
  AND o2.product_id <> o1.product_id
GROUP BY o2.product_id
ORDER BY together DESC
LIMIT 20
