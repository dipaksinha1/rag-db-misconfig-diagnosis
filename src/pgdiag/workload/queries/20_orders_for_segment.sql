-- id: orders_for_segment
-- component: customer_orders
-- description: Revenue of enterprise customers from one country and signup period (index lookups into orders).
-- sensitive_to: random_page_cost, shared_buffers
-- repeat: 3
SELECT c.customer_id, count(*) AS orders, sum(o.amount) AS revenue, max(o.order_date) AS last_order
FROM wl_customers c
JOIN wl_orders o ON o.customer_id = c.customer_id
WHERE c.country = (ARRAY['DE', 'GB', 'FR'])[1 + mod(%(i)s - 1, 3)]
  AND c.segment = 'enterprise'
  AND c.signup_date >= date '2016-01-01' + 120 * %(i)s
  AND c.signup_date < date '2016-01-01' + 120 * %(i)s + 240
GROUP BY c.customer_id
ORDER BY revenue DESC
LIMIT 50
