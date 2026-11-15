-- id: queue_running_by_country
-- component: tasks_report
-- description: Queue dashboard: running tasks per customer country.
-- sensitive_to: autovacuum
-- repeat: 2
SELECT c.country, count(*) AS running
FROM wl_tasks t
JOIN wl_customers c ON c.customer_id = t.customer_id
WHERE t.status = 'running'
GROUP BY c.country
ORDER BY running DESC
