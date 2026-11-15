-- id: top_customers
-- component: customer_reports
-- description: Customers ranked by revenue over the last 30 days (large aggregate and sort).
-- sensitive_to: work_mem
SELECT customer_id, revenue, orders, rank() OVER (ORDER BY revenue DESC) AS rank
FROM (SELECT customer_id, sum(amount) AS revenue, count(*) AS orders
      FROM wl_orders
      WHERE ordered_at >= timestamp '2023-01-01' + %(order_days)s * interval '1 day'
                          - interval '30 days'
      GROUP BY customer_id) AS per_customer
ORDER BY revenue DESC
LIMIT 100
