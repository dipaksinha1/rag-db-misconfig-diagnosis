-- id: top_customers_recent
-- component: sort_reports
-- description: Top 100 customers by spend over the last week, ranked.
-- sensitive_to: work_mem
SELECT customer_id, total, orders, rank() OVER (ORDER BY total DESC) AS rnk
FROM (SELECT customer_id, sum(amount) AS total, count(*) AS orders
      FROM wl_orders
      WHERE order_date >= date '2023-01-01' + %(order_days)s - 7
      GROUP BY customer_id) AS s
ORDER BY total DESC
LIMIT 100
