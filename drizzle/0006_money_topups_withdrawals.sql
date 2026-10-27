ALTER TABLE `purchase_requests` ADD COLUMN `proof_blob_key` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `purchase_requests` ADD COLUMN `proof_hash` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `purchase_requests` ADD COLUMN `reject_reason` text DEFAULT '' NOT NULL;
--> statement-breakpoint
CREATE TABLE `withdraw_requests` (
  `id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
  `player_id` integer NOT NULL,
  `coins` integer NOT NULL,
  `rate_kyat` integer DEFAULT 0 NOT NULL,
  `gross_kyat` integer DEFAULT 0 NOT NULL,
  `fee_percent` integer DEFAULT 0 NOT NULL,
  `fee_kyat` integer DEFAULT 0 NOT NULL,
  `net_kyat` integer DEFAULT 0 NOT NULL,
  `method` text DEFAULT 'kpay' NOT NULL,
  `payout_phone` text DEFAULT '' NOT NULL,
  `status` text DEFAULT 'pending' NOT NULL,
  `reason` text DEFAULT '' NOT NULL,
  `txn_code` text DEFAULT '' NOT NULL,
  `decided_by` text DEFAULT '' NOT NULL,
  `created_at` integer NOT NULL,
  `decided_at` integer
);
