ALTER TABLE `matches` ADD COLUMN `a_hero` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `matches` ADD COLUMN `b_hero` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `matches` ADD COLUMN `banned_heroes` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `matches` ADD COLUMN `referral_percent_x100` integer DEFAULT 100 NOT NULL;
--> statement-breakpoint
ALTER TABLE `matches` ADD COLUMN `ready_warned` integer DEFAULT 0 NOT NULL;
--> statement-breakpoint
ALTER TABLE `challenges` ADD COLUMN `banned_heroes` text DEFAULT '' NOT NULL;
--> statement-breakpoint
CREATE TABLE `sessions` (
  `id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  `token` text NOT NULL,
  `player_id` integer NOT NULL,
  `created_at` integer NOT NULL,
  `expires_at` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `purchase_requests` (
  `id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  `player_id` integer NOT NULL,
  `coins` integer NOT NULL,
  `label` text DEFAULT '' NOT NULL,
  `status` text DEFAULT 'pending' NOT NULL,
  `decided_by` text DEFAULT '' NOT NULL,
  `txn_code` text DEFAULT '' NOT NULL,
  `created_at` integer NOT NULL,
  `decided_at` integer
);
--> statement-breakpoint
CREATE TABLE `notification_reads` (
  `id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  `notification_id` integer NOT NULL,
  `player_id` integer NOT NULL,
  `created_at` integer NOT NULL
);
