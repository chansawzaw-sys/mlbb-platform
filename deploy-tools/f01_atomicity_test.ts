// F01 acceptance suite — wallet decisions are atomic and retry-safe.
//
// Drives the REAL action handlers (decidePurchase / decideWithdraw /
// decideDiamondOrder / requestWithdraw / listWithdrawRequests sweep)
// from the standalone tree against a fresh DISPOSABLE database built
// from the standalone migrations (0000–0024). Synthetic accounts only;
// no live data, no production anything.
//
// Run:  bun ~/workspace/mlbb-platform/deploy-tools/f01_atomicity_test.ts
// Exit code 0 = all cases pass.

import { Database } from "bun:sqlite";
import { drizzle } from "/home/hatch/workspace/mlbb-platform/standalone/node_modules/drizzle-orm/bun-sqlite/index.js";
import { migrate } from "/home/hatch/workspace/mlbb-platform/standalone/node_modules/drizzle-orm/bun-sqlite/migrator.js";
import * as schema from "/home/hatch/workspace/mlbb-platform/standalone/src/schema.ts";
import { Actions } from "/home/hatch/workspace/mlbb-platform/standalone/src/actions.ts";
import { rmSync, mkdirSync } from "node:fs";

const DIR = `/tmp/f01-atomicity-db-${process.pid}`;
rmSync(DIR, { recursive: true, force: true });
mkdirSync(DIR, { recursive: true });

const sqlite = new Database(DIR + "/app.db", { create: true });
sqlite.exec("PRAGMA journal_mode = WAL");
sqlite.exec("PRAGMA foreign_keys = ON");
const db = drizzle(sqlite, { schema });
await migrate(db, {
  migrationsFolder: "/home/hatch/workspace/mlbb-platform/standalone/drizzle",
});

const ctx: any = { db: () => db, blobs: {}, invalidateQueries: () => {} };
const A = Actions as any;

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
const eqSet = (a: number[], b: number[]) =>
  JSON.stringify([...a].sort((x, y) => x - y)) === JSON.stringify([...b].sort((x, y) => x - y));

// ---------- seeding helpers ----------
async function seedPlayer(name: string, email: string, balance: number) {
  const r = await db
    .insert(schema.players)
    .values({ name, email, balance })
    .returning({ id: schema.players.id });
  return r[0].id as number;
}
async function seedSession(playerId: number, token: string) {
  await db.insert(schema.sessions).values({
    token,
    playerId,
    expiresAt: new Date(Date.now() + 3600_000),
  });
}
async function seedPurchase(playerId: number, coins: number) {
  const r = await db
    .insert(schema.purchaseRequests)
    .values({ playerId, coins, label: "test pack", status: "pending" })
    .returning({ id: schema.purchaseRequests.id });
  return r[0].id as number;
}
async function balanceOf(playerId: number) {
  const rows = await db.select().from(schema.players);
  return rows.find((x) => x.id === playerId)?.balance ?? NaN;
}
async function ledger(playerId: number, type?: string) {
  const rows = await db.select().from(schema.transactions);
  return rows.filter(
    (t) => t.playerId === playerId && (type === undefined || t.type === type),
  );
}
async function reqRow(table: any, id: number) {
  const rows = await db.select().from(table);
  return rows.find((x: any) => x.id === id);
}

const decidePurchase = (requestId: number, decision = "approve") =>
  A.decidePurchase.handler(ctx, {
    actor_player_id: ownerId, actor: "admin", session_token: "tok-owner",
    request_id: requestId, decision, reason: "", confirmed: true, totp_code: "",
  });
const decideWithdraw = (requestId: number, decision = "reject") =>
  A.decideWithdraw.handler(ctx, {
    actor_player_id: ownerId, actor: "admin", session_token: "tok-owner",
    request_id: requestId, decision, reason: "", confirmed: true, totp_code: "",
  });
const decideDiamond = (orderId: number, decision = "reject") =>
  A.decideDiamondOrder.handler(ctx, {
    actor_player_id: ownerId, actor: "admin", session_token: "tok-owner",
    order_id: orderId, decision, reason: "", confirmed: true, totp_code: "",
  });
const requestWithdraw = (playerId: number, token: string, coins: number) =>
  A.requestWithdraw.handler(ctx, {
    player_id: playerId, session_token: token, coins, method: "kpay",
    payout_phone: "09123456789", payout_account_name: "Test Holder",
  });

