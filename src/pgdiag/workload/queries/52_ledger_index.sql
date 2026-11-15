-- id: ledger_index
-- component: ledger_batch
-- description: Nightly batch: build the account lookup index on the loaded ledger.
-- sensitive_to: maintenance_work_mem
CREATE INDEX wl_ledger_account_idx ON wl_ledger (account_id, posted_at)
