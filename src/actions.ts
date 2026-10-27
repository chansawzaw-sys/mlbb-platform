import { defineAction, z, type ActionsModule, type Ctx } from "@hatch/space-sdk";
import { and, desc, eq } from "drizzle-orm";
import * as schema from "./schema";

function getDb(ctx: Ctx) {
  return ctx.db<typeof schema>();
}
type Db = ReturnType<typeof getDb>;

const iso = (d: Date | null | undefined) => (d ? d.toISOString() : null);
const uid = (p: string) =>
  `${p}-${Date.now().toString(36).toUpperCase()}-${Math.floor(Math.random() * 1e6)
    .toString(36)
    .toUpperCase()}`;

/** HARD-FIXED admin/owner identity. This email is a server-side constant:
 *  it is NOT stored in settings and can never be changed from the admin UI.
 *  Only a Google login with exactly this email can be Owner. */
export const FIXED_ADMIN_EMAIL = "nnominalhuman@gmail.com";
export const isFixedAdminEmail = (email: string) =>
  (email ?? "").trim().toLowerCase() === FIXED_ADMIN_EMAIL;
/** The role a player effectively holds. A stored "owner" role only counts
 *  when the account email is the fixed admin Gmail; the fixed Gmail always
 *  counts as owner. Nobody else can be owner, whatever the DB row says. */
function effectiveRole(p: PlayerRow): string {
  if (isFixedAdminEmail(p.email)) return "owner";
  if (p.role === "owner") return "player";
  return p.role;
}

/** Hero catalog used for REAL ban / role enforcement (name -> role). */
export const HERO_CATALOG: Record<string, string> = {
  Miya: "marksman", Layla: "marksman", Clint: "marksman", Bruno: "marksman",
  Moskov: "marksman", Karrie: "marksman", Granger: "marksman", Wanwan: "marksman",
  Beatrix: "marksman", Melissa: "marksman", Brody: "marksman", Popol: "marksman",
  Alucard: "fighter", Zilong: "fighter", Balmond: "fighter", Freya: "fighter",
  Chou: "fighter", Guinevere: "fighter", Paquito: "fighter", Yin: "fighter",
  Martis: "fighter", Dyrroth: "fighter", Terizla: "fighter", Xborg: "fighter",
  Eudora: "mage", Gord: "mage", Kagura: "mage", Cyclops: "mage",
  Aurora: "mage", Odette: "mage", Zhask: "mage", Valir: "mage",
  Lunox: "mage", Harith: "mage", Cecilion: "mage", Yve: "mage",
  Saber: "assassin", Hayabusa: "assassin", Lancelot: "assassin", Gusion: "assassin",
  Selena: "assassin", Ling: "assassin", Benedetta: "assassin", Aamon: "assassin",
  Natalia: "assassin", Joy: "assassin", Nolan: "assassin", Hanzo: "assassin",
  Tigreal: "tank", Akai: "tank", Franco: "tank", Minotaur: "tank",
  Gatotkaca: "tank", Johnson: "tank", Khufra: "tank", Atlas: "tank",
  Belerick: "tank", Edith: "tank", Fredrinn: "tank", Barats: "tank",
  Rafaela: "support", Estes: "support", Angela: "support", Diggie: "support",
  Mathilda: "support", Floryn: "support", Carmilla: "support", Faramis: "support",
};
export const HERO_NAMES = Object.keys(HERO_CATALOG);
export const MAX_IMAGE_BYTES = 3 * 1024 * 1024;
export const ALLOWED_IMAGE_MIMES: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/jpg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
};
function normalizeImageMime(mime: string): string {
  const m = (mime ?? "").trim().toLowerCase();
  if (m === "image/jpg") return "image/jpeg";
  return m;
}
function validateImage(base64: string, mimeRaw: string): { error: string; bytes: Buffer | null; mime: string; ext: string } {
  const mime = normalizeImageMime(mimeRaw);
  const ext = ALLOWED_IMAGE_MIMES[mimeRaw.trim().toLowerCase()] ?? ALLOWED_IMAGE_MIMES[mime];
  if (!ext) return { error: "ပုံ အမျိုးအစား မမှန်ပါ — JPG, PNG, WebP ပုံပဲ တင်လို့ရပါတယ်", bytes: null, mime, ext: "" };
  if (!base64 || base64.length < 20) return { error: "ပုံ file မတွေ့ပါ — ပုံ တစ်ခုရွေးပြီး ပြန်ကြိုးစားပါ", bytes: null, mime, ext };
  let bytes: Buffer;
  try { bytes = Buffer.from(base64, "base64"); } catch { return { error: "ပုံ file ကို ဖတ်လို့ မရပါ — တခြားပုံနဲ့ ပြန်ကြိုးစားပါ", bytes: null, mime, ext }; }
  if (bytes.length === 0) return { error: "ပုံ file အလွတ် ဖြစ်နေပါတယ်", bytes: null, mime, ext };
  if (bytes.length > MAX_IMAGE_BYTES) return { error: "ပုံ size ကြီးလွန်းပါတယ် — အများဆုံး 3MB အောက် ပုံပဲ တင်လို့ရပါတယ်", bytes: null, mime, ext };
  // magic bytes check so a renamed non-image is rejected even if mime is spoofed
  const isJpeg = bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  const isPng = bytes.length >= 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47;
  const isWebp = bytes.length >= 12 && bytes.toString("ascii",0,4)==="RIFF" && bytes.toString("ascii",8,12)==="WEBP";
  if (mime==="image/jpeg" && !isJpeg) return { error: "ပုံ file အစစ် မဟုတ်ပါ — JPG ပုံ အစစ်ပဲ တင်ပါ", bytes: null, mime, ext };
  if (mime==="image/png" && !isPng) return { error: "ပုံ file အစစ် မဟုတ်ပါ — PNG ပုံ အစစ်ပဲ တင်ပါ", bytes: null, mime, ext };
  if (mime==="image/webp" && !isWebp) return { error: "ပုံ file အစစ် မဟုတ်ပါ — WebP ပုံ အစစ်ပဲ တင်ပါ", bytes: null, mime, ext };
  return { error: "", bytes, mime, ext };
}
async function blobUrlSafe(ctx: Ctx, key: string): Promise<string> {
  if (!key) return "";
  try { return await ctx.blobs.getUrl(key); } catch { return ""; }
}

function bannedHeroList(raw: string): string[] {
  return (raw ?? "").split(",").map((x) => x.trim()).filter(Boolean);
}
function isHeroBanned(hero: string, bannedRaw: string): boolean {
  const h = hero.trim().toLowerCase();
  return bannedHeroList(bannedRaw).some((b) => b.toLowerCase() === h);
}

const DEFAULT_SETTINGS: Record<string, string> = {
  announcement_my: "",
  announcement_en: "",
  banner_title: "FIGHT FOR GLORY",
  banner_subtitle: "PLAY • COMPETE • WIN",
  fee_1v1_type: "percent",
  fee_1v1_value: "5",
  fee_classic_type: "percent",
  fee_classic_value: "5",
  fee_brawl_type: "percent",
  fee_brawl_value: "5",
  referral_percent_x100: "100",
  packages: JSON.stringify([
    { coins: 100, price: 1000 },
    { coins: 500, price: 4500 },
    { coins: 1000, price: 8500 },
    { coins: 5000, price: 40000 },
  ]),
  banned_heroes: "",
  custom_hero_rules: "",
  maintenance: "0",
  emergency: "0",
  ready_timeout_min: "3",
  result_delay_min: "5",
  match_timeout_min: "180",
  accent: "#22d3ee",
  rules_version: "1",
  telegram_bot: "",
  telegram_bot_token: "",
  telegram_chat_id: "",
  google_client_id: "",
  google_enabled: "0",
  google_pending_client_id: "",
  google_setup_state: "",
  google_setup_by_id: "",
  google_setup_by_name: "",
  google_setup_at: "",
  kpay_phone: "",
  kpay_qr: "",
  kpay_qr_key: "",
  wave_phone: "",
  wave_qr: "",
  wave_qr_key: "",
  coin_rate_kyat: "8",
  withdraw_fee_percent: "0",
  withdraw_min_coins: "1000",
  withdraw_max_coins_per_day: "100000",
  topup_min_coins: "10",
  topup_max_coins: "1000000",
  terms_version: "1",
  terms_text: "",
  rules_text: "",
};

async function getSettingsMap(db: Db): Promise<Record<string, string>> {
  const rows = await db.select().from(schema.settings);
  const map: Record<string, string> = { ...DEFAULT_SETTINGS };
  for (const r of rows) map[r.key] = r.value;
  return map;
}

function numSetting(s: Record<string, string>, key: string, def: number): number {
  const v = Number(s[key]);
  return s[key] === undefined || s[key] === "" || Number.isNaN(v) ? def : v;
}

async function setSetting(db: Db, key: string, value: string) {
  await db
    .insert(schema.settings)
    .values({ key, value })
    .onConflictDoUpdate({ target: schema.settings.key, set: { value } });
}

/** A pending Google setup expires after 24h so a stale/abandoned first
 *  attempt cannot lock the Owner out forever. */
function pendingSetupExpired(s: Record<string, string>): boolean {
  if ((s["google_setup_state"] ?? "") !== "pending") return true;
  const at = Date.parse(s["google_setup_at"] ?? "");
  if (Number.isNaN(at)) return true;
  return Date.now() - at > 24 * 3600 * 1000;
}

/** Effective Google login state: an ACTIVE (owner-confirmed) client ID, or a
 *  PENDING one that may only be used for the fixed Owner's first sign-in. */
function googleState(s: Record<string, string>) {
  const activeId = (s["google_client_id"] ?? "").trim();
  const activeEnabled = s["google_enabled"] === "1" && activeId !== "";
  const rawState = s["google_setup_state"] ?? "";
  const state = activeEnabled ? "confirmed" : rawState;
  const pendingId =
    state === "pending" ? (s["google_pending_client_id"] ?? "").trim() : "";
  return {
    activeId,
    activeEnabled,
    state,
    pendingId,
    effectiveId: activeEnabled ? activeId : pendingId,
    effectiveEnabled: activeEnabled || pendingId !== "",
  };
}

function feeFor(
  s: Record<string, string>,
  arena: string,
  pot: number,
): { fee: number; label: string } {
  const type = s[`fee_${arena}_type`] ?? "percent";
  const value = Number(s[`fee_${arena}_value`] ?? "5") || 0;
  if (type === "fixed") {
    const fee = Math.min(value, pot);
    return { fee, label: `Fixed ${value} coins` };
  }
  const fee = Math.min(Math.round((pot * value) / 100), pot);
  return { fee, label: `${value}% of pot` };
}

type PlayerRow = typeof schema.players.$inferSelect;

function playerOut(p: PlayerRow, avatarUrl = "") {
  const total = p.wins + p.losses;
  return {
    id: p.id,
    name: p.name,
    mlbb_id: p.mlbbId,
    server_id: p.serverId,
    rank: p.rank,
    avatar_color: p.avatarColor,
    avatar_url: avatarUrl,
    avatar_blob_key: (p as PlayerRow & { avatarBlobKey?: string }).avatarBlobKey ?? "",
    role: p.role,
    status: p.status,
    balance: p.balance,
    locked_coins: p.lockedCoins,
    wins: p.wins,
    losses: p.losses,
    total_matches: total,
    win_rate: total > 0 ? Math.round((p.wins / total) * 100) : 0,
    win_streak: p.winStreak,
    best_streak: p.bestStreak,
    reputation: p.reputation,
    badges: p.badges,
    email: p.email,
    referral_code: p.referralCode,
    referred_by_id: p.referredById ?? null,
    has_google: p.googleSub !== "",
    totp_enabled: p.totpEnabled === 1,
    suspicious: p.suspicious === 1,
    suspicious_reason: p.suspiciousReason,
    created_at: iso(p.createdAt) ?? "",
  };
}

/** Sanitized view: balances, locked coins and email are private. Only the
 *  viewer themself sees them; the fixed owner's email is public by design. */
function playerPublicOut(p: PlayerRow, viewerId: number | null, avatarUrl = "") {
  const full = playerOut(p, avatarUrl);
  if (viewerId !== null && viewerId === p.id) return full;
  return {
    ...full,
    balance: 0,
    locked_coins: 0,
    email: isFixedAdminEmail(p.email) ? p.email : "",
    role: effectiveRole(p),
  };
}

async function playerOutAsync(ctx: Ctx, p: PlayerRow) {
  return playerOut(p, await blobUrlSafe(ctx, (p as PlayerRow & { avatarBlobKey?: string }).avatarBlobKey ?? ""));
}
async function playerPublicOutAsync(ctx: Ctx, p: PlayerRow, viewerId: number | null) {
  return playerPublicOut(p, viewerId, await blobUrlSafe(ctx, (p as PlayerRow & { avatarBlobKey?: string }).avatarBlobKey ?? ""));
}
async function playersPublicOutAsync(ctx: Ctx, rows: PlayerRow[], viewerId: number | null) {
  const out = [];
  for (const pl of rows) out.push(await playerPublicOutAsync(ctx, pl, viewerId));
  return out;
}
const playerShape = z.object({
  id: z.number(),
  name: z.string(),
  mlbb_id: z.string(),
  server_id: z.string(),
  rank: z.string(),
  avatar_color: z.string(),
  avatar_url: z.string(),
  avatar_blob_key: z.string(),
  role: z.string(),
  status: z.string(),
  balance: z.number(),
  locked_coins: z.number(),
  wins: z.number(),
  losses: z.number(),
  total_matches: z.number(),
  win_rate: z.number(),
  win_streak: z.number(),
  best_streak: z.number(),
  reputation: z.number(),
  badges: z.string(),
  email: z.string(),
  referral_code: z.string(),
  referred_by_id: z.number().nullable(),
  has_google: z.boolean(),
  totp_enabled: z.boolean(),
  suspicious: z.boolean(),
  suspicious_reason: z.string(),
  created_at: z.string(),
});

async function getPlayer(db: Db, id: number) {
  const rows = await db
    .select()
    .from(schema.players)
    .where(eq(schema.players.id, id))
    .limit(1);
  return rows[0] ?? null;
}

async function addTxn(
  db: Db,
  args: {
    playerId: number | null;
    type: string;
    amount: number;
    balanceAfter?: number | null;
    note?: string;
    matchId?: number | null;
  },
) {
  await db.insert(schema.transactions).values({
    code: uid("TXN"),
    playerId: args.playerId,
    type: args.type,
    amount: args.amount,
    balanceAfter: args.balanceAfter ?? null,
    note: args.note ?? "",
    matchId: args.matchId ?? null,
  });
}

async function notify(
  db: Db,
  args: {
    playerId: number | null;
    type: string;
    title: string;
    body?: string;
    matchId?: number | null;
  },
) {
  await db.insert(schema.notifications).values({
    playerId: args.playerId,
    type: args.type,
    title: args.title,
    body: args.body ?? "",
    matchId: args.matchId ?? null,
  });
}

async function logAct(db: Db, actor: string, action: string) {
  await db.insert(schema.activityLog).values({ actor, action });
}

/* ---------- Integrations helpers (Google / Telegram / TOTP / rate limit) ---------- */

async function telegramSend(
  s: Record<string, string>,
  text: string,
  replyMarkup?: unknown,
): Promise<{ sent: boolean; error: string }> {
  const token = (s["telegram_bot_token"] ?? "").trim();
  const chatId = (s["telegram_chat_id"] ?? "").trim();
  if (!token || !chatId)
    return { sent: false, error: "Telegram bot token / chat ID မထည့်ရသေးပါ (မချိတ်ရသေး)" };
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: text.slice(0, 3900),
        ...(replyMarkup ? { reply_markup: replyMarkup } : {}),
      }),
    });
    const data = (await res.json().catch(() => null)) as
      | { ok?: boolean; description?: string }
      | null;
    if (!res.ok || !data?.ok)
      return { sent: false, error: `Telegram API: ${data?.description ?? `HTTP ${res.status}`}` };
    return { sent: true, error: "" };
  } catch (e) {
    return { sent: false, error: `Telegram ချိတ်ဆက်မှု မအောင်မြင်ပါ: ${e instanceof Error ? e.message : "network error"}` };
  }
}

/** Generic Bot API call (JSON body). Fail-soft: never throws. */
async function telegramApi(
  token: string,
  method: string,
  payload: Record<string, unknown>,
): Promise<{ ok: boolean; error: string; result: unknown }> {
  if (!token) return { ok: false, error: "no token", result: null };
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = (await res.json().catch(() => null)) as
      | { ok?: boolean; description?: string; result?: unknown }
      | null;
    if (!res.ok || !data?.ok)
      return { ok: false, error: `Telegram API: ${data?.description ?? `HTTP ${res.status}`}`, result: null };
    return { ok: true, error: "", result: data.result ?? null };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "network error", result: null };
  }
}

/** Send a photo (raw bytes) with an optional inline keyboard. Fail-soft. */
async function telegramSendPhoto(
  s: Record<string, string>,
  bytes: Uint8Array,
  caption: string,
  replyMarkup?: unknown,
): Promise<{ sent: boolean; error: string }> {
  const token = (s["telegram_bot_token"] ?? "").trim();
  const chatId = (s["telegram_chat_id"] ?? "").trim();
  if (!token || !chatId) return { sent: false, error: "Telegram မချိတ်ရသေးပါ" };
  try {
    const form = new FormData();
    form.append("chat_id", chatId);
    form.append("caption", caption.slice(0, 1000));
    const photoAb = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    form.append("photo", new Blob([photoAb], { type: "image/jpeg" }), "proof.jpg");
    if (replyMarkup) form.append("reply_markup", JSON.stringify(replyMarkup));
    const res = await fetch(`https://api.telegram.org/bot${token}/sendPhoto`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      body: form,
    });
    const data = (await res.json().catch(() => null)) as
      | { ok?: boolean; description?: string }
      | null;
    if (!res.ok || !data?.ok)
      return { sent: false, error: `Telegram API: ${data?.description ?? `HTTP ${res.status}`}` };
    return { sent: true, error: "" };
  } catch (e) {
    return { sent: false, error: e instanceof Error ? e.message : "network error" };
  }
}

/** Inline decision keyboard for money requests in Telegram. callback_data
 *  format: "<kind>:<id>:<decision>" — kind "pur" (top-up) or "wd" (withdraw). */
function moneyKeyboard(kind: "pur" | "wd", id: number) {
  const positive =
    kind === "pur"
      ? { text: "✅ Approve", callback_data: `pur:${id}:approve` }
      : { text: "💸 Paid", callback_data: `wd:${id}:paid` };
  const negative = { text: "❌ Reject", callback_data: `${kind}:${id}:reject` };
  return { inline_keyboard: [[positive, negative]] };
}

/** Read raw blob bytes when the host provides them (the standalone host
 *  exposes ctx.blobBytes via its local-disk store). The portable BlobClient
 *  contract has no byte-read, so in other runtimes this returns null and
 *  callers fall back to text-only behaviour. Fail-soft by design. */
async function blobBytes(ctx: Ctx, key: string): Promise<Uint8Array | null> {
  if (!key) return null;
  const extra = (
    ctx as unknown as {
      blobBytes?: (k: string) => Promise<Uint8Array | null>;
    }
  ).blobBytes;
  if (typeof extra !== "function") return null;
  try {
    return await extra(key);
  } catch {
    return null;
  }
}

/** SHA-256 hex of image bytes (WebCrypto — portable across runtimes). */
async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const ab = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  const digest = await crypto.subtle.digest("SHA-256", ab);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Public display mask for payout names: first word, first 3 chars + "***"
 *  (single initial + "***" for very short names). Never a full name. */
function maskName(name: string): string {
  const first = (name ?? "").trim().split(/\s+/)[0] ?? "";
  if (first.length <= 2) return `${first.slice(0, 1)}***`;
  return `${first.slice(0, 3)}***`;
}

function telegramConfigured(s: Record<string, string>): boolean {
  return (
    (s["telegram_bot_token"] ?? "").trim() !== "" &&
    (s["telegram_chat_id"] ?? "").trim() !== ""
  );
}

