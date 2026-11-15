-- id: category_mix
-- component: revenue_rollup
-- description: Net revenue and units per product category over the last quarter.
-- sensitive_to: max_parallel_workers_per_gather, work_mem
SELECT p.category, count(*) AS orders, sum(o.quantity) AS units,
       sum(o.amount * (1 - o.discount)) AS net
FROM wl_orders o
JOIN wl_products p ON p.product_id = o.product_id
WHERE o.order_date >= date '2023-01-01' + %(order_days)s - 91
GROUP BY p.category
ORDER BY net DESC
