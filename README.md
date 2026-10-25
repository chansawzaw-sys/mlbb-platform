# MLBB Platform — Standalone Server

Muse အပြင်မှာ ကိုယ်ပိုင် server နဲ့ run လို့ရတဲ့ package ပါ။
Standalone package for the MLBB Platform web app — runs outside Muse
on any Bun host (Render, a VPS, a local machine). App behavior, auth and
the fixed admin email are unchanged: `src/actions.ts` and
`src/schema.ts` are byte-identical copies of the Muse app source.

## ဘာတွေ ပါလဲ / What's inside

- `server.ts` — Bun HTTP server: static client + action host
- `src/actions.ts`, `src/schema.ts` — the app's own server code (copies)
- `src/blobs.ts` — local-disk blob store (profile photos, team logos,
  match screenshots) implementing the SDK `BlobClient` contract
- `drizzle/` — SQL migrations (run automatically on boot)
- `client/dist/` — the built web client (unchanged; it already speaks
  `POST ./actions` with `{action, args}`, so no client rebuild was needed)
- `vendor/space-sdk/` — the `@hatch/space-sdk` package the actions
  import, vendored as extracted files so the package is self-contained
- `render.yaml` / `railway.json` — deploy config for Render / Railway

## Env vars

| Name       | Default   | Meaning                                        |
|------------|-----------|------------------------------------------------|
| `PORT`     | `3000`    | Port to listen on (host is always `0.0.0.0`)   |
| `DATA_DIR` | `./data`  | SQLite DB (`app.db`) + uploaded images (`blobs/`) live here. **Must be persistent storage**, or data is lost on restart/redeploy. |

## Local မှာ run ရန် / Run locally

```bash
bun install
bun run start          # = bun run server.ts
# open http://localhost:3000  •  health: http://localhost:3000/health
```

## Railway မှာ တင်ရန် / Deploy on Railway (current choice, 2026-10-25)

This package ships a `railway.json` with the build/start/healthcheck
settings filled in (Nixpacks detects Bun from `bun.lock`):

| Setting | Value |
|---|---|
| Builder | NIXPACKS (Bun auto-detected) |
| Build command | `bun install` |
| Start command | `bun run server.ts` |
| Health check path | `/health` (Railway injects `PORT` itself) |

**Steps:**

1. Push this package to GitHub (done: `chansawzaw-sys/mlbb-platform`).
2. railway.app → sign up with GitHub → **New Project → Deploy from
   GitHub repo** → select `mlbb-platform`. The first deploy starts
   automatically from `railway.json`.
3. **Add the Volume (REQUIRED):** open the service → **Settings →
   Volumes → Add Volume** → mount path `/data`, size **1 GB**.
   Volumes can only be added in the dashboard — no config file can
   define one. Without it, Railway's filesystem is ephemeral and all
   accounts, coins and photos are lost on every redeploy.
4. **Set the data dir:** service → **Variables** → add
   `DATA_DIR=/data` (must exactly match the volume mount path), then
   redeploy.
5. **Public URL:** service → **Settings → Networking → Generate
   Domain**. Open it and check the app loads.

Cost (railway.com/pricing, checked 2026-10-25 — recheck before
paying): new accounts get a **$5 trial credit for 30 days, no card** —
enough to test the real deploy first. Then **Hobby $5/month**, which
includes $5 of usage; this app running always-on costs roughly
**$5–8/month** total at current rates (RAM $10/GB/mo, CPU
$20/vCPU/mo, volume $0.15/GB/mo, billed per second of actual use).
No separate database is needed (SQLite lives on the volume).

After the deploy is live, do the in-app production steps: register a
normal account → Admin → Google Setup with your own Google Client
ID → sign in with Google using the fixed admin Gmail → you become
Owner → enable 2FA → Telegram → test on two real phones.

## Render မှာ တင်ရန် / Deploy on Render

This package ships a `render.yaml` Blueprint with every setting below
already filled in (verified against Render's docs, 2026-10-24).

**Option A — Blueprint (recommended):**

1. Push this package to a GitHub repo.
2. Render Dashboard → **New → Blueprint** → select the repo → **Apply**.
   Render reads `render.yaml` and creates the service + disk for you.
3. Deploy. First boot creates the DB and runs all migrations.

**Option B — Manual Web Service** (same settings, by hand):

| Setting | Value |
|---|---|
| Runtime | **Node** (Render's native Node.js/Bun runtime — Bun is preinstalled; there is no separate "bun" runtime) |
| Build command | `bun install` |
| Start command | `bun run server.ts` |
| Plan | Starter — 0.5 CPU / 512 MB (`0.5c-512mb`) |
| Health check path | `/health` |
| Persistent disk | mount path `/var/data`, size 1 GB |
| Env vars | `DATA_DIR=/var/data` (Render sets `PORT` itself — never hardcode it) |

**The persistent disk is REQUIRED, not optional.** `DATA_DIR` holds
the SQLite database and every uploaded image, and it must exactly
match the disk's mount path (`/var/data`). Without the disk, Render's
filesystem is ephemeral: all accounts, coins and photos are lost on
every redeploy or restart. (Disks attach only to paid services — the
Free plan cannot use one, and a service with a disk runs exactly one
instance.)

Cost to start (render.com/pricing, checked 2026-10-24 — recheck
before paying): Starter web service **$7/month** + disk
**$0.25/GB/month** → 1 GB disk = **$7.25/month** total. No separate
database is needed (SQLite lives on the disk).

After the deploy is live, do the in-app production steps: register a
normal account → Admin → Google Setup with your own Google Client
ID → sign in with Google using the fixed admin Gmail → you become
Owner → enable 2FA → Telegram → test on two real phones.

## VPS မှာ တင်ရန် / Deploy on a VPS

```bash
# install Bun (https://bun.sh), copy this folder to the server, then:
bun install
DATA_DIR=/var/lib/mlbb PORT=3000 bun run server.ts
```

Run it under a process manager (systemd / pm2) and put a reverse proxy
(nginx / Caddy) with HTTPS in front. Back up `DATA_DIR` regularly —
it contains the whole database and every uploaded image.

## မှတ်ချက် / Notes

- Sessions, roles and the Owner rule live entirely in the app's own
  actions (copied unchanged) — Owner comes only from Google login with
  the fixed admin Gmail, exactly as in the Muse version.
- Action protocol matches the SDK client exactly: `POST /actions`,
  body `{action, args, actionCallId}`, success `200 {data}`,
  errors `{error}` with 400 (bad args) / 404 (unknown action) / 500.
- Voice chat uses WebRTC peer-to-peer with the app's DB signaling —
  no extra server component is needed here. STUN only, as in the
  original; add a TURN server later only if voice fails on some networks.
