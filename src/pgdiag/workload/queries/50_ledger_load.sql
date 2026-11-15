-- id: ledger_load
-- component: ledger_batch
-- description: Nightly batch: bulk-load the day's ledger entries.
-- sensitive_to: shared_buffers
INSERT INTO wl_ledger (entry_id, account_id, posted_at, amount, balance, status, memo, tags)
SELECT g,
       1 + mod(g * 7919, 50000),
       timestamp '2024-12-01' + g * interval '1 second',
       round((mod(g * 104729, 100000) / 100.0 - 500)::numeric, 2),
       0,
       'posted',
       'batch entry ' || g,
       -- tags: cost centre, project, counterparty, hour, product line, region,
       -- channel and campaign codes (each code range is disjoint)
       ARRAY[(1 + mod(g, 50))::int, (100 + mod(g * 7, 400))::int,
             (1000 + mod(g * 13, 20000))::int, (100000 + mod(g, 86400) / 3600)::int,
             (200000 + mod(g * 17, 3000))::int, (300000 + mod(g * 3, 64))::int,
             (400000 + mod(g * 11, 12))::int, (500000 + mod(g * 31, 9000))::int]
-- generate_series in the target list streams rows (in FROM it would be materialized first)
FROM (SELECT generate_series(1::bigint, %(ledger)s) AS g) AS s
