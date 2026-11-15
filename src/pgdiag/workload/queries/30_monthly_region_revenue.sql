-- id: monthly_region_revenue
-- component: revenue_rollup
-- description: Monthly gross and net revenue per region for the last six months.
-- sensitive_to: max_parallel_workers_per_gather
SELECT date_trunc('month', order_date) AS month, region,
       count(*) AS orders, sum(amount) AS gross,
       sum(amount * (1 - discount)) AS net, avg(quantity) AS avg_quantity
FROM wl_orders
WHERE order_date >= date '2023-01-01' + %(order_days)s - 182
GROUP BY 1, 2
ORDER BY 1, 2
