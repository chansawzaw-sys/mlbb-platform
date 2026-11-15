// F11 + F12 acceptance suite.
//
// F11: getAdmin dashboard aggregates (SQL) must return a payload DEEP-EQUAL
// to the pre-fix implementation (whole-table loads + JS aggregation) on the
// same fixture, and equal to expectations recomputed independently in this
// test with the ORIGINAL filter/reduce expressions. Migration 0025's five
// indexes must exist. Fixture: 301 players, 2,000 matches, 5,000
// transactions, 300 purchase + 300 withdraw requests (synthetic only).
// The pre-fix snapshot is read from /tmp/f11-pre/src (actions.ts + schema.ts
// copied from the tree before the F11 edit, node_modules symlinked); when
// absent, the A/B section is skipped and the independent expectations still
// run.
//
// F12: restore-guard scenarios on DISPOSABLE copies only —
//   (i)   fresh empty dir boots clean through all 25 migrations;
//   (ii)  current-lineage DB (journal 0025): guard reports nothing-to-do,
//         boot clean;
//   (iii) synthetic pre-purge backup (journal at 0011) + guard: 0012/0018
//         recorded, DDL bridge applied, all accounts + financial rows
//         preserved after boot, post-0018 schema (0023 column, 0024 index)
//         present; guard refusal without flags; guard idempotency;
//   (iv)  the same backup WITHOUT the guard: boot replays 0012 and the
//         synthetic players are purged (hazard control — proves the test
//         detects it), and the F12 boot warning fires;
//   (v)   journal-at-0015 variant: guard records only 0018, boot preserves
//         the literal-id 40/42 players.
//
// Run:  bun ~/workspace/mlbb-platform/deploy-tools/f11_f12_test.ts
// Exit code 0 = all cases pass. No live data, no production anything.

import { Database } from "bun:sqlite";
import { drizzle } from "/home/hatch/workspace/mlbb-platform/standalone/node_modules/drizzle-orm/bun-sqlite/index.js";
import { migrate } from "/home/hatch/workspace/mlbb-platform/standalone/node_modules/drizzle-orm/bun-sqlite/migrator.js";
import * as schema from "/home/hatch/workspace/mlbb-platform/standalone/src/schema.ts";
import { Actions } from "/home/hatch/workspace/mlbb-platform/standalone/src/actions.ts";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

const STANDALONE = "/home/hatch/workspace/mlbb-platform/standalone";
const DRIZZLE_DIR = join(STANDALONE, "drizzle");
const SERVER_TS = join(STANDALONE, "server.ts");
const GUARD_TS = "/home/hatch/workspace/mlbb-platform/deploy-tools/restore-guard.ts";
const ROOT = `/tmp/f11f12-test-${process.pid}`;
rmSync(ROOT, { recursive: true, force: true });
mkdirSync(ROOT, { recursive: true });

// ---------- tiny test frame ----------
let passed = 0;
const failures: string[] = [];
function check(name: string, cond: boolean, detail = "") {
  if (cond) {
    passed++;
    console.log(`  PASS ${name}`);
  } else {
    failures.push(name);
    console.log(`  FAIL ${name} ${detail}`);
  }
}
const canon = (v: unknown): string =>
  JSON.stringify(v, (_k, val) =>
    val && typeof val === "object" && !Array.isArray(val)
      ? Object.fromEntries(
          Object.entries(val as Record<string, unknown>).sort(([a], [b]) =>
            a < b ? -1 : 1,
          ),
        )
      : val,
  );