// Owner (id 1): fixed admin email => effectiveRole owner, no TOTP.
const ownerId = await seedPlayer("Owner", "nnominalhuman@gmail.com", 0);
await seedSession(ownerId, "tok-owner");
// Withdraw settings for requestWithdraw cases.
await db.insert(schema.settings).values({ key: "coin_rate_kyat", value: "100" });
await db.insert(schema.settings).values({ key: "withdraw_min_coins", value: "10" });

// ---------- Case 0: migration 0024 — unique index on transactions.code ----------
console.log("Case 0: transactions_code_unique index exists and bites");
{
  const idx = sqlite
    .query("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='transactions'")
    .all() as { name: string }[];
  check("0a index present", idx.some((i) => i.name === "transactions_code_unique"), JSON.stringify(idx));
  const p = await seedPlayer("Idx", "idx@test", 0);
  await db.insert(schema.transactions).values({ code: "TXN-IDX-1", playerId: p, type: "purchase", amount: 1, balanceAfter: 1, note: "" });
  let dupFailed = false;
  try {
    await db.insert(schema.transactions).values({ code: "TXN-IDX-1", playerId: p, type: "purchase", amount: 1, balanceAfter: 2, note: "" });
  } catch { dupFailed = true; }
  check("0b duplicate code rejected", dupFailed);
}

// ---------- Case 1: concurrent DISTINCT approvals ----------
console.log("Case 1: balance 100, concurrent +20 and +30 approvals -> 150, 2 ledger rows");
{
  const buyer = await seedPlayer("BuyerA", "a@test", 100);
  const r1 = await seedPurchase(buyer, 20);
  const r2 = await seedPurchase(buyer, 30);
  const [res1, res2] = await Promise.all([decidePurchase(r1), decidePurchase(r2)]);
  check("1a both ok", res1.ok === true && res2.ok === true, JSON.stringify([res1, res2]));
  check("1b balance 150", (await balanceOf(buyer)) === 150, `got ${await balanceOf(buyer)}`);
  const rows = await ledger(buyer, "purchase");
  check("1c exactly 2 ledger rows", rows.length === 2, `got ${rows.length}`);
  check("1d balanceAfter are 120/150", eqSet(rows.map((t) => t.balanceAfter ?? -1), [120, 150]),
    JSON.stringify(rows.map((t) => t.balanceAfter)));
  check("1e amounts are 20/30", eqSet(rows.map((t) => t.amount), [20, 30]));
}

// ---------- Case 2: SAME request decided twice concurrently ----------
console.log("Case 2: same request twice concurrently -> credited once");
{
  const buyer = await seedPlayer("BuyerB", "b@test", 0);
  const r = await seedPurchase(buyer, 50);
  const [res1, res2] = await Promise.all([decidePurchase(r), decidePurchase(r)]);
  const oks = [res1, res2].filter((x) => x.ok === true).length;
  check("2a exactly one ok", oks === 1, JSON.stringify([res1, res2]));
  check("2b balance 50", (await balanceOf(buyer)) === 50, `got ${await balanceOf(buyer)}`);
  check("2c exactly 1 ledger row", (await ledger(buyer, "purchase")).length === 1);
}

// ---------- Case 3: fault injection on purchase approve ----------
console.log("Case 3: faults at claim / balance / ledger roll back fully; clean retry completes once");
{
  const faults: { name: string; sql: string }[] = [
    { name: "claim", sql: `CREATE TRIGGER f01_fail BEFORE UPDATE ON purchase_requests WHEN NEW.status='approved' BEGIN SELECT RAISE(ABORT, 'injected claim failure'); END` },
    { name: "balance", sql: `CREATE TRIGGER f01_fail BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'injected balance failure'); END` },
    { name: "ledger", sql: `CREATE TRIGGER f01_fail BEFORE INSERT ON transactions BEGIN SELECT RAISE(ABORT, 'injected ledger failure'); END` },
  ];
  for (const f of faults) {
    const buyer = await seedPlayer(`BuyerC-${f.name}`, `c-${f.name}@test`, 100);
    const r = await seedPurchase(buyer, 25);
    sqlite.exec(f.sql);
    let threw = false;
    try { await decidePurchase(r); } catch { threw = true; }
    sqlite.exec("DROP TRIGGER f01_fail");
    const req = await reqRow(schema.purchaseRequests, r);
    check(`3-${f.name}a decide threw`, threw);
    check(`3-${f.name}b request still pending`, req?.status === "pending", `got ${req?.status}`);
    check(`3-${f.name}c balance unchanged 100`, (await balanceOf(buyer)) === 100, `got ${await balanceOf(buyer)}`);
    check(`3-${f.name}d no ledger rows`, (await ledger(buyer, "purchase")).length === 0);
    const retry = await decidePurchase(r);
    check(`3-${f.name}e retry ok`, retry.ok === true, JSON.stringify(retry));
    check(`3-${f.name}f balance 125`, (await balanceOf(buyer)) === 125, `got ${await balanceOf(buyer)}`);
    check(`3-${f.name}g exactly 1 ledger row`, (await ledger(buyer, "purchase")).length === 1);
  }
}