const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
function base32Decode(str: string): Uint8Array {
  const clean = str.toUpperCase().replace(/[^A-Z2-7]/g, "");
  const bytes: number[] = [];
  let bits = 0;
  let value = 0;
  for (const ch of clean) {
    const idx = B32.indexOf(ch);
    if (idx < 0) continue;
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      bytes.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return new Uint8Array(bytes);
}
function base32Encode(bytes: Uint8Array): string {
  let out = "";
  let bits = 0;
  let value = 0;
  for (const b of bytes) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += B32[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
}
function randomBase32Secret(): string {
  const bytes = new Uint8Array(20);
  crypto.getRandomValues(bytes);
  return base32Encode(bytes);
}
async function totpCode(secret: string, atMs: number): Promise<string> {
  const counter = Math.floor(atMs / 30000);
  const msg = new Uint8Array(8);
  let c = counter;
  for (let i = 7; i >= 0; i--) {
    msg[i] = c & 0xff;
    c = Math.floor(c / 256);
  }
  const subtle = crypto.subtle as unknown as {
    importKey: (f: string, d: unknown, a: unknown, e: boolean, u: string[]) => Promise<CryptoKey>;
    sign: (a: unknown, k: CryptoKey, d: unknown) => Promise<ArrayBuffer>;
  };
  const key = await subtle.importKey(
    "raw",
    base32Decode(secret),
    { name: "HMAC", hash: "SHA-1" },
    false,
    ["sign"],
  );
  const sig = new Uint8Array(await subtle.sign("HMAC", key, msg));
  const offset = sig[19]! & 0x0f;
  const code =
    (((sig[offset]! & 0x7f) << 24) |
      ((sig[offset + 1]! & 0xff) << 16) |
      ((sig[offset + 2]! & 0xff) << 8) |
      (sig[offset + 3]! & 0xff)) %
    1000000;
  return String(code).padStart(6, "0");
}
async function totpValid(secret: string, code: string): Promise<boolean> {
  const clean = code.replace(/\D/g, "");
  if (clean.length !== 6 || !secret) return false;
  const now = Date.now();
  for (const drift of [-30000, 0, 30000]) {
    if ((await totpCode(secret, now + drift)) === clean) return true;
  }
  return false;
}

/* ---------- Session auth (server-side authorization) ---------- */

async function createSession(db: Db, playerId: number): Promise<string> {
  const token = `${uid("SES")}-${Math.floor(Math.random() * 1e12).toString(36).toUpperCase()}${Date.now().toString(36).toUpperCase()}`;
  await db.insert(schema.sessions).values({
    token,
    playerId,
    expiresAt: new Date(Date.now() + 30 * 24 * 3600 * 1000),
  });
  return token;
}

async function playerBySession(
  db: Db,
  playerId: number,
  token: string,
): Promise<PlayerRow | null> {
  if (!token || !playerId) return null;
  const rows = await db
    .select()
    .from(schema.sessions)
    .where(eq(schema.sessions.token, token))
    .limit(1);
  const sess = rows[0];
  if (!sess || sess.playerId !== playerId) return null;
  if (sess.expiresAt.getTime() < Date.now()) return null;
  const p = await getPlayer(db, playerId);
  if (!p || p.status === "perm_banned") return null;
  return p;
}

const AUTH_ERR = "Login session မမှန်ပါ — ပြန် login ဝင်ပါ (သူများ အကောင့်ကို ပြောင်းသုံးခွင့် မရှိပါ)";

/** Resolve an admin actor from (actor_player_id, session_token) and check
 *  the DB role. The client-sent actor NAME is never trusted. Returns an
 *  error string, or null when authorized. */
async function requireRole(
  db: Db,
  actorPlayerId: number,
  token: string,
  allowed: string[],
): Promise<{ error: string | null; actor: PlayerRow | null }> {
  const actor = await playerBySession(db, actorPlayerId, token);
  if (!actor) return { error: AUTH_ERR, actor: null };
  const role = effectiveRole(actor);
  if (!allowed.includes(role))
    return { error: `ဒီ လုပ်ဆောင်ချက်ကို ${allowed.join("/")} role ရှိသူပဲ လုပ်နိုင်ပါတယ် (သင့်ရဲ့ role: ${role})`, actor: null };
  return { error: null, actor };
}

async function requireOwner(
  db: Db,
  actorPlayerId: number,
  token: string,
): Promise<{ error: string | null; actor: PlayerRow | null }> {
  const r = await requireRole(db, actorPlayerId, token, ["owner"]);
  if (r.error) return r;
  if (!r.actor || !isFixedAdminEmail(r.actor.email))
    return { error: "Owner လုပ်ဆောင်ချက်ကို fixed Admin Gmail (nnominalhuman@gmail.com) နဲ့ ဝင်ထားသူပဲ လုပ်နိုင်ပါတယ်", actor: null };
  return r;
}

/** Team-ready gate: for team_size > 1 matches, every member of the player's
 *  team must exist and be marked ready in the Team section. */
async function teamGateFor(db: Db, playerId: number, teamSize: number): Promise<boolean> {
  if (teamSize <= 1) return true;
  const memberships = await db
    .select()
    .from(schema.teamMembers)
    .where(eq(schema.teamMembers.playerId, playerId));
  for (const ms of memberships) {
    const members = await db
      .select()
      .from(schema.teamMembers)
      .where(eq(schema.teamMembers.teamId, ms.teamId));
    if (members.length >= teamSize && members.every((mm) => mm.ready === 1)) return true;
  }
  return false;
}

/** Timeout sweep: ready-check warning -> cancel+refund; stale live matches.
 *  Runs lazily on reads (bootstrap/match/board) and via sweepTimeouts. */
async function sweepTimeoutsInternal(db: Db): Promise<number> {
  let acted = 0;
  acted += await expireMoneyRequestsInternal(db);
  const now = Date.now();
  const s = await getSettingsMap(db);
  const matchTimeoutMs = numSetting(s, "match_timeout_min", 180) * 60 * 1000;
  const rows = await db.select().from(schema.matches);
  for (const m of rows) {
    if (m.status === "ready_check" && m.readyDeadline.getTime() < now) {
      if (m.readyWarned === 0) {
        await db
          .update(schema.matches)
          .set({ readyWarned: 1, readyDeadline: new Date(now + 2 * 60 * 1000) })
          .where(eq(schema.matches.id, m.id));
        for (const pid of [m.playerAId, m.playerBId]) {
          await notify(db, {
            playerId: pid, type: "match", title: "⚠️ Ready warning",
            body: `${m.code} — Ready မနှိပ်ရင် ၂ မိနစ်အတွင်း match cancel ဖြစ်ပြီး coin ပြန်အမ်းပါမယ်။`,
            matchId: m.id,
          });
        }
        acted++;
      } else {
        const fresh = (await db.select().from(schema.matches).where(eq(schema.matches.id, m.id)).limit(1))[0];
        if (fresh && fresh.status === "ready_check") {
          await refundMatch(db, fresh, "Ready timeout — auto cancel");
          for (const pid of [fresh.playerAId, fresh.playerBId]) {
            const pp = await getPlayer(db, pid);
            const ready = pid === fresh.playerAId ? fresh.aReady === 1 : fresh.bReady === 1;
            if (pp && !ready) {
              await db.update(schema.players)
                .set({ reputation: Math.max(0, pp.reputation - 5) })
                .where(eq(schema.players.id, pid));
            }
          }
          await logAct(db, "system", `Ready timeout sweep cancelled ${fresh.code}`);
          acted++;
        }
      }
    } else if (
      (m.status === "live") && m.startedAt &&
      now - m.startedAt.getTime() > matchTimeoutMs
    ) {
      const fresh = (await db.select().from(schema.matches).where(eq(schema.matches.id, m.id)).limit(1))[0];
      if (fresh && fresh.status === "live") {
        await refundMatch(db, fresh, "Match timeout — auto cancel");
        await logAct(db, "system", `Match timeout sweep cancelled ${fresh.code}`);
        acted++;
      }
    }
  }
  return acted;
}

/** Money request expiry: pending top-ups and withdrawals older than 24h
 *  expire exactly once (atomic pending->expired claim). Expired top-ups
 *  never credit; expired withdrawals refund the deducted coins in full.
 *  The player is notified once — the status flip IS the notification gate.
 *  Runs inside the regular timeout sweep (invoked on bootstrap/app load)
 *  and lazily from the money actions themselves. */
async function expireMoneyRequestsInternal(db: Db): Promise<number> {
  let acted = 0;
  const cutoff = new Date(Date.now() - 24 * 3600 * 1000);
  const stalePurchases = await db
    .select()
    .from(schema.purchaseRequests)
    .where(eq(schema.purchaseRequests.status, "pending"));
  for (const r of stalePurchases) {
    if (r.createdAt >= cutoff) continue;
    const upd = await db
      .update(schema.purchaseRequests)
      .set({ status: "expired", decidedBy: "system (24h expiry)", decidedAt: new Date() })
      .where(
        and(
          eq(schema.purchaseRequests.id, r.id),
          eq(schema.purchaseRequests.status, "pending"),
        ),
      )
      .returning({ id: schema.purchaseRequests.id });
    if (upd.length === 0) continue;
    await notify(db, {
      playerId: r.playerId,
      type: "wallet",
      title: "Coin တောင်းဆိုချက် သက်တမ်းကုန်ပါပြီ ⏰",
      body: `${r.coins} coins တောင်းဆိုချက် (#${r.id}) ကို ၂၄ နာရီအတွင်း admin မဆုံးဖြတ်လို့ သက်တမ်းကုန်ပါပြီ — လိုအပ်ရင် အသစ် ပြန်တောင်းဆိုပါ။`,
    });
    await logAct(db, "system", `Purchase request #${r.id} expired after 24h (${r.coins} coins)`);
    acted++;
  }
  const staleWithdrawals = await db
    .select()
    .from(schema.withdrawRequests)
    .where(eq(schema.withdrawRequests.status, "pending"));
  for (const w of staleWithdrawals) {
    if (w.createdAt >= cutoff) continue;
    const upd = await db
      .update(schema.withdrawRequests)
      .set({ status: "expired", decidedBy: "system (24h expiry)", decidedAt: new Date() })
      .where(
        and(
          eq(schema.withdrawRequests.id, w.id),
          eq(schema.withdrawRequests.status, "pending"),
        ),
      )
      .returning({ id: schema.withdrawRequests.id });
    if (upd.length === 0) continue;
    const p = await getPlayer(db, w.playerId);
    if (p) {
      const nb = p.balance + w.coins;
      await db
        .update(schema.players)
        .set({ balance: nb })
        .where(eq(schema.players.id, p.id));
      await addTxn(db, {
        playerId: p.id,
        type: "withdraw_refund",
        amount: w.coins,
        balanceAfter: nb,
        note: `Withdraw #${w.id} expired (24h) — coins refunded`,
      });
    }
    await notify(db, {
      playerId: w.playerId,
      type: "wallet",
      title: "ငွေထုတ် တောင်းဆိုချက် သက်တမ်းကုန်ပါပြီ ⏰",
      body: `Withdraw #${w.id} (${w.coins} coins) ၂၄ နာရီ ကျော်လို့ သက်တမ်းကုန်ပြီး coin အပြည့် ပြန်အမ်းပြီးပါပြီ။`,
    });
    await logAct(db, "system", `Withdraw request #${w.id} expired after 24h — refunded ${w.coins} coins`);
    acted++;
  }
  return acted;
}

/** If the acting admin player has 2FA enabled, a valid TOTP code is required. */
async function checkAdmin2fa(
  db: Db,
  actorPlayerId: number,
  totpCodeInput: string,
): Promise<string | null> {
  if (!actorPlayerId) return "Admin actor မသိပါ — ပြန် login ဝင်ပါ";
  const actor = await getPlayer(db, actorPlayerId);
  if (!actor) return "Admin actor မတွေ့ပါ";
  // 2FA is enforced whenever the acting admin has it enabled — a valid TOTP
  // code is genuinely verified server-side; there is no bypass flag.
  if (actor.totpEnabled !== 1 || !actor.totpSecret) return null;
  if (!(await totpValid(actor.totpSecret, totpCodeInput)))
    return "Admin 2FA code မှားနေပါတယ် — Authenticator app ထဲက ၆ လုံး code ထည့်ပါ";
  return null;
}

/** Simple DB-backed rate limit. Returns an error string when over the limit. */
async function rateLimit(
  db: Db,
  key: string,
  limit: number,
  windowMs: number,
): Promise<string | null> {
  const since = new Date(Date.now() - windowMs);
  const rows = await db.select().from(schema.rateEvents);
  const recent = rows.filter((r) => r.key === key && r.createdAt >= since);
  if (recent.length >= limit)
    return "ခဏခဏ လုပ်ဆောင်နေပါတယ် — ခဏစောင့်ပြီး ပြန်ကြိုးစားပါ (spam protection)";
  await db.insert(schema.rateEvents).values({ key });
  return null;
}

/** Flag (never auto-ban) accounts that look like multi-account / referral fraud. */
function suspiciousCheck(
  all: PlayerRow[],
  cand: { name: string; mlbbId: string; serverId: string; referredById: number | null },
): string {
  const reasons: string[] = [];
  if (cand.mlbbId) {
    const dup = all.find(
      (p) => p.mlbbId && p.mlbbId === cand.mlbbId && p.serverId === cand.serverId,
    );
    if (dup) reasons.push(`MLBB ID + Server ID က ${dup.name} နဲ့ ထပ်နေပါတယ်`);
  }
  if (cand.referredById) {
    const ref = all.find((p) => p.id === cand.referredById);
    if (ref && cand.mlbbId && ref.mlbbId === cand.mlbbId)
      reasons.push("Referrer နဲ့ MLBB ID တူနေပါတယ် (self-referral သံသယ)");
    if (ref && ref.suspicious === 1) reasons.push("သံသယရှိ အကောင့်က ဖိတ်ခေါ်ထားပါတယ်");
  }
  const recent = all.filter(
    (p) => Date.now() - p.createdAt.getTime() < 10 * 60 * 1000,
  ).length;
  if (recent >= 5) reasons.push("ခဏအတွင်း အကောင့် အများကြီး ဖွင့်နေပါတယ်");
  return reasons.join(" • ");
}

/** Player display names are unique platform-wide, compared
 * case-insensitively after trimming. */
const normName = (n: string) => n.trim().toLowerCase();
function nameTaken(
  all: PlayerRow[],
  name: string,
  exceptId?: number,
): boolean {
  const wanted = normName(name);
  return all.some((p) => p.id !== exceptId && normName(p.name) === wanted);
}
/** A free display name: the base itself, or base + " #2", " #3", …
 * (kept within the 60-char name limit). */
function uniqueDisplayName(all: PlayerRow[], base: string): string {
  if (!nameTaken(all, base)) return base;
  for (let i = 2; ; i++) {
    const suffix = ` #${i}`;
    const cand = base.slice(0, 60 - suffix.length).trimEnd() + suffix;
    if (!nameTaken(all, cand)) return cand;
  }
}

const NAME_TAKEN_ERR =
  "ဒီနာမည် သုံးပြီးသား ဖြစ်နေပါတယ် — တခြားနာမည် တစ်ခု ရွေးပါ (This name is already taken — please choose a different name)";

/** Settle a match: payout winner, platform fee, referral commission. */
async function settleMatch(
  ctx: Ctx,
  db: Db,
  match: typeof schema.matches.$inferSelect,
  winnerId: number,
) {
  void (await getSettingsMap(db)); // settings intentionally NOT used for the locked referral %
  const loserId = winnerId === match.playerAId ? match.playerBId : match.playerAId;
  const winner = await getPlayer(db, winnerId);
  const loser = await getPlayer(db, loserId);
  if (!winner || !loser) throw new Error("players missing");
  const pot = match.stake * 2;
  const fee = match.feeAmount;
  const payout = pot - fee;

  const wBalance = winner.balance + payout;
  await db
    .update(schema.players)
    .set({
      balance: wBalance,
      lockedCoins: Math.max(0, winner.lockedCoins - match.stake),
      wins: winner.wins + 1,
      winStreak: winner.winStreak + 1,
      bestStreak: Math.max(winner.bestStreak, winner.winStreak + 1),
    })
    .where(eq(schema.players.id, winnerId));
  await db
    .update(schema.players)
    .set({
      lockedCoins: Math.max(0, loser.lockedCoins - match.stake),
      losses: loser.losses + 1,
      winStreak: 0,
    })
    .where(eq(schema.players.id, loserId));

  await addTxn(db, {
    playerId: winnerId,
    type: "payout",
    amount: payout,
    balanceAfter: wBalance,
    note: `Match ${match.code} prize (pot ${pot} − fee ${fee})`,
    matchId: match.id,
  });
  await addTxn(db, {
    playerId: null,
    type: "platform_fee",
    amount: fee,
    note: `Platform fee from ${match.code} (${match.feeLabel})`,
    matchId: match.id,
  });

  // Referral: direct only, % of the admin fee, only when the referred user wins.
  // Fraud guard: suspicious accounts (admin review pending) earn no commission.
  // The % is LOCKED at match-accept time (pending row / match snapshot) — the
  // current settings % is never re-read here, so later admin changes cannot
  // rewrite the commission of a match that already started.
  if (winner.referredById && winner.suspicious !== 1) {
    const referrer = await getPlayer(db, winner.referredById);
    const pending = await db
      .select()
      .from(schema.referralCommissions)
      .where(
        and(
          eq(schema.referralCommissions.matchId, match.id),
          eq(schema.referralCommissions.status, "pending"),
        ),
      );
    const ownPending = pending.find((c) => c.referredId === winnerId);
    const pctX100 = ownPending?.percent ?? match.referralPercentX100 ?? 100;
    const commission = Math.floor((fee * pctX100) / 10000);
    if (referrer && commission > 0) {
      if (ownPending) {
        await db
          .update(schema.referralCommissions)
          .set({ status: "completed", commission, feeAmount: fee, percent: pctX100 })
          .where(eq(schema.referralCommissions.id, ownPending.id));
      } else {
        await db.insert(schema.referralCommissions).values({
          referrerId: referrer.id,
          referredId: winnerId,
          matchId: match.id,
          feeAmount: fee,
          percent: pctX100,
          commission,
          status: "completed",
        });
      }
      const rBalance = referrer.balance + commission;
      await db
        .update(schema.players)
        .set({ balance: rBalance })
        .where(eq(schema.players.id, referrer.id));
      await addTxn(db, {
        playerId: referrer.id,
        type: "referral_commission",
        amount: commission,
        balanceAfter: rBalance,
        note: `Referral commission from ${match.code} (${pctX100 / 100}% of fee ${fee})`,
        matchId: match.id,
      });
      await notify(db, {
        playerId: referrer.id,
        type: "referral",
        title: "Referral commission ရရှိပါတယ်",
        body: `${match.code} မှ ${commission} coins commission ရရှိပါတယ်။`,
        matchId: match.id,
      });
    } else if (ownPending) {
      await db
        .update(schema.referralCommissions)
        .set({ status: "cancelled" })
        .where(eq(schema.referralCommissions.id, ownPending.id));
    }
  }

  if (winner.referredById && winner.suspicious === 1) {
    await db
      .update(schema.referralCommissions)
      .set({ status: "cancelled" })
      .where(
        and(
          eq(schema.referralCommissions.matchId, match.id),
          eq(schema.referralCommissions.status, "pending"),
        ),
      );
    await logAct(db, "system", `Referral commission held/cancelled for suspicious winner ${winner.name} (${match.code})`);
  }

  await db
    .update(schema.matches)
    .set({ status: "completed", winnerId, completedAt: new Date() })
    .where(eq(schema.matches.id, match.id));
  await notify(db, {
    playerId: winnerId,
    type: "result",
    title: "Match အနိုင်ရပါတယ် 🏆",
    body: `${match.code} — ${payout} coins ရရှိပါတယ်။`,
    matchId: match.id,
  });
  await notify(db, {
    playerId: loserId,
    type: "result",
    title: "Match ပြီးဆုံးပါပြီ",
    body: `${match.code} — နောက်ပွဲမှာ ပြန်ကြိုးစားပါ။`,
    matchId: match.id,
  });
  ctx.invalidateQueries();
}

async function refundMatch(
  db: Db,
  match: typeof schema.matches.$inferSelect,
  reason: string,
) {
  for (const pid of [match.playerAId, match.playerBId]) {
    const p = await getPlayer(db, pid);
    if (!p) continue;
    const nb = p.balance + match.stake;
    await db
      .update(schema.players)
      .set({
        balance: nb,
        lockedCoins: Math.max(0, p.lockedCoins - match.stake),
      })
      .where(eq(schema.players.id, pid));
    await addTxn(db, {
      playerId: pid,
      type: "refund",
      amount: match.stake,
      balanceAfter: nb,
      note: `${reason} — ${match.code}`,
      matchId: match.id,
    });
    await notify(db, {
      playerId: pid,
      type: "match",
      title: "Match cancel ဖြစ်သွားပါတယ်",
      body: `${match.code} — ${match.stake} coins ပြန်အမ်းပြီးပါပြီ။`,
      matchId: match.id,
    });
  }
  await db
    .update(schema.referralCommissions)
    .set({ status: "cancelled" })
    .where(
      and(
        eq(schema.referralCommissions.matchId, match.id),
        eq(schema.referralCommissions.status, "pending"),
      ),
    );
  await db
    .update(schema.matches)
    .set({ status: "cancelled" })
    .where(eq(schema.matches.id, match.id));
}

const matchShape = z.object({
  id: z.number(),
  code: z.string(),
  challenge_id: z.number(),
  arena: z.string(),
  team_size: z.number(),
  hero_rule: z.string(),
  custom_rules: z.string(),
  stake: z.number(),
  fee_amount: z.number(),
  fee_label: z.string(),
  pot: z.number(),
  payout_preview: z.number(),
  status: z.string(),
  a_ready: z.boolean(),
  b_ready: z.boolean(),
  ready_deadline: z.string(),
  started_at: z.string().nullable(),
  a_result: z.string(),
  b_result: z.string(),
  a_hero: z.string(),
  b_hero: z.string(),
  banned_heroes: z.string(),
  team_gate_required: z.boolean(),
  team_a_ready: z.boolean(),
  team_b_ready: z.boolean(),
  a_evidence_url: z.string(),
  b_evidence_url: z.string(),
  winner_id: z.number().nullable(),
  created_at: z.string(),
  player_a: playerShape,
  player_b: playerShape,
});

type MatchRow = typeof schema.matches.$inferSelect;

async function matchOut(ctx: Ctx, db: Db, m: MatchRow, viewerId: number | null = null) {
  const a = await getPlayer(db, m.playerAId);
  const b = await getPlayer(db, m.playerBId);
  const evUrl = async (key: string) => {
    if (!key) return "";
    if (key.startsWith("evidence/")) {
      try {
        return await ctx.blobs.getUrl(key);
      } catch {
        return "";
      }
    }
    return key;
  };
  const fallback = (id: number): PlayerRow => ({
    id,
    name: "Unknown",
    mlbbId: "",
    serverId: "",
    rank: "",
    avatarColor: "#475569",
    avatarBlobKey: "",
    role: "player",
    status: "active",
    balance: 0,
    lockedCoins: 0,
    wins: 0,
    losses: 0,
    winStreak: 0,
    bestStreak: 0,
    reputation: 0,
    badges: "",
    referralCode: "",
    referredById: null,
    googleSub: "",
    email: "",
    totpSecret: "",
    totpEnabled: 0,
    suspicious: 0,
    suspiciousReason: "",
    lastSeenAt: new Date(),
    createdAt: new Date(),
  });
  return {
    id: m.id,
    code: m.code,
    challenge_id: m.challengeId,
    arena: m.arena,
    team_size: m.teamSize,
    hero_rule: m.heroRule,
    custom_rules: m.customRules,
    stake: m.stake,
    fee_amount: m.feeAmount,
    fee_label: m.feeLabel,
    pot: m.stake * 2,
    payout_preview: m.stake * 2 - m.feeAmount,
    status: m.status,
    a_ready: m.aReady === 1,
    b_ready: m.bReady === 1,
    ready_deadline: m.readyDeadline.toISOString(),
    started_at: iso(m.startedAt),
    a_result: m.aResult,
    b_result: m.bResult,
    a_hero: m.aHero,
    b_hero: m.bHero,
    banned_heroes: m.bannedHeroes,
    team_gate_required: m.teamSize > 1,
    team_a_ready: await teamGateFor(db, m.playerAId, m.teamSize),
    team_b_ready: await teamGateFor(db, m.playerBId, m.teamSize),
    a_evidence_url: await evUrl(m.aEvidence),
    b_evidence_url: await evUrl(m.bEvidence),
    winner_id: m.winnerId ?? null,
    created_at: iso(m.createdAt) ?? "",
    player_a: await playerPublicOutAsync(ctx, a ?? fallback(m.playerAId), viewerId),
    player_b: await playerPublicOutAsync(ctx, b ?? fallback(m.playerBId), viewerId),
  };
}

const settingsShape = z.object({
  announcement_my: z.string(),
  announcement_en: z.string(),
  banner_title: z.string(),
  banner_subtitle: z.string(),
  fee_1v1_type: z.string(),
  fee_1v1_value: z.string(),
  fee_classic_type: z.string(),
  fee_classic_value: z.string(),
  fee_brawl_type: z.string(),
  fee_brawl_value: z.string(),
  referral_percent: z.number(),
  packages: z.array(z.object({ coins: z.number(), price: z.number() })),
  banned_heroes: z.string(),
  custom_hero_rules: z.string(),
  maintenance: z.boolean(),
  emergency: z.boolean(),
  ready_timeout_min: z.number(),
  result_delay_min: z.number(),
  accent: z.string(),
  rules_version: z.string(),
  telegram_bot: z.string(),
  telegram_configured: z.boolean(),
  telegram_chat_id: z.string(),
  google_client_id: z.string(),
  google_enabled: z.boolean(),
  google_setup_state: z.string(),
  google_pending_client_id: z.string(),
  google_setup_by_name: z.string(),
  google_setup_at: z.string(),
  google_effective_client_id: z.string(),
  google_effective_enabled: z.boolean(),
  terms_version: z.string(),
  terms_text: z.string(),
  rules_text: z.string(),
  fixed_admin_email: z.string(),
  match_timeout_min: z.number(),
  kpay_phone: z.string(),
  kpay_qr: z.string(),
  wave_phone: z.string(),
  wave_qr: z.string(),
  coin_rate_kyat: z.number(),
  withdraw_fee_percent: z.number(),
  withdraw_min_coins: z.number(),
  topup_min_coins: z.number(),
  topup_max_coins: z.number(),
  withdraw_max_coins_per_day: z.number(),
});

function settingsOut(s: Record<string, string>) {
  let packages: { coins: number; price: number }[] = [];
  try {
    const parsed = JSON.parse(s["packages"] ?? "[]") as {
      coins: number;
      price: number;
    }[];
    if (Array.isArray(parsed)) packages = parsed;
  } catch {
    packages = [];
  }
  return {
    announcement_my: s["announcement_my"] ?? "",
    announcement_en: s["announcement_en"] ?? "",
    banner_title: s["banner_title"] ?? "FIGHT FOR GLORY",
    banner_subtitle: s["banner_subtitle"] ?? "PLAY • COMPETE • WIN",
    fee_1v1_type: s["fee_1v1_type"] ?? "percent",
    fee_1v1_value: s["fee_1v1_value"] ?? "5",
    fee_classic_type: s["fee_classic_type"] ?? "percent",
    fee_classic_value: s["fee_classic_value"] ?? "5",
    fee_brawl_type: s["fee_brawl_type"] ?? "percent",
    fee_brawl_value: s["fee_brawl_value"] ?? "5",
    referral_percent: (Number(s["referral_percent_x100"] ?? "100") || 0) / 100,
    packages,
    banned_heroes: s["banned_heroes"] ?? "",
    custom_hero_rules: s["custom_hero_rules"] ?? "",
    maintenance: s["maintenance"] === "1",
    emergency: s["emergency"] === "1",
    ready_timeout_min: numSetting(s, "ready_timeout_min", 3),
    result_delay_min: numSetting(s, "result_delay_min", 5),
    accent: s["accent"] ?? "#22d3ee",
    rules_version: s["rules_version"] ?? "1",
    telegram_bot: s["telegram_bot"] ?? "",
    telegram_configured:
      (s["telegram_bot_token"] ?? "").trim() !== "" &&
      (s["telegram_chat_id"] ?? "").trim() !== "",
    telegram_chat_id: s["telegram_chat_id"] ?? "",
    google_client_id: s["google_client_id"] ?? "",
    google_enabled:
      s["google_enabled"] === "1" && (s["google_client_id"] ?? "").trim() !== "",
    google_setup_state: googleState(s).state,
    google_pending_client_id: googleState(s).pendingId,
    google_setup_by_name: s["google_setup_by_name"] ?? "",
    google_setup_at: s["google_setup_at"] ?? "",
    google_effective_client_id: googleState(s).effectiveId,
    google_effective_enabled: googleState(s).effectiveEnabled,
    terms_version: s["terms_version"] ?? "1",
    terms_text: s["terms_text"] ?? "",
    rules_text: s["rules_text"] ?? "",
    fixed_admin_email: FIXED_ADMIN_EMAIL,
    match_timeout_min: numSetting(s, "match_timeout_min", 180),
    kpay_phone: s["kpay_phone"] ?? "",
    kpay_qr: s["kpay_qr"] ?? "",
    wave_phone: s["wave_phone"] ?? "",
    wave_qr: s["wave_qr"] ?? "",
    coin_rate_kyat: numSetting(s, "coin_rate_kyat", 8),
    withdraw_fee_percent: numSetting(s, "withdraw_fee_percent", 0),
    withdraw_min_coins: numSetting(s, "withdraw_min_coins", 1000),
    topup_min_coins: numSetting(s, "topup_min_coins", 10),
    topup_max_coins: numSetting(s, "topup_max_coins", 1000000),
    withdraw_max_coins_per_day: numSetting(s, "withdraw_max_coins_per_day", 100000),
  };
}

/* ---------- Money decision core (shared by dashboard + Telegram) ----------
 * Authorization lives in the CALLERS (dashboard actions verify Owner +
 * confirmation + TOTP; the Telegram callback verifies the configured chat
 * id). These helpers own the state machine only: atomic pending->decided
 * claims make every money movement happen exactly once, whichever surface
 * decides first. */

async function decidePurchaseInternal(
  ctx: Ctx,
  db: Db,
  requestId: number,
  decision: "approve" | "reject",
  decidedByName: string,
  reason: string,
): Promise<{ ok: boolean; error: string; txn_code: string }> {
  const fail = (error: string) => ({ ok: false, error, txn_code: "" });
  const rows = await db
    .select()
    .from(schema.purchaseRequests)
    .where(eq(schema.purchaseRequests.id, requestId))
    .limit(1);
  const req = rows[0];
  if (!req) return fail("Request မတွေ့ပါ");
  if (req.status === "expired")
    return fail("ဒီ request က သက်တမ်းကုန်ပြီးသားပါ — ဆုံးဖြတ်လို့ မရပါ");
  if (req.status !== "pending")
    return fail("ဒီ request ကို ဆုံးဖြတ်ပြီးသားပါ — နှစ်ခါ credit မဖြစ်ပါ");
  if (decision === "reject") {
    const upd = await db
      .update(schema.purchaseRequests)
      .set({
        status: "rejected",
        decidedBy: decidedByName,
        decidedAt: new Date(),
        rejectReason: (reason ?? "").slice(0, 300),
      })
      .where(
        and(
          eq(schema.purchaseRequests.id, req.id),
          eq(schema.purchaseRequests.status, "pending"),
        ),
      )
      .returning({ id: schema.purchaseRequests.id });
    if (upd.length === 0) return fail("တခြားသူ အရင် ဆုံးဖြတ်သွားပါပြီ");
    await notify(db, {
      playerId: req.playerId,
      type: "wallet",
      title: "Coin ဝယ်ယူမှု ပယ်ချခံရပါတယ်",
      body: `${req.coins} coins တောင်းဆိုမှုကို admin က ပယ်ချပါတယ်။${reason ? `\nအကြောင်းရင်း: ${reason}` : ""}`,
    });
    await logAct(db, decidedByName, `Purchase request #${req.id} rejected (${req.coins} coins)${reason ? ` — ${reason}` : ""}`);
    ctx.invalidateQueries();
    return { ok: true, error: "", txn_code: "" };
  }
  // Atomic claim first (pending -> approved) so a double-click, a second
  // admin, or a Telegram tap can never credit twice.
  const code = uid("TXN");
  const upd = await db
    .update(schema.purchaseRequests)
    .set({ status: "approved", decidedBy: decidedByName, decidedAt: new Date(), txnCode: code, rejectReason: "" })
    .where(
      and(
        eq(schema.purchaseRequests.id, req.id),
        eq(schema.purchaseRequests.status, "pending"),
      ),
    )
    .returning({ id: schema.purchaseRequests.id });
  if (upd.length === 0) return fail("တခြားသူ အရင် ဆုံးဖြတ်သွားပါပြီ — နှစ်ခါ credit မဖြစ်ပါ");
  const buyer = await getPlayer(db, req.playerId);
  if (!buyer) return fail("Buyer မတွေ့ပါ");
  const nb = buyer.balance + req.coins;
  await db.update(schema.players).set({ balance: nb }).where(eq(schema.players.id, buyer.id));
  await db.insert(schema.transactions).values({
    code, playerId: buyer.id, type: "purchase", amount: req.coins,
    balanceAfter: nb, note: `${req.label} — အတည်ပြုပြီး (request #${req.id}, by ${decidedByName})`,
  });
  await notify(db, { playerId: buyer.id, type: "wallet", title: "Coin ရောက်ပါပြီ 🪙", body: `${req.coins} coins ထည့်ပြီးပါပြီ။ Balance: ${nb} (Txn ${code})` });
  await logAct(db, decidedByName, `Purchase request #${req.id} approved: +${req.coins} coins to ${buyer.name} (${code})`);
  ctx.invalidateQueries();
  return { ok: true, error: "", txn_code: code };
}

async function decideWithdrawInternal(
  ctx: Ctx,
  db: Db,
  requestId: number,
  decision: "paid" | "reject",
  decidedByName: string,
  reason: string,
): Promise<{ ok: boolean; error: string }> {
  const fail = (error: string) => ({ ok: false, error });
  const rows = await db
    .select()
    .from(schema.withdrawRequests)
    .where(eq(schema.withdrawRequests.id, requestId))
    .limit(1);
  const w = rows[0];
  if (!w) return fail("Withdraw request မတွေ့ပါ");
  if (w.status === "expired")
    return fail("ဒီ withdraw က သက်တမ်းကုန်ပြီး coin ပြန်အမ်းပြီးသားပါ");
  if (w.status !== "pending")
    return fail("ဒီ withdraw ကို ဆုံးဖြတ်ပြီးသားပါ — နှစ်ခါ ရွှေ့လို့ မရပါ");
  if (decision === "paid") {
    const upd = await db
      .update(schema.withdrawRequests)
      .set({ status: "paid", decidedBy: decidedByName, decidedAt: new Date() })
      .where(
        and(
          eq(schema.withdrawRequests.id, w.id),
          eq(schema.withdrawRequests.status, "pending"),
        ),
      )
      .returning({ id: schema.withdrawRequests.id });
    if (upd.length === 0) return fail("တခြားသူ အရင် ဆုံးဖြတ်သွားပါပြီ");
    await notify(db, {
      playerId: w.playerId,
      type: "wallet",
      title: "ငွေ ထုတ်ပေးပြီးပါပြီ 💸",
      body: `Withdraw #${w.id}: ${w.netKyat.toLocaleString()} ကျပ် ကို ${w.method === "wave" ? "Wave Pay" : "KBZPay"} (${w.payoutPhone}) ထဲ ပို့ပြီးပါပြီ။`,
    });
    await logAct(db, decidedByName, `Withdraw #${w.id} PAID: ${w.netKyat} kyat (${w.coins} coins) via ${w.method}`);
    ctx.invalidateQueries();
    return { ok: true, error: "" };
  }
  // Reject: claim first, then refund the deducted coins exactly once.
  const upd = await db
    .update(schema.withdrawRequests)
    .set({
      status: "rejected",
      decidedBy: decidedByName,
      decidedAt: new Date(),
      reason: (reason ?? "").slice(0, 300),
    })
    .where(
      and(
        eq(schema.withdrawRequests.id, w.id),
        eq(schema.withdrawRequests.status, "pending"),
      ),
    )
    .returning({ id: schema.withdrawRequests.id });
  if (upd.length === 0) return fail("တခြားသူ အရင် ဆုံးဖြတ်သွားပါပြီ");
  const p = await getPlayer(db, w.playerId);
  if (p) {
    const nb = p.balance + w.coins;
    await db.update(schema.players).set({ balance: nb }).where(eq(schema.players.id, p.id));
    await addTxn(db, {
      playerId: p.id,
      type: "withdraw_refund",
      amount: w.coins,
      balanceAfter: nb,
      note: `Withdraw #${w.id} rejected — coins refunded${reason ? ` (${reason})` : ""}`,
    });
  }
  await notify(db, {
    playerId: w.playerId,
    type: "wallet",
    title: "ငွေထုတ် တောင်းဆိုချက် ပယ်ချခံရပါတယ်",
    body: `Withdraw #${w.id} (${w.coins} coins) ကို admin က ပယ်ချပြီး coin အပြည့် ပြန်အမ်းပြီးပါပြီ။${reason ? `\nအကြောင်းရင်း: ${reason}` : ""}`,
  });
  await logAct(db, decidedByName, `Withdraw #${w.id} rejected — refunded ${w.coins} coins${reason ? ` — ${reason}` : ""}`);
  ctx.invalidateQueries();
  return { ok: true, error: "" };
}

type PurchaseRow = typeof schema.purchaseRequests.$inferSelect;
type WithdrawRow = typeof schema.withdrawRequests.$inferSelect;

async function purchaseOut(ctx: Ctx, r: PurchaseRow, playerName: string) {
  return {
    id: r.id,
    player_id: r.playerId,
    player_name: playerName,
    coins: r.coins,
    label: r.label,
    status: r.status,
    txn_code: r.txnCode,
    proof_url: r.proofBlobKey ? await blobUrlSafe(ctx, r.proofBlobKey) : "",
    has_proof: r.proofBlobKey !== "",
    reject_reason: r.rejectReason,
    decided_by: r.decidedBy,
    created_at: iso(r.createdAt) ?? "",
    decided_at: iso(r.decidedAt),
  };
}

function withdrawOut(r: WithdrawRow, playerName: string) {
  return {
    id: r.id,
    player_id: r.playerId,
    player_name: playerName,
    coins: r.coins,
    rate_kyat: r.rateKyat,
    gross_kyat: r.grossKyat,
    fee_percent: r.feePercent,
    fee_kyat: r.feeKyat,
    net_kyat: r.netKyat,
    method: r.method,
    payout_phone: r.payoutPhone,
    status: r.status,
    reason: r.reason,
    txn_code: r.txnCode,
    decided_by: r.decidedBy,
    created_at: iso(r.createdAt) ?? "",
    decided_at: iso(r.decidedAt),
  };
}

const purchaseReqShape = z.object({
  id: z.number(),
  player_id: z.number(),
  player_name: z.string(),
  coins: z.number(),
  label: z.string(),
  status: z.string(),
  txn_code: z.string(),
  proof_url: z.string(),
  has_proof: z.boolean(),
  reject_reason: z.string(),
  decided_by: z.string(),
  created_at: z.string(),
  decided_at: z.string().nullable(),
});

const withdrawReqShape = z.object({
  id: z.number(),
  player_id: z.number(),
  player_name: z.string(),
  coins: z.number(),
  rate_kyat: z.number(),
  gross_kyat: z.number(),
  fee_percent: z.number(),
  fee_kyat: z.number(),
  net_kyat: z.number(),
  method: z.string(),
  payout_phone: z.string(),
  status: z.string(),
  reason: z.string(),
  txn_code: z.string(),
  decided_by: z.string(),
  created_at: z.string(),
  decided_at: z.string().nullable(),
});

export const Actions = {
  getBootstrap: defineAction({
    request: z.object({
      player_id: z.number().nullable().default(null),
      session_token: z.string().default(""),
    }),
    response: z.object({
      settings: settingsShape,
      players: z.array(playerShape),
      current_player: playerShape.nullable(),
      stats: z.object({
        online_players: z.number(),
        total_players: z.number(),
        active_matches: z.number(),
        completed_matches: z.number(),
        open_challenges: z.number(),
      }),
      next_tournament: z
        .object({
          id: z.number(),
          name: z.string(),
          prize: z.number(),
          starts_at: z.string(),
          status: z.string(),
        })
        .nullable(),
      live_matches: z.array(matchShape),
      top_players: z.array(playerShape),
      recent_payouts: z.array(
        z.object({
          masked_name: z.string(),
          net_kyat: z.number(),
          decided_at: z.string(),
        }),
      ),
      total_paid_kyat: z.number(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      await sweepTimeoutsInternal(db).catch(() => 0);
      // Enforce the fixed-admin rule on every load: the fixed Gmail is owner,
      // any other stored "owner" is demoted to a normal player.
      const rawPlayers = await db.select().from(schema.players);
      for (const rp of rawPlayers) {
        const want = effectiveRole(rp);
        if (want !== rp.role) {
          await db.update(schema.players).set({
            role: want as "player" | "owner" | "admin" | "moderator" | "support",
          }).where(eq(schema.players.id, rp.id));
        }
      }
      const allPlayers = await db
        .select()
        .from(schema.players)
        .orderBy(desc(schema.players.wins));
      // The viewer is only "logged in" with a valid server session; a bare
      // player_id from the client is never trusted on its own.
      const viewer = args.player_id
        ? await playerBySession(db, args.player_id, args.session_token)
        : null;
      if (viewer) {
        await db
          .update(schema.players)
          .set({ lastSeenAt: new Date() })
          .where(eq(schema.players.id, viewer.id));
      }
      const cutoff = new Date(Date.now() - 10 * 60 * 1000);
      const online = allPlayers.filter((p) => p.lastSeenAt >= cutoff).length;
      const allMatches = await db.select().from(schema.matches);
      const active = allMatches.filter((m) =>
        ["ready_check", "live", "result_pending", "disputed"].includes(m.status),
      );
      const openCh = await db
        .select()
        .from(schema.challenges)
        .where(eq(schema.challenges.status, "open"));
      const tours = await db
        .select()
        .from(schema.tournaments)
        .orderBy(schema.tournaments.startsAt);
      const next =
        tours.find((t) => t.status !== "completed") ?? null;
      const liveOut = [];
      for (const m of active.slice(0, 10)) liveOut.push(await matchOut(ctx, db, m));
      const current = viewer
        ? (allPlayers.find((p) => p.id === viewer.id) ?? null)
        : null;
      const viewerId = viewer?.id ?? null;
      return {
        settings: settingsOut(s),
        players: await playersPublicOutAsync(ctx, allPlayers, viewerId),
        current_player: current ? await playerOutAsync(ctx, current) : null,
        stats: {
          online_players: online,
          total_players: allPlayers.length,
          active_matches: active.length,
          completed_matches: allMatches.filter((m) => m.status === "completed")
            .length,
          open_challenges: openCh.length,
        },
        next_tournament: next
          ? {
              id: next.id,
              name: next.name,
              prize: next.prize,
              starts_at: next.startsAt.toISOString(),
              status: next.status,
            }
          : null,
        live_matches: liveOut,
        top_players: await playersPublicOutAsync(ctx, allPlayers.slice(0, 5), viewerId),
        recent_payouts: await (async () => {
          // Public trust feed: PAID withdrawals only, names masked, and
          // NEVER a phone number or full name.
          const paid = await db
            .select()
            .from(schema.withdrawRequests)
            .where(eq(schema.withdrawRequests.status, "paid"))
            .orderBy(desc(schema.withdrawRequests.decidedAt))
            .limit(30);
          return paid.map((w) => ({
            masked_name: maskName(
              allPlayers.find((x) => x.id === w.playerId)?.name ?? "Player",
            ),
            net_kyat: w.netKyat,
            decided_at: iso(w.decidedAt) ?? iso(w.createdAt) ?? "",
          }));
        })(),
        total_paid_kyat: await (async () => {
          const paidAll = await db
            .select()
            .from(schema.withdrawRequests)
            .where(eq(schema.withdrawRequests.status, "paid"));
          return paidAll.reduce((a, w) => a + w.netKyat, 0);
        })(),
      };
    },
  }),

  registerPlayer: defineAction({
    request: z.object({
      name: z.string().min(1).max(60),
      mlbb_id: z.string().max(30).default(""),
      server_id: z.string().max(30).default(""),
      rank: z.string().default("Epic"),
      referral_code: z.string().max(30).default(""),
      avatar_color: z.string().default("#7c3aed"),
    }),
    response: z.object({
      ok: z.boolean(),
      player: playerShape.nullable(),
      error: z.string(),
      session_token: z.string(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const rl = await rateLimit(db, "register", 10, 10 * 60 * 1000);
      if (rl) return { ok: false, player: null, error: rl, session_token: "" };
      const existing = await db.select().from(schema.players);
      if (nameTaken(existing, args.name))
        return { ok: false, player: null, error: NAME_TAKEN_ERR, session_token: "" };
      let referredById: number | null = null;
      const code = args.referral_code.trim().toUpperCase();
      if (code) {
        const ref = existing.find((p) => p.referralCode.toUpperCase() === code);
        if (ref) referredById = ref.id;
      }
      const myCode = `${args.name.replace(/[^A-Za-z0-9]/g, "").slice(0, 6).toUpperCase() || "MLBB"}${Math.floor(1000 + Math.random() * 9000)}`;
      const suspicion = suspiciousCheck(existing, {
        name: args.name.trim(),
        mlbbId: args.mlbb_id.trim(),
        serverId: args.server_id.trim(),
        referredById,
      });
      const inserted = await db
        .insert(schema.players)
        .values({
          name: args.name.trim(),
          mlbbId: args.mlbb_id.trim(),
          serverId: args.server_id.trim(),
          rank: args.rank,
          avatarColor: args.avatar_color,
          // No first-registrant bootstrap any more: normal signup is ALWAYS a
          // plain player. Owner exists only via the fixed admin Gmail.
          role: "player",
          referralCode: myCode,
          referredById,
          suspicious: suspicion ? 1 : 0,
          suspiciousReason: suspicion,
        })
        .returning();
      const p = inserted[0];
      if (!p) return { ok: false, player: null, error: "create failed", session_token: "" };
      await notify(db, {
        playerId: p.id,
        type: "info",
        title: "ကြိုဆိုပါတယ် 🎮",
        body: "Profile ဆောက်ပြီးပါပြီ။ Challenge စတင်နိုင်ပါပြီ။",
      });
      const token = await createSession(db, p.id);
      ctx.invalidateQueries();
      return { ok: true, player: await playerOutAsync(ctx, p), error: "", session_token: token };
    },
  }),

  updateProfile: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      name: z.string().min(1).max(60),
      mlbb_id: z.string().max(30),
      server_id: z.string().max(30),
      rank: z.string(),
      avatar_color: z.string(),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false };
      const allPlayers = await db.select().from(schema.players);
      if (nameTaken(allPlayers, args.name, me.id)) return { ok: false };
      await db
        .update(schema.players)
        .set({
          name: args.name.trim(),
          mlbbId: args.mlbb_id.trim(),
          serverId: args.server_id.trim(),
          rank: args.rank,
          avatarColor: args.avatar_color,
        })
        .where(eq(schema.players.id, args.player_id));
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  heartbeat: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false };
      await db
        .update(schema.players)
        .set({ lastSeenAt: new Date() })
        .where(eq(schema.players.id, args.player_id));
      return { ok: true };
    },
  }),

  searchAll: defineAction({
    request: z.object({ q: z.string().min(1).max(60) }),
    response: z.object({
      players: z.array(playerShape),
      challenges: z.array(
        z.object({
          id: z.number(),
          arena: z.string(),
          team_size: z.number(),
          stake: z.number(),
          creator_name: z.string(),
        }),
      ),
      tournaments: z.array(
        z.object({ id: z.number(), name: z.string(), prize: z.number() }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const q = args.q.toLowerCase();
      const all = await db.select().from(schema.players);
      const players = all.filter(
        (p) =>
          p.name.toLowerCase().includes(q) || p.mlbbId.toLowerCase().includes(q),
      );
      const chs = await db
        .select()
        .from(schema.challenges)
        .where(eq(schema.challenges.status, "open"));
      const nameOf = (id: number) => all.find((p) => p.id === id)?.name ?? "?";
      const challenges = chs
        .filter((c) => nameOf(c.creatorId).toLowerCase().includes(q))
        .map((c) => ({
          id: c.id,
          arena: c.arena,
          team_size: c.teamSize,
          stake: c.stake,
          creator_name: nameOf(c.creatorId),
        }));
      const tours = await db.select().from(schema.tournaments);
      return {
        players: await playersPublicOutAsync(ctx, players, null),
        challenges,
        tournaments: tours
          .filter((t) => t.name.toLowerCase().includes(q))
          .map((t) => ({ id: t.id, name: t.name, prize: t.prize })),
      };
    },
  }),

  listChallenges: defineAction({
    request: z.object({
      arena: z.string().default("all"),
      hero_rule: z.string().default("all"),
      rank: z.string().default("all"),
      max_stake: z.number().default(0),
      team_size: z.number().default(0),
    }),
    response: z.object({
      challenges: z.array(
        z.object({
          id: z.number(),
          creator: playerShape,
          arena: z.string(),
          team_size: z.number(),
          hero_rule: z.string(),
          rank_restriction: z.string(),
          custom_rules: z.string(),
          stake: z.number(),
          fee_amount: z.number(),
          fee_label: z.string(),
          payout_preview: z.number(),
          status: z.string(),
          created_at: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      const rows = await db
        .select()
        .from(schema.challenges)
        .where(eq(schema.challenges.status, "open"))
        .orderBy(desc(schema.challenges.id));
      const all = await db.select().from(schema.players);
      const out = [];
      for (const c of rows) {
        if (args.arena !== "all" && c.arena !== args.arena) continue;
        if (args.hero_rule !== "all" && c.heroRule !== args.hero_rule) continue;
        if (args.rank !== "all" && c.rankRestriction !== args.rank) continue;
        if (args.max_stake > 0 && c.stake > args.max_stake) continue;
        if (args.team_size > 0 && c.teamSize !== args.team_size) continue;
        const creator = all.find((p) => p.id === c.creatorId);
        if (!creator) continue;
        const pot = c.stake * 2;
        const { fee, label } = feeFor(s, c.arena, pot);
        out.push({
          id: c.id,
          creator: await playerPublicOutAsync(ctx, creator, null),
          arena: c.arena,
          team_size: c.teamSize,
          hero_rule: c.heroRule,
          rank_restriction: c.rankRestriction,
          custom_rules: c.customRules,
          stake: c.stake,
          fee_amount: fee,
          fee_label: label,
          payout_preview: pot - fee,
          status: c.status,
          created_at: iso(c.createdAt) ?? "",
        });
      }
      return { challenges: out };
    },
  }),

  createChallenge: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      arena: z.enum(["1v1", "classic", "brawl"]),
      team_size: z.number().int().min(1).max(5),
      hero_rule: z.string().max(40),
      rank_restriction: z.string().max(30).default(""),
      custom_rules: z.string().max(500).default(""),
      stake: z.number().int().min(10).max(1000000),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), challenge_id: z.number() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      if (s["emergency"] === "1")
        return { ok: false, error: "Emergency mode — challenge အသစ် ယာယီပိတ်ထားပါတယ်", challenge_id: 0 };
      if (s["maintenance"] === "1")
        return { ok: false, error: "Maintenance လုပ်နေပါတယ်", challenge_id: 0 };
      const p = await playerBySession(db, args.player_id, args.session_token);
      if (!p) return { ok: false, error: AUTH_ERR, challenge_id: 0 };
      if (p.status === "perm_banned" || p.status === "temp_banned")
        return { ok: false, error: "Account ban ခံထားရပါတယ်", challenge_id: 0 };
      if (p.balance < args.stake)
        return { ok: false, error: "Coin မလုံလောက်ပါ — Wallet မှာ အရင်ဝယ်ပါ", challenge_id: 0 };
      const ins = await db
        .insert(schema.challenges)
        .values({
          creatorId: args.player_id,
          arena: args.arena,
          teamSize: args.team_size,
          heroRule: args.hero_rule,
          rankRestriction: args.rank_restriction,
          customRules: args.custom_rules,
          // Snapshot of the admin hero-ban list at creation time — enforced
          // later in the match room, not just displayed.
          bannedHeroes: s["banned_heroes"] ?? "",
          stake: args.stake,
        })
        .returning({ id: schema.challenges.id });
      ctx.invalidateQueries();
      return { ok: true, error: "", challenge_id: ins[0]?.id ?? 0 };
    },
  }),

  acceptChallenge: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      challenge_id: z.number(),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), match_id: z.number() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      if (s["emergency"] === "1")
        return { ok: false, error: "Emergency mode — match လက်ခံခြင်း ယာယီပိတ်ထားပါတယ်", match_id: 0 };
      const chRows = await db
        .select()
        .from(schema.challenges)
        .where(eq(schema.challenges.id, args.challenge_id))
        .limit(1);
      const ch = chRows[0];
      if (!ch || ch.status !== "open")
        return { ok: false, error: "ဒီ challenge ကို တခြားသူ လက်ခံပြီးပါပြီ", match_id: 0 };
      if (ch.creatorId === args.player_id)
        return { ok: false, error: "ကိုယ့် challenge ကို ကိုယ် လက်ခံလို့ မရပါ", match_id: 0 };
      const acceptor = await playerBySession(db, args.player_id, args.session_token);
      if (!acceptor)
        return { ok: false, error: AUTH_ERR, match_id: 0 };
      const creator = await getPlayer(db, ch.creatorId);
      if (!acceptor || !creator)
        return { ok: false, error: "Player မတွေ့ပါ", match_id: 0 };
      if (acceptor.status === "perm_banned" || acceptor.status === "temp_banned")
        return { ok: false, error: "Account ban ခံထားရပါတယ်", match_id: 0 };
      if (ch.rankRestriction && acceptor.rank !== ch.rankRestriction)
        return { ok: false, error: `Rank ကန့်သတ်ထားပါတယ် — ${ch.rankRestriction} ပဲ လက်ခံလို့ရပါတယ်`, match_id: 0 };
      if (acceptor.balance < ch.stake || creator.balance < ch.stake)
        return { ok: false, error: "Coin မလုံလောက်ပါ (နှစ်ဖက်လုံး stake ရှိရပါမယ်)", match_id: 0 };

      // Atomic claim: only succeeds while still open (double-accept prevention).
      const claimed = await db
        .update(schema.challenges)
        .set({ status: "matched" })
        .where(
          and(
            eq(schema.challenges.id, ch.id),
            eq(schema.challenges.status, "open"),
          ),
        )
        .returning({ id: schema.challenges.id });
      if (claimed.length === 0)
        return { ok: false, error: "တခြားသူ အရင် လက်ခံသွားပါပြီ", match_id: 0 };

      // Lock coins on both sides.
      for (const p of [creator, acceptor]) {
        const nb = p.balance - ch.stake;
        await db
          .update(schema.players)
          .set({ balance: nb, lockedCoins: p.lockedCoins + ch.stake })
          .where(eq(schema.players.id, p.id));
        await addTxn(db, {
          playerId: p.id,
          type: "lock",
          amount: -ch.stake,
          balanceAfter: nb,
          note: `Coin lock — challenge #${ch.id}`,
        });
      }

      const pot = ch.stake * 2;
      const { fee, label } = feeFor(s, ch.arena, pot);
      const readyTimeout = numSetting(s, "ready_timeout_min", 3);
      const lockedRefPct = Number(s["referral_percent_x100"] ?? "100") || 0;
      const ins = await db
        .insert(schema.matches)
        .values({
          code: uid("M"),
          challengeId: ch.id,
          playerAId: ch.creatorId,
          playerBId: args.player_id,
          arena: ch.arena,
          teamSize: ch.teamSize,
          heroRule: ch.heroRule,
          customRules: ch.customRules,
          bannedHeroes: ch.bannedHeroes || (s["banned_heroes"] ?? ""),
          referralPercentX100: lockedRefPct,
          stake: ch.stake,
          feeAmount: fee,
          feeLabel: label,
          readyDeadline: new Date(Date.now() + readyTimeout * 60 * 1000),
        })
        .returning({ id: schema.matches.id });
      const matchId = ins[0]?.id ?? 0;
      // Pending referral commission if either side was referred (settled at result).
      for (const side of [creator, acceptor]) {
        if (side.referredById) {
          await db.insert(schema.referralCommissions).values({
            referrerId: side.referredById,
            referredId: side.id,
            matchId,
            feeAmount: fee,
            percent: lockedRefPct,
            commission: 0,
            status: "pending",
          });
        }
      }
      await notify(db, {
        playerId: ch.creatorId,
        type: "challenge_accepted",
        title: "Challenge လက်ခံလိုက်ပါပြီ ⚔️",
        body: `${acceptor.name} က သင့် challenge ကို လက်ခံပါတယ်။ Match Room ထဲ ဝင်ပါ။`,
        matchId,
      });
      await notify(db, {
        playerId: args.player_id,
        type: "challenge_accepted",
        title: "Match Room ပွင့်ပါပြီ ⚔️",
        body: `${creator.name} နဲ့ ပွဲစရန် Ready နှိပ်ပါ။`,
        matchId,
      });
      ctx.invalidateQueries();
      return { ok: true, error: "", match_id: matchId };
    },
  }),

  cancelChallenge: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      challenge_id: z.number(),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false };
      await db
        .update(schema.challenges)
        .set({ status: "cancelled" })
        .where(
          and(
            eq(schema.challenges.id, args.challenge_id),
            eq(schema.challenges.creatorId, args.player_id),
            eq(schema.challenges.status, "open"),
          ),
        );
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  saveRule: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      name: z.string().min(1).max(60),
      arena: z.string(),
      team_size: z.number().int().min(1).max(5),
      hero_rule: z.string(),
      custom_rules: z.string().max(500).default(""),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false };
      await db.insert(schema.savedRules).values({
        playerId: args.player_id,
        name: args.name,
        arena: args.arena,
        teamSize: args.team_size,
        heroRule: args.hero_rule,
        customRules: args.custom_rules,
      });
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  listSavedRules: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({
      rules: z.array(
        z.object({
          id: z.number(),
          name: z.string(),
          arena: z.string(),
          team_size: z.number(),
          hero_rule: z.string(),
          custom_rules: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { rules: [] };
      const rows = await db
        .select()
        .from(schema.savedRules)
        .where(eq(schema.savedRules.playerId, args.player_id))
        .orderBy(desc(schema.savedRules.id));
      return {
        rules: rows.map((r) => ({
          id: r.id,
          name: r.name,
          arena: r.arena,
          team_size: r.teamSize,
          hero_rule: r.heroRule,
          custom_rules: r.customRules,
        })),
      };
    },
  }),

  deleteSavedRule: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      rule_id: z.number(),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false };
      await db
        .delete(schema.savedRules)
        .where(
          and(
            eq(schema.savedRules.id, args.rule_id),
            eq(schema.savedRules.playerId, args.player_id),
          ),
        );
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  myMatches: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({ matches: z.array(matchShape) }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { matches: [] };
      await sweepTimeoutsInternal(db).catch(() => 0);
      const rows = await db
        .select()
        .from(schema.matches)
        .orderBy(desc(schema.matches.id))
        .limit(100);
      const mine = rows.filter(
        (m) => m.playerAId === args.player_id || m.playerBId === args.player_id,
      );
      const out = [];
      for (const m of mine) out.push(await matchOut(ctx, db, m, args.player_id));
      return { matches: out };
    },
  }),

  getMatch: defineAction({
    request: z.object({
      match_id: z.number(),
      player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({
      match: matchShape.nullable(),
      chat: z.array(
        z.object({
          id: z.number(),
          sender_id: z.number(),
          sender_name: z.string(),
          sender_avatar_url: z.string(),
          sender_avatar_color: z.string(),
          text: z.string(),
          created_at: z.string(),
        }),
      ),
      result_unlocked: z.boolean(),
      unlock_at: z.string().nullable(),
      heroes: z.array(z.object({ name: z.string(), role: z.string(), banned: z.boolean() })),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      await sweepTimeoutsInternal(db).catch(() => 0);
      const s = await getSettingsMap(db);
      const viewer = args.player_id
        ? await playerBySession(db, args.player_id, args.session_token)
        : null;
      const heroesFor = (bannedRaw: string) =>
        HERO_NAMES.map((name) => ({
          name,
          role: HERO_CATALOG[name]!,
          banned: isHeroBanned(name, bannedRaw) || isHeroBanned(name, s["banned_heroes"] ?? ""),
        }));
      const rows = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.id, args.match_id))
        .limit(1);
      const m = rows[0];
      if (!m)
        return { match: null, chat: [], result_unlocked: false, unlock_at: null, heroes: heroesFor("") };
      const chat = await db
        .select()
        .from(schema.chatMessages)
        .where(eq(schema.chatMessages.matchId, m.id))
        .orderBy(schema.chatMessages.id)
        .limit(200);
      const chatPlayers = await db.select().from(schema.players);
      const chatAvatarMap = new Map<number, string>();
      const chatColorMap = new Map<number, string>();
      for (const cp of chatPlayers) { chatAvatarMap.set(cp.id, await blobUrlSafe(ctx, cp.avatarBlobKey ?? "")); chatColorMap.set(cp.id, cp.avatarColor); }
      const delay = numSetting(s, "result_delay_min", 5) * 60 * 1000;
      const unlockAt = m.startedAt ? new Date(m.startedAt.getTime() + delay) : null;
      return {
        heroes: heroesFor(m.bannedHeroes),
        match: await matchOut(ctx, db, m, viewer?.id ?? null),
        chat: chat.map((c) => ({
          id: c.id,
          sender_id: c.senderId,
          sender_name: c.senderName,
          sender_avatar_url: chatAvatarMap.get(c.senderId) ?? "",
          sender_avatar_color: chatColorMap.get(c.senderId) ?? "#7c3aed",
          text: c.text,
          created_at: iso(c.createdAt) ?? "",
        })),
        result_unlocked: unlockAt ? Date.now() >= unlockAt.getTime() : false,
        unlock_at: unlockAt ? unlockAt.toISOString() : null,
      };
    },
  }),

  sendChat: defineAction({
    request: z.object({
      match_id: z.number(),
      player_id: z.number(),
      session_token: z.string().default(""),
      text: z.string().min(1).max(500),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const p = await playerBySession(db, args.player_id, args.session_token);
      if (!p) return { ok: false };
      const mRows = await db.select().from(schema.matches)
        .where(eq(schema.matches.id, args.match_id)).limit(1);
      const mm = mRows[0];
      if (!mm || (mm.playerAId !== p.id && mm.playerBId !== p.id)) return { ok: false };
      const rl = await rateLimit(db, `chat:${args.player_id}`, 40, 60 * 1000);
      if (rl) return { ok: false };
      await db.insert(schema.chatMessages).values({
        matchId: args.match_id,
        senderId: args.player_id,
        senderName: p?.name ?? "",
        text: args.text.trim(),
      });
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  setReady: defineAction({
    request: z.object({
      match_id: z.number(),
      player_id: z.number(),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), started: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meReady = await playerBySession(db, args.player_id, args.session_token);
      if (!meReady) return { ok: false, error: AUTH_ERR, started: false };
      const rows = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.id, args.match_id))
        .limit(1);
      const m = rows[0];
      if (!m || m.status !== "ready_check")
        return { ok: false, error: "Match က ready အဆင့် မဟုတ်တော့ပါ", started: false };
      const isA = m.playerAId === args.player_id;
      const isB = m.playerBId === args.player_id;
      if (!isA && !isB) return { ok: false, error: "ဒီ match မှာ မပါဝင်ပါ", started: false };
      const aReady = isA ? 1 : m.aReady;
      const bReady = isB ? 1 : m.bReady;
      // TEAM READY GATE: in 2v2-5v5 every team member must be ready (Team
      // section) before the match may start — individual Ready is not enough.
      let gateNote = "";
      if (m.teamSize > 1) {
        const gA = await teamGateFor(db, m.playerAId, m.teamSize);
        const gB = await teamGateFor(db, m.playerBId, m.teamSize);
        if (!(gA && gB)) {
          gateNote = "အဖွဲ့လိုက် ပွဲမှာ Team section မှာ အဖွဲ့သား အားလုံး Ready ဖြစ်မှ ပွဲစနိုင်ပါတယ် — ကိုယ့် အဖွဲ့ကို အရင် ဖွဲ့ပြီး member အားလုံး Ready နှိပ်ခိုင်းပါ။";
          await db
            .update(schema.matches)
            .set({ aReady, bReady })
            .where(eq(schema.matches.id, m.id));
          ctx.invalidateQueries();
          return { ok: true, error: gateNote, started: false };
        }
      }
      const both = aReady === 1 && bReady === 1;
      await db
        .update(schema.matches)
        .set({
          aReady,
          bReady,
          status: both ? "live" : "ready_check",
          startedAt: both ? new Date() : m.startedAt,
        })
        .where(eq(schema.matches.id, m.id));
      const otherId = isA ? m.playerBId : m.playerAId;
      await notify(db, {
        playerId: otherId,
        type: "ready",
        title: "Opponent Ready ဖြစ်ပါပြီ ✅",
        body: `${m.code} — opponent ready နှိပ်ပြီးပါပြီ။`,
        matchId: m.id,
      });
      if (both) {
        for (const pid of [m.playerAId, m.playerBId]) {
          await notify(db, {
            playerId: pid,
            type: "ready",
            title: "Match စပါပြီ 🎮",
            body: `${m.code} စတင်ပါပြီ။ ရလဒ်ကို ပွဲစပြီး သတ်မှတ်ချိန်ကျော်မှ တင်နိုင်ပါတယ်။`,
            matchId: m.id,
          });
        }
      }
      ctx.invalidateQueries();
      return { ok: true, error: "", started: both };
    },
  }),

  pickHero: defineAction({
    request: z.object({
      match_id: z.number(),
      player_id: z.number(),
      session_token: z.string().default(""),
      hero: z.string().min(1).max(40),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), hero: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false, error: AUTH_ERR, hero: "" };
      const rows = await db.select().from(schema.matches)
        .where(eq(schema.matches.id, args.match_id)).limit(1);
      const m = rows[0];
      if (!m) return { ok: false, error: "Match မတွေ့ပါ", hero: "" };
      if (["completed", "cancelled"].includes(m.status))
        return { ok: false, error: "ပြီးဆုံးပြီး match မှာ hero ရွေးလို့ မရပါ", hero: "" };
      const isA = m.playerAId === me.id;
      const isB = m.playerBId === me.id;
      if (!isA && !isB) return { ok: false, error: "ဒီ match မှာ မပါဝင်ပါ", hero: "" };
      const s = await getSettingsMap(db);
      const bannedRaw = `${m.bannedHeroes},${s["banned_heroes"] ?? ""}`;
      let hero = args.hero.trim();
      // Random rule: the server assigns a random legal hero.
      if (m.heroRule === "random") {
        const legal = HERO_NAMES.filter((h) => !isHeroBanned(h, bannedRaw));
        hero = legal[Math.floor(Math.random() * legal.length)] ?? hero;
      }
      const role = HERO_CATALOG[hero];
      if (!role)
        return { ok: false, error: `"${hero}" ကို hero စာရင်းမှာ မတွေ့ပါ — စာရင်းထဲက hero ပဲ ရွေးပါ`, hero: "" };
      // HERO BAN ENFORCEMENT (server-side, not just display):
      if (isHeroBanned(hero, bannedRaw))
        return { ok: false, error: `🚫 ${hero} ကို admin က ban ထားပါတယ် — တခြား hero ရွေးပါ`, hero: "" };
      const roleRules = ["fighter", "mage", "assassin", "marksman", "tank", "support"];
      if (roleRules.includes(m.heroRule) && role !== m.heroRule)
        return { ok: false, error: `ဒီပွဲက ${m.heroRule} only ပါ — ${hero} က ${role} ဖြစ်နေပါတယ်`, hero: "" };
      const otherHero = isA ? m.bHero : m.aHero;
      if (m.heroRule === "same_hero" && otherHero && otherHero !== hero)
        return { ok: false, error: `Same Hero only — opponent က ${otherHero} ရွေးထားပါတယ်`, hero: "" };
      if (m.heroRule === "no_duplicate" && otherHero && otherHero === hero)
        return { ok: false, error: `No Duplicate — opponent က ${hero} ရွေးပြီးသားပါ`, hero: "" };
      await db.update(schema.matches)
        .set(isA ? { aHero: hero } : { bHero: hero })
        .where(eq(schema.matches.id, m.id));
      ctx.invalidateQueries();
      return { ok: true, error: "", hero };
    },
  }),

  submitResult: defineAction({
    request: z.object({
      match_id: z.number(),
      player_id: z.number(),
      session_token: z.string().default(""),
      result: z.enum(["win", "lose"]),
      evidence_base64: z.string().max(8_000_000).default(""),
      evidence_mime: z.string().default("image/png"),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), outcome: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      const rows = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.id, args.match_id))
        .limit(1);
      const m = rows[0];
      if (!m) return { ok: false, error: "Match မတွေ့ပါ", outcome: "" };
      const meSub = await playerBySession(db, args.player_id, args.session_token);
      if (!meSub) return { ok: false, error: AUTH_ERR, outcome: "" };
      if (m.status !== "live" && m.status !== "result_pending")
        return { ok: false, error: "ရလဒ် တင်လို့ မရသေးပါ", outcome: "" };
      if (!m.startedAt) return { ok: false, error: "Match မစသေးပါ", outcome: "" };
      const delay = numSetting(s, "result_delay_min", 5) * 60 * 1000;
      if (Date.now() - m.startedAt.getTime() < delay)
        return {
          ok: false,
          error: `ရလဒ်တင်ခွင့် မပွင့်သေးပါ — ပွဲစပြီး ${s["result_delay_min"]} မိနစ် ပြည့်မှ တင်နိုင်ပါတယ်`,
          outcome: "",
        };
      const isA = m.playerAId === args.player_id;
      const isB = m.playerBId === args.player_id;
      if (!isA && !isB) return { ok: false, error: "ဒီ match မှာ မပါဝင်ပါ", outcome: "" };
      const myHero = isA ? m.aHero : m.bHero;
      if (!myHero)
        return { ok: false, error: "အရင် Hero ရွေးပါ — ban စာရင်းထဲ မပါတဲ့၊ စည်းကမ်းနဲ့ ကိုက်တဲ့ hero ဖြစ်ရပါမယ်", outcome: "" };

      let evKey = isA ? m.aEvidence : m.bEvidence;
      if (args.evidence_base64) {
        try {
          const bytes = Buffer.from(args.evidence_base64, "base64");
          evKey = `evidence/${m.id}/${args.player_id}-${Date.now()}`;
          await ctx.blobs.put(evKey, bytes, { contentType: args.evidence_mime });
        } catch {
          evKey = isA ? m.aEvidence : m.bEvidence;
        }
      }
      // Write ONLY the submitter's own columns, then re-read: two players
      // submitting at the same moment must never overwrite each other.
      await db
        .update(schema.matches)
        .set(
          isA
            ? { aResult: args.result, aEvidence: evKey, status: "result_pending" }
            : { bResult: args.result, bEvidence: evKey, status: "result_pending" },
        )
        .where(eq(schema.matches.id, m.id));
      const freshRows = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.id, m.id))
        .limit(1);
      const fresh = freshRows[0] ?? m;
      const aResult = fresh.aResult;
      const bResult = fresh.bResult;

      if (aResult && bResult) {
        const updated = { ...m, aResult, bResult };
        if (
          (aResult === "win" && bResult === "lose") ||
          (aResult === "lose" && bResult === "win")
        ) {
          const winnerId = aResult === "win" ? m.playerAId : m.playerBId;
          await settleMatch(ctx, db, updated, winnerId);
          return { ok: true, error: "", outcome: "confirmed" };
        }
        await db
          .update(schema.matches)
          .set({ status: "disputed" })
          .where(eq(schema.matches.id, m.id));
        for (const pid of [m.playerAId, m.playerBId]) {
          await notify(db, {
            playerId: pid,
            type: "dispute",
            title: "Dispute ပွင့်ပါပြီ ⚠️",
            body: `${m.code} — နှစ်ဖက် ရလဒ် မတူပါ။ Admin စစ်ဆေးပါမယ်။ Coin တွေ lock ထားဆဲပါ။`,
            matchId: m.id,
          });
        }
        ctx.invalidateQueries();
        return { ok: true, error: "", outcome: "disputed" };
      }
      ctx.invalidateQueries();
      return { ok: true, error: "", outcome: "waiting_opponent" };
    },
  }),

  cancelMatch: defineAction({
    request: z.object({
      match_id: z.number(),
      player_id: z.number(),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const rows = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.id, args.match_id))
        .limit(1);
      const m = rows[0];
      if (!m) return { ok: false, error: "Match မတွေ့ပါ" };
      const meCan = await playerBySession(db, args.player_id, args.session_token);
      if (!meCan) return { ok: false, error: AUTH_ERR };
      if (m.status === "completed" || m.status === "cancelled")
        return { ok: false, error: "ပြီးဆုံးပြီး match ပါ" };
      if (m.playerAId !== args.player_id && m.playerBId !== args.player_id)
        return { ok: false, error: "ဒီ match မှာ မပါဝင်ပါ" };
      await refundMatch(db, m, "Match cancel — refund");
      // Reputation hit for the cancelling side when match had started.
      if (m.status === "live") {
        const p = await getPlayer(db, args.player_id);
        if (p) {
          await db
            .update(schema.players)
            .set({ reputation: Math.max(0, p.reputation - 5) })
            .where(eq(schema.players.id, p.id));
        }
      }
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  resolveDispute: defineAction({
    request: z.object({
      match_id: z.number(),
      winner_id: z.number(), // 0 = refund both
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "moderator"]);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR };
      const rows = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.id, args.match_id))
        .limit(1);
      const m = rows[0];
      if (!m || m.status !== "disputed")
        return { ok: false, error: "Dispute match မဟုတ်ပါ" };
      if (args.winner_id === 0) {
        await refundMatch(db, m, "Dispute — admin refund");
      } else {
        if (args.winner_id !== m.playerAId && args.winner_id !== m.playerBId)
          return { ok: false, error: "Winner က match ထဲက player ဖြစ်ရပါမယ်" };
        await settleMatch(ctx, db, m, args.winner_id);
      }
      await logAct(db, auth.actor.name, `Dispute ${m.code} resolved (winner: ${args.winner_id || "refund"})`);
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  listDisputes: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ matches: z.array(matchShape) }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "moderator"]);
      if (auth.error) return { matches: [] };
      const rows = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.status, "disputed"))
        .orderBy(desc(schema.matches.id));
      const out = [];
      for (const m of rows) out.push(await matchOut(ctx, db, m));
      return { matches: out };
    },
  }),

  getWallet: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({
      player: playerShape.nullable(),
      purchase_requests: z.array(purchaseReqShape),
      withdraw_requests: z.array(withdrawReqShape),
      transactions: z.array(
        z.object({
          id: z.number(),
          code: z.string(),
          type: z.string(),
          amount: z.number(),
          balance_after: z.number().nullable(),
          note: z.string(),
          created_at: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const p = await playerBySession(db, args.player_id, args.session_token);
      if (!p) return { player: null, purchase_requests: [], withdraw_requests: [], transactions: [] };
      await expireMoneyRequestsInternal(db).catch(() => 0);
      const reqs = await db
        .select()
        .from(schema.purchaseRequests)
        .where(eq(schema.purchaseRequests.playerId, args.player_id))
        .orderBy(desc(schema.purchaseRequests.id))
        .limit(50);
      const wreqs = await db
        .select()
        .from(schema.withdrawRequests)
        .where(eq(schema.withdrawRequests.playerId, args.player_id))
        .orderBy(desc(schema.withdrawRequests.id))
        .limit(50);
      const txns = await db
        .select()
        .from(schema.transactions)
        .where(eq(schema.transactions.playerId, args.player_id))
        .orderBy(desc(schema.transactions.id))
        .limit(100);
      const purchaseOutList = [];
      for (const r of reqs) purchaseOutList.push(await purchaseOut(ctx, r, p.name));
      return {
        player: p ? await playerOutAsync(ctx, p) : null,
        purchase_requests: purchaseOutList,
        withdraw_requests: wreqs.map((w) => withdrawOut(w, p.name)),
        transactions: txns.map((t) => ({
          id: t.id,
          code: t.code,
          type: t.type,
          amount: t.amount,
          balance_after: t.balanceAfter ?? null,
          note: t.note,
          created_at: iso(t.createdAt) ?? "",
        })),
      };
    },
  }),

  buyCoins: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      coins: z.number().int().min(1).max(10000000),
      label: z.string().max(80).default(""),
      proof_key: z.string().max(200).default(""),
    }),
    response: z.object({
      ok: z.boolean(),
      error: z.string(),
      txn_code: z.string(),
      request_id: z.number(),
      status: z.string(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const fail = (error: string) => ({ ok: false, error, txn_code: "", request_id: 0, status: "" });
      const s = await getSettingsMap(db);
      if (s["emergency"] === "1")
        return fail("Emergency mode — wallet transaction ယာယီပိတ်ထားပါတယ်");
      const p = await playerBySession(db, args.player_id, args.session_token);
      if (!p) return fail(AUTH_ERR);
      const rl = await rateLimit(db, `buy:${p.id}`, 20, 10 * 60 * 1000);
      if (rl) return fail(rl);
      await expireMoneyRequestsInternal(db).catch(() => 0);
      // Admin-configured top-up limits.
      const minCoins = numSetting(s, "topup_min_coins", 10);
      const maxCoins = numSetting(s, "topup_max_coins", 1000000);
      if (args.coins < minCoins)
        return fail(`အနည်းဆုံး ${minCoins.toLocaleString()} coins မှ စပြီး ဝယ်လို့ရပါတယ် (Minimum top-up is ${minCoins} coins)`);
      if (args.coins > maxCoins)
        return fail(`တစ်ကြိမ်ကို အများဆုံး ${maxCoins.toLocaleString()} coins ပဲ ဝယ်လို့ရပါတယ် (Maximum top-up is ${maxCoins} coins)`);
      // Payment proof: once the admin publishes any payment method, a
      // screenshot is REQUIRED. The proof blob key is content-addressed —
      // minted ONLY by uploadPurchaseProof as proofs/p<playerId>/<sha256>.<ext> —
      // so the hash embedded in the key was computed by the server itself.
      const paymentConfigured =
        (s["kpay_phone"] ?? "").trim() !== "" || (s["kpay_qr"] ?? "").trim() !== "" ||
        (s["wave_phone"] ?? "").trim() !== "" || (s["wave_qr"] ?? "").trim() !== "";
      const proofKey = (args.proof_key ?? "").trim();
      let proofHash = "";
      if (paymentConfigured && !proofKey)
        return fail("ငွေလွှဲပြီး screenshot တင်ရန် လိုပါတယ် — အရင် ငွေလွှဲ screenshot တင်ပြီးမှ တောင်းဆိုပါ (Payment screenshot is required)");
      if (proofKey) {
        const m = proofKey.match(new RegExp(`^proofs/p${p.id}/([0-9a-f]{64})\\.(jpg|png|webp)$`));
        if (!m) return fail("Screenshot အထောက်အထား မမှန်ပါ — ပုံကို ပြန်တင်ပြီး ထပ်ကြိုးစားပါ (Invalid payment proof)");
        const head = await ctx.blobs.head(proofKey).catch(() => null);
        if (!head) return fail("Screenshot မတွေ့ပါ — ပုံကို ပြန်တင်ပါ (Payment proof not found)");
        proofHash = m[1]!;
        // Duplicate guard: one screenshot funds exactly ONE request, ever —
        // any earlier request (any status, any player) with the same image
        // hash blocks a resubmission.
        const dup = await db
          .select()
          .from(schema.purchaseRequests)
          .where(eq(schema.purchaseRequests.proofHash, proofHash))
          .limit(1);
        if (dup.length > 0)
          return fail("ဒီ screenshot ကို သုံးပြီးသား ဖြစ်နေပါတယ် — screenshot တစ်ပုံကို တစ်ခါပဲ သုံးလို့ရပါတယ် (This screenshot has already been used)");
      }
      // Coin economy: a purchase is only a REQUEST. The balance changes later,
      // when the fixed Owner approves it in decidePurchase — a player can
      // never credit their own balance from here.
      const ins = await db
        .insert(schema.purchaseRequests)
        .values({
          playerId: p.id,
          coins: args.coins,
          label: args.label || `Coin ဝယ်ယူမှု ${args.coins} coins`,
          status: "pending",
          proofBlobKey: proofKey,
          proofHash,
        })
        .returning({ id: schema.purchaseRequests.id });
      const requestId = ins[0]?.id ?? 0;
      const ownerRow = (await db.select().from(schema.players))
        .find((x) => isFixedAdminEmail(x.email));
      if (ownerRow) {
        await notify(db, {
          playerId: ownerRow.id,
          type: "wallet",
          title: "🪙 Coin ဝယ်ယူမှု တောင်းဆိုချက် အသစ်",
          body: `${p.name} က ${args.coins} coins ဝယ်ဖို့ တောင်းဆိုထားပါတယ် — Admin မှာ အတည်ပြု/ပယ်ချ လုပ်ပါ။`,
        });
      }
      // Telegram: photo + one-tap decision buttons (fail-soft).
      if (telegramConfigured(s) && requestId > 0) {
        const caption =
          `🪙 Coin ဝယ်ယူမှု အသစ် #${requestId}\n` +
          `Player: ${p.name}\nCoins: ${args.coins.toLocaleString()}\n${args.label || ""}\n` +
          `Admin dashboard သို့မဟုတ် ဒီကပဲ ဆုံးဖြတ်နိုင်ပါတယ်။`;
        const kb = moneyKeyboard("pur", requestId);
        const bytes = proofKey ? await blobBytes(ctx, proofKey) : null;
        if (bytes) {
          const sent = await telegramSendPhoto(s, bytes, caption, kb);
          if (!sent.sent) await telegramSend(s, caption, kb);
        } else {
          await telegramSend(s, caption, kb);
        }
      }
      ctx.invalidateQueries();
      return { ok: true, error: "", txn_code: "", request_id: requestId, status: "pending" };
    },
  }),

  listPurchaseRequests: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ requests: z.array(purchaseReqShape) }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin"]);
      if (auth.error) return { requests: [] };
      await expireMoneyRequestsInternal(db).catch(() => 0);
      const rows = await db.select().from(schema.purchaseRequests)
        .orderBy(desc(schema.purchaseRequests.id)).limit(100);
      const all = await db.select().from(schema.players);
      const out = [];
      for (const r of rows)
        out.push(await purchaseOut(ctx, r, all.find((x) => x.id === r.playerId)?.name ?? "?"));
      return { requests: out };
    },
  }),

  decidePurchase: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      actor: z.string().default("admin"),
      session_token: z.string().default(""),
      request_id: z.number(),
      decision: z.enum(["approve", "reject"]),
      reason: z.string().max(300).default(""),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), txn_code: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const fail = (error: string) => ({ ok: false, error, txn_code: "" });
      // Wallet decisions are Owner-only (fixed admin Gmail) + confirmation,
      // plus a real TOTP check when the owner has 2FA enabled. The state
      // machine itself lives in decidePurchaseInternal (shared with the
      // Telegram callback path, which authenticates by chat id instead).
      const auth = await requireOwner(db, args.actor_player_id, args.session_token);
      if (auth.error || !auth.actor) return fail(auth.error ?? AUTH_ERR);
      if (!args.confirmed) return fail("Coin အတည်ပြုရန် security confirmation လိုပါတယ်");
      const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
      if (err2fa) return fail(err2fa);
      await expireMoneyRequestsInternal(db).catch(() => 0);
      return decidePurchaseInternal(ctx, db, args.request_id, args.decision, auth.actor.name, args.reason ?? "");
    },
  }),

  uploadPurchaseProof: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      image_base64: z.string().max(12_000_000).default(""),
      image_mime: z.string().max(60).default(""),
    }),
    response: z.object({
      ok: z.boolean(),
      error: z.string(),
      proof_key: z.string(),
      proof_url: z.string(),
      proof_hash: z.string(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const fail = (error: string) => ({ ok: false, error, proof_key: "", proof_url: "", proof_hash: "" });
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return fail(AUTH_ERR);
      const rl = await rateLimit(db, `proof:${me.id}`, 30, 60 * 60 * 1000);
      if (rl) return fail(rl);
      const v = validateImage(args.image_base64, args.image_mime);
      if (v.error || !v.bytes) return fail(v.error || "ပုံ မမှန်ပါ");
      // Content-addressed: the SHA-256 of the image IS the file name, minted
      // here by the server — buyCoins can trust the hash in the key.
      const hash = await sha256Hex(v.bytes);
      const key = `proofs/p${me.id}/${hash}.${v.ext}`;
      const existing = await ctx.blobs.head(key).catch(() => null);
      if (!existing) {
        try {
          await ctx.blobs.put(key, v.bytes, { contentType: v.mime });
        } catch {
          return fail("ပုံ သိမ်းလို့ မရပါ — ပြန်ကြိုးစားပါ");
        }
      }
      return { ok: true, error: "", proof_key: key, proof_url: await blobUrlSafe(ctx, key), proof_hash: hash };
    },
  }),

  requestWithdraw: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      coins: z.number().int().min(1).max(10000000),
      method: z.enum(["kpay", "wave"]).default("kpay"),
      payout_phone: z.string().max(30).default(""),
    }),
    response: z.object({
      ok: z.boolean(),
      error: z.string(),
      request_id: z.number(),
      gross_kyat: z.number(),
      fee_kyat: z.number(),
      net_kyat: z.number(),
      txn_code: z.string(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const fail = (error: string) => ({ ok: false, error, request_id: 0, gross_kyat: 0, fee_kyat: 0, net_kyat: 0, txn_code: "" });
      const s = await getSettingsMap(db);
      if (s["emergency"] === "1")
        return fail("Emergency mode — wallet transaction ယာယီပိတ်ထားပါတယ်");
      const p = await playerBySession(db, args.player_id, args.session_token);
      if (!p) return fail(AUTH_ERR);
      const rl = await rateLimit(db, `withdraw:${p.id}`, 20, 10 * 60 * 1000);
      if (rl) return fail(rl);
      await expireMoneyRequestsInternal(db).catch(() => 0);
      const phone = (args.payout_phone ?? "").trim();
      if (!phone) return fail("ငွေလက်ခံမယ့် ဖုန်းနံပါတ် ထည့်ပါ (Payout phone number is required)");
      const rate = Math.floor(numSetting(s, "coin_rate_kyat", 0));
      if (rate <= 0)
        return fail("ငွေထုတ် စနစ် မဖွင့်ရသေးပါ — admin က လဲနှုန်း မသတ်မှတ်ရသေးပါ (Withdrawals are not configured yet)");
      const minCoins = numSetting(s, "withdraw_min_coins", 1000);
      if (args.coins < minCoins)
        return fail(`အနည်းဆုံး ${minCoins.toLocaleString()} coins မှ ထုတ်လို့ရပါတယ် (Minimum withdrawal is ${minCoins} coins)`);
      if (p.balance < args.coins)
        return fail(`Balance မလုံလောက်ပါ — သုံးနိုင်သော coin: ${p.balance.toLocaleString()} (Insufficient balance)`);
      // Daily cap: today's pending + paid withdrawals (UTC day) count
      // against it; rejected/expired ones do not.
      const dailyCap = numSetting(s, "withdraw_max_coins_per_day", 100000);
      if (dailyCap > 0) {
        const dayStart = new Date();
        dayStart.setUTCHours(0, 0, 0, 0);
        const mine = await db
          .select()
          .from(schema.withdrawRequests)
          .where(eq(schema.withdrawRequests.playerId, p.id));
        const usedToday = mine
          .filter((w) => (w.status === "pending" || w.status === "paid") && w.createdAt >= dayStart)
          .reduce((a, w) => a + w.coins, 0);
        if (usedToday + args.coins > dailyCap)
          return fail(`ဒီနေ့ ထုတ်နိုင်တဲ့ ပမာဏ ကျော်နေပါတယ် — တစ်နေ့ အများဆုံး ${dailyCap.toLocaleString()} coins (ဒီနေ့ သုံးပြီး: ${usedToday.toLocaleString()})`);
      }
      const feePct = Math.min(100, Math.max(0, numSetting(s, "withdraw_fee_percent", 0)));
      const gross = args.coins * rate;
      const fee = Math.floor((gross * feePct) / 100);
      const net = gross - fee;
      // Coins are deducted (locked) up-front; a reject/expiry refunds them
      // exactly, a "paid" decision keeps them deducted.
      const code = uid("TXN");
      const nb = p.balance - args.coins;
      await db.update(schema.players).set({ balance: nb }).where(eq(schema.players.id, p.id));
      await addTxn(db, {
        playerId: p.id,
        type: "withdraw",
        amount: -args.coins,
        balanceAfter: nb,
        note: `Withdraw request — ${args.coins} coins → ${net.toLocaleString()} ကျပ် (${args.method === "wave" ? "Wave" : "KPay"} ${phone})`,
      });
      const ins = await db
        .insert(schema.withdrawRequests)
        .values({
          playerId: p.id,
          coins: args.coins,
          rateKyat: rate,
          grossKyat: gross,
          feePercent: feePct,
          feeKyat: fee,
          netKyat: net,
          method: args.method,
          payoutPhone: phone,
          status: "pending",
          txnCode: code,
        })
        .returning({ id: schema.withdrawRequests.id });
      const requestId = ins[0]?.id ?? 0;
      const ownerRow = (await db.select().from(schema.players))
        .find((x) => isFixedAdminEmail(x.email));
      if (ownerRow) {
        await notify(db, {
          playerId: ownerRow.id,
          type: "wallet",
          title: "💸 ငွေထုတ် တောင်းဆိုချက် အသစ်",
          body: `${p.name} က ${args.coins} coins (${net.toLocaleString()} ကျပ်) ထုတ်ဖို့ တောင်းဆိုထားပါတယ် — Admin မှာ စစ်ပြီး ပေးပါ။`,
        });
      }
      if (telegramConfigured(s) && requestId > 0) {
        const text =
          `💸 ငွေထုတ် တောင်းဆိုချက် #${requestId}\n` +
          `Player: ${p.name}\nCoins: ${args.coins.toLocaleString()} × ${rate} = ${gross.toLocaleString()} ကျပ်\n` +
          `Fee: ${fee.toLocaleString()} ကျပ် (${feePct}%)\nပေးရန်: ${net.toLocaleString()} ကျပ်\n` +
          `${args.method === "wave" ? "Wave Pay" : "KBZPay"}: ${phone}\nငွေလွှဲပြီးမှ Paid နှိပ်ပါ။`;
        await telegramSend(s, text, moneyKeyboard("wd", requestId));
      }
      ctx.invalidateQueries();
      return { ok: true, error: "", request_id: requestId, gross_kyat: gross, fee_kyat: fee, net_kyat: net, txn_code: code };
    },
  }),

  listWithdrawRequests: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ requests: z.array(withdrawReqShape) }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin"]);
      if (auth.error) return { requests: [] };
      await expireMoneyRequestsInternal(db).catch(() => 0);
      const rows = await db.select().from(schema.withdrawRequests)
        .orderBy(desc(schema.withdrawRequests.id)).limit(100);
      const all = await db.select().from(schema.players);
      return {
        requests: rows.map((w) => withdrawOut(w, all.find((x) => x.id === w.playerId)?.name ?? "?")),
      };
    },
  }),

  decideWithdraw: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      actor: z.string().default("admin"),
      session_token: z.string().default(""),
      request_id: z.number(),
      decision: z.enum(["paid", "reject"]),
      reason: z.string().max(300).default(""),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      // Same gates as decidePurchase: Owner-only + confirmation + TOTP.
      const auth = await requireOwner(db, args.actor_player_id, args.session_token);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR };
      if (!args.confirmed) return { ok: false, error: "ငွေထုတ် ဆုံးဖြတ်ရန် security confirmation လိုပါတယ်" };
      const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
      if (err2fa) return { ok: false, error: err2fa };
      await expireMoneyRequestsInternal(db).catch(() => 0);
      return decideWithdrawInternal(ctx, db, args.request_id, args.decision, auth.actor.name, args.reason ?? "");
    },
  }),

  uploadPaymentQr: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      method: z.enum(["kpay", "wave"]),
      image_base64: z.string().max(12_000_000).default(""),
      image_mime: z.string().max(60).default(""),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), qr_url: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const fail = (error: string) => ({ ok: false, error, qr_url: "" });
      const auth = await requireOwner(db, args.actor_player_id, args.session_token);
      if (auth.error || !auth.actor) return fail(auth.error ?? AUTH_ERR);
      if (!args.confirmed) return fail("QR ပြောင်းရန် security confirmation လိုပါတယ်");
      const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
      if (err2fa) return fail(err2fa);
      const v = validateImage(args.image_base64, args.image_mime);
      if (v.error || !v.bytes) return fail(v.error || "ပုံ မမှန်ပါ");
      const s = await getSettingsMap(db);
      const keyField = args.method === "wave" ? "wave_qr_key" : "kpay_qr_key";
      const urlField = args.method === "wave" ? "wave_qr" : "kpay_qr";
      const oldKey = s[keyField] ?? "";
      const key = `payments/${args.method}-qr-${Date.now()}.${v.ext}`;
      try {
        await ctx.blobs.put(key, v.bytes, { contentType: v.mime });
      } catch {
        return fail("QR ပုံ သိမ်းလို့ မရပါ — ပြန်ကြိုးစားပါ");
      }
      const url = await blobUrlSafe(ctx, key);
      await setSetting(db, urlField, url);
      await setSetting(db, keyField, key);
      if (oldKey && oldKey !== key) {
        try { await ctx.blobs.delete(oldKey); } catch { /* best-effort */ }
      }
      await logAct(db, auth.actor.name, `Payment QR updated (${args.method})`);
      ctx.invalidateQueries();
      return { ok: true, error: "", qr_url: url };
    },
  }),

  deletePaymentQr: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      method: z.enum(["kpay", "wave"]),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireOwner(db, args.actor_player_id, args.session_token);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR };
      if (!args.confirmed) return { ok: false, error: "QR ဖျက်ရန် security confirmation လိုပါတယ်" };
      const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
      if (err2fa) return { ok: false, error: err2fa };
      const s = await getSettingsMap(db);
      const keyField = args.method === "wave" ? "wave_qr_key" : "kpay_qr_key";
      const urlField = args.method === "wave" ? "wave_qr" : "kpay_qr";
      const oldKey = s[keyField] ?? "";
      await setSetting(db, urlField, "");
      await setSetting(db, keyField, "");
      if (oldKey) {
        try { await ctx.blobs.delete(oldKey); } catch { /* best-effort */ }
      }
      await logAct(db, auth.actor.name, `Payment QR removed (${args.method})`);
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  telegramCallback: defineAction({
    request: z.object({
      update: z.record(z.string(), z.unknown()),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), handled: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const ignore = { ok: true, error: "", handled: false };
      const s = await getSettingsMap(db);
      const token = (s["telegram_bot_token"] ?? "").trim();
      const chatId = (s["telegram_chat_id"] ?? "").trim();
      if (!token || !chatId) return { ok: false, error: "Telegram မချိတ်ရသေးပါ", handled: false };
      const update = (args.update ?? {}) as Record<string, unknown>;
      const cq = update["callback_query"] as
        | {
            id?: unknown;
            data?: unknown;
            from?: { first_name?: unknown };
            message?: {
              message_id?: unknown;
              text?: unknown;
              caption?: unknown;
              chat?: { id?: unknown };
            };
          }
        | undefined;
      if (!cq) return ignore;
      // The ONLY authorization on this path: the tap must come from the
      // configured admin chat. Anything else is rejected with no effects.
      const fromChat = cq.message?.chat?.id;
      if (fromChat === undefined || String(fromChat) !== chatId)
        return { ok: false, error: "Telegram chat မကိုက်ပါ — ဆုံးဖြတ်ခွင့် မရှိပါ", handled: false };
      const data = typeof cq.data === "string" ? cq.data : "";
      const m = data.match(/^(pur|wd):(\d+):(approve|reject|paid)$/);
      if (!m) return ignore;
      const kind = m[1]!;
      const reqId = Number(m[2]);
      const rawDecision = m[3]!;
      if (kind === "pur" && rawDecision === "paid") return ignore;
      if (kind === "wd" && rawDecision === "approve") return ignore;
      await expireMoneyRequestsInternal(db).catch(() => 0);
      const fromName =
        typeof cq.from?.first_name === "string" && cq.from.first_name
          ? cq.from.first_name
          : "Telegram admin";
      const decidedBy = `Telegram (${fromName})`;
      let outcome: string;
      let already = false;
      if (kind === "pur") {
        const r = await decidePurchaseInternal(
          ctx, db, reqId, rawDecision as "approve" | "reject", decidedBy, "",
        );
        outcome = r.ok
          ? rawDecision === "approve" ? "✅ အတည်ပြုပြီး — coin credit ဝင်ပြီးပါပြီ" : "❌ ပယ်ချပြီးပါပြီ"
          : r.error;
        already = !r.ok && r.error.includes("ဆုံးဖြတ်ပြီးသား");
      } else {
        const r = await decideWithdrawInternal(
          ctx, db, reqId, rawDecision as "paid" | "reject", decidedBy, "",
        );
        outcome = r.ok
          ? rawDecision === "paid" ? "💸 Paid မှတ်ပြီးပါပြီ" : "❌ ပယ်ချပြီး coin ပြန်အမ်းပြီးပါပြီ"
          : r.error;
        already = !r.ok && r.error.includes("ဆုံးဖြတ်ပြီးသား");
      }
      // Best-effort Bot API feedback: answer the tap + stamp the outcome
      // onto the original message. Failures here never roll back the DB.
      const cqId = typeof cq.id === "string" ? cq.id : "";
      if (cqId) {
        await telegramApi(token, "answerCallbackQuery", {
          callback_query_id: cqId,
          text: already ? "ဆုံးဖြတ်ပြီးသား ဖြစ်နေပါတယ်" : outcome.slice(0, 190),
        });
      }
      const msgId = cq.message?.message_id;
      const origText =
        (typeof cq.message?.text === "string" && cq.message.text) ||
        (typeof cq.message?.caption === "string" && cq.message.caption) ||
        "";
      if (typeof msgId === "number" && origText) {
        // Photo messages need editMessageCaption; plain ones editMessageText.
        const isPhoto = typeof cq.message?.text !== "string";
        await telegramApi(token, isPhoto ? "editMessageCaption" : "editMessageText", {
          chat_id: chatId,
          message_id: msgId,
          ...(isPhoto
            ? { caption: `${origText.slice(0, 900)}\n\n— ${outcome} (by ${fromName})` }
            : { text: `${origText.slice(0, 3700)}\n\n— ${outcome} (by ${fromName})` }),
        });
      }
      await logAct(db, decidedBy, `Telegram callback ${data} → ${outcome.slice(0, 80)}`);
      return { ok: true, error: "", handled: true };
    },
  }),

  sweepTimeouts: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), acted: z.number(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "moderator", "support"]);
      if (auth.error) return { ok: false, acted: 0, error: auth.error };
      const acted = await sweepTimeoutsInternal(db);
      ctx.invalidateQueries();
      return { ok: true, acted, error: "" };
    },
  }),

  feePreview: defineAction({
    request: z.object({ arena: z.enum(["1v1", "classic", "brawl"]), stake: z.number().int().min(0) }),
    response: z.object({
      pot: z.number(),
      fee_amount: z.number(),
      fee_label: z.string(),
      payout: z.number(),
      referral_example: z.number(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      const pot = args.stake * 2;
      const { fee, label } = feeFor(s, args.arena, pot);
      const pctX100 = Number(s["referral_percent_x100"] ?? "100") || 0;
      return {
        pot,
        fee_amount: fee,
        fee_label: label,
        payout: pot - fee,
        referral_example: Math.floor((fee * pctX100) / 10000),
      };
    },
  }),

  listTournaments: defineAction({
    request: z.object({}),
    response: z.object({
      tournaments: z.array(
        z.object({
          id: z.number(),
          name: z.string(),
          prize: z.number(),
          rules: z.string(),
          team_size: z.number(),
          entry_type: z.string(),
          starts_at: z.string(),
          status: z.string(),
          banner_idx: z.number(),
          champion: z.string(),
          entries: z.array(z.object({ id: z.number(), entrant_name: z.string(), image_url: z.string() })),
          bracket: z.array(
            z.object({
              id: z.number(),
              round: z.number(),
              label_a: z.string(),
              label_b: z.string(),
              winner: z.string(),
              image_a_url: z.string(),
              image_b_url: z.string(),
              winner_image_url: z.string(),
            }),
          ),
        }),
      ),
    }),
    async handler(ctx) {
      const db = ctx.db<typeof schema>();
      const rows = await db
        .select()
        .from(schema.tournaments)
        .orderBy(desc(schema.tournaments.id));
      const allPlayersT = await db.select().from(schema.players);
      const allTeamsT = await db.select().from(schema.teams);
      const entrantImage = async (name: string, playerId: number | null): Promise<string> => {
        if (playerId) { const pl = allPlayersT.find((x)=>x.id===playerId); if (pl) return blobUrlSafe(ctx, pl.avatarBlobKey ?? ""); }
        const tm = allTeamsT.find((x)=>x.name===name); if (tm) return blobUrlSafe(ctx, tm.logoBlobKey ?? "");
        const pl2 = allPlayersT.find((x)=>x.name===name); if (pl2) return blobUrlSafe(ctx, pl2.avatarBlobKey ?? "");
        return "";
      };
      const out = [];
      for (const t of rows) {
        const entries = await db
          .select()
          .from(schema.tournamentEntries)
          .where(eq(schema.tournamentEntries.tournamentId, t.id))
          .orderBy(schema.tournamentEntries.id);
        const bracket = await db
          .select()
          .from(schema.bracketMatches)
          .where(eq(schema.bracketMatches.tournamentId, t.id))
          .orderBy(schema.bracketMatches.id);
        out.push({
          id: t.id,
          name: t.name,
          prize: t.prize,
          rules: t.rules,
          team_size: t.teamSize,
          entry_type: t.entryType,
          starts_at: t.startsAt.toISOString(),
          status: t.status,
          banner_idx: t.bannerIdx,
          champion: t.champion,
          entries: await (async () => { const arr = []; for (const e of entries) arr.push({ id: e.id, entrant_name: e.entrantName, image_url: await entrantImage(e.entrantName, e.playerId ?? null) }); return arr; })(),
          bracket: await (async () => { const arr = []; for (const b of bracket) arr.push({ id: b.id, round: b.round, label_a: b.labelA, label_b: b.labelB, winner: b.winner, image_a_url: await entrantImage(b.labelA, null), image_b_url: await entrantImage(b.labelB, null), winner_image_url: b.winner ? await entrantImage(b.winner, null) : "" }); return arr; })(),
        });
      }
      return { tournaments: out };
    },
  }),

  createTournament: defineAction({
    request: z.object({
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      name: z.string().min(1).max(80),
      prize: z.number().int().min(0),
      rules: z.string().max(1000).default(""),
      team_size: z.number().int().min(1).max(5),
      entry_type: z.enum(["solo", "team"]),
      starts_at: z.string(),
      banner_idx: z.number().int().min(0).max(2).default(0),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin"]);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR };
      const starts = new Date(args.starts_at);
      if (Number.isNaN(starts.getTime()))
        return { ok: false, error: "Start time မှားနေပါတယ်" };
      await db.insert(schema.tournaments).values({
        name: args.name,
        prize: args.prize,
        rules: args.rules,
        teamSize: args.team_size,
        entryType: args.entry_type,
        startsAt: starts,
        bannerIdx: args.banner_idx,
      });
      await notify(db, {
        playerId: null,
        type: "tournament",
        title: "Tournament အသစ် 🏆",
        body: `${args.name} — ဆုကြေး ${args.prize} coins။ စာရင်းသွင်းနိုင်ပါပြီ။`,
      });
      await logAct(db, auth.actor.name, `Tournament created: ${args.name}`);
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  registerTournament: defineAction({
    request: z.object({
      tournament_id: z.number(),
      player_id: z.number().nullable().default(null),
      session_token: z.string().default(""),
      entrant_name: z.string().min(1).max(60),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      if (args.player_id) {
        const meT = await playerBySession(db, args.player_id, args.session_token);
        if (!meT) return { ok: false, error: AUTH_ERR };
      }
      const t = await db
        .select()
        .from(schema.tournaments)
        .where(eq(schema.tournaments.id, args.tournament_id))
        .limit(1);
      if (!t[0]) return { ok: false, error: "Tournament မတွေ့ပါ" };
      if (t[0].status === "completed") return { ok: false, error: "ပြီးဆုံးသွားပါပြီ" };
      await db.insert(schema.tournamentEntries).values({
        tournamentId: args.tournament_id,
        entrantName: args.entrant_name.trim(),
        playerId: args.player_id,
      });
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  generateBracket: defineAction({
    request: z.object({
      tournament_id: z.number(),
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin"]);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR };
      const entries = await db
        .select()
        .from(schema.tournamentEntries)
        .where(eq(schema.tournamentEntries.tournamentId, args.tournament_id))
        .orderBy(schema.tournamentEntries.id);
      if (entries.length < 2) return { ok: false, error: "အနည်းဆုံး ၂ ဦး စာရင်းသွင်းရပါမယ်" };
      await db
        .delete(schema.bracketMatches)
        .where(eq(schema.bracketMatches.tournamentId, args.tournament_id));
      for (let i = 0; i < entries.length; i += 2) {
        await db.insert(schema.bracketMatches).values({
          tournamentId: args.tournament_id,
          round: 1,
          labelA: entries[i]?.entrantName ?? "TBD",
          labelB: entries[i + 1]?.entrantName ?? "BYE",
        });
      }
      await db
        .update(schema.tournaments)
        .set({ status: "live" })
        .where(eq(schema.tournaments.id, args.tournament_id));
      await logAct(db, auth.actor.name, `Bracket generated for tournament #${args.tournament_id}`);
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  setBracketWinner: defineAction({
    request: z.object({
      bracket_id: z.number(),
      winner: z.string().min(1).max(60),
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin"]);
      if (auth.error || !auth.actor) return { ok: false };
      const rows = await db
        .select()
        .from(schema.bracketMatches)
        .where(eq(schema.bracketMatches.id, args.bracket_id))
        .limit(1);
      const b = rows[0];
      if (!b) return { ok: true };
      await db
        .update(schema.bracketMatches)
        .set({ winner: args.winner })
        .where(eq(schema.bracketMatches.id, args.bracket_id));
      // If all matches of the final round have winners and it is a single match, set champion.
      const all = await db
        .select()
        .from(schema.bracketMatches)
        .where(eq(schema.bracketMatches.tournamentId, b.tournamentId));
      const maxRound = Math.max(...all.map((x) => x.round));
      const finals = all.filter((x) => x.round === maxRound);
      if (finals.length === 1) {
        await db
          .update(schema.tournaments)
          .set({ champion: args.winner, status: "completed" })
          .where(eq(schema.tournaments.id, b.tournamentId));
        await notify(db, {
          playerId: null,
          type: "tournament",
          title: "Tournament Champion 👑",
          body: `${args.winner} က champion ဖြစ်သွားပါပြီ။`,
        });
      } else if (finals.every((x) => x.winner)) {
        const nextRound = maxRound + 1;
        const winners = finals.map((x) => x.winner);
        for (let i = 0; i < winners.length; i += 2) {
          await db.insert(schema.bracketMatches).values({
            tournamentId: b.tournamentId,
            round: nextRound,
            labelA: winners[i] ?? "TBD",
            labelB: winners[i + 1] ?? "BYE",
          });
        }
      }
      await logAct(db, auth.actor.name, `Bracket winner set: ${args.winner}`);
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  leaderboard: defineAction({
    request: z.object({
      category: z.enum(["top_player", "most_wins", "win_streak", "top_team", "champion"]),
      period: z.enum(["daily", "weekly", "monthly", "all_time"]),
    }),
    response: z.object({
      rows: z.array(
        z.object({
          rank_no: z.number(),
          name: z.string(),
          detail: z.string(),
          value: z.number(),
          color: z.string(),
          image_url: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const now = Date.now();
      const windowMs =
        args.period === "daily"
          ? 24 * 3600 * 1000
          : args.period === "weekly"
            ? 7 * 24 * 3600 * 1000
            : args.period === "monthly"
              ? 30 * 24 * 3600 * 1000
              : Number.POSITIVE_INFINITY;
      const since = new Date(now - windowMs);
      const completed = await db
        .select()
        .from(schema.matches)
        .where(eq(schema.matches.status, "completed"));
      const inWindow =
        windowMs === Number.POSITIVE_INFINITY
          ? completed
          : completed.filter((m) => !!m.completedAt && m.completedAt >= since);
      const playersAll = await db.select().from(schema.players);
      const nameOf = (id: number | null) =>
        playersAll.find((p) => p.id === id)?.name ?? "?";
      const colorOf = (id: number | null) =>
        playersAll.find((p) => p.id === id)?.avatarColor ?? "#7c3aed";
      const avatarUrlMap = new Map<number, string>();
      for (const pl of playersAll) avatarUrlMap.set(pl.id, await blobUrlSafe(ctx, pl.avatarBlobKey ?? ""));
      const allTeamsForLb = await db.select().from(schema.teams);
      const teamLogoMap = new Map<string, string>();
      for (const tm of allTeamsForLb) teamLogoMap.set(tm.name, await blobUrlSafe(ctx, tm.logoBlobKey ?? ""));
      const playerAvatarByName = new Map<string, string>();
      for (const pl of playersAll) playerAvatarByName.set(pl.name, avatarUrlMap.get(pl.id) ?? "");
      type Row = { rank_no: number; name: string; detail: string; value: number; color: string; image_url: string };
      let rows: Row[] = [];
      if (args.category === "top_team") {
        const allTeams = await db
          .select()
          .from(schema.teams)
          .orderBy(desc(schema.teams.wins));
        // Period filter applies here too: only teams formed in the window.
        const teamRows =
          windowMs === Number.POSITIVE_INFINITY
            ? allTeams
            : allTeams.filter((t) => t.createdAt >= since);
        rows = teamRows.map((t, i) => ({
          rank_no: i + 1,
          name: t.name,
          detail: "Team wins",
          value: t.wins,
          color: t.color,
          image_url: teamLogoMap.get(t.name) ?? "",
        }));
      } else if (args.category === "champion") {
        const tours = await db
          .select()
          .from(schema.tournaments)
          .where(eq(schema.tournaments.status, "completed"));
        rows = tours
          .filter((t) => t.champion)
          // Period filter: tournament must have started inside the window.
          .filter((t) => windowMs === Number.POSITIVE_INFINITY || t.startsAt >= since)
          .map((t, i) => ({
            rank_no: i + 1,
            name: t.champion,
            detail: t.name,
            value: t.prize,
            color: "#f59e0b",
            image_url: teamLogoMap.get(t.champion) ?? playerAvatarByName.get(t.champion) ?? "",
          }));
      } else {
        const winsMap = new Map<number, number>();
        for (const m of inWindow) {
          if (m.winnerId) winsMap.set(m.winnerId, (winsMap.get(m.winnerId) ?? 0) + 1);
        }
        if (args.category === "win_streak") {
          // Best streak COMPUTED FROM MATCHES INSIDE THE WINDOW (the old
          // code ignored the period and used all-time best_streak).
          const ordered = [...inWindow].sort(
            (a, b) => (a.completedAt?.getTime() ?? 0) - (b.completedAt?.getTime() ?? 0),
          );
          const cur = new Map<number, number>();
          const best = new Map<number, number>();
          for (const m of ordered) {
            for (const pid of [m.playerAId, m.playerBId]) {
              if (pid === m.winnerId) {
                const c = (cur.get(pid) ?? 0) + 1;
                cur.set(pid, c);
                best.set(pid, Math.max(best.get(pid) ?? 0, c));
              } else {
                cur.set(pid, 0);
              }
            }
          }
          rows = [...best.entries()]
            .sort((a, b) => b[1] - a[1])
            .slice(0, 20)
            .map(([pid, v], i) => {
              const p = playersAll.find((x) => x.id === pid);
              return {
                rank_no: i + 1,
                name: nameOf(pid),
                detail: `${p?.rank ?? ""} • best streak in period`,
                value: v,
                color: colorOf(pid),
                image_url: avatarUrlMap.get(pid) ?? "",
              };
            });
        } else {
          rows = [...winsMap.entries()]
            .sort((a, b) => b[1] - a[1])
            .slice(0, 20)
            .map(([pid, w], i) => {
              const p = playersAll.find((x) => x.id === pid);
              return {
                rank_no: i + 1,
                name: nameOf(pid),
                detail: `${p?.rank ?? ""} • ${p ? Math.round((p.wins / Math.max(1, p.wins + p.losses)) * 100) : 0}% win rate`,
                value: w,
                color: colorOf(pid),
                image_url: avatarUrlMap.get(pid) ?? "",
              };
            });
          if (args.category === "top_player") {
            rows = rows.map((r) => ({ ...r }));
          }
        }
      }
      return { rows };
    },
  }),

  matchHistory: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({ matches: z.array(matchShape) }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meHist = await playerBySession(db, args.player_id, args.session_token);
      if (!meHist) return { matches: [] };
      const rows = await db
        .select()
        .from(schema.matches)
        .orderBy(desc(schema.matches.id))
        .limit(200);
      const mine = rows.filter(
        (m) =>
          (m.playerAId === args.player_id || m.playerBId === args.player_id) &&
          ["completed", "cancelled", "disputed"].includes(m.status),
      );
      const out = [];
      for (const m of mine) out.push(await matchOut(ctx, db, m, args.player_id));
      return { matches: out };
    },
  }),

  listTeams: defineAction({
    request: z.object({}),
    response: z.object({
      teams: z.array(
        z.object({
          id: z.number(),
          name: z.string(),
          color: z.string(),
          logo_url: z.string(),
          leader_id: z.number(),
          leader_name: z.string(),
          wins: z.number(),
          members: z.array(
            z.object({
              member_id: z.number(),
              player: playerShape,
              ready: z.boolean(),
            }),
          ),
        }),
      ),
    }),
    async handler(ctx) {
      const db = ctx.db<typeof schema>();
      const rows = await db.select().from(schema.teams).orderBy(desc(schema.teams.id));
      const all = await db.select().from(schema.players);
      const out = [];
      for (const t of rows) {
        const members = await db
          .select()
          .from(schema.teamMembers)
          .where(eq(schema.teamMembers.teamId, t.id));
        const memberOut = [];
        for (const mm of members) {
          const p = all.find((x) => x.id === mm.playerId);
          if (p) memberOut.push({ member_id: mm.id, player: await playerPublicOutAsync(ctx, p, null), ready: mm.ready === 1 });
        }
        out.push({
          id: t.id,
          name: t.name,
          color: t.color,
          logo_url: await blobUrlSafe(ctx, t.logoBlobKey ?? ""),
          leader_id: t.leaderId,
          leader_name: all.find((p) => p.id === t.leaderId)?.name ?? "?",
          wins: t.wins,
          members: memberOut,
        });
      }
      return { teams: out };
    },
  }),

  createTeam: defineAction({
    request: z.object({
      leader_id: z.number(),
      session_token: z.string().default(""),
      name: z.string().min(1).max(40),
      color: z.string().default("#0ea5e9"),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meTeam = await playerBySession(db, args.leader_id, args.session_token);
      if (!meTeam) return { ok: false, error: AUTH_ERR };
      const ins = await db
        .insert(schema.teams)
        .values({ name: args.name.trim(), color: args.color, leaderId: args.leader_id })
        .returning({ id: schema.teams.id });
      const tid = ins[0]?.id ?? 0;
      if (tid) {
        await db.insert(schema.teamMembers).values({
          teamId: tid,
          playerId: args.leader_id,
          ready: 0,
        });
      }
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  teamMemberAction: defineAction({
    request: z.object({
      action: z.enum(["add", "remove", "ready"]),
      team_id: z.number(),
      player_id: z.number(),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      ready: z.boolean().default(false),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      // The actor is the session player (injected by the client). Ready can
      // only be set for oneself; add/remove only by the team leader or self.
      const actorTm = await playerBySession(db, args.actor_player_id, args.session_token);
      if (!actorTm) return { ok: false, error: AUTH_ERR };
      const teamRows = await db.select().from(schema.teams)
        .where(eq(schema.teams.id, args.team_id)).limit(1);
      const teamRow = teamRows[0];
      if (!teamRow) return { ok: false, error: "Team မတွေ့ပါ" };
      if (args.action === "ready" && args.player_id !== actorTm.id)
        return { ok: false, error: "ကိုယ့် ready ပဲ ပြောင်းနိုင်ပါတယ်" };
      if (args.action !== "ready" && teamRow.leaderId !== actorTm.id && args.player_id !== actorTm.id)
        return { ok: false, error: "Team leader သို့မဟုတ် ကိုယ်တိုင်ပဲ လုပ်နိုင်ပါတယ်" };
      if (args.action === "add") {
        const existing = await db
          .select()
          .from(schema.teamMembers)
          .where(
            and(
              eq(schema.teamMembers.teamId, args.team_id),
              eq(schema.teamMembers.playerId, args.player_id),
            ),
          );
        if (existing.length === 0) {
          const count = await db
            .select()
            .from(schema.teamMembers)
            .where(eq(schema.teamMembers.teamId, args.team_id));
          if (count.length >= 5) return { ok: false, error: "Team ပြည့်ပါပြီ (အများဆုံး ၅ ယောက်)" };
          await db.insert(schema.teamMembers).values({
            teamId: args.team_id,
            playerId: args.player_id,
          });
        }
      } else if (args.action === "remove") {
        await db
          .delete(schema.teamMembers)
          .where(
            and(
              eq(schema.teamMembers.teamId, args.team_id),
              eq(schema.teamMembers.playerId, args.player_id),
            ),
          );
      } else {
        await db
          .update(schema.teamMembers)
          .set({ ready: args.ready ? 1 : 0 })
          .where(
            and(
              eq(schema.teamMembers.teamId, args.team_id),
              eq(schema.teamMembers.playerId, args.player_id),
            ),
          );
      }
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  friendsAction: defineAction({
    request: z.object({
      action: z.enum(["list", "add", "remove"]),
      player_id: z.number(),
      session_token: z.string().default(""),
      friend_id: z.number().default(0),
    }),
    response: z.object({ friends: z.array(playerShape), ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meFr = await playerBySession(db, args.player_id, args.session_token);
      if (!meFr) return { friends: [], ok: false };
      if (args.action === "add" && args.friend_id && args.friend_id !== args.player_id) {
        const ex = await db
          .select()
          .from(schema.friends)
          .where(
            and(
              eq(schema.friends.playerId, args.player_id),
              eq(schema.friends.friendId, args.friend_id),
            ),
          );
        if (ex.length === 0) {
          await db.insert(schema.friends).values({
            playerId: args.player_id,
            friendId: args.friend_id,
          });
        }
      }
      if (args.action === "remove" && args.friend_id) {
        await db
          .delete(schema.friends)
          .where(
            and(
              eq(schema.friends.playerId, args.player_id),
              eq(schema.friends.friendId, args.friend_id),
            ),
          );
      }
      const rows = await db
        .select()
        .from(schema.friends)
        .where(eq(schema.friends.playerId, args.player_id));
      const all = await db.select().from(schema.players);
      const friendRows = rows
        .map((r) => all.find((p) => p.id === r.friendId))
        .filter((p): p is PlayerRow => !!p);
      const friends = await playersPublicOutAsync(ctx, friendRows, args.player_id);
      ctx.invalidateQueries();
      return { friends, ok: true };
    },
  }),

  getReferral: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({
      code: z.string(),
      invited: z.array(
        z.object({
          id: z.number(),
          name: z.string(),
          wins: z.number(),
          joined_at: z.string(),
        }),
      ),
      commissions: z.array(
        z.object({
          id: z.number(),
          referred_name: z.string(),
          match_code: z.string(),
          fee_amount: z.number(),
          percent: z.number(),
          commission: z.number(),
          status: z.string(),
          created_at: z.string(),
        }),
      ),
      total_earned: z.number(),
      pending_total: z.number(),
      percent: z.number(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) {
        return { code: "", invited: [], commissions: [], total_earned: 0, pending_total: 0, percent: (Number(s["referral_percent_x100"] ?? "100") || 0) / 100 };
      }
      const all = await db.select().from(schema.players);
      const invited = all.filter((p) => p.referredById === args.player_id);
      const comms = await db
        .select()
        .from(schema.referralCommissions)
        .where(eq(schema.referralCommissions.referrerId, args.player_id))
        .orderBy(desc(schema.referralCommissions.id))
        .limit(100);
      const matchRows = await db.select().from(schema.matches);
      const codeOf = (mid: number) => matchRows.find((m) => m.id === mid)?.code ?? `#${mid}`;
      return {
        code: me?.referralCode ?? "",
        invited: invited.map((p) => ({
          id: p.id,
          name: p.name,
          wins: p.wins,
          joined_at: iso(p.createdAt) ?? "",
        })),
        commissions: comms.map((c) => ({
          id: c.id,
          referred_name: all.find((p) => p.id === c.referredId)?.name ?? "?",
          match_code: codeOf(c.matchId),
          fee_amount: c.feeAmount,
          percent: c.percent / 100,
          commission: c.commission,
          status: c.status,
          created_at: iso(c.createdAt) ?? "",
        })),
        total_earned: comms
          .filter((c) => c.status === "completed")
          .reduce((a, c) => a + c.commission, 0),
        pending_total: comms
          .filter((c) => c.status === "pending")
          .reduce((a, c) => a + c.commission, 0),
        percent: (Number(s["referral_percent_x100"] ?? "100") || 0) / 100,
      };
    },
  }),

  listNotifications: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({
      notifications: z.array(
        z.object({
          id: z.number(),
          type: z.string(),
          title: z.string(),
          body: z.string(),
          match_id: z.number().nullable(),
          is_read: z.boolean(),
          created_at: z.string(),
        }),
      ),
      unread: z.number(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meN = await playerBySession(db, args.player_id, args.session_token);
      if (!meN) return { notifications: [], unread: 0 };
      const rows = await db
        .select()
        .from(schema.notifications)
        .orderBy(desc(schema.notifications.id))
        .limit(100);
      const mine = rows.filter(
        (n) => n.playerId === args.player_id || n.playerId === null,
      );
      const reads = await db.select().from(schema.notificationReads)
        .where(eq(schema.notificationReads.playerId, args.player_id));
      const readSet = new Set(reads.map((r) => r.notificationId));
      const isReadOf = (n: (typeof mine)[number]) =>
        n.playerId === null ? readSet.has(n.id) : n.isRead === 1;
      return {
        notifications: mine.map((n) => ({
          id: n.id,
          type: n.type,
          title: n.title,
          body: n.body,
          match_id: n.matchId ?? null,
          is_read: isReadOf(n),
          created_at: iso(n.createdAt) ?? "",
        })),
        unread: mine.filter((n) => !isReadOf(n)).length,
      };
    },
  }),

  markNotificationsRead: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meN = await playerBySession(db, args.player_id, args.session_token);
      if (!meN) return { ok: false };
      // Only THIS player's personal notifications are flipped…
      await db
        .update(schema.notifications)
        .set({ isRead: 1 })
        .where(eq(schema.notifications.playerId, args.player_id));
      // …and broadcasts are recorded per-user in notification_reads, so one
      // player marking read can never touch another player's notifications.
      const allN = await db.select().from(schema.notifications);
      const mine = allN.filter((n) => n.playerId === null);
      const existing = await db.select().from(schema.notificationReads)
        .where(eq(schema.notificationReads.playerId, args.player_id));
      const have = new Set(existing.map((r) => r.notificationId));
      for (const n of mine) {
        if (!have.has(n.id)) {
          await db.insert(schema.notificationReads).values({
            notificationId: n.id,
            playerId: args.player_id,
          });
        }
      }
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  createReport: defineAction({
    request: z.object({
      reporter_id: z.number().nullable().default(null),
      session_token: z.string().default(""),
      reporter_name: z.string().default(""),
      target_name: z.string().min(1).max(60),
      reason: z.string().min(1).max(60),
      detail: z.string().max(1000).default(""),
    }),
    response: z.object({ ok: z.boolean(), case_code: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      let reporterName = "";
      let reporterId: number | null = null;
      if (args.reporter_id) {
        const meR = await playerBySession(db, args.reporter_id, args.session_token);
        if (!meR) return { ok: false, case_code: "" };
        reporterName = meR.name;
        reporterId = meR.id;
      }
      const rl = await rateLimit(db, `report:${reporterId ?? "anon"}`, 10, 60 * 60 * 1000);
      if (rl) return { ok: false, case_code: "" };
      const caseCode = uid("CASE");
      await db.insert(schema.reports).values({
        caseCode,
        reporterId,
        reporterName,
        targetName: args.target_name,
        reason: args.reason,
        detail: args.detail,
      });
      ctx.invalidateQueries();
      return { ok: true, case_code: caseCode };
    },
  }),

  listReports: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({
      reports: z.array(
        z.object({
          id: z.number(),
          case_code: z.string(),
          reporter_name: z.string(),
          target_name: z.string(),
          reason: z.string(),
          detail: z.string(),
          status: z.string(),
          action_taken: z.string(),
          created_at: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "moderator"]);
      if (auth.error) return { reports: [] };
      const rows = await db
        .select()
        .from(schema.reports)
        .orderBy(desc(schema.reports.id))
        .limit(100);
      return {
        reports: rows.map((r) => ({
          id: r.id,
          case_code: r.caseCode,
          reporter_name: r.reporterName,
          target_name: r.targetName,
          reason: r.reason,
          detail: r.detail,
          status: r.status,
          action_taken: r.actionTaken,
          created_at: iso(r.createdAt) ?? "",
        })),
      };
    },
  }),

  resolveReport: defineAction({
    request: z.object({
      report_id: z.number(),
      action: z.enum(["none", "warning", "temp_ban", "perm_ban"]),
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "moderator"]);
      if (auth.error || !auth.actor) return { ok: false };
      const rows = await db
        .select()
        .from(schema.reports)
        .where(eq(schema.reports.id, args.report_id))
        .limit(1);
      const r = rows[0];
      if (!r) return { ok: true };
      const label =
        args.action === "warning"
          ? "Warning ပေးပြီး"
          : args.action === "temp_ban"
            ? "Temporary ban"
            : args.action === "perm_ban"
              ? "Permanent ban"
              : "လုပ်ဆောင်ချက် မရှိ";
      await db
        .update(schema.reports)
        .set({ status: "resolved", actionTaken: label })
        .where(eq(schema.reports.id, args.report_id));
      if (args.action !== "none") {
        const all = await db.select().from(schema.players);
        const target = all.find(
          (p) => p.name.toLowerCase() === r.targetName.toLowerCase(),
        );
        if (target) {
          await db
            .update(schema.players)
            .set({
              status:
                args.action === "warning"
                  ? "warned"
                  : args.action === "temp_ban"
                    ? "temp_banned"
                    : "perm_banned",
              reputation: Math.max(
                0,
                target.reputation - (args.action === "warning" ? 10 : 25),
              ),
            })
            .where(eq(schema.players.id, target.id));
          await notify(db, {
            playerId: target.id,
            type: "report",
            title: "Admin ဆုံးဖြတ်ချက်",
            body: `Case ${r.caseCode}: ${label}`,
          });
        }
      }
      await logAct(db, auth.actor.name, `Report ${r.caseCode} resolved: ${label}`);
      ctx.invalidateQueries();
      return { ok: true };
    },
  }),

  ticketsAction: defineAction({
    request: z.object({
      action: z.enum(["list", "create", "reply"]),
      player_id: z.number().nullable().default(null),
      session_token: z.string().default(""),
      actor_player_id: z.number().default(0),
      name: z.string().default(""),
      topic: z.string().default(""),
      message: z.string().default(""),
      ticket_id: z.number().default(0),
      reply: z.string().default(""),
    }),
    response: z.object({
      ok: z.boolean(),
      tickets: z.array(
        z.object({
          id: z.number(),
          name: z.string(),
          topic: z.string(),
          message: z.string(),
          status: z.string(),
          reply: z.string(),
          created_at: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      // "Admin view" = list with player_id null. That now requires a real
      // support/admin session; a plain player can only see their own tickets.
      const wantsAdminView = args.action !== "create" && args.player_id == null;
      let adminViewer: PlayerRow | null = null;
      if (wantsAdminView) {
        const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "support", "moderator"]);
        if (auth.error || !auth.actor) return { ok: false, tickets: [] };
        adminViewer = auth.actor;
      }
      if (args.action === "create") {
        if (args.player_id) {
          const meTk = await playerBySession(db, args.player_id, args.session_token);
          if (!meTk) return { ok: false, tickets: [] };
        }
        const rl = await rateLimit(db, `ticket:${args.player_id ?? "anon"}`, 10, 60 * 60 * 1000);
        if (rl) return { ok: false, tickets: [] };
        await db.insert(schema.tickets).values({
          playerId: args.player_id,
          name: args.name,
          topic: args.topic,
          message: args.message,
        });
      } else if (args.action === "reply" && args.ticket_id) {
        // Ticket replies are Support work (owner/admin can too).
        const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "support"]);
        if (auth.error) return { ok: false, tickets: [] };
        await db
          .update(schema.tickets)
          .set({ reply: args.reply, status: "answered" })
          .where(eq(schema.tickets.id, args.ticket_id));
      } else if (args.action === "list" && args.player_id != null) {
        const meTk = await playerBySession(db, args.player_id, args.session_token);
        if (!meTk) return { ok: false, tickets: [] };
      }
      void adminViewer;
      const rows = await db
        .select()
        .from(schema.tickets)
        .orderBy(desc(schema.tickets.id))
        .limit(100);
      const mine =
        args.player_id != null
          ? rows.filter((t) => t.playerId === args.player_id)
          : rows;
      ctx.invalidateQueries();
      return {
        ok: true,
        tickets: (args.action === "list" && args.player_id == null ? rows : mine).map(
          (t) => ({
            id: t.id,
            name: t.name,
            topic: t.topic,
            message: t.message,
            status: t.status,
            reply: t.reply,
            created_at: iso(t.createdAt) ?? "",
          }),
        ),
      };
    },
  }),

  getAdmin: defineAction({
    request: z.object({
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({
      settings: settingsShape,
      health: z.object({
        total_players: z.number(),
        online_players: z.number(),
        active_matches: z.number(),
        pending_disputes: z.number(),
        pending_reports: z.number(),
        open_tickets: z.number(),
        total_transactions: z.number(),
        total_fees: z.number(),
        db_status: z.string(),
      }),
      money: z.object({
        topups_today_count: z.number(),
        topups_today_coins: z.number(),
        withdrawals_today_count: z.number(),
        withdrawals_today_kyat: z.number(),
        fees_today_coins: z.number(),
        pending_topups: z.number(),
        pending_withdrawals: z.number(),
      }),
      activity: z.array(
        z.object({ id: z.number(), actor: z.string(), action: z.string(), created_at: z.string() }),
      ),
      players: z.array(playerShape),
      suspicious_players: z.array(playerShape),
      terms_history: z.array(
        z.object({
          id: z.number(),
          kind: z.string(),
          version: z.string(),
          content: z.string(),
          actor: z.string(),
          created_at: z.string(),
        }),
      ),
      backups: z.array(
        z.object({
          id: z.number(),
          code: z.string(),
          size_bytes: z.number(),
          actor: z.string(),
          created_at: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "moderator", "support"]);
      if (auth.error) {
        const emptySettings = settingsOut(await getSettingsMap(db));
        return {
          settings: emptySettings,
          health: { total_players: 0, online_players: 0, active_matches: 0, pending_disputes: 0, pending_reports: 0, open_tickets: 0, total_transactions: 0, total_fees: 0, db_status: "FORBIDDEN" },
          money: { topups_today_count: 0, topups_today_coins: 0, withdrawals_today_count: 0, withdrawals_today_kyat: 0, fees_today_coins: 0, pending_topups: 0, pending_withdrawals: 0 },
          activity: [], players: [], suspicious_players: [], terms_history: [], backups: [],
        };
      }
      const s = await getSettingsMap(db);
      const all = await db.select().from(schema.players);
      const cutoff = new Date(Date.now() - 10 * 60 * 1000);
      const matchRows = await db.select().from(schema.matches);
      const reportRows = await db
        .select()
        .from(schema.reports)
        .where(eq(schema.reports.status, "open"));
      const ticketRows = await db
        .select()
        .from(schema.tickets)
        .where(eq(schema.tickets.status, "open"));
      const txnRows = await db.select().from(schema.transactions);
      const act = await db
        .select()
        .from(schema.activityLog)
        .orderBy(desc(schema.activityLog.id))
        .limit(50);
      const termsHist = await db
        .select()
        .from(schema.termsVersions)
        .orderBy(desc(schema.termsVersions.id))
        .limit(30);
      const backupRows = await db
        .select()
        .from(schema.backupSnapshots)
        .orderBy(desc(schema.backupSnapshots.id))
        .limit(20);
      return {
        settings: settingsOut(s),
        health: {
          total_players: all.length,
          online_players: all.filter((p) => p.lastSeenAt >= cutoff).length,
          active_matches: matchRows.filter((m) =>
            ["ready_check", "live", "result_pending", "disputed"].includes(m.status),
          ).length,
          pending_disputes: matchRows.filter((m) => m.status === "disputed").length,
          pending_reports: reportRows.length,
          open_tickets: ticketRows.length,
          total_transactions: txnRows.length,
          total_fees: txnRows
            .filter((t) => t.type === "platform_fee")
            .reduce((a, t) => a + t.amount, 0),
          db_status: "OK",
        },
        money: await (async () => {
          // Today's money summary (UTC day). Top-ups are coin-denominated
          // (kyat actually received is off-platform); fees come from the
          // platform_fee ledger rows written at match settlement.
          const dayStart = new Date();
          dayStart.setUTCHours(0, 0, 0, 0);
          const purchases = await db.select().from(schema.purchaseRequests);
          const withdrawals = await db.select().from(schema.withdrawRequests);
          const approvedToday = purchases.filter(
            (r) => r.status === "approved" && r.decidedAt && r.decidedAt >= dayStart,
          );
          const paidToday = withdrawals.filter(
            (w) => w.status === "paid" && w.decidedAt && w.decidedAt >= dayStart,
          );
          return {
            topups_today_count: approvedToday.length,
            topups_today_coins: approvedToday.reduce((a, r) => a + r.coins, 0),
            withdrawals_today_count: paidToday.length,
            withdrawals_today_kyat: paidToday.reduce((a, w) => a + w.netKyat, 0),
            fees_today_coins: txnRows
              .filter((t) => t.type === "platform_fee" && t.createdAt >= dayStart)
              .reduce((a, t) => a + t.amount, 0),
            pending_topups: purchases.filter((r) => r.status === "pending").length,
            pending_withdrawals: withdrawals.filter((w) => w.status === "pending").length,
          };
        })(),
        activity: act.map((a) => ({
          id: a.id,
          actor: a.actor,
          action: a.action,
          created_at: iso(a.createdAt) ?? "",
        })),
        players: await (async () => { const arr = []; for (const pl of all) arr.push({ ...(await playerOutAsync(ctx, pl)), role: effectiveRole(pl) }); return arr; })(),
        suspicious_players: await (async () => { const arr = []; for (const pl of all.filter((p) => p.suspicious === 1)) arr.push({ ...(await playerOutAsync(ctx, pl)), role: effectiveRole(pl) }); return arr; })(),
        terms_history: termsHist.map((t) => ({
          id: t.id,
          kind: t.kind,
          version: t.version,
          content: t.content,
          actor: t.actor,
          created_at: iso(t.createdAt) ?? "",
        })),
        backups: backupRows.map((b) => ({
          id: b.id,
          code: b.code,
          size_bytes: b.sizeBytes,
          actor: b.actor,
          created_at: iso(b.createdAt) ?? "",
        })),
      };
    },
  }),

  updateSettings: defineAction({
    request: z.object({
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
      values: z.record(z.string(), z.string()),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      // ALL settings are Owner-only, and Owner means the fixed admin Gmail —
      // verified from the DB via the session, never from the client actor.
      // The fixed admin email itself can never be written into settings.
      if (Object.keys(args.values).some((k) => ["admin_email", "owner_email", "fixed_admin_email"].includes(k)))
        return { ok: false, error: "Admin Gmail (nnominalhuman@gmail.com) က အသေ သတ်မှတ်ထားတာပါ — settings မှာ ပြောင်းလို့ မရပါ" };
      const before = await getSettingsMap(db);
      const valueKeys = Object.keys(args.values);
      const googleKeys = ["google_client_id", "google_enabled"];
      const touchesGoogle = valueKeys.some((k) => googleKeys.includes(k));
      const googleKeysOnly =
        valueKeys.length > 0 && valueKeys.every((k) => googleKeys.includes(k));
      const allPlayersForSetup = await db.select().from(schema.players);
      const ownerExists = allPlayersForSetup.some((x) => isFixedAdminEmail(x.email));

      /* ---- Google setup window, NARROWED (no Owner exists yet) ----
       * While no fixed-owner account exists, a signed-in player may submit
       * a Google Client ID — but it is stored ONLY as PENDING. It does NOT
       * activate Google login and does NOT touch the active client ID.
       * The pending ID becomes active only when googleLogin verifies a
       * token whose aud is this ID AND whose email is the fixed admin
       * Gmail; any other email rejects and clears the pending setup.
       * The first pending setup locks: nobody else can overwrite it for
       * 24h. Once an Owner exists this whole branch is unreachable —
       * Google keys become Owner-only like every other setting. */
      if (touchesGoogle && !ownerExists) {
        if (!googleKeysOnly)
          return { ok: false, error: "Owner မရှိသေးခင် setup window က Google Client ID တစ်ခုတည်းအတွက်ပါ — တခြား setting (fee/banner/…) ရောပြောင်းလို့ မရပါ" };
        // A valid login session (the setup token) is mandatory — anonymous
        // or forged calls can never write integration keys.
        const meSetup = await playerBySession(db, args.actor_player_id, args.session_token);
        if (!meSetup)
          return { ok: false, error: "Setup token (login session) မပါဘဲ integration key ပြောင်းလို့ မရပါ — အရင် ပုံမှန် အကောင့်နဲ့ login ဝင်ပါ" };
        const newId = (args.values["google_client_id"] ?? "").trim();
        if (!newId)
          return { ok: false, error: "Google Client ID ထည့်ပါ" };
        if (!/^[A-Za-z0-9\-_]+\.apps\.googleusercontent\.com$/.test(newId))
          return { ok: false, error: "Client ID ပုံစံ မမှန်ပါ — …apps.googleusercontent.com နဲ့ ဆုံးရပါမယ်" };
        const gs = googleState(before);
        if (gs.state === "pending" && gs.pendingId && !pendingSetupExpired(before)) {
          if (gs.pendingId === newId) {
            await logAct(db, meSetup.name, "Google setup pending re-saved (same Client ID)");
            return { ok: true, error: "" };
          }
          await logAct(db, meSetup.name, `Google setup OVERWRITE attempt rejected — pending locked by ${before["google_setup_by_name"] ?? "?"}`);
          return { ok: false, error: "Pending setup တစ်ခု ရှိနေပြီးသားပါ — ပထမ တင်ထားတဲ့ Client ID ကို တခြားသူ ပြောင်းလို့ မရပါ။ Owner က fixed Gmail နဲ့ ဝင်ပြီး အတည်ပြုရပါမယ် (မဟုတ်ရင် ၂၄ နာရီ ကြာမှ သက်တမ်းကုန်ပါမယ်)။" };
        }
        await setSetting(db, "google_pending_client_id", newId);
        await setSetting(db, "google_setup_state", "pending");
        await setSetting(db, "google_setup_by_id", String(meSetup.id));
        await setSetting(db, "google_setup_by_name", meSetup.name);
        await setSetting(db, "google_setup_at", new Date().toISOString());
        await logAct(db, meSetup.name, `Google setup PENDING created (client …${newId.slice(-12)}) — Owner (fixed Gmail) confirmation required; Google login NOT active yet`);
        ctx.invalidateQueries();
        return { ok: true, error: "" };
      }

      const auth = await requireOwner(db, args.actor_player_id, args.session_token);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR };
      const actorName = auth.actor.name;
      const critical = [
        "fee_1v1_type", "fee_1v1_value", "fee_classic_type", "fee_classic_value",
        "fee_brawl_type", "fee_brawl_value", "referral_percent_x100", "packages",
        "emergency", "maintenance", "telegram_bot_token", "telegram_chat_id",
        "google_client_id", "google_enabled",
        "coin_rate_kyat", "withdraw_fee_percent", "withdraw_min_coins",
        "withdraw_max_coins_per_day", "topup_min_coins", "topup_max_coins",
        "kpay_phone", "wave_phone",
      ];
      const touchesCritical = Object.keys(args.values).some((k) => critical.includes(k));
      if (touchesCritical && !args.confirmed)
        return { ok: false, error: "အရေးကြီး setting ပြောင်းရန် security confirmation လိုပါတယ်" };
      if (touchesCritical) {
        const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
        if (err2fa) return { ok: false, error: err2fa };
      }
      for (const [k, v] of Object.entries(args.values)) {
        if (!(k in DEFAULT_SETTINGS) && k !== "announcement_my" && k !== "announcement_en")
          continue;
        // Never wipe a stored secret with an empty value from a stale form.
        if ((k === "telegram_bot_token" || k === "google_client_id") && v.trim() === "")
          continue;
        await db
          .insert(schema.settings)
          .values({ key: k, value: v })
          .onConflictDoUpdate({
            target: schema.settings.key,
            set: { value: v },
          });
      }
      // An Owner saving Google keys confirms the setup: any pending state
      // is cleared and the setup window stays closed from now on.
      if (touchesGoogle) {
        await setSetting(db, "google_setup_state", "confirmed");
        await setSetting(db, "google_pending_client_id", "");
        await setSetting(db, "google_setup_by_id", "");
        await setSetting(db, "google_setup_by_name", actorName);
        await setSetting(db, "google_setup_at", new Date().toISOString());
        await logAct(db, actorName, "Google setup CONFIRMED by Owner — setup window closed");
      }
      // Terms / rules version history (started matches keep their old version).
      if (
        args.values["terms_version"] &&
        args.values["terms_version"] !== before["terms_version"]
      ) {
        await db.insert(schema.termsVersions).values({
          kind: "terms",
          version: args.values["terms_version"],
          content: args.values["terms_text"] ?? before["terms_text"] ?? "",
          actor: actorName,
        });
      }
      if (
        args.values["rules_version"] &&
        args.values["rules_version"] !== before["rules_version"]
      ) {
        await db.insert(schema.termsVersions).values({
          kind: "rules",
          version: args.values["rules_version"],
          content: args.values["rules_text"] ?? before["rules_text"] ?? "",
          actor: actorName,
        });
      }
      const changed = Object.keys(args.values).join(", ");
      await logAct(db, actorName, `Settings updated: ${changed}`);
      if (args.values["announcement_my"] || args.values["announcement_en"]) {
        const body = args.values["announcement_my"] || args.values["announcement_en"] || "";
        await notify(db, {
          playerId: null,
          type: "announcement",
          title: "ကြေညာချက် အသစ် 📢",
          body,
        });
        const after = await getSettingsMap(db);
        if (body) await telegramSend(after, `📢 MLBB Platform ကြေညာချက်\n${body}`);
      }
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  adminPlayerAction: defineAction({
    request: z.object({
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      player_id: z.number(),
      action: z.enum(["warn", "temp_ban", "perm_ban", "unban", "adjust_coins", "set_role", "flag_suspicious", "clear_suspicious"]),
      amount: z.number().default(0),
      role: z.string().default("player"),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const p = await getPlayer(db, args.player_id);
      if (!p) return { ok: false, error: "Player မတွေ့ပါ" };
      // Role split: money + role changes are Owner(fixed Gmail)-only;
      // moderation actions need owner/admin/moderator — all from DB session.
      const ownerOnly = ["perm_ban", "adjust_coins", "set_role"].includes(args.action);
      const auth = ownerOnly
        ? await requireOwner(db, args.actor_player_id, args.session_token)
        : await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin", "moderator"]);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR };
      const actorName = auth.actor.name;
      if (isFixedAdminEmail(p.email) && args.action !== "clear_suspicious")
        return { ok: false, error: "Fixed Owner အကောင့်ကို ပြင်/ ban လို့ မရပါ" };
      if (
        (args.action === "perm_ban" || args.action === "adjust_coins" || args.action === "set_role") &&
        !args.confirmed
      )
        return { ok: false, error: "Security confirmation လိုပါတယ်" };
      if (args.action === "perm_ban" || args.action === "adjust_coins" || args.action === "set_role") {
        const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
        if (err2fa) return { ok: false, error: err2fa };
      }
      if (args.action === "warn") {
        await db.update(schema.players).set({ status: "warned" }).where(eq(schema.players.id, p.id));
      } else if (args.action === "temp_ban") {
        await db.update(schema.players).set({ status: "temp_banned" }).where(eq(schema.players.id, p.id));
      } else if (args.action === "perm_ban") {
        await db.update(schema.players).set({ status: "perm_banned" }).where(eq(schema.players.id, p.id));
      } else if (args.action === "unban") {
        await db.update(schema.players).set({ status: "active" }).where(eq(schema.players.id, p.id));
      } else if (args.action === "adjust_coins") {
        const nb = p.balance + args.amount;
        await db.update(schema.players).set({ balance: nb }).where(eq(schema.players.id, p.id));
        await addTxn(db, {
          playerId: p.id,
          type: "admin_adjust",
          amount: args.amount,
          balanceAfter: nb,
          note: `Admin adjustment by ${actorName}`,
        });
      } else if (args.action === "set_role") {
        // Owner role can only ever belong to the fixed admin Gmail.
        if (args.role === "owner" && !isFixedAdminEmail(p.email))
          return { ok: false, error: "Owner role ကို fixed Admin Gmail (nnominalhuman@gmail.com) အကောင့်ပဲ ရနိုင်ပါတယ်" };
        const role = ["player", "owner", "admin", "moderator", "support"].includes(args.role)
          ? (args.role as "player" | "owner" | "admin" | "moderator" | "support")
          : "player";
        await db.update(schema.players).set({ role }).where(eq(schema.players.id, p.id));
      } else if (args.action === "flag_suspicious") {
        await db
          .update(schema.players)
          .set({ suspicious: 1, suspiciousReason: "Admin က ကိုယ်တိုင် flag လုပ်ထားပါတယ် — review လိုပါတယ်" })
          .where(eq(schema.players.id, p.id));
      } else if (args.action === "clear_suspicious") {
        await db
          .update(schema.players)
          .set({ suspicious: 0, suspiciousReason: "" })
          .where(eq(schema.players.id, p.id));
      }
      await logAct(db, actorName, `Player ${p.name}: ${args.action}${args.action === "adjust_coins" ? ` ${args.amount}` : ""}`);
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  telegramPost: defineAction({
    request: z.object({
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      text: z.string().min(1).max(1000),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), telegram_sent: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin"]);
      if (auth.error || !auth.actor)
        return { ok: false, error: auth.error ?? AUTH_ERR, telegram_sent: false };
      if (!args.confirmed)
        return { ok: false, error: "Telegram announcement ပို့ရန် confirmation လိုပါတယ်", telegram_sent: false };
      const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
      if (err2fa) return { ok: false, error: err2fa, telegram_sent: false };
      const s = await getSettingsMap(db);
      const tg = await telegramSend(s, `📢 MLBB Platform\n${args.text}`);
      await notify(db, {
        playerId: null,
        type: "announcement",
        title: "ကြေညာချက် 📢",
        body: args.text,
      });
      await logAct(
        db,
        auth.actor.name,
        `Announcement posted (telegram: ${tg.sent ? "sent" : "not configured/failed"}): ${args.text.slice(0, 60)}`,
      );
      ctx.invalidateQueries();
      return {
        ok: true,
        error: tg.sent ? "" : `In-app ကြေညာချက် တင်ပြီးပါပြီ။ Telegram: ${tg.error}`,
        telegram_sent: tg.sent,
      };
    },
  }),

  telegramTest: defineAction({
    request: z.object({
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      mode: z.enum(["test", "stats"]).default("test"),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), configured: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const auth = await requireRole(db, args.actor_player_id, args.session_token, ["owner", "admin"]);
      if (auth.error || !auth.actor) return { ok: false, error: auth.error ?? AUTH_ERR, configured: false };
      const s = await getSettingsMap(db);
      const configured =
        (s["telegram_bot_token"] ?? "").trim() !== "" && (s["telegram_chat_id"] ?? "").trim() !== "";
      if (!configured)
        return { ok: false, error: "မချိတ်ရသေး — Bot Token နဲ့ Chat ID အရင် ထည့်ပြီး သိမ်းပါ", configured: false };
      let text = "✅ MLBB Platform — Telegram bot test message အောင်မြင်ပါတယ်။";
      if (args.mode === "stats") {
        const all = await db.select().from(schema.players);
        const cutoff = new Date(Date.now() - 10 * 60 * 1000);
        const matchRows = await db.select().from(schema.matches);
        const reportRows = await db
          .select()
          .from(schema.reports)
          .where(eq(schema.reports.status, "open"));
        text = [
          "📊 MLBB Platform Stats",
          `Online: ${all.filter((p) => p.lastSeenAt >= cutoff).length} / Players: ${all.length}`,
          `Active matches: ${matchRows.filter((m) => ["ready_check", "live", "result_pending", "disputed"].includes(m.status)).length}`,
          `Pending reports: ${reportRows.length} • Disputes: ${matchRows.filter((m) => m.status === "disputed").length}`,
        ].join("\n");
      }
      const tg = await telegramSend(s, text);
      await logAct(db, auth.actor.name, `Telegram ${args.mode} message: ${tg.sent ? "sent" : tg.error}`);
      return { ok: tg.sent, error: tg.error, configured: true };
    },
  }),

  googleLogin: defineAction({
    request: z.object({ id_token: z.string().min(10).max(4000) }),
    response: z.object({
      ok: z.boolean(),
      player: playerShape.nullable(),
      error: z.string(),
      session_token: z.string(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const s = await getSettingsMap(db);
      const gs = googleState(s);
      if (!gs.effectiveEnabled || !gs.effectiveId)
        return {
          ok: false,
          player: null,
          session_token: "",
          error: "Google login မချိတ်ရသေးပါ — Admin မှာ Google Client ID အရင် တင်ပါ (pending)၊ Owner က fixed Gmail နဲ့ ဝင်ပြီး အတည်ပြုပါ။ ပုံမှန် အကောင့်နဲ့ ဆက်ဝင်နိုင်ပါတယ်။",
        };
      type GoogleInfo = { sub?: string; email?: string; name?: string; aud?: string; email_verified?: string };
      let info: GoogleInfo | null = null;
      try {
        const res = await fetch(
          `https://oauth2.googleapis.com/tokeninfo?id_token=${encodeURIComponent(args.id_token)}`,
        );
        if (!res.ok) return { ok: false, player: null, session_token: "", error: "Google token စစ်ဆေးမှု မအောင်မြင်ပါ — ပြန်ကြိုးစားပါ" };
        info = (await res.json()) as GoogleInfo;
      } catch {
        return { ok: false, player: null, session_token: "", error: "Google server နဲ့ ချိတ်ဆက်လို့ မရပါ" };
      }
      if (!info?.sub)
        return { ok: false, player: null, session_token: "", error: "Google token မမှန်ပါ" };
      const email = (info.email ?? "").trim();
      const aud = (info.aud ?? "").trim();
      if (gs.activeEnabled && aud === gs.activeId) {
        // Confirmed setup — normal Google login path.
      } else if (gs.state === "pending" && gs.pendingId && aud === gs.pendingId) {
        /* PENDING setup: this sign-in decides the setup's fate. ONLY the
         * fixed admin Gmail may confirm it. Any other verified email means
         * the pending Client ID must never be trusted — reject the login,
         * clear the pending setup completely, and log it. */
        if (!isFixedAdminEmail(email)) {
          await setSetting(db, "google_pending_client_id", "");
          await setSetting(db, "google_setup_state", "rejected");
          await setSetting(db, "google_setup_at", new Date().toISOString());
          await logAct(db, "system", `Google PENDING setup REJECTED — sign-in email was not the fixed admin Gmail; pending Client ID discarded`);
          return {
            ok: false,
            player: null,
            session_token: "",
            error: "ဒီ Google setup က pending (အတည်မပြုရသေး) ပါ — ပထမဆုံး ဝင်ရမယ့်သူက fixed Admin Gmail (nnominalhuman@gmail.com) ပဲ ဖြစ်ရပါမယ်။ တခြား Gmail နဲ့ ဝင်လို့ setup ကို ပယ်ပြီး pending ကို ရှင်းလိုက်ပါပြီ။ Owner ကိုယ်တိုင် ပြန်တင်ပါ။",
          };
        }
        if (info.email_verified !== "true" && info.email_verified !== undefined && String(info.email_verified) !== "true") {
          return { ok: false, player: null, session_token: "", error: "Google email verified မဖြစ်သေးပါ" };
        }
        // Fixed Owner confirmed the pending setup → promote it to ACTIVE.
        await setSetting(db, "google_client_id", gs.pendingId);
        await setSetting(db, "google_enabled", "1");
        await setSetting(db, "google_pending_client_id", "");
        await setSetting(db, "google_setup_state", "confirmed");
        await setSetting(db, "google_setup_by_name", "Owner (Google)");
        await setSetting(db, "google_setup_at", new Date().toISOString());
        await logAct(db, "system", "Google setup CONFIRMED by fixed admin Gmail — pending Client ID promoted to active; setup window closed");
      } else {
        return { ok: false, player: null, session_token: "", error: "Google token က ဒီ app အတွက် မဟုတ်ပါ (client ID မကိုက်ပါ)" };
      }
      // HARD-FIXED rule: only this exact Gmail becomes Owner. Every other
      // Google account is a normal player — no exceptions, no bootstrap.
      const roleFor = (em: string): "owner" | "player" =>
        isFixedAdminEmail(em) ? "owner" : "player";
      const all = await db.select().from(schema.players);
      let p = all.find((x) => x.googleSub === info!.sub) ?? null;
      if (!p && email) p = all.find((x) => x.email !== "" && x.email === email) ?? null;
      if (p) {
        const fixedRole = roleFor(email || p.email);
        // Keep any admin-granted staff role for non-fixed accounts, but a
        // stored "owner" on a non-fixed account is always corrected.
        const correctedRole =
          fixedRole === "owner" ? "owner" : p.role === "owner" ? "player" : p.role;
        await db
          .update(schema.players)
          .set({
            googleSub: info.sub,
            email: email || p.email,
            role: correctedRole,
            lastSeenAt: new Date(),
          })
          .where(eq(schema.players.id, p.id));
        p = { ...p, googleSub: info.sub, email: email || p.email, role: correctedRole };
        if (p.status === "perm_banned" || p.status === "temp_banned")
          return { ok: false, player: null, session_token: "", error: "ဒီ account က ban ခံထားရပါတယ်" };
        const token = await createSession(db, p.id);
        ctx.invalidateQueries();
        return { ok: true, player: await playerOutAsync(ctx, p), error: "", session_token: token };
      }
      const rl = await rateLimit(db, "register", 10, 10 * 60 * 1000);
      if (rl) return { ok: false, player: null, session_token: "", error: rl };
      // A name collision must never block a Google signup — least of all
      // the fixed-Gmail Owner confirmation — so suffix instead of failing.
      const displayName = uniqueDisplayName(
        all,
        (info.name || email.split("@")[0] || "Player").slice(0, 60),
      );
      const myCode = `${displayName.replace(/[^A-Za-z0-9]/g, "").slice(0, 6).toUpperCase() || "MLBB"}${Math.floor(1000 + Math.random() * 9000)}`;
      const inserted = await db
        .insert(schema.players)
        .values({
          name: displayName,
          rank: "Epic",
          role: roleFor(email),
          referralCode: myCode,
          googleSub: info.sub,
          email,
        })
        .returning();
      const np = inserted[0];
      if (!np) return { ok: false, player: null, session_token: "", error: "Account ဖွင့်လို့ မရပါ" };
      await notify(db, {
        playerId: np.id,
        type: "info",
        title: "ကြိုဆိုပါတယ် 🎮",
        body: "Google နဲ့ အကောင့် ဖွင့်ပြီးပါပြီ။ Profile မှာ MLBB ID / Server ID ဖြည့်ပါ။",
      });
      const npToken = await createSession(db, np.id);
      ctx.invalidateQueries();
      return { ok: true, player: await playerOutAsync(ctx, np), error: "", session_token: npToken };
    },
  }),

  totpAction: defineAction({
    request: z.object({
      action: z.enum(["setup", "enable", "disable"]),
      player_id: z.number(),
      session_token: z.string().default(""),
      code: z.string().default(""),
    }),
    response: z.object({
      ok: z.boolean(),
      error: z.string(),
      secret: z.string(),
      otpauth_url: z.string(),
      enabled: z.boolean(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const p = await playerBySession(db, args.player_id, args.session_token);
      if (!p) return { ok: false, error: AUTH_ERR, secret: "", otpauth_url: "", enabled: false };
      const fail = (error: string) => ({
        ok: false, error, secret: "", otpauth_url: "", enabled: p.totpEnabled === 1,
      });
      if (!["owner", "admin", "moderator", "support"].includes(effectiveRole(p)))
        return fail("2FA ကို admin role ရှိသူတွေပဲ သုံးနိုင်ပါတယ်");
      if (args.action === "setup") {
        if (p.totpEnabled === 1) return fail("2FA ဖွင့်ထားပြီးသားပါ — အရင် ပိတ်ပြီးမှ အသစ် setup လုပ်ပါ");
        const secret = randomBase32Secret();
        await db
          .update(schema.players)
          .set({ totpSecret: secret })
          .where(eq(schema.players.id, p.id));
        const url = `otpauth://totp/MLBB%20Platform:${encodeURIComponent(p.name)}?secret=${secret}&issuer=MLBB%20Platform`;
        await logAct(db, p.name, "2FA setup started");
        return { ok: true, error: "", secret, otpauth_url: url, enabled: false };
      }
      if (args.action === "enable") {
        if (!p.totpSecret) return fail("အရင် Setup လုပ်ပါ");
        if (!(await totpValid(p.totpSecret, args.code)))
          return fail("Code မှားနေပါတယ် — Authenticator ထဲက လက်ရှိ ၆ လုံး code ထည့်ပါ");
        await db
          .update(schema.players)
          .set({ totpEnabled: 1 })
          .where(eq(schema.players.id, p.id));
        await logAct(db, p.name, "2FA enabled");
        ctx.invalidateQueries();
        return { ok: true, error: "", secret: "", otpauth_url: "", enabled: true };
      }
      // disable
      if (p.totpEnabled === 1 && !(await totpValid(p.totpSecret, args.code)))
        return fail("2FA ပိတ်ရန် လက်ရှိ code မှန်ရပါမယ်");
      await db
        .update(schema.players)
        .set({ totpEnabled: 0, totpSecret: "" })
        .where(eq(schema.players.id, p.id));
      await logAct(db, p.name, "2FA disabled");
      ctx.invalidateQueries();
      return { ok: true, error: "", secret: "", otpauth_url: "", enabled: false };
    },
  }),

  voiceSignalSend: defineAction({
    request: z.object({
      match_id: z.number(),
      from_player_id: z.number(),
      session_token: z.string().default(""),
      to_player_id: z.number(),
      kind: z.enum(["offer", "answer", "ice", "bye", "join"]),
      payload: z.string().max(60000).default(""),
    }),
    response: z.object({ ok: z.boolean() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meV = await playerBySession(db, args.from_player_id, args.session_token);
      if (!meV) return { ok: false };
      const mRows = await db.select().from(schema.matches)
        .where(eq(schema.matches.id, args.match_id)).limit(1);
      const mm = mRows[0];
      if (!mm || (mm.playerAId !== meV.id && mm.playerBId !== meV.id)) return { ok: false };
      if (args.to_player_id !== mm.playerAId && args.to_player_id !== mm.playerBId) return { ok: false };
      const rl = await rateLimit(db, `voice:${args.from_player_id}`, 300, 60 * 1000);
      if (rl) return { ok: false };
      await db.insert(schema.voiceSignals).values({
        matchId: args.match_id,
        fromPlayerId: args.from_player_id,
        toPlayerId: args.to_player_id,
        kind: args.kind,
        payload: args.payload,
      });
      return { ok: true };
    },
  }),

  voiceSignalPoll: defineAction({
    request: z.object({
      match_id: z.number(),
      player_id: z.number(),
      session_token: z.string().default(""),
      since_id: z.number().default(0),
    }),
    response: z.object({
      signals: z.array(
        z.object({
          id: z.number(),
          from_player_id: z.number(),
          kind: z.string(),
          payload: z.string(),
        }),
      ),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const meV = await playerBySession(db, args.player_id, args.session_token);
      if (!meV) return { signals: [] };
      const rows = await db
        .select()
        .from(schema.voiceSignals)
        .where(eq(schema.voiceSignals.matchId, args.match_id))
        .orderBy(schema.voiceSignals.id);
      return {
        signals: rows
          .filter((r) => r.toPlayerId === args.player_id && r.id > args.since_id)
          .slice(-100)
          .map((r) => ({
            id: r.id,
            from_player_id: r.fromPlayerId,
            kind: r.kind,
            payload: r.payload,
          })),
      };
    },
  }),

  backupAction: defineAction({
    request: z.object({
      action: z.enum(["export", "snapshot", "restore"]),
      actor: z.string().default("admin"),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      confirmed: z.boolean().default(false),
      totp_code: z.string().default(""),
      payload_in: z.string().max(4_000_000).default(""),
      snapshot_code: z.string().default(""),
    }),
    response: z.object({
      ok: z.boolean(),
      error: z.string(),
      code: z.string(),
      payload: z.string(),
      size_bytes: z.number(),
    }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const fail = (error: string) => ({ ok: false, error, code: "", payload: "", size_bytes: 0 });
      // Backup AND restore are Owner-only: fixed admin Gmail, verified by
      // server session + DB role, plus confirmation and real TOTP (when on).
      const auth = await requireOwner(db, args.actor_player_id, args.session_token);
      if (auth.error || !auth.actor) return fail(auth.error ?? AUTH_ERR);
      const actorName = auth.actor.name;
      if (!args.confirmed) return fail("Backup အတွက် security confirmation လိုပါတယ်");
      const err2fa = await checkAdmin2fa(db, auth.actor.id, args.totp_code);
      if (err2fa) return fail(err2fa);
      if (args.action === "restore") {
        let raw = args.payload_in.trim();
        if (!raw && args.snapshot_code) {
          const snaps = await db.select().from(schema.backupSnapshots);
          const snap = snaps.find((x) => x.code === args.snapshot_code);
          if (!snap) return fail("Snapshot မတွေ့ပါ");
          raw = snap.payload;
        }
        if (!raw) return fail("Restore လုပ်မယ့် JSON (သို့) snapshot code ထည့်ပါ");
        type BackupFile = { app?: string; tables?: Record<string, Record<string, unknown>[]> };
        let parsed: BackupFile | null = null;
        try { parsed = JSON.parse(raw) as BackupFile; } catch { return fail("JSON ဖိုင် မမှန်ပါ"); }
        if (!parsed || parsed.app !== "mlbb-platform" || !parsed.tables)
          return fail("ဒါက MLBB Platform backup ဖိုင် မဟုတ်ပါ");
        const tablesIn = parsed.tables;
        let restored = 0;
        // Settings (secrets redacted as "***" in exports are never restored).
        for (const row of tablesIn["settings"] ?? []) {
          const k = String(row["key"] ?? "");
          const v = String(row["value"] ?? "");
          if (!k || v === "***") continue;
          if (["admin_email", "owner_email", "fixed_admin_email"].includes(k)) continue;
          if (!(k in DEFAULT_SETTINGS)) continue;
          await db.insert(schema.settings).values({ key: k, value: v })
            .onConflictDoUpdate({ target: schema.settings.key, set: { value: v } });
          restored++;
        }
        // Players: update existing rows by id (balance/locked/stats/status).
        // Roles are re-derived: owner only for the fixed admin Gmail.
        for (const row of tablesIn["players"] ?? []) {
          const id = Number(row["id"] ?? 0);
          if (!id) continue;
          const existing = await getPlayer(db, id);
          if (!existing) continue;
          const email = String(row["email"] ?? existing.email);
          await db.update(schema.players).set({
            balance: Number(row["balance"] ?? existing.balance),
            lockedCoins: Number(row["lockedCoins"] ?? existing.lockedCoins),
            wins: Number(row["wins"] ?? existing.wins),
            losses: Number(row["losses"] ?? existing.losses),
            winStreak: Number(row["winStreak"] ?? existing.winStreak),
            bestStreak: Number(row["bestStreak"] ?? existing.bestStreak),
            reputation: Number(row["reputation"] ?? existing.reputation),
            status: (["active", "warned", "temp_banned", "perm_banned"].includes(String(row["status"]))
              ? String(row["status"]) : existing.status) as "active" | "warned" | "temp_banned" | "perm_banned",
            role: isFixedAdminEmail(email)
              ? "owner"
              : existing.role === "owner" ? "player" : existing.role,
          }).where(eq(schema.players.id, id));
          restored++;
        }
        await logAct(db, actorName, `Backup RESTORE done (${restored} rows from ${args.snapshot_code || "uploaded JSON"})`);
        ctx.invalidateQueries();
        return { ok: true, error: "", code: args.snapshot_code, payload: "", size_bytes: raw.length };
      }
      const tables: Record<string, unknown[]> = {};
      tables["players"] = (await db.select().from(schema.players)).map((p) => ({
        ...p,
        totpSecret: p.totpSecret ? "***" : "",
        createdAt: iso(p.createdAt),
        lastSeenAt: iso(p.lastSeenAt),
      }));
      tables["settings"] = (await db.select().from(schema.settings)).map((r) =>
        r.key === "telegram_bot_token" ? { ...r, value: "***" } : r,
      );
      tables["challenges"] = (await db.select().from(schema.challenges)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["matches"] = (await db.select().from(schema.matches)).map((r) => ({
        ...r,
        createdAt: iso(r.createdAt),
        readyDeadline: iso(r.readyDeadline),
        startedAt: iso(r.startedAt),
        completedAt: iso(r.completedAt),
      }));
      tables["transactions"] = (await db.select().from(schema.transactions)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["notifications"] = (await db.select().from(schema.notifications)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["teams"] = await db.select().from(schema.teams);
      tables["team_members"] = await db.select().from(schema.teamMembers);
      tables["tournaments"] = (await db.select().from(schema.tournaments)).map((r) => ({ ...r, startsAt: iso(r.startsAt), createdAt: iso(r.createdAt) }));
      tables["tournament_entries"] = await db.select().from(schema.tournamentEntries);
      tables["bracket_matches"] = await db.select().from(schema.bracketMatches);
      tables["referral_commissions"] = (await db.select().from(schema.referralCommissions)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["reports"] = (await db.select().from(schema.reports)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["tickets"] = (await db.select().from(schema.tickets)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["saved_rules"] = await db.select().from(schema.savedRules);
      tables["friends"] = await db.select().from(schema.friends);
      tables["activity_log"] = (await db.select().from(schema.activityLog)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["terms_versions"] = (await db.select().from(schema.termsVersions)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      tables["chat_messages"] = (await db.select().from(schema.chatMessages)).map((r) => ({ ...r, createdAt: iso(r.createdAt) }));
      const payload = JSON.stringify(
        { app: "mlbb-platform", exported_at: new Date().toISOString(), tables },
        null,
        1,
      );
      const size = payload.length;
      if (args.action === "export") {
        await logAct(db, actorName, `Backup export downloaded (${size} bytes)`);
        return { ok: true, error: "", code: "", payload, size_bytes: size };
      }
      const code = uid("BAK");
      const stored =
        size > 1_500_000
          ? JSON.stringify({
              app: "mlbb-platform",
              note: "Payload too large to store in-DB; use Export download instead.",
              exported_at: new Date().toISOString(),
              counts: Object.fromEntries(Object.entries(tables).map(([k, v]) => [k, v.length])),
            })
          : payload;
      await db.insert(schema.backupSnapshots).values({
        code,
        payload: stored,
        sizeBytes: size,
        actor: actorName,
      });
      await logAct(db, actorName, `Backup snapshot ${code} created (${size} bytes)`);
      const tgText = `💾 MLBB Platform backup ${code} ဆောက်ပြီးပါပြီ (${size} bytes)`;
      const s = await getSettingsMap(db);
      await telegramSend(s, tgText);
      ctx.invalidateQueries();
      return { ok: true, error: "", code, payload: "", size_bytes: size };
    },
  }),

  uploadAvatar: defineAction({
    request: z.object({
      player_id: z.number(),
      session_token: z.string().default(""),
      image_base64: z.string().max(12_000_000).default(""),
      image_mime: z.string().max(60).default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), avatar_url: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false, error: AUTH_ERR, avatar_url: "" };
      // Only own avatar: session player must equal target (playerBySession already binds token to player_id)
      if (me.id !== args.player_id) return { ok: false, error: "ကိုယ့် profile ပုံပဲ ပြောင်းနိုင်ပါတယ် — သူများပုံ ပြောင်းလို့ မရပါ", avatar_url: "" };
      const rl = await rateLimit(db, `avatar:${me.id}`, 20, 60*60*1000);
      if (rl) return { ok: false, error: rl, avatar_url: "" };
      const v = validateImage(args.image_base64, args.image_mime);
      if (v.error || !v.bytes) return { ok: false, error: v.error || "ပုံ မမှန်ပါ", avatar_url: "" };
      const oldKey = me.avatarBlobKey ?? "";
      const key = `avatars/player-${me.id}-${Date.now()}.${v.ext}`;
      try { await ctx.blobs.put(key, v.bytes, { contentType: v.mime }); } catch { return { ok: false, error: "ပုံ သိမ်းလို့ မရပါ — ပြန်ကြိုးစားပါ", avatar_url: "" }; }
      await db.update(schema.players).set({ avatarBlobKey: key }).where(eq(schema.players.id, me.id));
      if (oldKey && oldKey !== key) { try { await ctx.blobs.delete(oldKey); } catch { /* old blob cleanup best-effort */ } }
      ctx.invalidateQueries();
      return { ok: true, error: "", avatar_url: await blobUrlSafe(ctx, key) };
    },
  }),

  deleteAvatar: defineAction({
    request: z.object({ player_id: z.number(), session_token: z.string().default("") }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const me = await playerBySession(db, args.player_id, args.session_token);
      if (!me) return { ok: false, error: AUTH_ERR };
      if (me.id !== args.player_id) return { ok: false, error: "ကိုယ့် profile ပုံပဲ ဖျက်နိုင်ပါတယ်" };
      const oldKey = me.avatarBlobKey ?? "";
      await db.update(schema.players).set({ avatarBlobKey: "" }).where(eq(schema.players.id, me.id));
      if (oldKey) { try { await ctx.blobs.delete(oldKey); } catch { /* ignore */ } }
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

  uploadTeamLogo: defineAction({
    request: z.object({
      team_id: z.number(),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
      image_base64: z.string().max(12_000_000).default(""),
      image_mime: z.string().max(60).default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string(), logo_url: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const actor = await playerBySession(db, args.actor_player_id, args.session_token);
      if (!actor) return { ok: false, error: AUTH_ERR, logo_url: "" };
      const rows = await db.select().from(schema.teams).where(eq(schema.teams.id, args.team_id)).limit(1);
      const team = rows[0];
      if (!team) return { ok: false, error: "Team မတွေ့ပါ", logo_url: "" };
      if (team.leaderId !== actor.id) return { ok: false, error: "Team leader ပဲ logo ပြောင်းနိုင်ပါတယ် — သူများ team logo ပြောင်းလို့ မရပါ", logo_url: "" };
      const rl = await rateLimit(db, `teamlogo:${actor.id}`, 20, 60*60*1000);
      if (rl) return { ok: false, error: rl, logo_url: "" };
      const v = validateImage(args.image_base64, args.image_mime);
      if (v.error || !v.bytes) return { ok: false, error: v.error || "ပုံ မမှန်ပါ", logo_url: "" };
      const oldKey = team.logoBlobKey ?? "";
      const key = `team-logos/team-${team.id}-${Date.now()}.${v.ext}`;
      try { await ctx.blobs.put(key, v.bytes, { contentType: v.mime }); } catch { return { ok: false, error: "Logo သိမ်းလို့ မရပါ — ပြန်ကြိုးစားပါ", logo_url: "" }; }
      await db.update(schema.teams).set({ logoBlobKey: key }).where(eq(schema.teams.id, team.id));
      if (oldKey && oldKey !== key) { try { await ctx.blobs.delete(oldKey); } catch { /* ignore */ } }
      ctx.invalidateQueries();
      return { ok: true, error: "", logo_url: await blobUrlSafe(ctx, key) };
    },
  }),

  deleteTeamLogo: defineAction({
    request: z.object({
      team_id: z.number(),
      actor_player_id: z.number().default(0),
      session_token: z.string().default(""),
    }),
    response: z.object({ ok: z.boolean(), error: z.string() }),
    async handler(ctx, args) {
      const db = ctx.db<typeof schema>();
      const actor = await playerBySession(db, args.actor_player_id, args.session_token);
      if (!actor) return { ok: false, error: AUTH_ERR };
      const rows = await db.select().from(schema.teams).where(eq(schema.teams.id, args.team_id)).limit(1);
      const team = rows[0];
      if (!team) return { ok: false, error: "Team မတွေ့ပါ" };
      if (team.leaderId !== actor.id) return { ok: false, error: "Team leader ပဲ logo ဖျက်နိုင်ပါတယ်" };
      const oldKey = team.logoBlobKey ?? "";
      await db.update(schema.teams).set({ logoBlobKey: "" }).where(eq(schema.teams.id, team.id));
      if (oldKey) { try { await ctx.blobs.delete(oldKey); } catch { /* ignore */ } }
      ctx.invalidateQueries();
      return { ok: true, error: "" };
    },
  }),

} satisfies ActionsModule;
