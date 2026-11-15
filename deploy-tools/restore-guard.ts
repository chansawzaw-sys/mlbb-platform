// F12 restore guard — make a restored backup safe to boot around the
// historic purge migrations 0012 (mass purge of non-Owner players) and
// 0018 (literal player-id 40/42 removal).
//
// WHY: the drizzle migrator applies every journal entry whose `when` is
// greater than the latest created_at recorded in __drizzle_migrations.
// A backup whose journal predates 0012/0018 will REPLAY those data-only
// purges on first boot and destroy the backup's accounts and money
// history. Both purges are DATA-ONLY (no DDL), so on a backup whose data
// already post-dates the real-world purge events the correct restore is
// to record 0012/0018 as applied — with the exact sha256 hashes the
// migrator computes over the unchanged files, the same mechanism
// server.ts already uses for migration 0017 — while still applying the
// genuinely missing DDL migrations around them (0013–0016 via the real
// migrator, 0017 via the server.ts reconciliation recipe). Migrations
// 0019+ are left for the normal app boot.
//
// WHAT THIS TOOL NEVER DOES: it never executes the purge SQL, never
// deletes or updates a data row, and never touches a live server. It
// only (a) copies the DB file to a backup path, (b) inserts
// __drizzle_migrations rows, and (c) applies the DDL-only migrations
// 0013–0016 / the 0017 objects when bridging past them.
//
// Usage:
//   bun deploy-tools/restore-guard.ts <app.db> --confirm --backup-out <copy.db>
//   bun deploy-tools/restore-guard.ts <app.db>            (report-only:
//                                                          prints state;
//                                                          refuses to
//                                                          change anything
//                                                          without flags)
// Exit codes: 0 = done / nothing to do · 2 = usage or refusal ·
//             3 = failure after changes began or audit mismatch
//             (restore the --backup-out copy).

import { Database } from "bun:sqlite";
import { createHash } from "node:crypto";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const STANDALONE = join(import.meta.dir, "..", "standalone");
const { drizzle } = await import(
  join(STANDALONE, "node_modules", "drizzle-orm", "bun-sqlite", "index.js")
);
const { migrate } = await import(
  join(STANDALONE, "node_modules", "drizzle-orm", "bun-sqlite", "migrator.js")
);

// ---------- args -----------------------------------------------------------
const argv = process.argv.slice(2);
let dbPath = "";
let confirm = false;
let backupOut = "";
let migrationsDir = join(STANDALONE, "drizzle");
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === "--confirm") confirm = true;
  else if (a === "--backup-out") backupOut = argv[++i] ?? "";
  else if (a === "--migrations-dir") migrationsDir = resolve(argv[++i] ?? "");
  else if (a.startsWith("--")) {
    console.error(`restore-guard: unknown flag ${a}`);
    process.exit(2);
  } else if (!dbPath) dbPath = resolve(a);
  else {
    console.error(`restore-guard: unexpected extra argument ${a}`);
    process.exit(2);
  }
}
if (!dbPath) {
  console.error(
    "usage: bun deploy-tools/restore-guard.ts <app.db> --confirm --backup-out <copy.db>",
  );
  process.exit(2);
}
if (!existsSync(dbPath)) {
  console.error(`restore-guard: database file not found: ${dbPath}`);
  process.exit(2);
}
if (backupOut && resolve(backupOut) === dbPath) {
  console.error("restore-guard: --backup-out must differ from the database path");
  process.exit(2);
}

// ---------- journal facts --------------------------------------------------
type JournalEntry = {
  idx: number;
  version: string;
  when: number;
  tag: string;
  breakpoints: boolean;
};
const journal = JSON.parse(
  readFileSync(join(migrationsDir, "meta", "_journal.json"), "utf8"),
) as { entries: JournalEntry[] };
const entryByPrefix = (prefix: string) =>
  journal.entries.find((e) => e.tag.startsWith(prefix));
const hashOf = (entry: JournalEntry) =>
  createHash("sha256")
    .update(readFileSync(join(migrationsDir, `${entry.tag}.sql`), "utf8"))
    .digest("hex");

const e0012 = entryByPrefix("0012_");
const e0017 = entryByPrefix("0017_");
const e0018 = entryByPrefix("0018_");
if (!e0012 || !e0017 || !e0018) {
  console.error("restore-guard: journal is missing 0012/0017/0018 entries — aborting");
  process.exit(2);
}
const hash0012 = hashOf(e0012);
const hash0017 = hashOf(e0017);
const hash0018 = hashOf(e0018);
// DDL migrations strictly between the two purges (0013–0016 today).
const bridgeCandidates = journal.entries
  .filter((e) => e.when > e0012.when && e.when < e0017.when)
  .sort((a, b) => a.when - b.when);
