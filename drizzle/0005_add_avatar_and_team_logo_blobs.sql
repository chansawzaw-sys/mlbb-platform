ALTER TABLE `players` ADD COLUMN `avatar_blob_key` text DEFAULT '' NOT NULL;
--> statement-breakpoint
ALTER TABLE `teams` ADD COLUMN `logo_blob_key` text DEFAULT '' NOT NULL;
