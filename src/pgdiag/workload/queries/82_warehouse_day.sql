-- id: warehouse_day
-- component: small_lookups
-- description: Warehouse dashboard: one warehouse's orders by status on one recent day.
-- sensitive_to: jit_above_cost, shared_buffers
-- repeat: 100
SELECT status, count(*) AS orders, sum(amount) AS amount, avg(quantity) AS avg_quantity
FROM wl_orders
WHERE order_date = date '2023-01-01' + %(order_days)s - 1 - mod(%(i)s, 14)
  AND warehouse_id = 10 + 10 * mod(%(i)s, 3) + mod(%(i)s, 6)
GROUP BY status
