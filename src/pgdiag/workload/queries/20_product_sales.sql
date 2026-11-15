-- id: product_sales
-- component: order_lookups
-- description: Daily sales chart of one product over the last 90 days (selective index lookup).
-- sensitive_to: random_page_cost
-- repeat: 30
SELECT date_trunc('day', ordered_at) AS day, count(*) AS orders, sum(amount) AS revenue
FROM wl_orders
WHERE product_id = mod((%(i)s + 20) * 3917, %(products)s) + 1
  AND ordered_at >= timestamp '2023-01-01' + %(order_days)s * interval '1 day' - interval '90 days'
GROUP BY 1
ORDER BY 1
