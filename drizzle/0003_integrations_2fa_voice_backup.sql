ALTER TABLE players ADD COLUMN google_sub TEXT NOT NULL DEFAULT '';
--> statement-breakpoint
ALTER TABLE players ADD COLUMN email TEXT NOT NULL DEFAULT '';
--> statement-breakpoint
ALTER TABLE players ADD COLUMN totp_secret TEXT NOT NULL DEFAULT '';
--> statement-breakpoint
ALTER TABLE players ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0;
--> statement-breakpoint
ALTER TABLE players ADD COLUMN suspicious INTEGER NOT NULL DEFAULT 0;
--> statement-breakpoint
ALTER TABLE players ADD COLUMN suspicious_reason TEXT NOT NULL DEFAULT '';
--> statement-breakpoint
CREATE TABLE voice_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL,
  from_player_id INTEGER NOT NULL,
  to_player_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE terms_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'terms',
  version TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT 'admin',
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE rate_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
--> statement-breakpoint
CREATE TABLE backup_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL DEFAULT 'admin',
  created_at INTEGER NOT NULL
);
