CREATE TABLE IF NOT EXISTS `staff_grants` (`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL, `email` text DEFAULT '' NOT NULL, `role` text DEFAULT 'support' NOT NULL, `granted_by` text DEFAULT '' NOT NULL, `created_at` integer DEFAULT 0 NOT NULL, `updated_at` integer DEFAULT 0 NOT NULL);
--> statement-breakpoint
CREATE UNIQUE INDEX IF NOT EXISTS `staff_grants_email_unique` ON `staff_grants` (`email`);
