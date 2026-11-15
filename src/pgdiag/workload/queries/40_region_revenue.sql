-- id: region_revenue
-- component: revenue_reports
-- description: All-time revenue statistics per region and warehouse (full scan and aggregate).
-- sensitive_to: max_parallel_workers_per_gather
SELECT region, warehouse_id, count(*) AS orders, sum(amount) AS gross,
       sum(amount * (1 - discount)) AS net, avg(quantity) AS avg_quantity,
       stddev(amount) AS amount_stddev, avg(amount / quantity) AS avg_unit_price
FROM wl_orders
GROUP BY 1, 2
ORDER BY 1, 2