// ---------- db helpers ----------
async function buildDb(dir: string, migrationsFolder: string) {
  mkdirSync(dir, { recursive: true });
  const sqlite = new Database(join(dir, "app.db"), { create: true });
  sqlite.exec("PRAGMA journal_mode = WAL");
  sqlite.exec("PRAGMA foreign_keys = ON");
  const db = drizzle(sqlite, { schema });
  await migrate(db, { migrationsFolder });
  return { sqlite, db };
}
function filteredMigrationsDir(uptoIdx: number, dest: string) {
  const journal = JSON.parse(
    readFileSync(join(DRIZZLE_DIR, "meta/_journal.json"), "utf8"),
  );
  const entries = journal.entries.filter((e: any) => e.idx <= uptoIdx);
  mkdirSync(join(dest, "meta"), { recursive: true });
  writeFileSync(
    join(dest, "meta/_journal.json"),
    JSON.stringify({ version: "7", dialect: "sqlite", entries }, null, 2),
  );
  for (const e of entries) {
    copyFileSync(join(DRIZZLE_DIR, `${e.tag}.sql`), join(dest, `${e.tag}.sql`));
  }
  return dest;
}
function roDb(path: string) {
  return new Database(path, { readonly: true });
}
function migRowCount(path: string) {
  const d = roDb(path);
  const n = (
    d.query("SELECT COUNT(*) AS n FROM __drizzle_migrations").get() as { n: number }
  ).n;
  d.close();
  return n;
}
function colExists(path: string, table: string, col: string) {
  const d = roDb(path);
  const rows = d.query(`SELECT name FROM pragma_table_info('${table}')`).all() as {
    name: string;
  }[];
  d.close();
  return rows.some((r) => r.name === col);
}
function tableExistsRo(path: string, table: string) {
  const d = roDb(path);
  const n = (
    d
      .query("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name = ?")
      .get(table) as { n: number }
  ).n;
  d.close();
  return n > 0;
}
function indexExists(path: string, name: string) {
  const d = roDb(path);
  const n = (
    d
      .query("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='index' AND name = ?")
      .get(name) as { n: number }
  ).n;
  d.close();
  return n > 0;
}
/** Insert into a partially-migrated DB whose exact column set varies:
 *  fills NOT NULL columns that have no default with a type-based zero. */
function rawInsert(
  sqlite: Database,
  table: string,
  values: Record<string, unknown>,
): number {
  const cols = sqlite
    .query(
      `SELECT name, type, "notnull" AS nn, dflt_value AS dv, pk FROM pragma_table_info('${table}')`,
    )
    .all() as any[];
  const names: string[] = [];
  const params: unknown[] = [];
  for (const c of cols) {
    if (c.name in values) {
      names.push(c.name);
      params.push(values[c.name]);
    } else if (c.dflt_value !== null) {
      // DB default applies.
    } else if (c.pk === 1 && String(c.type).toUpperCase().includes("INT")) {
      // Autoincrement.
    } else if (c.nn === 1) {
      names.push(c.name);
      params.push(String(c.type).toUpperCase().includes("INT") ? 0 : "");
    }
  }
  const res = sqlite
    .query(
      `INSERT INTO ${table} (${names.map((n) => `"${n}"`).join(",")}) VALUES (${names
        .map(() => "?")
        .join(",")})`,
    )
    .run(...params);
  return Number(res.lastInsertRowid);
}

// ---------- server boot helper ----------
async function bootServer(dataDir: string, port: number) {
  const proc = Bun.spawn(["bun", "run", SERVER_TS], {
    env: { ...process.env, PORT: String(port), DATA_DIR: dataDir },
    stdout: "pipe",
    stderr: "pipe",
  });
  let logs = "";
  let exited = false;
  proc.exited.then(() => {
    exited = true;
  });
  const drain = async (stream: ReadableStream<Uint8Array>) => {
    const dec = new TextDecoder();
    try {
      for await (const chunk of stream) logs += dec.decode(chunk as Uint8Array);
    } catch {
      // Stream closed on kill.
    }
  };
  const drains = Promise.all([drain(proc.stdout), drain(proc.stderr)]);
  const deadline = Date.now() + 90_000;
  let health: any = null;
  while (Date.now() < deadline && !exited) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/health`);
      if (r.ok) {
        health = await r.json();
        break;
      }
    } catch {
      // Not up yet.
    }
    await Bun.sleep(250);
  }
  return {
    health,
    logs: () => logs,
    async stop() {
      try {
        proc.kill();
      } catch {}
      await Promise.race([proc.exited, Bun.sleep(5000)]);
      await Promise.race([drains, Bun.sleep(1000)]);
    },
  };
}
async function postAction(port: number, action: string, args: unknown) {
  const r = await fetch(`http://127.0.0.1:${port}/actions`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, args }),
  });
  return { status: r.status, body: (await r.json()) as any };
}
function runGuard(dbPath: string, args: string[]) {
  const r = Bun.spawnSync(["bun", GUARD_TS, dbPath, ...args]);
  return {
    code: r.exitCode,
    out: r.stdout.toString() + r.stderr.toString(),
  };
}

// =====================================================================
// F11 — dashboard aggregates
// =====================================================================
console.log("== F11: getAdmin aggregates ==");
const F11_DIR = join(ROOT, "f11");
const { sqlite: f11sqlite, db: f11db } = await buildDb(F11_DIR, DRIZZLE_DIR);
const f11ctx: any = { db: () => f11db, blobs: {}, invalidateQueries: () => {} };
const NOW = Date.now();
const OLD = NOW - 3600_000;

