-- id: customer_profile
-- component: small_lookups
-- description: Customer profile page: one customer and their number of orders.
-- sensitive_to: jit_above_cost, shared_buffers
-- repeat: 150
SELECT c.customer_id, c.country, c.city, c.segment, c.credit_limit,
       (SELECT count(*) FROM wl_orders o WHERE o.customer_id = c.customer_id) AS orders
FROM wl_customers c
WHERE c.customer_id = 1 + mod(%(i)s * 104729, %(customers)s)
