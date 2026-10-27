import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

const ts = (name: string) =>
  integer(name, { mode: "timestamp_ms" })
    .notNull()
    .$defaultFn(() => new Date());

export const players = sqliteTable("players", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  name: text("name").notNull(),
  mlbbId: text("mlbb_id").notNull().default(""),
  serverId: text("server_id").notNull().default(""),
  rank: text("rank").notNull().default("Epic"),
  avatarColor: text("avatar_color").notNull().default("#7c3aed"),
  avatarBlobKey: text("avatar_blob_key").notNull().default(""),
  role: text("role", {
    enum: ["player", "owner", "admin", "moderator", "support"],
  })
    .notNull()
    .default("player"),
  status: text("status", {
    enum: ["active", "warned", "temp_banned", "perm_banned"],
  })
    .notNull()
    .default("active"),
  balance: integer("balance").notNull().default(0),
  lockedCoins: integer("locked_coins").notNull().default(0),
  wins: integer("wins").notNull().default(0),
  losses: integer("losses").notNull().default(0),
  winStreak: integer("win_streak").notNull().default(0),
  bestStreak: integer("best_streak").notNull().default(0),
  reputation: integer("reputation").notNull().default(100),
  badges: text("badges").notNull().default(""),
  referralCode: text("referral_code").notNull().default(""),
  referredById: integer("referred_by_id"),
  googleSub: text("google_sub").notNull().default(""),
  email: text("email").notNull().default(""),
  totpSecret: text("totp_secret").notNull().default(""),
  totpEnabled: integer("totp_enabled").notNull().default(0),
  suspicious: integer("suspicious").notNull().default(0),
  suspiciousReason: text("suspicious_reason").notNull().default(""),
  lastSeenAt: ts("last_seen_at"),
  createdAt: ts("created_at"),
});

export const settings = sqliteTable("settings", {
  key: text("key").primaryKey(),
  value: text("value").notNull().default(""),
});

export const challenges = sqliteTable("challenges", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  creatorId: integer("creator_id").notNull(),
  arena: text("arena", { enum: ["1v1", "classic", "brawl"] }).notNull(),
  teamSize: integer("team_size").notNull().default(1),
  heroRule: text("hero_rule").notNull().default("open"),
  rankRestriction: text("rank_restriction").notNull().default(""),
  customRules: text("custom_rules").notNull().default(""),
  bannedHeroes: text("banned_heroes").notNull().default(""),
  stake: integer("stake").notNull(),
  status: text("status", { enum: ["open", "matched", "cancelled"] })
    .notNull()
    .default("open"),
  createdAt: ts("created_at"),
});

export const matches = sqliteTable("matches", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  code: text("code").notNull(),
  challengeId: integer("challenge_id").notNull(),
  playerAId: integer("player_a_id").notNull(),
  playerBId: integer("player_b_id").notNull(),
  arena: text("arena").notNull(),
  teamSize: integer("team_size").notNull().default(1),
  heroRule: text("hero_rule").notNull().default("open"),
  customRules: text("custom_rules").notNull().default(""),
  stake: integer("stake").notNull(),
  feeAmount: integer("fee_amount").notNull().default(0),
  feeLabel: text("fee_label").notNull().default(""),
  status: text("status", {
    enum: [
      "ready_check",
      "live",
      "result_pending",
      "completed",
      "disputed",
      "cancelled",
    ],
  })
    .notNull()
    .default("ready_check"),
  aReady: integer("a_ready").notNull().default(0),
  bReady: integer("b_ready").notNull().default(0),
  readyDeadline: integer("ready_deadline", { mode: "timestamp_ms" }).notNull(),
  startedAt: integer("started_at", { mode: "timestamp_ms" }),
  aResult: text("a_result").notNull().default(""),
  bResult: text("b_result").notNull().default(""),
  aHero: text("a_hero").notNull().default(""),
  bHero: text("b_hero").notNull().default(""),
  bannedHeroes: text("banned_heroes").notNull().default(""),
  referralPercentX100: integer("referral_percent_x100").notNull().default(100),
  readyWarned: integer("ready_warned").notNull().default(0),
  aEvidence: text("a_evidence").notNull().default(""),
  bEvidence: text("b_evidence").notNull().default(""),
  winnerId: integer("winner_id"),
  createdAt: ts("created_at"),
  completedAt: integer("completed_at", { mode: "timestamp_ms" }),
});

export const chatMessages = sqliteTable("chat_messages", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  matchId: integer("match_id").notNull(),
  senderId: integer("sender_id").notNull(),
  senderName: text("sender_name").notNull().default(""),
  text: text("text").notNull(),
  createdAt: ts("created_at"),
});