// --- seed ---
{
  const playerVals: any[] = [
    {
      name: "Owner",
      email: "nnominalhuman@gmail.com",
      balance: 0,
      lastSeenAt: new Date(OLD),
    },
  ];
  for (let i = 1; i <= 300; i++) {
    const v: any = {
      name: `P${i}`,
      email: `p${i}@test`,
      balance: (i * 37) % 5000,
      wins: i % 20,
      losses: i % 15,
      lastSeenAt: new Date(OLD),
    };
    if (i === 1) v.lastSeenAt = new Date(NOW - 10_000);
    if (i === 2) v.lastSeenAt = new Date(NOW - 60_000);
    if (i === 3) v.lastSeenAt = new Date(NOW - 119_000);
    if (i === 4) v.lastSeenAt = new Date(NOW - 121_000);
    if (i === 10 || i === 20) {
      v.suspicious = 1;
      v.suspiciousReason = "test flag";
    }
    if (i === 30 || i === 31) {
      v.mlbbId = "111";
      v.serverId = "2222";
      v.gameName = `G${i}`;
    }
    if (i >= 32 && i <= 34) {
      v.mlbbId = "333";
      v.serverId = "4444";
      v.gameName = `G${i}`;
    }
    if (i === 35) {
      v.mlbbId = " 111 ";
      v.serverId = " 2222 ";
      v.gameName = "G35";
    }
    if (i === 36) {
      v.mlbbId = "111";
      v.serverId = "";
    }
    if (i === 37) {
      v.mlbbId = " ";
      v.serverId = "2222";
    }
    if (i === 38) {
      v.mlbbId = "111";
      v.serverId = "9999";
    }
    playerVals.push(v);
  }
  for (let i = 0; i < playerVals.length; i += 200) {
    await f11db.insert(schema.players).values(playerVals.slice(i, i + 200));
  }
  await f11db.insert(schema.sessions).values([
    { token: "tok-owner", playerId: 1, expiresAt: new Date(NOW + 3600_000) },
    { token: "tok-player", playerId: 2, expiresAt: new Date(NOW + 3600_000) },
  ]);

  const matchStatuses = [
    ...Array(100).fill("ready_check"),
    ...Array(150).fill("live"),
    ...Array(50).fill("result_pending"),
    ...Array(30).fill("disputed"),
    ...Array(1500).fill("completed"),
    ...Array(170).fill("cancelled"),
  ];
  const matchVals = matchStatuses.map((status, i) => ({
    code: `M${i}`,
    challengeId: 1,
    playerAId: 1,
    playerBId: 2,
    arena: "classic",
    stake: 100,
    status,
    readyDeadline: new Date(NOW),
  }));
  for (let i = 0; i < matchVals.length; i += 200) {
    await f11db.insert(schema.matches).values(matchVals.slice(i, i + 200) as any);
  }

  const txnVals: any[] = [];
  for (let i = 0; i < 5000; i++) {
    const type =
      i < 2000
        ? "platform_fee"
        : i < 3000
          ? "purchase"
          : i < 3500
            ? "withdraw"
            : i < 4500
              ? "stake_lock"
              : "payout";
    txnVals.push({
      code: `TXN-${i}`,
      playerId: (i % 300) + 2,
      type,
      amount:
        type === "platform_fee"
          ? 5 + (i % 46)
          : type === "purchase"
            ? 100
            : type === "withdraw"
              ? 50
              : type === "stake_lock"
                ? 200
                : 300,
      balanceAfter: 0,
      createdAt: new Date(i % 2 === 0 ? NOW - 60_000 : NOW - 3 * 86400_000),
    });
  }
  for (let i = 0; i < txnVals.length; i += 200) {
    await f11db.insert(schema.transactions).values(txnVals.slice(i, i + 200));
  }

  const purchaseVals: any[] = [];
  for (let i = 0; i < 300; i++) {
    const status =
      i < 40 ? "pending" : i < 190 ? "approved" : i < 250 ? "rejected" : "expired";
    purchaseVals.push({
      playerId: 2,
      coins: 100 + (i % 10) * 50,
      label: "pack",
      status,
      decidedAt:
        status === "approved"
          ? new Date(i - 40 < 100 ? NOW - 1800_000 : NOW - 3 * 86400_000)
          : status === "rejected"
            ? new Date(NOW - 3 * 86400_000)
            : null,
    });
  }
  await f11db.insert(schema.purchaseRequests).values(purchaseVals);

  const withdrawVals: any[] = [];
  for (let i = 0; i < 300; i++) {
    const status =
      i < 35 ? "pending" : i < 215 ? "paid" : i < 265 ? "rejected" : "expired";
    const paidIdx = i - 35;
    withdrawVals.push({
      playerId: 2,
      coins: 1000,
      netKyat: 900 + (i % 7) * 100,
      method: status === "paid" && paidIdx >= 100 ? "wave" : "kpay",
      status,
      decidedAt:
        status === "paid"
          ? new Date(paidIdx < 90 ? NOW - 1800_000 : NOW - 3 * 86400_000)
          : status === "rejected"
            ? new Date(NOW - 3 * 86400_000)
            : null,
    });
  }
  await f11db.insert(schema.withdrawRequests).values(withdrawVals);

  await f11db.insert(schema.reports).values(
    Array.from({ length: 20 }, (_, i) => ({
      caseCode: `CASE-${i}`,
      reporterId: 2,
      targetName: "P5",
      reason: "test",
      status: i < 12 ? "open" : "resolved",
    })),
  );
  await f11db.insert(schema.tickets).values(
    Array.from({ length: 12 }, (_, i) => ({
      playerId: 2,
      name: "P1",
      topic: "t",
      message: "m",
      status: i < 7 ? "open" : "closed",
    })),
  );
  await f11db.insert(schema.activityLog).values(
    Array.from({ length: 3 }, (_, i) => ({ actor: "Owner", action: `act ${i}` })),
  );
  await f11db.insert(schema.termsVersions).values(
    Array.from({ length: 2 }, (_, i) => ({
      kind: "terms",
      version: `v${i}`,
      content: "c",
      actor: "Owner",
    })),
  );
  await f11db.insert(schema.backupSnapshots).values([
    { code: "B1", payload: "{}", sizeBytes: 12345, actor: "Owner" },
  ]);
  await f11db.insert(schema.staffGrants).values([
    { email: "mod@test", role: "moderator", grantedBy: "Owner" },
  ]);
}

