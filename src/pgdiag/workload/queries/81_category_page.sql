-- id: category_page
-- component: small_lookups
-- description: Category page: the 20 most expensive products of one category.
-- sensitive_to: jit_above_cost, shared_buffers
-- repeat: 100
SELECT p.product_id, p.name, p.list_price
FROM wl_products p
WHERE p.category = 'cat-' || lpad((1 + mod(%(i)s * 7, 200))::text, 3, '0')
ORDER BY p.list_price DESC
LIMIT 20
