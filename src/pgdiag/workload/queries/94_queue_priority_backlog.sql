-- id: queue_priority_backlog
-- component: tasks_report
-- description: Queue dashboard (polled): running tasks per priority.
-- sensitive_to: autovacuum
-- repeat: 40
SELECT priority, count(*) AS running
FROM wl_tasks
WHERE status = 'running'
GROUP BY priority
ORDER BY priority
