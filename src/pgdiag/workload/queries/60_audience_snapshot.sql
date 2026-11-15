-- id: audience_snapshot
-- component: campaign_join
-- description: Snapshot one campaign's audience with customer details into a table (INSERT ... SELECT, never parallel).
-- sensitive_to: effective_cache_size, random_page_cost, shared_buffers
-- repeat: 50
INSERT INTO wl_audience (campaign, customer_id, country, segment, credit_limit, score)
SELECT t.campaign, c.customer_id, c.country, c.segment, c.credit_limit, t.score
FROM wl_campaign_targets t
JOIN wl_customers c ON c.customer_id = t.customer_id
WHERE t.campaign = 1 + mod(%(i)s - 1, %(campaigns)s)
