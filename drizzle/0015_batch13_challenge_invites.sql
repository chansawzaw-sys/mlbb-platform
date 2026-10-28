ALTER TABLE `notifications` ADD COLUMN `challenge_id` integer;
--> statement-breakpoint
UPDATE `settings` SET `value` = '' WHERE (`key` = 'banner_title' AND `value` = 'FIGHT FOR GLORY') OR (`key` = 'banner_subtitle' AND `value` = 'PLAY • COMPETE • WIN');
