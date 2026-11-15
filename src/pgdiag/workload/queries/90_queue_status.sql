-- id: queue_status
-- component: tasks_report
-- description: Queue dashboard (polled): number of tasks per status.
-- sensitive_to: autovacuum
-- repeat: 40
SELECT status, count(*) AS tasks
FROM wl_tasks
GROUP BY status
