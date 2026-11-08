# Video Translator — VPS Deploy Notes

One-script deployment of the Myanmar video translator app onto the shared
MLBB VPS (Ubuntu 24.04, 1 vCPU / 2 GB RAM / 25 GB disk, Hong Kong).
The app itself is **not modified** — everything lives in this `deploy/`
folder plus the artifacts the script installs on the server.

---

## 0. Before you run (operator checklist)

1. **Run the MLBB bootstrap first** (recommended order). It installs Docker
   and the `mlbb` container on host port **8080**. This script shares the
   box politely: different ports, capped CPU/RAM, no Docker involvement.
2. **Put the app source at `/opt/video-translator`** on the server. The
   script refuses to continue without it. The source lives in the public
   repo `chansawzaw-sys/mlbb-platform` under **`translator-app/`**, and
   the MLBB bootstrap has already cloned that repo to `/opt/mlbb-platform`
   on the server — so deploy day is just:

   ```bash
   git -C /opt/mlbb-platform pull --ff-only   # make sure the clone is current
   cp -r /opt/mlbb-platform/translator-app /opt/video-translator
   ```

   What is **intentionally absent** from the repo copy — the script
   handles all of it on the server:
   - `models/` — the script downloads `Systran/faster-whisper-small`
     (~464 MB) from HuggingFace and smoke-loads it, so the first job is
     not slow. (If a local `models/` is ever copied in by hand, the
     script reuses it instead.)
   - `assets/fonts/` — the binary TTFs don't travel through the repo
     push; the script repairs the 6 bundled Myanmar fonts from distro
     `.deb` packages (same recipes as `setup-codespace.sh`) and fails
     loudly if it cannot.
   - `jobs/`, `previews/`, `uploads/`, `.venv/`, `__pycache__/` —
     runtime data and rebuildables; the script creates the empty dirs
     it needs and rebuilds the venv.
   - `.env` — never committed (see §8 for the optional Gemini key).

   *Fallback (no repo handy):* tar up the local tree and copy it over by
   hand — from the operator machine:

   ```bash
   tar --exclude='.venv' --exclude='__pycache__' --exclude='*/__pycache__' \
       -czf /tmp/video-translator.tar.gz -C ~/workspace video-translator
   # transfer /tmp/video-translator.tar.gz to the server, then on the server:
   mkdir -p /opt/video-translator /tmp/vt-src
   tar -xzf video-translator.tar.gz -C /tmp/vt-src
   cp -a /tmp/vt-src/video-translator/. /opt/video-translator/
   ```

   Including `models/` in that tarball makes the deploy faster.
   (The script also supports `TRANSLATOR_REPO_URL=<git url>` — it clones
   into `/opt/video-translator` when that directory is empty.)
3. You need a root shell on the server (web console / SSH). Nothing else —
   no API keys, no accounts.

## 1. The deploy commands

The script ships inside the source copy, so once §0 step 2 is done, run
(as root, from any directory):

```bash
bash /opt/video-translator/deploy/translator-bootstrap.sh
```

The full deploy-day sequence, start to finish:

```bash
git -C /opt/mlbb-platform pull --ff-only
cp -r /opt/mlbb-platform/translator-app /opt/video-translator
bash /opt/video-translator/deploy/translator-bootstrap.sh
```

The script alone is also fetchable from its raw URL —
`https://raw.githubusercontent.com/chansawzaw-sys/mlbb-platform/main/translator-app/deploy/translator-bootstrap.sh`
— but the app source must still be in place at `/opt/video-translator`
first; the script by itself is not the app.

Success ends with:

```
TRANSLATOR_DEPLOY_DONE
TRANSLATOR_AUTH king <password>
```

Failure prints `TRANSLATOR_DEPLOY_FAILED` plus the last service log lines.
Full log: `/root/translator-deploy.log` (root-only). The script is
idempotent — re-running is safe and keeps the same auth password.

## 2. What it installs

