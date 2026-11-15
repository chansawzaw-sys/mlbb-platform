-- F11: secondary indexes backing the admin dashboard's SQL aggregates.
-- getAdmin used to load the full transactions / matches / purchase_requests
-- / withdraw_requests tables and aggregate in JS; it now runs COUNT/SUM
-- aggregates whose WHERE/GROUP BY columns are these five. Index-only,
-- forward-only, no data touched.
CREATE INDEX `transactions_created_at_idx` ON `transactions` (`created_at`);
--> statement-breakpoint
CREATE INDEX `transactions_type_idx` ON `transactions` (`type`);
--> statement-breakpoint
CREATE INDEX `matches_status_idx` ON `matches` (`status`);
--> statement-breakpoint
CREATE INDEX `purchase_requests_status_idx` ON `purchase_requests` (`status`);
--> statement-breakpoint
CREATE INDEX `withdraw_requests_status_idx` ON `withdraw_requests` (`status`);
