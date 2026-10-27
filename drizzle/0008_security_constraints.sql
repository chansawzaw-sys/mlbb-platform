CREATE UNIQUE INDEX `purchase_requests_proof_hash_unique` ON `purchase_requests` (`proof_hash`) WHERE `proof_hash` <> '';
--> statement-breakpoint
CREATE UNIQUE INDEX `purchase_requests_txn_code_unique` ON `purchase_requests` (`txn_code`) WHERE `txn_code` <> '';
--> statement-breakpoint
CREATE UNIQUE INDEX `withdraw_requests_txn_code_unique` ON `withdraw_requests` (`txn_code`) WHERE `txn_code` <> '';