| Piece | Detail |
|---|---|
| Swap | 2 GB `/swapfile` — same guarded block as the MLBB bootstrap (no-op if swap already exists) |
| Packages | `python3`, `python3-venv`, `ffmpeg`/`ffprobe`, `curl`, `git`, `ufw`, `file`; Caddy from the official Cloudsmith apt repo |
| User | dedicated system user `translator` (nologin shell) — the app never runs as root |
| App env | `/opt/video-translator/.venv` from the pinned `requirements.txt` (faster-whisper, edge-tts, Flask, yt-dlp, …) |
| Whisper model | `models/faster-whisper-small/` — reused if present, else `Systran/faster-whisper-small` from HuggingFace, then **smoke-loaded once** so the first job is fast |
| Fonts | the app's 6 bundled Myanmar TTFs in `assets/fonts/` are verified (and repaired from distro `.deb`s if missing). The app uses *only* these, via ffmpeg `fontsdir` — no system font config involved |
| Launcher | `/opt/translator-deploy/serve.py` — runs the app's own Flask object through its venv, binds **127.0.0.1:8613**, and adds the one-job gate (below). App files are untouched |
| Service | systemd `translator.service`: auto-start on boot, `Restart=on-failure`, `MemoryMax=1500M`, `CPUQuota=90%` |
| Proxy | Caddy site block in `/etc/caddy/translator.caddy` (imported from the main Caddyfile, so a future MLBB domain block can coexist), public port **8611**, HTTP **basic auth**, gzip for text |
| Firewall | ufw: allows 22 + **8611** (8080/80/443 are MLBB's, untouched by this script) |

## 3. Ports

| Port | Owner | Exposure |
|---|---|---|
| 8080 | MLBB container (other bootstrap) | public (theirs) |
| **8611** | **Caddy → translator** | **public, basic-auth** |
| 8613 | translator Flask app | 127.0.0.1 only, never opened in ufw |
| 80 / 443 | reserved for the future MLBB domain | — |

User-facing URL for the trial: **`http://<server-ip>:8611/`** (plain HTTP —
there is no domain yet; basic auth is the only protection, so treat the
link + password as private).

## 4. Basic auth

- Username: **`king`** (fixed).
- Password: random 16-char alphanumeric, generated **on the server** at
  first deploy. It appears exactly once in the script output as
  `TRANSLATOR_AUTH king <password>`, and is stored root-only at
  **`/root/translator-auth.txt`** (`chmod 600`). Re-runs reuse it.
- Read it later with: `cat /root/translator-auth.txt`
- The Caddyfile stores only the bcrypt hash, never the plaintext.
- To rotate: delete `/root/translator-auth.txt` and re-run the script
  (a new password is generated; the old one stops working on Caddy restart).

## 5. Health checks

```bash
systemctl status translator          # service state
journalctl -u translator -f          # live app logs
curl -s http://127.0.0.1:8613/ | head   # app directly (no auth needed, localhost)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8611/        # expect 401
curl -s -u king:<password> http://127.0.0.1:8611/ | head                # expect the UI HTML
curl -s -u king:<password> http://<server-ip>:8611/ | head              # same, from outside
systemctl status caddy
free -h; df -h /                     # RAM/swap and disk headroom
```

## 6. The one-job-at-a-time gate

The app starts a thread per job with no internal serialization. On this
box, two overlapping jobs (whisper + ffmpeg each) can exhaust RAM and get
the service OOM-killed, losing both jobs. The launcher therefore rejects
`POST /api/jobs` and `POST /api/jobs/<id>/review` (the two phase starts)
with **HTTP 429** and a Myanmar explanation while another job is
`queued`/`running`. Preview downloads, exports and bulk-text input are not
gated. A `queued`/`running` entry older than 6 hours is treated as a stale
leftover and ignored. To disable the gate entirely, set
`TRANSLATOR_GATE=off` in `/etc/systemd/system/translator.service` and
`systemctl daemon-reload && systemctl restart translator`.

## 7. Resource expectations (25 GB disk / 2 GB RAM, shared with MLBB)

- Whisper model on disk: **~464 MB** (`models/faster-whisper-small/model.bin`
  alone is 483,546,902 bytes ≈ 461 MiB). In RAM, the int8 `small` model
  runs at roughly 0.5–0.8 GB RSS.
- venv: ~0.7–1 GB. App source + fonts: ~10 MB. Caddy + ffmpeg: ~150 MB.
- Each job keeps its source video, intermediates and `output.mp4` under
  `jobs/<id>/` — **this grows unbounded**. Prune finished jobs regularly:
  `du -sh /opt/video-translator/jobs` and delete old job folders (the app
  has no built-in cleanup). Budget a few GB for jobs; the MLBB data volume
  lives separately under `/srv/mlbb/data`.
- Job speed on 1 vCPU: transcription is the slow part (int8 `small` on one
  core ≈ real-time or slower for long videos). That is expected on this
  tier; keep trial videos short.
- `WHISPER_MODEL=tiny|base` in the service unit would cut RAM/time at some
  accuracy cost — not set by default (the app defaults to `small`, and the
  local model folder is used automatically when present).

## 8. Known limits / deploy-day notes

- **No Gemini key** → in-app auto-translation uses the app's free provider
  chain (Google via deep-translator → Bing → source text). That is the
  app's designed fallback and worked in trials; quality varies by network.
  To enable Gemini later: create `/opt/video-translator/.env` containing
  `GEMINI_API_KEY=...` (owner `translator`, mode 600) and
  `systemctl restart translator`. Never put the key in chat or in git.
- **Downloads and edge-tts work on the VPS** — yt-dlp (YouTube/TikTok) and
  the edge-tts websockets were blocked from the build sandbox, but a normal
  VPS connection reaches them; they were the primary providers in the
  app's design. If edge-tts ever fails, the app falls back to Google
  classic TTS, then gTTS, per segment (the provider used is recorded per
  segment).
- **Voice default**: the UI offers Nilar (female) and Thiha (male,
  `my-MM-ThihaNeural`); the trial preference was **Thiha**.
- **Plain HTTP trial**: until a domain is attached, the link is
  `http://<ip>:8611/` with basic auth. A commented placeholder in
  `/etc/caddy/translator.caddy` shows exactly where the future
  `translator.<domain>` HTTPS block goes (Caddy auto-provisions the cert
  once DNS points here). When that day comes, also update nothing else —
  the app and gate are domain-agnostic.
- **Shared-box etiquette**: during a translator job, expect the MLBB app to
  feel slightly slower (90% CPU cap still leaves it responsive). The MLBB
  container has no memory cap in its bootstrap; the translator's 1.5 GB
  cap plus 2 GB swap is what keeps the peace.
- If Caddy was already configured by hand for something else, the script
  still only *adds* its import line + its own `translator.caddy` file; it
  never rewrites an existing main Caddyfile.
- Rollback: `systemctl disable --now translator caddy` stops the trial;
  delete `/etc/caddy/translator.caddy` and the import line to remove the
  site. App data stays under `/opt/video-translator` (jobs included).