for (const e of bridgeCandidates) {
  if (e.tag.startsWith("0012_") || e.tag.startsWith("0018_")) {
    console.error(`restore-guard: bridge set unexpectedly contains ${e.tag} — aborting`);
    process.exit(2);
  }
}

// ---------- inspect the database -------------------------------------------
const sqlite = new Database(dbPath);
const tableExists = (name: string) =>
  (
    sqlite
      .query("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name = ?")
      .get(name) as { n: number }
  ).n > 0;

if (!tableExists("players") || !tableExists("__drizzle_migrations")) {
  console.log(
    "restore-guard: fresh/uninitialized database (no players or no migration journal).",
  );
  console.log("restore-guard: nothing to guard — a normal first boot is safe.");
  sqlite.close();
  process.exit(0);
}
const recorded = sqlite
  .query("SELECT hash, created_at FROM __drizzle_migrations")
  .all() as { hash: string; created_at: number }[];
if (recorded.length === 0) {
  console.error(
    "restore-guard: __drizzle_migrations is EMPTY but tables exist. This is not a",
  );
  console.error(
    "restore-guard: migration-journal restore state and is outside this tool's scope.",
  );
  sqlite.close();
  process.exit(2);
}
const maxCreated = Math.max(...recorded.map((r) => Number(r.created_at)));
const hasHash = (h: string) => recorded.some((r) => r.hash === h);
const positionTag =
  journal.entries.filter((e) => e.when <= maxCreated).at(-1)?.tag ?? "(before 0001)";

const need0012 = !hasHash(hash0012) && maxCreated < e0012.when;
const bridge = bridgeCandidates.filter((e) => e.when > maxCreated);
const need0017 = !hasHash(hash0017) && maxCreated < e0017.when;
const need0018 = !hasHash(hash0018) && maxCreated < e0018.when;

console.log(`restore-guard: database        = ${dbPath}`);
console.log(
  `restore-guard: journal position = ${positionTag} (created_at ${maxCreated})`,
);
console.log(
  `restore-guard: 0012 ${e0012.tag}: ${hasHash(hash0012) ? "recorded" : maxCreated >= e0012.when ? "skipped by position" : "NOT recorded — purge would replay on boot"}`,
);
console.log(
  `restore-guard: 0018 ${e0018.tag}: ${hasHash(hash0018) ? "recorded" : maxCreated >= e0018.when ? "skipped by position" : "NOT recorded — purge would replay on boot"}`,
);
if (bridge.length > 0) {
  console.log(
    `restore-guard: DDL bridge needed: ${bridge.map((e) => e.tag).join(", ")}`,
  );
}
if (need0017) console.log("restore-guard: 0017 objects/journal need reconciliation");

if (!need0012 && bridge.length === 0 && !need0017 && !need0018) {
  console.log("restore-guard: nothing to do — both purge migrations are covered.");
  sqlite.close();
  process.exit(0);
}

// ---------- flags gate (no writes have happened yet) ------------------------
if (!confirm || !backupOut) {
  console.error("restore-guard: REFUSING to change anything without both flags:");
  console.error("restore-guard:   --confirm and --backup-out <copy.db> are required.");
  console.error(
    "restore-guard: re-run with them once you have verified this backup's data",
  );
  console.error("restore-guard: post-dates the real purge events (see RUNBOOK §8).");
  sqlite.close();
  process.exit(2);
}
if (existsSync(backupOut)) {
  console.error(`restore-guard: --backup-out already exists: ${backupOut}`);
  console.error("restore-guard: refusing to overwrite an existing backup copy.");
  sqlite.close();
  process.exit(2);
}

// ---------- backup copy + audit (before) ------------------------------------
try {
  sqlite.exec("PRAGMA wal_checkpoint(TRUNCATE)");
} catch {
  // Not a WAL database — nothing to checkpoint.
}
copyFileSync(dbPath, backupOut);
console.log(
  `restore-guard: backup copy written: ${backupOut} (${statSync(backupOut).size} bytes)`,
);

const AUDIT_TABLES = [
  "players",
  "transactions",
  "matches",
  "purchase_requests",
  "withdraw_requests",
];
const audit = () => {
  const out: Record<string, number | null> = {};
  for (const t of AUDIT_TABLES) {
    out[t] = tableExists(t)
      ? (sqlite.query(`SELECT COUNT(*) AS n FROM ${t}`).get() as { n: number }).n
      : null;
  }
  return out;
};
const printAudit = (label: string, a: Record<string, number | null>) => {
  console.log(`restore-guard: audit ${label}:`);
  for (const t of AUDIT_TABLES) {
    console.log(`  ${t.padEnd(18)} ${a[t] === null ? "n/a (table absent)" : a[t]}`);
  }
};
const before = audit();
printAudit("BEFORE", before);