// ---------- Case 3R: fault injection on withdraw reject (refund path) ----------
console.log("Case 3R: same fault trio on the withdraw-reject refund path");
{
  const faults: { name: string; sql: string }[] = [
    { name: "claim", sql: `CREATE TRIGGER f01_fail BEFORE UPDATE ON withdraw_requests WHEN NEW.status='rejected' BEGIN SELECT RAISE(ABORT, 'injected claim failure'); END` },
    { name: "balance", sql: `CREATE TRIGGER f01_fail BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'injected balance failure'); END` },
    { name: "ledger", sql: `CREATE TRIGGER f01_fail BEFORE INSERT ON transactions BEGIN SELECT RAISE(ABORT, 'injected ledger failure'); END` },
  ];
  for (const f of faults) {
    const player = await seedPlayer(`BuyerR-${f.name}`, `r-${f.name}@test`, 200);
    const tok = `tok-r-${f.name}`;
    await seedSession(player, tok);
    const wd = await requestWithdraw(player, tok, 40);
    if (!wd.ok) { check(`3R-${f.name} setup withdraw ok`, false, JSON.stringify(wd)); continue; }
    check(`3R-${f.name} setup: deducted to 160`, (await balanceOf(player)) === 160, `got ${await balanceOf(player)}`);
    sqlite.exec(f.sql);
    let threw = false;
    try { await decideWithdraw(wd.request_id); } catch { threw = true; }
    sqlite.exec("DROP TRIGGER f01_fail");
    const req = await reqRow(schema.withdrawRequests, wd.request_id);
    check(`3R-${f.name}a decide threw`, threw);
    check(`3R-${f.name}b request still pending`, req?.status === "pending", `got ${req?.status}`);
    check(`3R-${f.name}c balance still 160 (no partial refund)`, (await balanceOf(player)) === 160, `got ${await balanceOf(player)}`);
    check(`3R-${f.name}d no refund ledger rows`, (await ledger(player, "withdraw_refund")).length === 0);
    const retry = await decideWithdraw(wd.request_id);
    check(`3R-${f.name}e retry ok`, retry.ok === true, JSON.stringify(retry));
    check(`3R-${f.name}f balance refunded to 200`, (await balanceOf(player)) === 200, `got ${await balanceOf(player)}`);
    const refunds = await ledger(player, "withdraw_refund");
    check(`3R-${f.name}g exactly 1 refund row, keyed by request txn_code`,
      refunds.length === 1 && refunds[0]!.code === req?.txnCode,
      JSON.stringify(refunds.map((t) => t.code)) + " vs " + req?.txnCode);
  }
}

// ---------- Case 4: simultaneous credit + withdrawal creation ----------
console.log("Case 4: +50 approval and -40 withdrawal creation at once -> both land, balance 110");
{
  const player = await seedPlayer("BuyerD", "d@test", 100);
  await seedSession(player, "tok-d");
  const r = await seedPurchase(player, 50);
  const [dec, wd] = await Promise.all([decidePurchase(r), requestWithdraw(player, "tok-d", 40)]);
  check("4a approval ok", dec.ok === true, JSON.stringify(dec));
  check("4b withdrawal ok", wd.ok === true, JSON.stringify(wd));
  check("4c balance 110", (await balanceOf(player)) === 110, `got ${await balanceOf(player)}`);
  const rows = await ledger(player);
  check("4d purchase +50 row present", rows.some((t) => t.type === "purchase" && t.amount === 50));
  check("4e withdraw -40 row present", rows.some((t) => t.type === "withdraw" && t.amount === -40));
  check("4f balance never negative in ledger", rows.every((t) => (t.balanceAfter ?? 0) >= 0),
    JSON.stringify(rows.map((t) => t.balanceAfter)));
}