// --- independent expectations (ORIGINAL filter/reduce semantics) ---
const allPlayers = await f11db.select().from(schema.players);
const allMatches = await f11db.select().from(schema.matches);
const allTxns = await f11db.select().from(schema.transactions);
const allPurchases = await f11db.select().from(schema.purchaseRequests);
const allWithdraws = await f11db.select().from(schema.withdrawRequests);
const dayStart = new Date();
dayStart.setUTCHours(0, 0, 0, 0);
const ONLINE_WINDOW_MS = 2 * 60 * 1000;
const exp = {
  total_players: allPlayers.length,
  online_players: allPlayers.filter(
    (p) => !!p.lastSeenAt && p.lastSeenAt.getTime() >= Date.now() - ONLINE_WINDOW_MS,
  ).length,
  active_matches: allMatches.filter((m) =>
    ["ready_check", "live", "result_pending", "disputed"].includes(m.status),
  ).length,
  pending_disputes: allMatches.filter((m) => m.status === "disputed").length,
  pending_reports: 12,
  open_tickets: 7,
  total_transactions: allTxns.length,
  total_fees: allTxns
    .filter((t) => t.type === "platform_fee")
    .reduce((a, t) => a + t.amount, 0),
  fees_today: allTxns
    .filter((t) => t.type === "platform_fee" && t.createdAt >= dayStart)
    .reduce((a, t) => a + t.amount, 0),
  topups_today_count: allPurchases.filter(
    (r) => r.status === "approved" && r.decidedAt && r.decidedAt >= dayStart,
  ).length,
  topups_today_coins: allPurchases
    .filter((r) => r.status === "approved" && r.decidedAt && r.decidedAt >= dayStart)
    .reduce((a, r) => a + r.coins, 0),
  withdrawals_today_count: allWithdraws.filter(
    (w) => w.status === "paid" && w.decidedAt && w.decidedAt >= dayStart,
  ).length,
  withdrawals_today_kyat: allWithdraws
    .filter((w) => w.status === "paid" && w.decidedAt && w.decidedAt >= dayStart)
    .reduce((a, w) => a + w.netKyat, 0),
  pending_topups: allPurchases.filter((r) => r.status === "pending").length,
  pending_withdrawals: allWithdraws.filter((w) => w.status === "pending").length,
  kpay_total: allWithdraws
    .filter((w) => w.status === "paid" && w.method === "kpay")
    .reduce((a, w) => a + w.netKyat, 0),
  kpay_count: allWithdraws.filter((w) => w.status === "paid" && w.method === "kpay")
    .length,
  wave_total: allWithdraws
    .filter((w) => w.status === "paid" && w.method === "wave")
    .reduce((a, w) => a + w.netKyat, 0),
  wave_count: allWithdraws.filter((w) => w.status === "paid" && w.method === "wave")
    .length,
};
// Anchor the expectations to the seed design (guards against a broken
// expectation computation silently agreeing with a broken handler).
check("F11 fixture anchors: online/active/disputed", exp.online_players === 3 && exp.active_matches === 330 && exp.pending_disputes === 30, JSON.stringify(exp));
check("F11 fixture anchors: money counts", exp.topups_today_count === 100 && exp.withdrawals_today_count === 90 && exp.pending_topups === 40 && exp.pending_withdrawals === 35 && exp.kpay_count === 100 && exp.wave_count === 80, JSON.stringify(exp));

