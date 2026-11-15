-- id: ledger_vacuum
-- component: ledger_batch
-- description: Nightly batch: vacuum and analyze the ledger after the bulk update.
-- sensitive_to: maintenance_work_mem
VACUUM (ANALYZE) wl_ledger
