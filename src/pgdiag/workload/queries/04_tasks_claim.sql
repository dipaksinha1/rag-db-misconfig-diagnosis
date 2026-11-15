-- id: tasks_claim
-- component: tasks_queue
-- description: Work queue: workers claim every queued task.
-- sensitive_to: autovacuum
UPDATE wl_tasks
SET status = 'running', worker = 'w' || mod(task_id, 8), started_at = now()
WHERE status = 'queued'
