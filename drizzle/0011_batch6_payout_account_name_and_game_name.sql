ALTER TABLE `withdraw_requests` ADD COLUMN `payout_account_name` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `players` ADD COLUMN `game_name` text DEFAULT '' NOT NULL;