// --- post-fix payload ---
const getAdminPost = (Actions as any).getAdmin.handler;
const t0 = performance.now();
const post: any = await getAdminPost(f11ctx, {
  actor_player_id: 1,
  session_token: "tok-owner",
});
const postMs = performance.now() - t0;
check("F11 health matches expectations",
  post.health.total_players === exp.total_players &&
  post.health.online_players === exp.online_players &&
  post.health.active_matches === exp.active_matches &&
  post.health.pending_disputes === exp.pending_disputes &&
  post.health.pending_reports === exp.pending_reports &&
  post.health.open_tickets === exp.open_tickets &&
  post.health.total_transactions === exp.total_transactions &&
  post.health.total_fees === exp.total_fees &&
  post.health.db_status === "OK",
  JSON.stringify(post.health));
check("F11 money matches expectations",
  post.money.topups_today_count === exp.topups_today_count &&
  post.money.topups_today_coins === exp.topups_today_coins &&
  post.money.withdrawals_today_count === exp.withdrawals_today_count &&
  post.money.withdrawals_today_kyat === exp.withdrawals_today_kyat &&
  post.money.fees_today_coins === exp.fees_today &&
  post.money.pending_topups === exp.pending_topups &&
  post.money.pending_withdrawals === exp.pending_withdrawals,
  JSON.stringify(post.money));
check("F11 payout_split matches expectations",
  post.payout_split.kpay.total_kyat === exp.kpay_total &&
  post.payout_split.kpay.count === exp.kpay_count &&
  post.payout_split.wave.total_kyat === exp.wave_total &&
  post.payout_split.wave.count === exp.wave_count,
  JSON.stringify(post.payout_split));
// Duplicate pairs: original grouping over ALL players, computed here.
const byPair = new Map<string, any>();
for (const pl of allPlayers) {
  const id = (pl.mlbbId ?? "").trim();
  const zone = (pl.serverId ?? "").trim();
  if (!id || !zone) continue;
  const key = `${id} ${zone}`;
  const group = byPair.get(key) ?? { mlbb_id: id, server_id: zone, players: [] };
  group.players.push({ id: pl.id, name: pl.name, game_name: pl.gameName ?? "" });
  byPair.set(key, group);
}
const expDupPairs = [...byPair.values()].filter((g) => g.players.length > 1);
check("F11 duplicate_pairs deep-equal (incl. whitespace-trim pair)",
  canon(post.duplicate_pairs) === canon(expDupPairs),
  `post=${JSON.stringify(post.duplicate_pairs)} exp=${JSON.stringify(expDupPairs)}`);
check("F11 duplicate_pairs anchors: groups of 3 and 3",
  expDupPairs.length === 2 && expDupPairs.every((g) => g.players.length === 3),
  JSON.stringify(expDupPairs.map((g) => g.players.length)));
check("F11 suspicious_players are exactly the flagged two, id order",
  JSON.stringify(post.suspicious_players.map((p: any) => p.id)) ===
    JSON.stringify(allPlayers.filter((p) => p.suspicious === 1).map((p) => p.id)) &&
    post.suspicious_players.length === 2,
  JSON.stringify(post.suspicious_players.map((p: any) => p.id)));
check("F11 bounded lists intact (activity/terms/backups/grants)",
  post.activity.length === 3 && post.terms_history.length === 2 &&
  post.backups.length === 1 && post.staff_grants.length === 1,
  `act=${post.activity.length} terms=${post.terms_history.length} bk=${post.backups.length} sg=${post.staff_grants.length}`);

