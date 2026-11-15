-- id: queue_latency
-- component: tasks_report
-- description: Queue dashboard: processing time of finished tasks per kind and priority.
-- sensitive_to: autovacuum
-- repeat: 10
SELECT kind, priority, count(*) AS done, avg(finished_at - started_at) AS avg_runtime
FROM wl_tasks
WHERE status = 'done'
GROUP BY kind, priority
ORDER BY kind, priority