export const transactions = sqliteTable("transactions", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  code: text("code").notNull(),
  playerId: integer("player_id"),
  type: text("type").notNull(),
  amount: integer("amount").notNull(),
  balanceAfter: integer("balance_after"),
  note: text("note").notNull().default(""),
  matchId: integer("match_id"),
  createdAt: ts("created_at"),
});

export const notifications = sqliteTable("notifications", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  playerId: integer("player_id"),
  type: text("type").notNull().default("info"),
  title: text("title").notNull(),
  body: text("body").notNull().default(""),
  matchId: integer("match_id"),
  isRead: integer("is_read").notNull().default(0),
  createdAt: ts("created_at"),
});

export const teams = sqliteTable("teams", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  name: text("name").notNull(),
  color: text("color").notNull().default("#0ea5e9"),
  logoBlobKey: text("logo_blob_key").notNull().default(""),
  leaderId: integer("leader_id").notNull(),
  wins: integer("wins").notNull().default(0),
  createdAt: ts("created_at"),
});

export const teamMembers = sqliteTable("team_members", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  teamId: integer("team_id").notNull(),
  playerId: integer("player_id").notNull(),
  ready: integer("ready").notNull().default(0),
  createdAt: ts("created_at"),
});

export const tournaments = sqliteTable("tournaments", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  name: text("name").notNull(),
  prize: integer("prize").notNull().default(0),
  rules: text("rules").notNull().default(""),
  teamSize: integer("team_size").notNull().default(5),
  entryType: text("entry_type", { enum: ["solo", "team"] })
    .notNull()
    .default("solo"),
  startsAt: integer("starts_at", { mode: "timestamp_ms" }).notNull(),
  status: text("status", { enum: ["registration", "live", "completed"] })
    .notNull()
    .default("registration"),
  bannerIdx: integer("banner_idx").notNull().default(0),
  champion: text("champion").notNull().default(""),
  createdAt: ts("created_at"),
});

export const tournamentEntries = sqliteTable("tournament_entries", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  tournamentId: integer("tournament_id").notNull(),
  entrantName: text("entrant_name").notNull(),
  playerId: integer("player_id"),
  createdAt: ts("created_at"),
});

export const bracketMatches = sqliteTable("bracket_matches", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  tournamentId: integer("tournament_id").notNull(),
  round: integer("round").notNull().default(1),
  labelA: text("label_a").notNull().default("TBD"),
  labelB: text("label_b").notNull().default("TBD"),
  winner: text("winner").notNull().default(""),
  createdAt: ts("created_at"),
});

export const referralCommissions = sqliteTable("referral_commissions", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  referrerId: integer("referrer_id").notNull(),
  referredId: integer("referred_id").notNull(),
  matchId: integer("match_id").notNull(),
  feeAmount: integer("fee_amount").notNull(),
  percent: integer("percent_x100").notNull().default(100),
  commission: integer("commission").notNull(),
  status: text("status", { enum: ["pending", "completed", "cancelled"] })
    .notNull()
    .default("pending"),
  createdAt: ts("created_at"),
});

export const reports = sqliteTable("reports", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  caseCode: text("case_code").notNull(),
  reporterId: integer("reporter_id"),
  reporterName: text("reporter_name").notNull().default(""),
  targetName: text("target_name").notNull(),
  reason: text("reason").notNull(),
  detail: text("detail").notNull().default(""),
  status: text("status", { enum: ["open", "reviewing", "resolved"] })
    .notNull()
    .default("open"),
  actionTaken: text("action_taken").notNull().default(""),
  createdAt: ts("created_at"),
});

export const tickets = sqliteTable("tickets", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  playerId: integer("player_id"),
  name: text("name").notNull().default(""),
  topic: text("topic").notNull(),
  message: text("message").notNull(),
  status: text("status", { enum: ["open", "answered", "closed"] })
    .notNull()
    .default("open"),
  reply: text("reply").notNull().default(""),
  createdAt: ts("created_at"),
});

export const savedRules = sqliteTable("saved_rules", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  playerId: integer("player_id").notNull(),
  name: text("name").notNull(),
  arena: text("arena").notNull().default("classic"),
  teamSize: integer("team_size").notNull().default(1),
  heroRule: text("hero_rule").notNull().default("open"),
  customRules: text("custom_rules").notNull().default(""),
  createdAt: ts("created_at"),
});

export const friends = sqliteTable("friends", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  playerId: integer("player_id").notNull(),
  friendId: integer("friend_id").notNull(),
  createdAt: ts("created_at"),
});

