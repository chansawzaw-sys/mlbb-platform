DROP TABLE IF EXISTS entries;
--> statement-breakpoint
CREATE TABLE players (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  mlbb_id TEXT NOT NULL DEFAULT '',
  server_id TEXT NOT NULL DEFAULT '',
  rank TEXT NOT NULL DEFAULT 'Epic',
  avatar_color TEXT NOT NULL DEFAULT '#7c3aed',
  role TEXT NOT NULL DEFAULT 'player',
  status TEXT NOT NULL DEFAULT 'active',
  balance INTEGER NOT NULL DEFAULT 0,
  locked_coins INTEGER NOT NULL DEFAULT 0,
  wins INTEGER NOT NULL DEFAULT 0,
  losses INTEGER NOT NULL DEFAULT 0,
  win_streak INTEGER NOT NULL DEFAULT 0,
  best_streak INTEGER NOT NULL DEFAULT 0,
  reputation INTEGER NOT NULL DEFAULT 100,
  badges TEXT NOT NULL DEFAULT '',
  referral_code TEXT NOT NULL DEFAULT '',
  referred_by_id INTEGER,
  last_seen_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
--> statement-breakpoint
CREATE TABLE challenges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  creator_id INTEGER NOT NULL,
  arena TEXT NOT NULL,
  team_size INTEGER NOT NULL DEFAULT 1,
  hero_rule TEXT NOT NULL DEFAULT 'open',
  rank_restriction TEXT NOT NULL DEFAULT '',
  custom_rules TEXT NOT NULL DEFAULT '',
  stake INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  challenge_id INTEGER NOT NULL,
  player_a_id INTEGER NOT NULL,
  player_b_id INTEGER NOT NULL,
  arena TEXT NOT NULL,
  team_size INTEGER NOT NULL DEFAULT 1,
  hero_rule TEXT NOT NULL DEFAULT 'open',
  custom_rules TEXT NOT NULL DEFAULT '',
  stake INTEGER NOT NULL,
  fee_amount INTEGER NOT NULL DEFAULT 0,
  fee_label TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ready_check',
  a_ready INTEGER NOT NULL DEFAULT 0,
  b_ready INTEGER NOT NULL DEFAULT 0,
  ready_deadline INTEGER NOT NULL,
  started_at INTEGER,
  a_result TEXT NOT NULL DEFAULT '',
  b_result TEXT NOT NULL DEFAULT '',
  a_evidence TEXT NOT NULL DEFAULT '',
  b_evidence TEXT NOT NULL DEFAULT '',
  winner_id INTEGER,
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);
--> statement-breakpoint
CREATE TABLE chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL,
  sender_id INTEGER NOT NULL,
  sender_name TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  player_id INTEGER,
  type TEXT NOT NULL,
  amount INTEGER NOT NULL,
  balance_after INTEGER,
  note TEXT NOT NULL DEFAULT '',
  match_id INTEGER,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id INTEGER,
  type TEXT NOT NULL DEFAULT 'info',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  match_id INTEGER,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE teams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#0ea5e9',
  leader_id INTEGER NOT NULL,
  wins INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE team_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id INTEGER NOT NULL,
  player_id INTEGER NOT NULL,
  ready INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE tournaments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  prize INTEGER NOT NULL DEFAULT 0,
  rules TEXT NOT NULL DEFAULT '',
  team_size INTEGER NOT NULL DEFAULT 5,
  entry_type TEXT NOT NULL DEFAULT 'solo',
  starts_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'registration',
  banner_idx INTEGER NOT NULL DEFAULT 0,
  champion TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE tournament_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tournament_id INTEGER NOT NULL,
  entrant_name TEXT NOT NULL,
  player_id INTEGER,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE bracket_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tournament_id INTEGER NOT NULL,
  round INTEGER NOT NULL DEFAULT 1,
  label_a TEXT NOT NULL DEFAULT 'TBD',
  label_b TEXT NOT NULL DEFAULT 'TBD',
  winner TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE referral_commissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  referrer_id INTEGER NOT NULL,
  referred_id INTEGER NOT NULL,
  match_id INTEGER NOT NULL,
  fee_amount INTEGER NOT NULL,
  percent_x100 INTEGER NOT NULL DEFAULT 100,
  commission INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_code TEXT NOT NULL,
  reporter_id INTEGER,
  reporter_name TEXT NOT NULL DEFAULT '',
  target_name TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  action_taken TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id INTEGER,
  name TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  reply TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE saved_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  arena TEXT NOT NULL DEFAULT 'classic',
  team_size INTEGER NOT NULL DEFAULT 1,
  hero_rule TEXT NOT NULL DEFAULT 'open',
  custom_rules TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE friends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id INTEGER NOT NULL,
  friend_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE activity_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL DEFAULT 'admin',
  action TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
