-- F01: transactions.code is the idempotency key for wallet decisions.
-- The decide* cores write the request's txn_code as the ledger code, and
-- the retry/resume rule looks the ledger up by that code, so the code
-- must be unique. Codes are random uid("TXN")/uid("DIA") values minted
-- server-side, so existing rows are unique in practice; if this CREATE
-- ever fails on a duplicate, resolve the duplicate rows manually —
-- never delete financial history.
CREATE UNIQUE INDEX `transactions_code_unique` ON `transactions` (`code`);
