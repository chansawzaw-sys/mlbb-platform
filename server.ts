// Standalone host for the MLBB Platform space.
//
// Replicates the Muse runtime's action host outside Muse:
//   - POST /actions  — the exact protocol the built client speaks
//     (see @hatch/space-sdk/dist/client.js: body {action, args, actionCallId},
//     success -> 200 {data}, failure -> non-2xx {error})
//   - ctx per invocation, same shape the runtime worker builds
//     (worker.js buildCtx): { slug, invocationId, spaceDir, db, blobs,
//     executePrivileged, emit, invalidateQueries } — this app's actions
//     only use ctx.db, ctx.blobs and ctx.invalidateQueries.
//   - db: drizzle over bun:sqlite (the runtime uses libsql; both expose
//     the same SpaceDb surface the actions were written against)
//   - blobs: local-disk store under DATA_DIR/blobs (src/blobs.ts);
//     getUrl() returns ./blobs/<token>, served by this server
//   - drizzle migrations run on boot; static client served from
//     client/dist with SPA fallback
//
// Env: PORT (default 3000), DATA_DIR (default ./data)
import { Database } from "bun:sqlite";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { drizzle } from "drizzle-orm/bun-sqlite";
import { migrate } from "drizzle-orm/bun-sqlite/migrator";
import { isAction } from "@hatch/space-sdk";
import { Actions } from "./src/actions";
import * as schema from "./src/schema";
import { LocalBlobStore } from "./src/blobs";

const ROOT = import.meta.dir;
const PORT = Number(process.env.PORT ?? 3000);
const DATA_DIR = resolve(process.env.DATA_DIR ?? join(ROOT, "data"));
const DB_PATH = join(DATA_DIR, "app.db");
const CLIENT_DIR = join(ROOT, "client", "dist");
const MIGRATIONS_DIR = join(ROOT, "drizzle");
const SLUG = "mlbb-platform";

mkdirSync(DATA_DIR, { recursive: true });

// --- database ------------------------------------------------------------
const sqlite = new Database(DB_PATH, { create: true });
sqlite.exec("PRAGMA journal_mode = WAL");
sqlite.exec("PRAGMA foreign_keys = ON");
const db = drizzle(sqlite, { schema });
await migrate(db, { migrationsFolder: MIGRATIONS_DIR });

// --- blobs ---------------------------------------------------------------
const blobs = new LocalBlobStore(join(DATA_DIR, "blobs"));

// --- bundled binary assets -------------------------------------------------
// The hero images ship in the repo as base64 text (hero*.webp.b64) so the
// repository stays text-only; restore the real .webp files on boot.
const ASSETS_DIR = join(CLIENT_DIR, "assets");
if (existsSync(ASSETS_DIR)) {
  for (const name of readdirSync(ASSETS_DIR)) {
    if (!name.endsWith(".b64")) continue;
    const target = join(ASSETS_DIR, name.slice(0, -4));
    if (!existsSync(target)) {
      writeFileSync(target, Buffer.from(readFileSync(join(ASSETS_DIR, name), "utf8"), "base64"));
    }
  }
}

// --- action dispatch table ----------------------------------------------
// The runtime worker also accepts the slugified name as a fallback.
function slugifyActionName(raw: string): string {
  let normalized = "";
  let lastWasSeparator = true;
  for (const ch of raw.trim()) {
    if (/[A-Za-z0-9]/.test(ch)) {
      normalized += ch.toLowerCase();
      lastWasSeparator = false;
    } else if (!lastWasSeparator) {
      normalized += "_";
      lastWasSeparator = true;
    }
  }
  normalized = normalized.replace(/^_+|_+$/g, "");
  return normalized.length > 0 ? normalized : "action";
}

const actionsByName = new Map<string, (typeof Actions)[keyof typeof Actions]>();
for (const [name, value] of Object.entries(Actions)) {
  if (isAction(value)) {
    actionsByName.set(name, value);
    actionsByName.set(slugifyActionName(name), value);
  }
}

function buildCtx() {
  return {
    slug: SLUG,
    invocationId: randomUUID(),
    spaceDir: ROOT,
    db: () => db,
    blobs,
    // No privileged contracts are declared by this app's actions; any
    // attempt to use one standalone fails loudly instead of silently.
    executePrivileged: async () => {
      throw new Error("privileged contracts are not available in standalone hosting");
    },
    emit: () => {},
    invalidateQueries: () => {},
  };
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function handleAction(req: Request): Promise<Response> {
  let body: { action?: unknown; args?: unknown };
  try {
    body = (await req.json()) as { action?: unknown; args?: unknown };
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }
  const actionName = typeof body.action === "string" ? body.action : "";
  const definition = actionsByName.get(actionName) ?? actionsByName.get(slugifyActionName(actionName));
  if (!definition) {
    return json({ error: `action '${actionName}' is not defined` }, 404);
  }
  let parsedArgs: unknown;
  try {
    parsedArgs = definition.request.parse(body.args ?? {});
  } catch (err) {
    return json({ error: `invalid args: ${err instanceof Error ? err.message : String(err)}` }, 400);
  }
  try {
    const result = await definition.handler(buildCtx() as never, parsedArgs as never);
    const validated = definition.response.parse(result);
    return json({ data: validated });
  } catch (err) {
    return json({ error: err instanceof Error ? err.message : String(err) }, 500);
  }
}

// --- static client --------------------------------------------------------
const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".webp": "image/webp",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

async function serveStatic(pathname: string): Promise<Response> {
  const rel = pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
  const filePath = resolve(join(CLIENT_DIR, rel));
  if (filePath.startsWith(CLIENT_DIR + sep) && existsSync(filePath)) {
    const file = Bun.file(filePath);
    if (file.size > 0 || (await file.exists())) {
      const ext = filePath.slice(filePath.lastIndexOf("."));
      return new Response(file, {
        headers: { "content-type": MIME[ext] ?? file.type ?? "application/octet-stream" },
      });
    }
  }
  // SPA fallback
  const index = Bun.file(join(CLIENT_DIR, "index.html"));
  return new Response(index, { headers: { "content-type": MIME[".html"] } });
}

// --- server ---------------------------------------------------------------
const server = Bun.serve({
  hostname: "0.0.0.0",
  port: PORT,
  // avatar uploads travel as base64 JSON (up to ~12M chars from the client)
  maxRequestBodySize: 64 * 1024 * 1024,
  async fetch(req) {
    const url = new URL(req.url);
    const path = url.pathname;

    if (req.method === "GET" && path === "/health") {
      return json({ ok: true, slug: SLUG, actions: Object.keys(Actions).length, time: new Date().toISOString() });
    }
    if (req.method === "POST" && (path === "/actions" || path.endsWith("/actions"))) {
      return handleAction(req);
    }
    if (req.method === "GET" && path.startsWith("/blobs/")) {
      const found = await blobs.fileForToken(decodeURIComponent(path.slice("/blobs/".length)));
      if (!found) return new Response("not found", { status: 404 });
      return new Response(found.file, { headers: { "content-type": found.contentType } });
    }
    if (req.method === "GET") {
      return serveStatic(path);
    }
    return new Response("not found", { status: 404 });
  },
});

console.log(`[mlbb-standalone] listening on http://0.0.0.0:${server.port}`);
console.log(`[mlbb-standalone] data dir: ${DATA_DIR}`);
console.log(`[mlbb-standalone] actions loaded: ${Object.keys(Actions).length}`);
