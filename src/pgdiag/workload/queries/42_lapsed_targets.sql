-- id: lapsed_targets
-- component: sort_reports
-- description: High-value campaign targets who did not order from regions 2-3 on the last day (NOT IN subquery).
-- sensitive_to: work_mem
SELECT t.campaign, count(*) AS lapsed, avg(t.score) AS avg_score
FROM wl_campaign_targets t
WHERE t.campaign = 1
  AND t.score < 0.5
  AND t.customer_id NOT IN (SELECT o.customer_id
                            FROM wl_orders o
                            WHERE o.order_date = date '2023-01-01' + %(order_days)s - 1
                              AND o.region IN (2, 3))
GROUP BY t.campaign
