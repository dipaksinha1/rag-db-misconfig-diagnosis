-- id: order_export
-- component: order_export
-- description: Per-market order export of the last 45 days; the customer's country is looked up row by row (ORM-style).
-- sensitive_to: shared_buffers
-- repeat: 5
SELECT count(*) AS orders, sum(amount) AS revenue, max(ordered_at) AS last_order
FROM wl_orders o
WHERE o.ordered_at >= timestamp '2023-01-01' + %(order_days)s * interval '1 day'
                      - interval '45 days'
  AND (SELECT c.country FROM wl_customers c WHERE c.customer_id = o.customer_id)
      = (ARRAY['US', 'DE', 'GB', 'FR', 'IN'])[%(i)s]
