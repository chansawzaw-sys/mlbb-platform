CREATE TABLE IF NOT EXISTS `push_config` (
	`id` integer PRIMARY KEY NOT NULL,
	`public_key` text DEFAULT '' NOT NULL,
	`private_jwk` text DEFAULT '' NOT NULL,
	`created_at` integer DEFAULT (unixepoch() * 1000) NOT NULL
);
--> statement-breakpoint
CREATE TABLE IF NOT EXISTS `push_subscriptions` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`player_id` integer NOT NULL,
	`endpoint` text DEFAULT '' NOT NULL,
	`p256dh` text DEFAULT '' NOT NULL,
	`auth` text DEFAULT '' NOT NULL,
	`user_agent` text DEFAULT '' NOT NULL,
	`created_at` integer DEFAULT (unixepoch() * 1000) NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS `push_subscriptions_endpoint_unique` ON `push_subscriptions` (`endpoint`);
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS `push_subscriptions_player_id_idx` ON `push_subscriptions` (`player_id`);