// ---------- Case 5a: withdraw reject refunds exactly once ----------
console.log("Case 5a: withdraw reject refunds once; second decide refused, no double refund");
{
  const player = await seedPlayer("BuyerE", "e@test", 200);
  await seedSession(player, "tok-e");
  const wd = await requestWithdraw(player, "tok-e", 40);
  check("5a0 creation ok, balance 160", wd.ok === true && (await balanceOf(player)) === 160, JSON.stringify(wd));
  const res = await decideWithdraw(wd.request_id);
  check("5a1 reject ok", res.ok === true, JSON.stringify(res));
  check("5a2 balance back to 200", (await balanceOf(player)) === 200, `got ${await balanceOf(player)}`);
  const refunds = await ledger(player, "withdraw_refund");
  const req = await reqRow(schema.withdrawRequests, wd.request_id);
  check("5a3 one refund row keyed by txn_code", refunds.length === 1 && refunds[0]!.code === req?.txnCode,
    JSON.stringify(refunds.map((t) => t.code)));
  const again = await decideWithdraw(wd.request_id);
  check("5a4 second decide refused", again.ok === false, JSON.stringify(again));
  check("5a5 balance still 200, still 1 refund row",
    (await balanceOf(player)) === 200 && (await ledger(player, "withdraw_refund")).length === 1);
}

// ---------- Case 5b: diamond reject refunds exactly once ----------
console.log("Case 5b: diamond reject refunds once, keyed by the order txn_code");
{
  const player = await seedPlayer("BuyerF", "f@test", 500);
  const ins = await db.insert(schema.diamondOrders).values({
    playerId: player, packageId: 0, packageName: "Test Pack", diamonds: 100,
    priceCoins: 120, status: "pending", txnCode: "DIA-F01-TEST",
  }).returning({ id: schema.diamondOrders.id });
  const orderId = ins[0].id as number;
  const res = await decideDiamond(orderId);
  check("5b1 reject ok", res.ok === true, JSON.stringify(res));
  check("5b2 balance 620", (await balanceOf(player)) === 620, `got ${await balanceOf(player)}`);
  const refunds = await ledger(player, "diamond_refund");
  check("5b3 one refund row keyed by DIA code", refunds.length === 1 && refunds[0]!.code === "DIA-F01-TEST",
    JSON.stringify(refunds.map((t) => t.code)));
  const again = await decideDiamond(orderId);
  check("5b4 second decide refused, unchanged",
    again.ok === false && (await balanceOf(player)) === 620 && (await ledger(player, "diamond_refund")).length === 1,
    JSON.stringify(again));
}

// ---------- Case 5c: expiry sweep refunds exactly once ----------
console.log("Case 5c: 24h expiry refunds (withdraw + diamond) land once and match ledger rows");
{
  const old = new Date(Date.now() - 25 * 3600 * 1000);
  const pw = await seedPlayer("BuyerG", "g@test", 160); // 40 already deducted
  const wdIns = await db.insert(schema.withdrawRequests).values({
    playerId: pw, coins: 40, rateKyat: 100, grossKyat: 4000, feePercent: 0, feeKyat: 0,
    netKyat: 4000, method: "kpay", payoutPhone: "09123456789", payoutAccountName: "Test Holder",
    status: "pending", txnCode: "TXN-EXP-W", createdAt: old,
  }).returning({ id: schema.withdrawRequests.id });
  const pd = await seedPlayer("BuyerH", "h@test", 380); // 120 already deducted
  const diaIns = await db.insert(schema.diamondOrders).values({
    playerId: pd, packageId: 0, packageName: "Old Pack", diamonds: 50,
    priceCoins: 120, status: "pending", txnCode: "DIA-EXP-1", createdAt: old,
  }).returning({ id: schema.diamondOrders.id });

  await A.listWithdrawRequests.handler(ctx, { actor_player_id: ownerId, session_token: "tok-owner" });
  check("5c1 withdraw refunded to 200", (await balanceOf(pw)) === 200, `got ${await balanceOf(pw)}`);
  check("5c2 diamond refunded to 500", (await balanceOf(pd)) === 500, `got ${await balanceOf(pd)}`);
  const wr = await ledger(pw, "withdraw_refund");
  const dr = await ledger(pd, "diamond_refund");
  check("5c3 refund rows keyed by request codes",
    wr.length === 1 && wr[0]!.code === "TXN-EXP-W" && dr.length === 1 && dr[0]!.code === "DIA-EXP-1",
    JSON.stringify([wr.map((t) => t.code), dr.map((t) => t.code)]));
  const wRow = await reqRow(schema.withdrawRequests, wdIns[0].id as number);
  const dRow = await reqRow(schema.diamondOrders, diaIns[0].id as number);
  check("5c4 both marked expired", wRow?.status === "expired" && dRow?.status === "expired",
    `${wRow?.status}/${dRow?.status}`);
  // Sweep again: nothing may move twice.
  await A.listWithdrawRequests.handler(ctx, { actor_player_id: ownerId, session_token: "tok-owner" });
  check("5c5 second sweep is a no-op",
    (await balanceOf(pw)) === 200 && (await balanceOf(pd)) === 500 &&
    (await ledger(pw, "withdraw_refund")).length === 1 && (await ledger(pd, "diamond_refund")).length === 1);
}

