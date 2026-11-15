-- id: ledger_tags_index
-- component: ledger_batch
-- description: Nightly batch: build the GIN index for tag searches on the loaded ledger.
-- sensitive_to: maintenance_work_mem
CREATE INDEX wl_ledger_tags_idx ON wl_ledger USING gin (tags)
