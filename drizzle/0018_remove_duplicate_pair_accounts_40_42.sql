-- Owner decision 2026-10-29: surgical removal of exactly TWO app accounts
-- that duplicate a kept game account. MLBB ID 155038073 / Server 2783
-- (game name "Min Aung Hlaing") was held by three app accounts: id 40
-- "Nyi Naing", id 42 "Nay Toe", id 43 "Sigma Boy". The Owner ordered ids
-- 40 and 42 deleted completely and id 43 KEPT as the pair's sole account.
-- Scoped descendant of purge migration 0012's dependency handling: every
-- statement below is keyed to the literal id set (40, 42) or to matches /
-- challenges belonging to those two, so no other player (Owner id 3,
-- id 41, id 43) or admin content can be touched. Pre-flight (2026-10-29)
-- verified neither target had an open challenge, a non-terminal match, or
-- locked coins (public balance/locked read 0/0), so no opponent funds are
-- stranded. Data-only migration: idempotent DELETE/UPDATE statements.
DELETE FROM `notification_reads` WHERE `player_id` IN (40, 42) OR `notification_id` IN (SELECT `id` FROM `notifications` WHERE `player_id` IN (40, 42) OR `match_id` IN (SELECT `id` FROM `matches` WHERE `player_a_id` IN (40, 42) OR `player_b_id` IN (40, 42) OR `id` IN (SELECT `match_id` FROM `match_participants` WHERE `player_id` IN (40, 42))) OR `challenge_id` IN (SELECT `id` FROM `challenges` WHERE `creator_id` IN (40, 42) OR `invited_player_id` IN (40, 42)));
--> statement-breakpoint
DELETE FROM `notifications` WHERE `player_id` IN (40, 42) OR `match_id` IN (SELECT `id` FROM `matches` WHERE `player_a_id` IN (40, 42) OR `player_b_id` IN (40, 42) OR `id` IN (SELECT `match_id` FROM `match_participants` WHERE `player_id` IN (40, 42))) OR `challenge_id` IN (SELECT `id` FROM `challenges` WHERE `creator_id` IN (40, 42) OR `invited_player_id` IN (40, 42));
--> statement-breakpoint
DELETE FROM `voice_signals` WHERE `from_player_id` IN (40, 42) OR `to_player_id` IN (40, 42) OR `match_id` IN (SELECT `id` FROM `matches` WHERE `player_a_id` IN (40, 42) OR `player_b_id` IN (40, 42) OR `id` IN (SELECT `match_id` FROM `match_participants` WHERE `player_id` IN (40, 42)));
--> statement-breakpoint
DELETE FROM `chat_messages` WHERE `sender_id` IN (40, 42) OR `match_id` IN (SELECT `id` FROM `matches` WHERE `player_a_id` IN (40, 42) OR `player_b_id` IN (40, 42) OR `id` IN (SELECT `match_id` FROM `match_participants` WHERE `player_id` IN (40, 42)));
--> statement-breakpoint
DELETE FROM `referral_commissions` WHERE `referrer_id` IN (40, 42) OR `referred_id` IN (40, 42) OR `match_id` IN (SELECT `id` FROM `matches` WHERE `player_a_id` IN (40, 42) OR `player_b_id` IN (40, 42) OR `id` IN (SELECT `match_id` FROM `match_participants` WHERE `player_id` IN (40, 42)));
--> statement-breakpoint
DELETE FROM `transactions` WHERE `player_id` IN (40, 42) OR `match_id` IN (SELECT `id` FROM `matches` WHERE `player_a_id` IN (40, 42) OR `player_b_id` IN (40, 42) OR `id` IN (SELECT `match_id` FROM `match_participants` WHERE `player_id` IN (40, 42)));
--> statement-breakpoint
DELETE FROM `matches` WHERE `player_a_id` IN (40, 42) OR `player_b_id` IN (40, 42) OR `id` IN (SELECT `match_id` FROM `match_participants` WHERE `player_id` IN (40, 42));
--> statement-breakpoint
DELETE FROM `match_participants` WHERE `player_id` IN (40, 42) OR `match_id` NOT IN (SELECT `id` FROM `matches`);
--> statement-breakpoint
DELETE FROM `challenges` WHERE `creator_id` IN (40, 42) OR `invited_player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `dm_messages` WHERE `sender_id` IN (40, 42) OR `recipient_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `dm_reads` WHERE `player_id` IN (40, 42) OR `partner_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `friends` WHERE `player_id` IN (40, 42) OR `friend_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `team_members` WHERE `team_id` IN (SELECT `id` FROM `teams` WHERE `leader_id` IN (40, 42));
--> statement-breakpoint
DELETE FROM `teams` WHERE `leader_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `team_members` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `tournament_entries` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `saved_rules` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `tickets` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `reports` WHERE `reporter_id` IN (40, 42) OR `target_name` IN (SELECT `name` FROM `players` WHERE `id` IN (40, 42));
--> statement-breakpoint
DELETE FROM `purchase_requests` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `withdraw_requests` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `diamond_orders` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `sessions` WHERE `player_id` IN (40, 42);
--> statement-breakpoint
UPDATE `players` SET `referred_by_id` = NULL WHERE `referred_by_id` IN (40, 42);
--> statement-breakpoint
DELETE FROM `players` WHERE `id` IN (40, 42);
