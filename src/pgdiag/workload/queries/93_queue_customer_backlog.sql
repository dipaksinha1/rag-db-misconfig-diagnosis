-- id: queue_customer_backlog
-- component: tasks_report
-- description: Support page: one customer's tasks in the queue.
-- sensitive_to: autovacuum
-- repeat: 50
SELECT status, count(*) AS tasks, max(created_at) AS latest
FROM wl_tasks
WHERE customer_id = 1 + mod(%(i)s * 7919 * 13, %(customers)s)
GROUP BY status
