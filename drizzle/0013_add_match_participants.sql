CREATE TABLE `match_participants` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`match_id` integer NOT NULL,
	`player_id` integer NOT NULL,
	`side` text NOT NULL,
	`stake` integer DEFAULT 0 NOT NULL,
	`ready` integer DEFAULT 0 NOT NULL,
	`state` text DEFAULT 'locked' NOT NULL,
	`payout_amount` integer DEFAULT 0 NOT NULL,
	`created_at` integer DEFAULT 0 NOT NULL
);
