ALTER TABLE `players` ADD COLUMN `game_name_locked` integer DEFAULT 0 NOT NULL;
--> statement-breakpoint
CREATE TABLE `google_pending` (
	`token_hash` text PRIMARY KEY NOT NULL,
	`google_sub` text DEFAULT '' NOT NULL,
	`email` text DEFAULT '' NOT NULL,
	`name` text DEFAULT '' NOT NULL,
	`created_at` integer NOT NULL,
	`expires_at` integer NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `players_mlbb_pair_unique` ON `players` (`mlbb_id`,`server_id`) WHERE `mlbb_id` <> '';
