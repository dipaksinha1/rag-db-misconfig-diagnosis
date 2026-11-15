-- id: ledger_settle
-- component: ledger_batch
-- description: Nightly batch: settle every ledger entry (bulk update of the whole table).
-- sensitive_to: shared_buffers
UPDATE wl_ledger
SET balance = amount * 1.05, status = 'settled'