// --- A/B against the pre-fix implementation, same fixture ---
const PRE_SRC = "/tmp/f11-pre/src/actions.ts";
if (existsSync(PRE_SRC)) {
  const pre = (await import(PRE_SRC)) as any;
  const t1 = performance.now();
  const prePayload: any = await pre.Actions.getAdmin.handler(f11ctx, {
    actor_player_id: 1,
    session_token: "tok-owner",
  });
  const preMs = performance.now() - t1;
  check("F11 A/B: post-fix payload DEEP-EQUALS pre-fix payload",
    canon(post) === canon(prePayload),
    canon(post) === canon(prePayload) ? "" : "payloads differ");
  console.log(
    `  INFO F11 getAdmin duration on fixture (301 players / 2,000 matches / 5,000 txns): pre-fix ${preMs.toFixed(1)}ms, post-fix ${postMs.toFixed(1)}ms (this sandbox only)`,
  );
} else {
  console.log("  INFO F11 A/B skipped: no pre-fix snapshot at /tmp/f11-pre/src");
}

// --- authorization semantics preserved ---
const forbidden: any = await getAdminPost(f11ctx, {
  actor_player_id: 2,
  session_token: "tok-player",
});
check("F11 plain player still gets the FORBIDDEN zero payload",
  forbidden.health.db_status === "FORBIDDEN" && forbidden.health.total_players === 0,
  JSON.stringify(forbidden.health));

// --- migration 0025 indexes ---
{
  const names = f11sqlite
    .query("SELECT name FROM sqlite_master WHERE type='index'")
    .all() as { name: string }[];
  const have = new Set(names.map((r) => r.name));
  for (const idx of [
    "transactions_created_at_idx",
    "transactions_type_idx",
    "matches_status_idx",
    "purchase_requests_status_idx",
    "withdraw_requests_status_idx",
  ]) {
    check(`F11 index exists: ${idx}`, have.has(idx));
  }
}
f11sqlite.close();

// --- boot the real server on the fixture: zod-validated HTTP path ---
{
  const srv = await bootServer(F11_DIR, 3941);
  check("F11 boot: /health ok on fixture DB", !!srv.health?.ok, JSON.stringify(srv.health));
  if (srv.health) {
    const ga = await postAction(3941, "getAdmin", {
      actor_player_id: 1,
      session_token: "tok-owner",
    });
    check("F11 HTTP getAdmin: 200 + aggregate values through zod validation",
      ga.status === 200 && ga.body?.data?.health?.total_players === 301 &&
      ga.body?.data?.health?.total_transactions === 5000,
      `status=${ga.status} body=${JSON.stringify(ga.body).slice(0, 200)}`);
    const gb = await postAction(3941, "getBootstrap", {});
    check("F11 HTTP getBootstrap: 200", gb.status === 200, `status=${gb.status}`);
  }
  await srv.stop();
}

// =====================================================================
// F12 — restore guard scenarios (disposable copies only)
// =====================================================================
console.log("== F12: restore guard ==");
const POLD = NOW - 86400_000;
function seedPartial(sqlite: Database) {
  rawInsert(sqlite, "players", {
    name: "Owner",
    email: "nnominalhuman@gmail.com",
    role: "owner",
    balance: 500,
    last_seen_at: POLD,
    created_at: POLD,
  });
  for (let i = 1; i <= 5; i++) {
    rawInsert(sqlite, "players", {
      name: `U${i}`,
      email: `u${i}@test`,
      balance: 100 * i,
      last_seen_at: POLD,
      created_at: POLD,
    });
  }
  rawInsert(sqlite, "players", {
    id: 40,
    name: "Dup A",
    email: "dupa@test",
    balance: 777,
    mlbb_id: "155038073",
    server_id: "2783",
    last_seen_at: POLD,
    created_at: POLD,
  });
  rawInsert(sqlite, "players", {
    id: 42,
    name: "Dup B",
    email: "dupb@test",
    balance: 888,
    mlbb_id: "155038073",
    server_id: "2783",
    last_seen_at: POLD,
    created_at: POLD,
  });
  let k = 0;
  for (const pid of [1, 2, 40, 42]) {
    for (let j = 0; j < 2; j++) {
      rawInsert(sqlite, "transactions", {
        code: `TXN-S${k++}`,
        player_id: pid,
        type: "purchase",
        amount: 100,
        balance_after: 0,
        created_at: POLD,
      });
    }
  }
  rawInsert(sqlite, "purchase_requests", {
    player_id: 2,
    coins: 500,
    status: "pending",
    created_at: POLD,
  });
  rawInsert(sqlite, "purchase_requests", {
    player_id: 40,
    coins: 700,
    status: "pending",
    created_at: POLD,
  });
  rawInsert(sqlite, "withdraw_requests", {
    player_id: 42,
    coins: 300,
    net_kyat: 2700,
    method: "kpay",
    status: "pending",
    created_at: POLD,
  });
}
function partialCounts(path: string) {
  const d = roDb(path);
  const q = (t: string) =>
    (d.query(`SELECT COUNT(*) AS n FROM ${t}`).get() as { n: number }).n;
  const out = {
    players: q("players"),
    transactions: q("transactions"),
    purchase_requests: q("purchase_requests"),
    withdraw_requests: q("withdraw_requests"),
    bal40: (
      d.query("SELECT balance FROM players WHERE id = 40").get() as {
        balance: number;
      } | null
    )?.balance,
    bal42: (
      d.query("SELECT balance FROM players WHERE id = 42").get() as {
        balance: number;
      } | null
    )?.balance,
  };
  d.close();
  return out;
}