export const activityLog = sqliteTable("activity_log", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  actor: text("actor").notNull().default("admin"),
  action: text("action").notNull(),
  createdAt: ts("created_at"),
});

export const voiceSignals = sqliteTable("voice_signals", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  matchId: integer("match_id").notNull(),
  fromPlayerId: integer("from_player_id").notNull(),
  toPlayerId: integer("to_player_id").notNull(),
  kind: text("kind").notNull(), // offer | answer | ice | bye
  payload: text("payload").notNull().default(""),
  createdAt: ts("created_at"),
});

export const termsVersions = sqliteTable("terms_versions", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  kind: text("kind").notNull().default("terms"), // terms | rules
  version: text("version").notNull(),
  content: text("content").notNull().default(""),
  actor: text("actor").notNull().default("admin"),
  createdAt: ts("created_at"),
});

export const rateEvents = sqliteTable("rate_events", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  key: text("key").notNull(),
  createdAt: ts("created_at"),
});

export const sessions = sqliteTable("sessions", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  token: text("token").notNull(),
  playerId: integer("player_id").notNull(),
  createdAt: ts("created_at"),
  expiresAt: integer("expires_at", { mode: "timestamp_ms" }).notNull(),
});

export const purchaseRequests = sqliteTable("purchase_requests", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  playerId: integer("player_id").notNull(),
  coins: integer("coins").notNull(),
  label: text("label").notNull().default(""),
  status: text("status", { enum: ["pending", "approved", "rejected", "expired"] })
    .notNull()
    .default("pending"),
  decidedBy: text("decided_by").notNull().default(""),
  txnCode: text("txn_code").notNull().default(""),
  proofBlobKey: text("proof_blob_key").notNull().default(""),
  proofHash: text("proof_hash").notNull().default(""),
  rejectReason: text("reject_reason").notNull().default(""),
  createdAt: ts("created_at"),
  decidedAt: integer("decided_at", { mode: "timestamp_ms" }),
});

export const withdrawRequests = sqliteTable("withdraw_requests", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  playerId: integer("player_id").notNull(),
  coins: integer("coins").notNull(),
  rateKyat: integer("rate_kyat").notNull().default(0),
  grossKyat: integer("gross_kyat").notNull().default(0),
  feePercent: integer("fee_percent").notNull().default(0),
  feeKyat: integer("fee_kyat").notNull().default(0),
  netKyat: integer("net_kyat").notNull().default(0),
  method: text("method", { enum: ["kpay", "wave"] }).notNull().default("kpay"),
  payoutPhone: text("payout_phone").notNull().default(""),
  status: text("status", { enum: ["pending", "paid", "rejected", "expired"] })
    .notNull()
    .default("pending"),
  reason: text("reason").notNull().default(""),
  txnCode: text("txn_code").notNull().default(""),
  decidedBy: text("decided_by").notNull().default(""),
  createdAt: ts("created_at"),
  decidedAt: integer("decided_at", { mode: "timestamp_ms" }),
});

export const diamondPackages = sqliteTable("diamond_packages", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  name: text("name").notNull().default(""),
  diamonds: integer("diamonds").notNull().default(0),
  bonusText: text("bonus_text").notNull().default(""),
  priceCoins: integer("price_coins").notNull().default(0),
  enabled: integer("enabled").notNull().default(1),
  sort: integer("sort").notNull().default(0),
  createdAt: ts("created_at"),
});

export const diamondOrders = sqliteTable("diamond_orders", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  playerId: integer("player_id").notNull(),
  packageId: integer("package_id").notNull().default(0),
  packageName: text("package_name").notNull().default(""),
  diamonds: integer("diamonds").notNull().default(0),
  priceCoins: integer("price_coins").notNull().default(0),
  status: text("status", {
    enum: ["pending", "fulfilled", "rejected", "expired"],
  })
    .notNull()
    .default("pending"),
  txnCode: text("txn_code").notNull().default(""),
  rejectReason: text("reject_reason").notNull().default(""),
  decidedBy: text("decided_by").notNull().default(""),
  createdAt: ts("created_at"),
  decidedAt: integer("decided_at", { mode: "timestamp_ms" }),
});

export const notificationReads = sqliteTable("notification_reads", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  notificationId: integer("notification_id").notNull(),
  playerId: integer("player_id").notNull(),
  createdAt: ts("created_at"),
});

export const backupSnapshots = sqliteTable("backup_snapshots", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  code: text("code").notNull(),
  payload: text("payload").notNull().default(""),
  sizeBytes: integer("size_bytes").notNull().default(0),
  actor: text("actor").notNull().default("admin"),
  createdAt: ts("created_at"),
});
