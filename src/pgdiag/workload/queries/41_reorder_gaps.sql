-- id: reorder_gaps
-- component: sort_reports
-- description: Days between consecutive orders of the same customer over the last two weeks.
-- sensitive_to: work_mem
SELECT avg(gap) AS avg_gap, max(gap) AS max_gap, count(gap) AS reorders
FROM (SELECT order_date - lag(order_date) OVER (PARTITION BY customer_id ORDER BY order_date, order_id) AS gap
      FROM wl_orders
      WHERE order_date >= date '2023-01-01' + %(order_days)s - 14) AS s
