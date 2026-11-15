-- id: tasks_purge
-- component: tasks_queue
-- description: Work queue: delete the oldest batch of finished tasks (retention).
-- sensitive_to: autovacuum
DELETE FROM wl_tasks
WHERE task_id < (SELECT min(task_id) FROM wl_tasks) + %(task_batch)s
