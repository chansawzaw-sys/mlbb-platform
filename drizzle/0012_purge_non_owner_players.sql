-- Batch 6 Phase B (user order 2026-10-28): one-time purge of ALL player
-- accounts except the Owner (role='owner' AND the fixed admin Gmail).
-- Everyone re-registers from scratch. Keeps: the Owner identity row, all
-- settings, admin content (slideshow/packages/rules/fees/Google setup),
-- tournaments + brackets (admin content), the admin activity log, and
-- reference-free rows (NULL-player transactions/tickets, broadcast
-- notifications, name-only tournament entries). Deletes every other player
-- and all dependent/participation data in FK-safe order, including the
-- Owner's own player-participation rows; zeroes Owner coin balances.
-- Fail-safe: if no row matches the Owner predicate, the scalar subquery is
-- NULL and every guarded delete below matches nothing.
DELETE FROM `reports` WHERE `reporter_id` IN (SELECT `id` FROM `players` WHERE `id` <> (SELECT MIN(`id`) FROM `players` WHERE `role` = 'owner' AND `email` = 'nnominalhuman@gmail.com')) OR `target_name` IN (SELECT `name` FROM `players` WHERE `id` <> (SELECT MIN(`id`) FROM `players` WHERE `role` = 'owner' AND `email` = 'nnominalhuman@gmail.com'));
--> statement-breakpoint
DELETE FROM `voice_signals`;
--> statement-breakpoint
DELETE FROM `chat_messages`;
--> statement-breakpoint
DELETE FROM `matches`;
--> statement-breakpoint
DELETE FROM `challenges`;
--> statement-breakpoint
DELETE FROM `referral_commissions`;
--> statement-breakpoint
DELETE FROM `transactions` WHERE `player_id` IS NOT NULL;
--> statement-breakpoint
DELETE FROM `notification_reads`;
--> statement-breakpoint
DELETE FROM `notifications` WHERE `player_id` IS NOT NULL OR `match_id` IS NOT NULL;
--> statement-breakpoint
DELETE FROM `purchase_requests`;
--> statement-breakpoint
DELETE FROM `withdraw_requests`;
--> statement-breakpoint
DELETE FROM `diamond_orders`;
--> statement-breakpoint
DELETE FROM `tickets` WHERE `player_id` IS NOT NULL;
--> statement-breakpoint
DELETE FROM `friends`;
--> statement-breakpoint
DELETE FROM `team_members`;
--> statement-breakpoint
DELETE FROM `teams`;
--> statement-breakpoint
DELETE FROM `tournament_entries` WHERE `player_id` IS NOT NULL;
--> statement-breakpoint
DELETE FROM `saved_rules`;
--> statement-breakpoint
DELETE FROM `rate_events`;
--> statement-breakpoint
DELETE FROM `backup_snapshots`;
--> statement-breakpoint
DELETE FROM `sessions` WHERE `player_id` <> (SELECT MIN(`id`) FROM `players` WHERE `role` = 'owner' AND `email` = 'nnominalhuman@gmail.com');
--> statement-breakpoint
UPDATE `players` SET `balance` = 0, `locked_coins` = 0, `referred_by_id` = NULL WHERE `id` = (SELECT MIN(`id`) FROM `players` WHERE `role` = 'owner' AND `email` = 'nnominalhuman@gmail.com');
--> statement-breakpoint
DELETE FROM `players` WHERE `id` <> (SELECT MIN(`id`) FROM `players` WHERE `role` = 'owner' AND `email` = 'nnominalhuman@gmail.com');
