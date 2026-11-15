-- id: tasks_complete
-- component: tasks_queue
-- description: Work queue: mark the batch claimed in the previous pass as done.
-- sensitive_to: autovacuum
UPDATE wl_tasks
SET status = 'done', finished_at = now(), result = 'ok by ' || worker
WHERE status = 'running'