// ---------- Case 5d: notification failure after commit ----------
console.log("Case 5d: notification insert failure does NOT roll back or duplicate committed money");
{
  const buyer = await seedPlayer("BuyerI", "i@test", 100);
  const r = await seedPurchase(buyer, 25);
  sqlite.exec(`CREATE TRIGGER f01_fail BEFORE INSERT ON notifications BEGIN SELECT RAISE(ABORT, 'injected notify failure'); END`);
  let threw = false;
  try { await decidePurchase(r); } catch { threw = true; }
  sqlite.exec("DROP TRIGGER f01_fail");
  const req = await reqRow(schema.purchaseRequests, r);
  check("5d1 handler threw on notify", threw);
  check("5d2 decision stayed approved", req?.status === "approved", `got ${req?.status}`);
  check("5d3 money stayed committed (125)", (await balanceOf(buyer)) === 125, `got ${await balanceOf(buyer)}`);
  check("5d4 exactly 1 ledger row", (await ledger(buyer, "purchase")).length === 1);
  const retry = await decidePurchase(r);
  check("5d5 retry refused (no resume — ledger exists)", retry.ok === false, JSON.stringify(retry));
  check("5d6 balance still 125, still 1 row",
    (await balanceOf(buyer)) === 125 && (await ledger(buyer, "purchase")).length === 1);
}

// ---------- Case 6: resume rule (degraded/pre-F01 partial states) ----------
console.log("Case 6: resume rule completes claim-without-ledger decisions exactly once");
{
  // Purchase: claim committed (approved + txn_code) but credit never landed.
  const buyer = await seedPlayer("BuyerJ", "j@test", 100);
  const ins = await db.insert(schema.purchaseRequests).values({
    playerId: buyer, coins: 25, label: "test pack", status: "approved",
    decidedBy: "admin", decidedAt: new Date(), txnCode: "TXN-RESUME-1",
  }).returning({ id: schema.purchaseRequests.id });
  const res = await decidePurchase(ins[0].id as number);
  check("6a resume approve ok with original code", res.ok === true && res.txn_code === "TXN-RESUME-1", JSON.stringify(res));
  check("6b balance 125, 1 ledger row with the code",
    (await balanceOf(buyer)) === 125 &&
    (await ledger(buyer, "purchase")).filter((t) => t.code === "TXN-RESUME-1").length === 1,
    `bal ${await balanceOf(buyer)}`);
  const again = await decidePurchase(ins[0].id as number);
  check("6c second retry refused, unchanged",
    again.ok === false && (await balanceOf(buyer)) === 125 && (await ledger(buyer, "purchase")).length === 1,
    JSON.stringify(again));

  // Withdraw: rejection claimed but refund never landed (balance still deducted).
  const player = await seedPlayer("BuyerK", "k@test", 160);
  const wdIns = await db.insert(schema.withdrawRequests).values({
    playerId: player, coins: 40, rateKyat: 100, grossKyat: 4000, feePercent: 0, feeKyat: 0,
    netKyat: 4000, method: "kpay", payoutPhone: "09123456789", payoutAccountName: "Test Holder",
    status: "rejected", reason: "bad proof", decidedBy: "admin", decidedAt: new Date(), txnCode: "TXN-RESUME-2",
  }).returning({ id: schema.withdrawRequests.id });
  const res2 = await decideWithdraw(wdIns[0].id as number);
  check("6d resume reject ok", res2.ok === true, JSON.stringify(res2));
  check("6e balance refunded to 200, 1 refund row with the code",
    (await balanceOf(player)) === 200 &&
    (await ledger(player, "withdraw_refund")).filter((t) => t.code === "TXN-RESUME-2").length === 1,
    `bal ${await balanceOf(player)}`);
  const again2 = await decideWithdraw(wdIns[0].id as number);
  check("6f second retry refused, unchanged",
    again2.ok === false && (await balanceOf(player)) === 200 && (await ledger(player, "withdraw_refund")).length === 1,
    JSON.stringify(again2));
}

sqlite.close();
rmSync(DIR, { recursive: true, force: true });
console.log(`\n==== F01 SUITE: ${passed} passed, ${failures.length} failed ====`);
if (failures.length) {
  console.log("FAILED:", failures.join(" | "));
  process.exit(1);
}
console.log("ALL PASS");
