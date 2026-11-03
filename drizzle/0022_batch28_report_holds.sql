ALTER TABLE `players` ADD COLUMN `held_coins` integer DEFAULT 0 NOT NULL;
--> statement-breakpoint
ALTER TABLE `reports` ADD COLUMN `target_id` integer;
--> statement-breakpoint
CREATE TABLE `report_holds` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`match_id` integer NOT NULL,
	`player_id` integer NOT NULL,
	`received_amount` integer DEFAULT 0 NOT NULL,
	`held_amount` integer DEFAULT 0 NOT NULL,
	`status` text DEFAULT 'active' NOT NULL,
	`resolution` text DEFAULT '' NOT NULL,
	`resolved_to_id` integer,
	`first_upheld_case_id` integer,
	`first_upheld_reporter_id` integer,
	`created_at` integer NOT NULL,
	`resolved_at` integer
);
--> statement-breakpoint
CREATE UNIQUE INDEX `report_holds_match_player_unique` ON `report_holds` (`match_id`, `player_id`);
