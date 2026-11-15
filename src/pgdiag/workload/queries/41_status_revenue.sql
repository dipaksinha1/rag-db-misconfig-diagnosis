-- id: status_revenue
-- component: revenue_reports
-- description: Revenue per order status and region, including returns and cancellations (full scan).
-- sensitive_to: max_parallel_workers_per_gather
SELECT status, region, count(*) AS orders, sum(amount) AS gross,
       stddev(amount * (1 - discount)) AS net_stddev, max(amount) AS largest
FROM wl_orders
GROUP BY 1, 2
ORDER BY 1, 2