// ---------- apply ------------------------------------------------------------
const insertMigRow = (hash: string, when: number) =>
  sqlite
    .query('INSERT INTO `__drizzle_migrations` ("hash", "created_at") VALUES (?, ?)')
    .run(hash, when);

try {
  if (need0012) {
    insertMigRow(hash0012, e0012.when);
    console.log(`restore-guard: recorded 0012 as applied (hash ${hash0012.slice(0, 12)}…)`);
  }
  if (bridge.length > 0) {
    const tmp = mkdtempSync(join(tmpdir(), "restore-guard-"));
    try {
      mkdirSync(join(tmp, "meta"));
      writeFileSync(
        join(tmp, "meta", "_journal.json"),
        JSON.stringify({ version: "7", dialect: "sqlite", entries: bridge }, null, 2),
      );
      for (const e of bridge) {
        copyFileSync(join(migrationsDir, `${e.tag}.sql`), join(tmp, `${e.tag}.sql`));
      }
      const db = drizzle(sqlite);
      await migrate(db, { migrationsFolder: tmp });
      console.log(
        `restore-guard: applied DDL bridge via migrator: ${bridge.map((e) => e.tag).join(", ")}`,
      );
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  }
  if (need0017 || need0018) {
    sqlite.exec("BEGIN");
    try {
      if (need0017) {
        // Mirror server.ts's migration-0017 reconciliation exactly:
        // ensure the objects, then record the journal row with the real
        // sha256 of the unchanged file. (The raw 0017 SQL also creates a
        // UNIQUE INDEX that cannot succeed on legacy duplicate pairs —
        // production deliberately runs without it; pair uniqueness is
        // enforced at the application level.)
        const cols = sqlite
          .query("SELECT name FROM pragma_table_info('players')")
          .all() as { name: string }[];
        if (!cols.some((c) => c.name === "game_name_locked")) {
          sqlite.exec(
            "ALTER TABLE `players` ADD COLUMN `game_name_locked` integer DEFAULT 0 NOT NULL",
          );
        }
        sqlite.exec(
          "CREATE TABLE IF NOT EXISTS `google_pending` (`token_hash` text PRIMARY KEY NOT NULL, `google_sub` text DEFAULT '' NOT NULL, `email` text DEFAULT '' NOT NULL, `name` text DEFAULT '' NOT NULL, `created_at` integer NOT NULL, `expires_at` integer NOT NULL)",
        );
        insertMigRow(hash0017, e0017.when);
        console.log("restore-guard: reconciled 0017 (objects ensured, journal recorded)");
      }
      if (need0018) {
        insertMigRow(hash0018, e0018.when);
        console.log(
          `restore-guard: recorded 0018 as applied (hash ${hash0018.slice(0, 12)}…)`,
        );
      }
      sqlite.exec("COMMIT");
    } catch (err) {
      sqlite.exec("ROLLBACK");
      throw err;
    }
  }
} catch (err) {
  console.error(`restore-guard: FAILED while applying changes: ${String(err)}`);
  console.error(`restore-guard: restore the pre-guard copy before booting: ${backupOut}`);
  sqlite.close();
  process.exit(3);
}

// ---------- audit (after) + verdict ------------------------------------------
const after = audit();
printAudit("AFTER", after);
const mismatch = AUDIT_TABLES.filter((t) => before[t] !== after[t]);
if (mismatch.length > 0) {
  console.error(
    `restore-guard: AUDIT MISMATCH on: ${mismatch.join(", ")} — row counts changed!`,
  );
  console.error(`restore-guard: restore the pre-guard copy: ${backupOut}`);
  sqlite.close();
  process.exit(3);
}
const newMax = (
  sqlite
    .query("SELECT MAX(created_at) AS m FROM __drizzle_migrations")
    .get() as { m: number }
).m;
const newTag =
  journal.entries.filter((e) => e.when <= Number(newMax)).at(-1)?.tag ?? "?";
console.log(
  `restore-guard: done. Journal position now ${newTag} (created_at ${newMax}).`,
);
console.log(
  "restore-guard: boot the app normally — 0012/0018 will be skipped and any",
);
console.log("restore-guard: genuinely missing later migrations will still apply.");
sqlite.close();
process.exit(0);
