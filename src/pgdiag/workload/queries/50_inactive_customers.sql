-- id: inactive_customers
-- component: customer_reports
-- description: Small-business customers in three markets who did not order yesterday (NOT IN subquery).
-- sensitive_to: work_mem
SELECT c.country, count(*) AS inactive
FROM wl_customers c
WHERE c.segment = 'smb'
  AND c.country IN ('DE', 'GB', 'FR')
  AND c.customer_id NOT IN (SELECT o.customer_id
                            FROM wl_orders o
                            WHERE o.ordered_at >= timestamp '2023-01-01'
                                                 + %(order_days)s * interval '1 day'
                                                 - interval '1 day')
GROUP BY 1
ORDER BY 1
