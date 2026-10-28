CREATE TABLE `dm_messages` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`sender_id` integer NOT NULL,
	`recipient_id` integer NOT NULL,
	`text` text NOT NULL,
	`created_at` integer DEFAULT (unixepoch() * 1000) NOT NULL
);
--> statement-breakpoint
CREATE TABLE `dm_reads` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`player_id` integer NOT NULL,
	`partner_id` integer NOT NULL,
	`last_read_message_id` integer NOT NULL DEFAULT 0,
	`created_at` integer DEFAULT (unixepoch() * 1000) NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `dm_reads_player_partner_unique` ON `dm_reads` (`player_id`,`partner_id`);
