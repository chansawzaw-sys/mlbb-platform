CREATE TABLE `diamond_packages` (
  `id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  `name` text DEFAULT '' NOT NULL,
  `diamonds` integer DEFAULT 0 NOT NULL,
  `bonus_text` text DEFAULT '' NOT NULL,
  `price_coins` integer DEFAULT 0 NOT NULL,
  `enabled` integer DEFAULT 1 NOT NULL,
  `sort` integer DEFAULT 0 NOT NULL,
  `created_at` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `diamond_orders` (
  `id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  `player_id` integer NOT NULL,
  `package_id` integer DEFAULT 0 NOT NULL,
  `package_name` text DEFAULT '' NOT NULL,
  `diamonds` integer DEFAULT 0 NOT NULL,
  `price_coins` integer DEFAULT 0 NOT NULL,
  `status` text DEFAULT 'pending' NOT NULL,
  `txn_code` text DEFAULT '' NOT NULL,
  `reject_reason` text DEFAULT '' NOT NULL,
  `decided_by` text DEFAULT '' NOT NULL,
  `created_at` integer NOT NULL,
  `decided_at` integer
);
--> statement-breakpoint
CREATE UNIQUE INDEX `diamond_orders_txn_code_unique` ON `diamond_orders` (`txn_code`);
