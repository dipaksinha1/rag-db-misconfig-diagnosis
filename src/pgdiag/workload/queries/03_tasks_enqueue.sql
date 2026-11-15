-- id: tasks_enqueue
-- component: tasks_queue
-- description: Work queue: enqueue a new batch of tasks.
-- sensitive_to: autovacuum
INSERT INTO wl_tasks (customer_id, kind, priority, status, created_at, payload)
SELECT 1 + mod(g * 7919, %(customers)s),
       (ARRAY['email', 'export', 'invoice', 'sync'])[1 + mod(g, 4)],
       mod(g, 5),
       'queued',
       now(),
       repeat('p', 300 + mod(g, 100))
FROM (SELECT generate_series(1, %(task_batch)s) AS g) AS s