// (i) fresh empty dir boots clean through all migrations
{
  const dir = join(ROOT, "fresh");
  mkdirSync(dir, { recursive: true });
  const srv = await bootServer(dir, 3942);
  check("F12(i) fresh boot: /health ok", !!srv.health?.ok, JSON.stringify(srv.health));
  await srv.stop();
  check("F12(i) fresh boot: 25 migration rows recorded",
    migRowCount(join(dir, "app.db")) === 25,
    `rows=${migRowCount(join(dir, "app.db"))}`);
  check("F12(i) fresh boot: no restore warning on an empty DB",
    !srv.logs().includes("RESTORE SAFETY WARNING"));
}

// (ii) current-lineage DB: guard nothing-to-do, boot clean
{
  const dir = join(ROOT, "current");
  const { sqlite } = await buildDb(dir, DRIZZLE_DIR);
  sqlite.close();
  const g = runGuard(join(dir, "app.db"), []);
  check("F12(ii) guard on current lineage: exit 0 + nothing to do",
    g.code === 0 && g.out.includes("nothing to do"),
    `code=${g.code} out=${g.out.slice(0, 300)}`);
  const srv = await bootServer(dir, 3943);
  check("F12(ii) current lineage boot: /health ok", !!srv.health?.ok);
  await srv.stop();
}

// (iii) pre-purge backup (journal at 0011) + guard
{
  const dir = join(ROOT, "prepurge");
  const folder = filteredMigrationsDir(10, join(ROOT, "mig-0011"));
  const { sqlite } = await buildDb(dir, folder);
  seedPartial(sqlite);
  sqlite.close();
  const dbFile = join(dir, "app.db");
  check("F12(iii) fixture: journal at 0011 (11 rows)", migRowCount(dbFile) === 11,
    `rows=${migRowCount(dbFile)}`);
  const seeded = partialCounts(dbFile);
  check("F12(iii) fixture: 8 players / 8 txns seeded",
    seeded.players === 8 && seeded.transactions === 8, JSON.stringify(seeded));

  const refuse = runGuard(dbFile, []);
  check("F12(iii) guard without flags: refuses (exit 2), DB untouched",
    refuse.code === 2 && refuse.out.includes("REFUSING") &&
      migRowCount(dbFile) === 11 && partialCounts(dbFile).players === 8,
    `code=${refuse.code}`);

  const backupCopy = join(dir, "pre-guard-copy.db");
  const g = runGuard(dbFile, ["--confirm", "--backup-out", backupCopy]);
  check("F12(iii) guard with flags: exit 0, records 0012 + 0018, audits equal",
    g.code === 0 && g.out.includes("recorded 0012 as applied") &&
      g.out.includes("recorded 0018 as applied") &&
      g.out.includes("audit BEFORE") && g.out.includes("audit AFTER"),
    `code=${g.code} out=${g.out.slice(0, 600)}`);
  check("F12(iii) guard: backup copy holds the pre-guard state (11 rows)",
    existsSync(backupCopy) && migRowCount(backupCopy) === 11);
  const afterGuard = partialCounts(dbFile);
  check("F12(iii) guard changed no data rows",
    afterGuard.players === seeded.players &&
      afterGuard.transactions === seeded.transactions &&
      afterGuard.bal40 === 777 && afterGuard.bal42 === 888,
    JSON.stringify(afterGuard));

  const again = runGuard(dbFile, [
    "--confirm",
    "--backup-out",
    join(dir, "second-copy.db"),
  ]);
  check("F12(iii) guard is idempotent: second run = nothing to do",
    again.code === 0 && again.out.includes("nothing to do"),
    `code=${again.code} out=${again.out.slice(0, 200)}`);

  const srv = await bootServer(dir, 3944);
  check("F12(iii) guarded boot: /health ok", !!srv.health?.ok);
  await srv.stop();
  const after = partialCounts(dbFile);
  check("F12(iii) after boot: ALL accounts + financial rows preserved",
    after.players === 8 && after.transactions === 8 &&
      after.purchase_requests === 2 && after.withdraw_requests === 1 &&
      after.bal40 === 777 && after.bal42 === 888,
    JSON.stringify(after));
  check("F12(iii) after boot: journal complete (25 rows)",
    migRowCount(dbFile) === 25, `rows=${migRowCount(dbFile)}`);
  check("F12(iii) bridged schema: match_participants (0013) + google_pending (0017) exist",
    tableExistsRo(dbFile, "match_participants") && tableExistsRo(dbFile, "google_pending"));
  check("F12(iii) bridged schema: players.game_name_locked (0017) exists",
    colExists(dbFile, "players", "game_name_locked"));
  check("F12(iii) post-0018 schema applied at boot: reports.screenshot_key (0023)",
    colExists(dbFile, "reports", "screenshot_key"));
  check("F12(iii) post-0018 schema applied at boot: transactions_code_unique (0024)",
    indexExists(dbFile, "transactions_code_unique"));
  check("F12(iii) guarded boot: no restore warning (journal already past 0012)",
    !srv.logs().includes("RESTORE SAFETY WARNING"));
}

