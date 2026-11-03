ALTER TABLE `matches` ADD COLUMN `first_ready_at` integer;
--> statement-breakpoint
ALTER TABLE `matches` ADD COLUMN `finish_reason` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `reports` ADD COLUMN `match_id` integer;