// (iv) hazard control: same backup WITHOUT the guard
{
  const dir = join(ROOT, "hazard");
  const folder = filteredMigrationsDir(10, join(ROOT, "mig-0011-b"));
  const { sqlite } = await buildDb(dir, folder);
  seedPartial(sqlite);
  sqlite.close();
  const dbFile = join(dir, "app.db");
  const srv = await bootServer(dir, 3945);
  check("F12(iv) unguarded boot: server still comes up", !!srv.health?.ok);
  await srv.stop();
  const after = partialCounts(dbFile);
  check("F12(iv) HAZARD REPRODUCED: 0012 purge left only the Owner",
    after.players === 1 && after.transactions === 0,
    JSON.stringify(after));
  check("F12(iv) unguarded boot: F12 restore warning fired in the boot log",
    srv.logs().includes("RESTORE SAFETY WARNING"));
}

// (v) journal-at-0015 variant: guard records only 0018
{
  const dir = join(ROOT, "mid0015");
  const folder = filteredMigrationsDir(14, join(ROOT, "mig-0015"));
  const { sqlite } = await buildDb(dir, folder);
  seedPartial(sqlite);
  sqlite.close();
  const dbFile = join(dir, "app.db");
  check("F12(v) fixture: journal at 0015 (15 rows)", migRowCount(dbFile) === 15,
    `rows=${migRowCount(dbFile)}`);
  const g = runGuard(dbFile, [
    "--confirm",
    "--backup-out",
    join(dir, "pre-guard-copy.db"),
  ]);
  check("F12(v) guard: exit 0; 0012 already recorded, only 0018 recorded now",
    g.code === 0 && g.out.includes("0012 0012_purge_non_owner_players: recorded") &&
      g.out.includes("recorded 0018 as applied") &&
      !g.out.includes("recorded 0012 as applied"),
    `code=${g.code} out=${g.out.slice(0, 600)}`);
  const srv = await bootServer(dir, 3946);
  check("F12(v) guarded boot: /health ok", !!srv.health?.ok);
  await srv.stop();
  const after = partialCounts(dbFile);
  check("F12(v) after boot: id 40/42 players + all rows preserved",
    after.players === 8 && after.transactions === 8 &&
      after.bal40 === 777 && after.bal42 === 888,
    JSON.stringify(after));
  check("F12(v) after boot: journal complete (25 rows)",
    migRowCount(dbFile) === 25, `rows=${migRowCount(dbFile)}`);
  check("F12(v) bridged schema: challenges.invited_player_id (0016) + game_name_locked (0017)",
    colExists(dbFile, "challenges", "invited_player_id") &&
      colExists(dbFile, "players", "game_name_locked"));
}

// ---------- summary ----------
console.log(
  `\nf11_f12_test: ${passed} passed, ${failures.length} failed` +
    (failures.length ? `\nFAILED: ${failures.join(" | ")}` : ""),
);
process.exit(failures.length ? 1 : 0);
