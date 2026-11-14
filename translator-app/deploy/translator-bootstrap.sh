#!/usr/bin/env bash
# Video Translator — one-shot VPS deployment for the Myanmar video translator
# app (companion to the MLBB KING bootstrap; the two share one small VPS).
#
# Target: Ubuntu 24.04, 1 vCPU / 2 GB RAM / 25 GB disk, root shell.
# Intended run order: AFTER the MLBB bootstrap (which installs Docker and a
# container on host port 8080). This script uses none of MLBB's ports and is
# safe to run before it, too — shared steps (swap) are guarded no-ops then.
#
#   curl -fsSL <RAW_URL> | bash        # see DEPLOY-NOTES.md for the real URL
#
# What it does (idempotent — safe to re-run):
#   1. Ensures a 2 GB swapfile (identical guard to the MLBB bootstrap).
#   2. Installs python3 + venv, ffmpeg, Caddy (official repo), base tools.
#   3. Creates a dedicated unprivileged "translator" system user.
#   4. Verifies the app source at /opt/video-translator (see SOURCE below),
#      repairs the bundled Myanmar fonts if the copy dropped them, builds
#      the app's .venv from requirements.txt, and pre-downloads the
#      faster-whisper "small" model (~464 MB) if it is not already there,
#      then smoke-loads it once so the first real job is not slow.
#   5. Installs a systemd service "translator" running the app UNCHANGED
#      through a tiny launcher (deploy artifact, not an app file). The
#      launcher binds the app to 127.0.0.1:8613 and adds a queue
#      sanity cap (the app's own dispatcher runs four heavy jobs
#      at once and queues the rest — build 180). TRANSLATOR_GATE=off
#      in the unit disables the cap.
#   6. Puts Caddy in front on public port 8611 with HTTP basic auth
#      (user "htetheather"; password generated once, stored root-only in
#      /root/translator-auth.txt, printed once at the end).
#   7. Opens ONLY port 8611 in ufw (the app port 8613 stays private;
#      8080/80/443 remain MLBB's business).
#
# Deliberately NOT done here: no domain/HTTPS yet (a marked block in the
# Caddy config shows where the domain site will slot in), no app source
# edits, no GEMINI_API_KEY (optional — the app runs fine without it; see
# DEPLOY-NOTES.md). Full output is logged to /root/translator-deploy.log.
# Success ends with TRANSLATOR_DEPLOY_DONE; any failure prints
# TRANSLATOR_DEPLOY_FAILED.

set -euo pipefail

# --- configuration ----------------------------------------------------------
APP_DIR='/opt/video-translator'          # app source must live here
APP_USER='translator'                    # dedicated system user
DEPLOY_DIR='/opt/translator-deploy'      # launcher + deploy artifacts
INTERNAL_PORT='8613'                     # Flask app, 127.0.0.1 only
PUBLIC_PORT='8611'                       # Caddy, public, basic-auth
TLS_NAME='85-155-149-58.sslip.io'        # free sslip.io name -> this server's IP; Caddy gets a real Let's Encrypt cert for it (HTTPS = full PWA install). Empty = HTTP only.
AUTH_USER='htetheather'
AUTH_FILE='/root/translator-auth.txt'
LOG_FILE='/root/translator-deploy.log'
SERVICE_NAME='translator'

# --- wall flip (owner-controlled) ---------------------------------------------------
# VT_FLIP=1 in the app .env (armed from the app's Admin card) means:
# serve WITHOUT the Caddy basic-auth wall and enforce web accounts
# (VT_ACCOUNTS=1 in the unit env). Absent/0 = the wall stands and
# accounts stay in shadow mode. The auth file is kept either way, so
# disarming the flag brings the identical wall back on the next run.
FLIP=0
if grep -q '^VT_FLIP=1' "$APP_DIR/.env" 2>/dev/null; then
  FLIP=1
fi
VT_ENV_LINE=""
if [ "$FLIP" -eq 1 ]; then
  VT_ENV_LINE="Environment=VT_ACCOUNTS=1"
fi
# Optional: if the app source is ever published to a git repo, put its clone
# URL here (or export TRANSLATOR_REPO_URL before running) and the script
# will clone it when $APP_DIR is empty. Default: source is placed by hand.
TRANSLATOR_REPO_URL="${TRANSLATOR_REPO_URL:-}"

# --- logging + failure trap ---------------------------------------------------
mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
exec </dev/null

on_error() {
  echo "TRANSLATOR_DEPLOY_FAILED (exit=$?, line=$1)"
  echo "--- last service log lines (if any) ---"
  journalctl -u "$SERVICE_NAME" -n 50 --no-pager 2>/dev/null || true
  echo "--- full log: $LOG_FILE ---"
}
trap 'on_error $LINENO' ERR

echo "=== Translator deploy starting: $(date -Is) ==="

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: this script must run as root."
  exit 1
fi

# --- 1. swap (2 GB; same guard as the MLBB bootstrap) -------------------------
if [ -z "$(swapon --show --noheadings 2>/dev/null || true)" ]; then
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
fi
if ! grep -q '^/swapfile' /etc/fstab; then
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
free -h || true

# --- 2. packages: python, ffmpeg, caddy, tools --------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl git ufw file python3 python3-venv ffmpeg nodejs

if ! command -v caddy >/dev/null 2>&1; then
  # Official Caddy apt repository (stable, deterministic on Ubuntu 24.04).
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi
caddy version
ffmpeg -version | head -1 || true
python3 --version

# --- 3. dedicated user ----------------------------------------------------------
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --no-create-home \
    --shell /usr/sbin/nologin "$APP_USER"
fi
echo "Service user: $APP_USER"

# --- 4. app source ----------------------------------------------------------------
if [ ! -f "$APP_DIR/app.py" ]; then
  if [ -n "$TRANSLATOR_REPO_URL" ]; then
    if [ -d "$APP_DIR/.git" ]; then
      git -C "$APP_DIR" pull --ff-only
    else
      mkdir -p "$(dirname "$APP_DIR")"
      git clone "$TRANSLATOR_REPO_URL" "$APP_DIR"
    fi
  fi
fi
if [ ! -f "$APP_DIR/app.py" ] || [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "ERROR: app source not found at $APP_DIR (app.py / requirements.txt missing)."
  echo "Place the video-translator source tree there first (see DEPLOY-NOTES.md),"
  echo "or re-run with TRANSLATOR_REPO_URL=<git url> set."
  exit 1
fi
cd "$APP_DIR"
mkdir -p jobs previews uploads models assets/fonts

# --- 5. Myanmar fonts (bundled in assets/fonts; repair only if missing) -----------
# The app points ffmpeg's subtitles filter at assets/fonts via fontsdir, so
# these files ARE the fonts the app uses. The extraction recipes below are
# the app's own proven ones (setup-codespace.sh), used only for files the
# source copy dropped.
font_ok() { [ -s "assets/fonts/$1" ] && file "assets/fonts/$1" | grep -qi 'font'; }
if ! font_ok NotoSansMyanmar-Bold.ttf || ! font_ok NotoSansMyanmar-Regular.ttf \
   || ! font_ok Padauk-Bold.ttf || ! font_ok Padauk-Regular.ttf \
   || ! font_ok Pyidaungsu-2.3_Bold.ttf || ! font_ok Pyidaungsu-2.3_Regular.ttf; then
  echo "Some bundled Myanmar fonts are missing — repairing from distro packages..."
  FONT_TMP="$(mktemp -d)"
  if ! font_ok Pyidaungsu-2.3_Regular.ttf || ! font_ok Pyidaungsu-2.3_Bold.ttf; then
    DEB="$(curl -s http://archive.ubuntu.com/ubuntu/pool/universe/f/fonts-myanmar/ \
      | grep -oE 'fonts-myanmar_[^"]*_all\.deb' | grep -v '~' | sort -u | tail -1)"
    DEB="${DEB:-fonts-myanmar_0.0-1_all.deb}"
    curl -sL -o "$FONT_TMP/fm.deb" \
      "http://archive.ubuntu.com/ubuntu/pool/universe/f/fonts-myanmar/$DEB"
    dpkg-deb -x "$FONT_TMP/fm.deb" "$FONT_TMP/fm"
    cp "$FONT_TMP/fm/usr/share/fonts/truetype/mm/Pyidaungsu-2.3_Regular.ttf" assets/fonts/
    cp "$FONT_TMP/fm/usr/share/fonts/truetype/mm/Pyidaungsu-2.3_Bold.ttf" assets/fonts/
  fi
  if ! font_ok Padauk-Regular.ttf || ! font_ok Padauk-Bold.ttf; then
    DEB="$(curl -s https://deb.debian.org/debian/pool/main/f/fonts-sil-padauk/ \
      | grep -oE 'fonts-sil-padauk_[^"]*_all\.deb' | grep -v '~' | sort -uV | tail -1)"
    DEB="${DEB:-fonts-sil-padauk_5.001-1_all.deb}"
    curl -sL -o "$FONT_TMP/pad.deb" \
      "https://deb.debian.org/debian/pool/main/f/fonts-sil-padauk/$DEB"
    dpkg-deb -x "$FONT_TMP/pad.deb" "$FONT_TMP/pad"
    cp "$FONT_TMP/pad/usr/share/fonts/truetype/padauk/Padauk-Regular.ttf" assets/fonts/
    cp "$FONT_TMP/pad/usr/share/fonts/truetype/padauk/Padauk-Bold.ttf" assets/fonts/
  fi
  if ! font_ok NotoSansMyanmar-Regular.ttf || ! font_ok NotoSansMyanmar-Bold.ttf; then
    DEB="$(curl -s https://deb.debian.org/debian/pool/main/f/fonts-noto/ \
      | grep -oE 'fonts-noto-core_[^"]*_all\.deb' | grep -v '~' | sort -uV | tail -1)"
    DEB="${DEB:-fonts-noto-core_20201225-6_all.deb}"
    curl -sL -o "$FONT_TMP/nc.deb" \
      "https://deb.debian.org/debian/pool/main/f/fonts-noto/$DEB"
    dpkg-deb -x "$FONT_TMP/nc.deb" "$FONT_TMP/nc"
    cp "$FONT_TMP/nc/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf" assets/fonts/
    cp "$FONT_TMP/nc/usr/share/fonts/truetype/noto/NotoSansMyanmar-Bold.ttf" assets/fonts/
  fi
  rm -rf "$FONT_TMP"
fi
for f in NotoSansMyanmar-Regular.ttf NotoSansMyanmar-Bold.ttf \
         Pyidaungsu-2.3_Regular.ttf Pyidaungsu-2.3_Bold.ttf \
         Padauk-Regular.ttf Padauk-Bold.ttf; do
  if ! font_ok "$f"; then
    echo "ERROR: assets/fonts/$f is missing or not a font file — source copy is incomplete."
    exit 1
  fi
done
echo "Myanmar fonts OK (6 bundled files verified)."

# --- 6. python venv + requirements --------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --no-cache-dir -r requirements.txt
echo "venv ready: $(.venv/bin/python --version)"

# --- 7. whisper model (pre-download + smoke load) --------------------------------------
MODEL_DIR='models/faster-whisper-small'
if [ -s "$MODEL_DIR/model.bin" ]; then
  echo "Whisper model already present ($(du -sh "$MODEL_DIR" | cut -f1)), skipping download."
else
  echo "Downloading faster-whisper 'small' model (~464 MB) from HuggingFace..."
  .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', local_dir='$MODEL_DIR')"
fi
echo "Smoke-loading the whisper model once (warms caches, proves it loads)..."
.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('$MODEL_DIR', device='cpu', compute_type='int8'); print('WHISPER_OK')"

# --- 8. launcher (serves the app unchanged + queue sanity cap) -----------------
mkdir -p "$DEPLOY_DIR"
cat > "$DEPLOY_DIR/serve.py" <<'PYEOF'
#!/usr/bin/env python3
"""Video Translator production launcher (deploy artifact — NOT an app file).

Runs the exact Flask app object from app.py through the app's own venv, the
same way run.sh does (.venv/bin/python + app), with two deliberate deltas:

1. Binds 127.0.0.1 instead of app.py's hardcoded 0.0.0.0, on the internal
   port (PORT env). Caddy is the only public door.
2. A queue sanity cap. app.py itself serializes the heavy work:
   its dispatcher runs at most MAX_CONCURRENT_JOBS (4) worker
   threads at once and parks every other submission in an in-app
   FIFO queue — submissions are never refused for concurrency.
   This gate is only a backstop against a runaway queue: it
   rejects a new phase start (POST /api/jobs,
   POST /api/jobs/<id>/review) with HTTP 429 only when
   QUEUE_CAP or more jobs are already queued/running. Preview
   downloads, exports and bulk text input are not gated (they
   start no heavy work). Disable with TRANSLATOR_GATE=off, tune
   with TRANSLATOR_QUEUE_CAP.

Note: JOBS is the app's in-memory dict; entries loaded from disk keep their
created_at, so a stale "running" entry older than STALE_AFTER seconds is
ignored (it can only be a leftover of a previous, dead process).
"""
import os
import sys
import time

APP_DIR = os.environ.get("TRANSLATOR_APP_DIR", "/opt/video-translator")
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

import app as appmod  # noqa: E402  (the user's app, unmodified)
from flask import jsonify, request  # noqa: E402

ACTIVE_STATUSES = {"queued", "running"}
STALE_AFTER = 25 * 60  # seconds since the job's last heartbeat
QUEUE_CAP = int(os.environ.get("TRANSLATOR_QUEUE_CAP", "20"))


def _active_count(exclude_jid=None):
    """Queued + running jobs with a fresh heartbeat. A stale entry
    (a leftover of a previous, dead process) never counts."""
    now = time.time()
    n = 0
    for jid, job in list(appmod.JOBS.items()):
        if jid == exclude_jid:
            continue
        if job.get("status") not in ACTIVE_STATUSES:
            continue
        if job.get("cancel_requested"):
            continue  # stopping anyway — never block on it
        # A live worker heartbeats updated_at every ~45 s. Judging by
        # created_at instead (the old rule, 6 h) meant one wedged job
        # could block every new job for hours.
        updated = job.get("updated_at") or job.get("created_at") or now
        if now - float(updated) < STALE_AFTER:
            n += 1
    return n


if os.environ.get("TRANSLATOR_GATE", "on") != "off":

    @appmod.app.before_request
    def _queue_sanity_cap():
        if request.method != "POST":
            return None
        path = request.path.rstrip("/")
        if path == "/api/jobs":
            count = _active_count()
        elif path.startswith("/api/jobs/") and path.endswith("/review"):
            count = _active_count(exclude_jid=path.split("/")[3])
        else:
            return None
        if count >= QUEUE_CAP:
            return jsonify({
                "error": "တန်းစီ ပြည့်နေပါတယ် — ခဏ စောင့်ပြီး ပြန် ကြိုးစားပေးပါ။ "
                         "(queue is full, please retry shortly)"
            }), 429
        return None


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8613"))
    appmod.app.run(host="127.0.0.1", port=port, threaded=True)
PYEOF
.venv/bin/python -m py_compile "$DEPLOY_DIR/serve.py"
echo "Launcher installed at $DEPLOY_DIR/serve.py"

# --- 8b. decode any base64-packed binary assets (e.g. PWA icons) ---------------
# Binary files travel through the git snapshot flow as <name>.b64 text.
for b64 in "$APP_DIR"/static/*.b64; do
  [ -e "$b64" ] || continue
  base64 -d "$b64" > "${b64%.b64}"
  echo "Decoded asset: ${b64%.b64}"
done

# --- 9. ownership ----------------------------------------------------------------------
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chown -R root:root "$DEPLOY_DIR"
chmod 755 "$DEPLOY_DIR/serve.py"

# --- 10. systemd service ------------------------------------------------------------------
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Video Translator (Myanmar) — Flask app behind Caddy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PORT=$INTERNAL_PORT
Environment=TRANSLATOR_APP_DIR=$APP_DIR
Environment=TRANSLATOR_GATE=on
Environment=PYTHONUNBUFFERED=1
$VT_ENV_LINE
ExecStart=$APP_DIR/.venv/bin/python $DEPLOY_DIR/serve.py
Restart=on-failure
RestartSec=3
# Sane caps for the 4 vCPU / 8 GB YTA 3 box shared with the MLBB
# container: whisper base (int8) + ffmpeg passes, FOUR jobs at
# once (build 180 — the owner wants the full 4 cores working);
# 400% is the whole machine, and 6.5G covers four whisper +
# ffmpeg working sets while leaving the OS + MLBB their share.
MemoryMax=6.5G
CPUQuota=400%
OOMPolicy=continue
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
echo "systemd unit ${SERVICE_NAME}.service installed."

# --- 11. basic-auth password (generated once, then reused) --------------------------------
if [ -f "$AUTH_FILE" ] && grep -q '^password:' "$AUTH_FILE"; then
  AUTH_PW="$(sed -n 's/^password:[[:space:]]*//p' "$AUTH_FILE" | head -1 || true)"
  echo "Reusing the existing basic-auth password from $AUTH_FILE."
else
  AUTH_PW="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || true)"
  {
    echo "# Video Translator basic auth — keep private (root only)."
    echo "username: $AUTH_USER"
    echo "password: $AUTH_PW"
    echo "url: http://<server-ip>:${PUBLIC_PORT}/"
  } > "$AUTH_FILE"
  chmod 600 "$AUTH_FILE"
  echo "Generated a new basic-auth password (stored in $AUTH_FILE)."
fi
AUTH_HASH="$(caddy hash-password --plaintext "$AUTH_PW")"

# --- 12. Caddy site block (own file, imported — coexists with future MLBB blocks) ---------
mkdir -p /etc/caddy
# Keep the previous (working) config so a bad new config can be rolled back.
if [ -f /etc/caddy/translator.caddy ]; then
  cp /etc/caddy/translator.caddy /etc/caddy/translator.caddy.bak
fi
AUTH_BLOCK=""
if [ "$FLIP" -eq 0 ]; then
  AUTH_BLOCK="	basic_auth {
		${AUTH_USER} ${AUTH_HASH}
	}"
fi
cat > /etc/caddy/translator.caddy <<EOF
# === video-translator (managed by translator-bootstrap.sh) ===
# Public port -> app on localhost. Basic-auth wall stands unless the
# owner armed the flip (VT_FLIP=1 in the app .env), in which case the
# app's own account system is the only gate.
:${PUBLIC_PORT} {
$AUTH_BLOCK
	reverse_proxy 127.0.0.1:${INTERNAL_PORT}
	encode gzip
}
# === end video-translator ===
EOF
if [ -n "$TLS_NAME" ]; then
  cat >> /etc/caddy/translator.caddy <<EOF

# HTTPS site — Caddy provisions a Let's Encrypt certificate automatically.
# A secure context is what makes the PWA fully installable. Same gate + app.
${TLS_NAME} {
$AUTH_BLOCK
	reverse_proxy 127.0.0.1:${INTERNAL_PORT}
	encode gzip
}
EOF
  ufw allow 443/tcp
  echo "HTTPS site block added for ${TLS_NAME}."
fi
touch /etc/caddy/Caddyfile
if ! grep -q 'translator\.caddy' /etc/caddy/Caddyfile; then
  echo 'import /etc/caddy/translator.caddy' >> /etc/caddy/Caddyfile
fi
caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
sleep 2
if ! systemctl is-active --quiet caddy; then
  echo "WARN: Caddy failed to start with the new config; restoring the previous one."
  if [ -f /etc/caddy/translator.caddy.bak ]; then
    cp /etc/caddy/translator.caddy.bak /etc/caddy/translator.caddy
    systemctl restart caddy
    sleep 2
  fi
fi
if [ "$FLIP" -eq 1 ]; then
  echo "Caddy serving :${PUBLIC_PORT} -> 127.0.0.1:${INTERNAL_PORT} (WALL DOWN — accounts enforced)."
else
  echo "Caddy serving :${PUBLIC_PORT} -> 127.0.0.1:${INTERNAL_PORT} (basic auth on)."
fi

# --- 13. firewall: only the Caddy port is public for this app ------------------------------
ufw allow 22/tcp
ufw allow "${PUBLIC_PORT}/tcp"
ufw --force enable
ufw status || true

# --- 13b. port 80 -> Caddy redirect (some networks block non-standard ports) --
# Idempotent now, and re-applied at boot by a tiny oneshot unit (iptables
# rules do not survive reboots on their own).
if ! iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port "${PUBLIC_PORT}" 2>/dev/null; then
  iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port "${PUBLIC_PORT}"
fi
cat > /etc/systemd/system/translator-port80.service <<EOF2
[Unit]
Description=Re-apply port 80 redirect to the translator Caddy port
After=network.target docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port ${PUBLIC_PORT} 2>/dev/null || iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port ${PUBLIC_PORT}'

[Install]
WantedBy=multi-user.target
EOF2
systemctl daemon-reload
systemctl enable translator-port80.service >/dev/null 2>&1 || true
echo "Port 80 redirect to :${PUBLIC_PORT} active (and boot-persistent)."

# --- 13c. auto-update timer ------------------------------------------------------
# Every 15 min the server pulls the repo itself and, only when something
# changed, applies the update (syntax-gated, locked). This ends the manual
# console trips: shipping an update = pushing to GitHub.
cat > /etc/systemd/system/translator-autoupdate.service <<EOF3
[Unit]
Description=Auto-update the video translator from GitHub
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'cp ${APP_DIR}/deploy/apply-update.sh /tmp/apply-update-run.sh && bash /tmp/apply-update-run.sh --auto'
StandardOutput=append:/root/translator-autoupdate.log
StandardError=append:/root/translator-autoupdate.log
EOF3
cat > /etc/systemd/system/translator-autoupdate.timer <<EOF3
[Unit]
Description=Run translator auto-update every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF3
systemctl daemon-reload
systemctl enable --now translator-autoupdate.timer >/dev/null 2>&1 || true
echo "Auto-update timer installed (checks GitHub every 15 min)."

# --- 13d. local Telegram Bot API server (build 184) ---------------------------
# The cloud Bot API caps bot sends at 50 MB. When the owner has saved a
# Telegram api_id + api_hash (my.telegram.org) in the app's Admin card
# (TG_API_ID / TG_API_HASH in the app .env), this section installs a
# self-hosted telegram-bot-api server on THIS box and points the bot at
# it (TG_API_BASE=http://127.0.0.1:8081 in the app .env), which lifts
# sends to 2 GB so big finished videos land in the chat directly.
#
# Binary source: upstream tdlib publishes source only (no release
# binaries), so the pinned build is the community prebuilt of the
# UNMODIFIED upstream source (SwissCore92/telegram-bot-api-bin, built
# by GitHub Actions from tdlib/telegram-bot-api; the tarball carries
# the upstream source-commit). The download is pinned by version AND
# sha256 below, and is verified before anything is installed.
#
# FAIL-SAFE BY CONSTRUCTION: the whole section is one subshell with
# set +e, called behind `|| true` — no failure in here (offline,
# bad download, server won't start) can fail or even slow the
# translator deploy; the bot simply stays on the cloud API. When
# the credentials are ABSENT the section only clears TG_API_BASE
# (cloud default); a previously installed tg-bot-api unit is left
# installed and running but unused — removing it is a manual call.
TGBA_VERSION='10.3'
TGBA_HOME="${TGBA_HOME:-/opt/tg-bot-api}"
TGBA_UNIT="${TGBA_UNIT:-/etc/systemd/system/tg-bot-api.service}"
TGBA_DL_BASE="${TGBA_DL_BASE:-https://github.com/SwissCore92/telegram-bot-api-bin/releases/download/v${TGBA_VERSION}}"
TGBA_PORT='8081'

tgba_env_get() {  # $1 = key -> last value in the app .env ("" if absent)
  [ -f "$APP_DIR/.env" ] || return 0
  grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' \r' || true
}

tgba_env_set_base() {  # $1 = base URL, or "" to remove TG_API_BASE
  local env_file="$APP_DIR/.env" tmp
  [ -f "$env_file" ] || return 0
  tmp="$(mktemp)" || return 0
  grep -v '^TG_API_BASE=' "$env_file" > "$tmp" 2>/dev/null || true
  if [ -n "$1" ]; then
    echo "TG_API_BASE=$1" >> "$tmp"
  fi
  cat "$tmp" > "$env_file"
  rm -f "$tmp"
  chmod 600 "$env_file" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$env_file" 2>/dev/null || true
}

tgba_setup() (
  set +e  # contained to this subshell: nothing here may fail the deploy
  api_id="$(tgba_env_get TG_API_ID)"
  api_hash="$(tgba_env_get TG_API_HASH)"
  if [ -z "$api_id" ] || [ -z "$api_hash" ]; then
    if grep -q '^TG_API_BASE=' "$APP_DIR/.env" 2>/dev/null; then
      tgba_env_set_base ""
      echo "Local Telegram API: no credentials — TG_API_BASE cleared (bot on the cloud API, 50 MB)."
    else
      echo "Local Telegram API: not configured (cloud API, 50 MB sends)."
    fi
    exit 0
  fi

  case "$(uname -m)" in
    x86_64|amd64)   tgba_arch='amd64'; tgba_sha='e4235392510fef9244225cf7d4da09dbf5ffa059ca497b9f711d20cac9a9bf35' ;;
    aarch64|arm64)  tgba_arch='arm64'; tgba_sha='984a36745fda466bd8e3c7cfd40207a7e34f44f80250a36de014bd8a84a865b7' ;;
    *) echo "Local Telegram API: unsupported arch $(uname -m) — skipped."; exit 0 ;;
  esac
  tgba_asset="telegram-bot-api-v${TGBA_VERSION}-linux-${tgba_arch}.tar.gz"
  tgba_bin="$TGBA_HOME/bin/telegram-bot-api"

  # (a) binary: download once per pinned version, sha256-verified.
  if [ ! -x "$tgba_bin" ] || [ "$(cat "$TGBA_HOME/VERSION" 2>/dev/null)" != "$TGBA_VERSION" ]; then
    tgba_dl="$(mktemp -d)"
    echo "Local Telegram API: downloading telegram-bot-api v${TGBA_VERSION} (${tgba_arch})..."
    if curl -fsSL --max-time 300 -o "$tgba_dl/$tgba_asset" "$TGBA_DL_BASE/$tgba_asset" \
       && echo "${tgba_sha}  $tgba_dl/$tgba_asset" | sha256sum -c - >/dev/null 2>&1 \
       && tar -xzf "$tgba_dl/$tgba_asset" -C "$tgba_dl"; then
      mkdir -p "$TGBA_HOME/bin"
      install -m 755 "$tgba_dl/telegram-bot-api-v${TGBA_VERSION}-linux-${tgba_arch}/telegram-bot-api" "$tgba_bin"
      echo "$TGBA_VERSION" > "$TGBA_HOME/VERSION"
      echo "Local Telegram API: binary v${TGBA_VERSION} installed at $tgba_bin."
    else
      echo "WARN: Local Telegram API binary download/verify failed — skipping (bot stays on the cloud API)."
      rm -rf "$tgba_dl"
      exit 0
    fi
    rm -rf "$tgba_dl"
  fi
  if [ ! -x "$tgba_bin" ]; then
    echo "WARN: Local Telegram API binary missing — skipping."
    exit 0
  fi

  mkdir -p "$TGBA_HOME/data"
  chown -R "$APP_USER:$APP_USER" "$TGBA_HOME/data" 2>/dev/null || true

  # One-time cloud -> local migration (Telegram's rule): the bot must
  # log OUT of the cloud Bot API server once before a local server may
  # receive its updates. Guarded by a marker file so it runs exactly
  # once, and failure-tolerant — an unconfirmed logOut is retried on
  # the next bootstrap run, never blocking this one. NOTE: after a
  # successful logOut, Telegram can take up to ~10 minutes before the
  # token authorizes on the local server; the app poller retries on
  # its own, so delivery self-heals with no further action.
  tgba_marker="$TGBA_HOME/.cloud-logout-done"
  if [ ! -f "$tgba_marker" ]; then
    tgba_tok="$(tgba_env_get TELEGRAM_BOT_TOKEN)"
    if [ -n "$tgba_tok" ]; then
      tgba_resp="$(curl -fsS --max-time 30 "https://api.telegram.org/bot${tgba_tok}/logOut" 2>/dev/null)"
      if echo "$tgba_resp" | grep -q '"ok":true' || echo "$tgba_resp" | grep -qi 'not logged in'; then
        touch "$tgba_marker"
        echo "Local Telegram API: one-time cloud logOut done."
      else
        echo "WARN: cloud logOut not confirmed (retried next run): $tgba_resp"
      fi
    else
      echo "Local Telegram API: no bot token yet — cloud logOut deferred."
    fi
  fi

  # (b) systemd unit. It embeds the api_hash, so it is mode 600.
  cat > "$TGBA_UNIT" <<EOF
[Unit]
Description=Local Telegram Bot API server (video translator bot, build 184)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$TGBA_HOME
ExecStart=$tgba_bin --api-id=$api_id --api-hash=$api_hash --http-ip-address=127.0.0.1 --http-port=$TGBA_PORT --dir=$TGBA_HOME/data --local
Restart=on-failure
RestartSec=5
MemoryMax=1G
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  chmod 600 "$TGBA_UNIT"
  systemctl daemon-reload
  systemctl enable tg-bot-api.service >/dev/null 2>&1 || true
  systemctl restart tg-bot-api.service || echo "WARN: tg-bot-api failed to start."

  # (c)+(d) point the app at the local server only when it is really
  # up; the translator restart at step 14 then picks the base up.
  if systemctl is-active --quiet tg-bot-api.service; then
    tgba_env_set_base "http://127.0.0.1:${TGBA_PORT}"
    echo "Local Telegram API: live on 127.0.0.1:${TGBA_PORT} — TG_API_BASE set for the app."
  else
    echo "WARN: tg-bot-api is not active; TG_API_BASE left unchanged (bot stays on the cloud API)."
  fi
  exit 0
)
tgba_setup || echo "WARN: local Telegram API setup hit an error — continuing the translator deploy."

# --- 13e. VPN tunnel for Gemini/OpenAI (build 189) -------------------------
# Direct Gemini is location-blocked and direct OpenAI is
# territory-blocked from this VPS (the engines judge the API
# caller's IP country; the box is in Hong Kong). When the owner
# has saved a node from his own V2Ray subscription in the app's
# Admin card, the app has already parsed it into an xray client
# config at $APP_DIR/tun-xray.json (chmod 600 — it carries the
# node credential). This section installs xray-core (the standard
# V2Ray client; pinned release + sha256 below, verified before
# anything is installed) and runs it as the tun-xray unit with
# ONE loopback HTTP proxy inbound on 127.0.0.1:18119. The app's
# Gemini/OpenAI clients — and nothing else in the system — pass
# that proxy explicitly per request, and only after this section
# has verified real egress through it: only then are
# VT_TUNNEL_PROXY / VT_TUNNEL_COUNTRY / VT_TUNNEL_CONF_SHA (the
# sha of the very config verified) written into the app .env,
# and the app re-checks that sha on every call, so a re-saved
# node is never trusted until it has been verified here.
#
# FAIL-SAFE BY CONSTRUCTION (same shape as 13d): one subshell
# with set +e behind `|| true` — no failure in here (offline,
# bad download, dead node, xray won't start) can fail or slow
# the translator deploy; the engines simply stay on their
# direct route, exactly as without a tunnel. When the config is
# ABSENT the section clears the app-side pointers and stops the
# unit; the binary is left installed but unused.
TUNX_VERSION='26.11.9'
TUNX_HOME="${TUNX_HOME:-/opt/tun-xray}"
TUNX_UNIT="${TUNX_UNIT:-/etc/systemd/system/tun-xray.service}"
TUNX_DL_BASE="${TUNX_DL_BASE:-https://github.com/XTLS/Xray-core/releases/download/v${TUNX_VERSION}}"
TUNX_PORT='18119'
TUNX_CONF='tun-xray.json'

tunx_env_get() {  # $1 = key -> last value in the app .env ("" if absent)
  [ -f "$APP_DIR/.env" ] || return 0
  grep -E "^$1=" "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' \r' || true
}

tunx_env_set() {  # $1 = key, $2 = value ("" removes the line)
  local env_file="$APP_DIR/.env" tmp
  [ -f "$env_file" ] || return 0
  tmp="$(mktemp)" || return 0
  grep -v "^$1=" "$env_file" > "$tmp" 2>/dev/null || true
  if [ -n "$2" ]; then
    echo "$1=$2" >> "$tmp"
  fi
  cat "$tmp" > "$env_file"
  rm -f "$tmp"
  chmod 600 "$env_file" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$env_file" 2>/dev/null || true
}

tunx_clear_pointers() {
  tunx_env_set VT_TUNNEL_PROXY ""
  tunx_env_set VT_TUNNEL_COUNTRY ""
  tunx_env_set VT_TUNNEL_CONF_SHA ""
}

tunx_setup() (
  set +e  # contained to this subshell: nothing here may fail the deploy
  conf_src="$APP_DIR/$TUNX_CONF"
  if [ ! -s "$conf_src" ] \
     || ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$conf_src" 2>/dev/null; then
    if grep -q '^VT_TUNNEL_PROXY=' "$APP_DIR/.env" 2>/dev/null; then
      tunx_clear_pointers
      echo "VPN tunnel: no config — tunnel pointers cleared (Gemini/OpenAI direct)."
    else
      echo "VPN tunnel: not configured (Gemini/OpenAI direct)."
    fi
    systemctl disable --now tun-xray.service >/dev/null 2>&1 || true
    exit 0
  fi

  case "$(uname -m)" in
    x86_64|amd64)   tunx_arch='64';        tunx_sha='b651f7e1d9966ee2c1b1ee1bf8b52c72771824d3065dce96bbc8bcc19ed95f82' ;;
    aarch64|arm64)  tunx_arch='arm64-v8a'; tunx_sha='64ed6858982a7f5eb85d88b811089063bd4f17561f4c8f44e0f1dc8e6f6430be' ;;
    *) echo "VPN tunnel: unsupported arch $(uname -m) — skipped."; exit 0 ;;
  esac
  tunx_asset="Xray-linux-${tunx_arch}.zip"
  tunx_bin="$TUNX_HOME/bin/xray"

  # (a) binary: download once per pinned version, sha256-verified.
  # Only the `xray` member is extracted (the geo data files are
  # not referenced by a proxy-only client config).
  if [ ! -x "$tunx_bin" ] || [ "$(cat "$TUNX_HOME/VERSION" 2>/dev/null)" != "$TUNX_VERSION" ]; then
    tunx_dl="$(mktemp -d)"
    echo "VPN tunnel: downloading xray-core v${TUNX_VERSION} (linux-${tunx_arch})..."
    if curl -fsSL --max-time 300 -o "$tunx_dl/$tunx_asset" "$TUNX_DL_BASE/$tunx_asset" \
       && echo "${tunx_sha}  $tunx_dl/$tunx_asset" | sha256sum -c - >/dev/null 2>&1 \
       && python3 -c 'import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract("xray", sys.argv[2])' "$tunx_dl/$tunx_asset" "$tunx_dl"; then
      mkdir -p "$TUNX_HOME/bin"
      install -m 755 "$tunx_dl/xray" "$tunx_bin"
      echo "$TUNX_VERSION" > "$TUNX_HOME/VERSION"
      echo "VPN tunnel: xray v${TUNX_VERSION} installed at $tunx_bin."
    else
      echo "WARN: xray download/verify failed — skipping (Gemini/OpenAI stay direct)."
      rm -rf "$tunx_dl"
      exit 0
    fi
    rm -rf "$tunx_dl"
  fi
  if [ ! -x "$tunx_bin" ]; then
    echo "WARN: xray binary missing — skipping."
    exit 0
  fi

  mkdir -p "$TUNX_HOME"
  chown "$APP_USER:$APP_USER" "$TUNX_HOME" 2>/dev/null || true

  # (b) config: install the app's rendered config (it carries the
  # node credential, so 600), and let xray itself judge it before
  # anything is pointed at the tunnel.
  conf_dst="$TUNX_HOME/config.json"
  conf_sha="$(sha256sum "$conf_src" | cut -d' ' -f1)"
  conf_changed=0
  if [ ! -f "$conf_dst" ] || [ "$(sha256sum "$conf_dst" 2>/dev/null | cut -d' ' -f1)" != "$conf_sha" ]; then
    cp "$conf_src" "$conf_dst"
    conf_changed=1
  fi
  chmod 600 "$conf_dst" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$conf_dst" 2>/dev/null || true
  if ! "$tunx_bin" run -test -config "$conf_dst" >/dev/null 2>&1; then
    echo "WARN: xray rejects the saved config — tunnel pointers cleared (Gemini/OpenAI stay direct)."
    tunx_clear_pointers
    systemctl stop tun-xray.service >/dev/null 2>&1 || true
    exit 0
  fi

  # (c) systemd unit. Unlike 13d's unit it embeds NO secret (the
  # credential lives in the 600 config file), so default perms.
  unit_changed=0
  tunx_unit_tmp="$(mktemp)"
  cat > "$tunx_unit_tmp" <<EOF
[Unit]
Description=VPN tunnel (xray) for the translator's Gemini/OpenAI (build 189)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$TUNX_HOME
ExecStart=$tunx_bin run -config $conf_dst
Restart=on-failure
RestartSec=5
MemoryMax=512M
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  if [ ! -f "$TUNX_UNIT" ] || ! cmp -s "$tunx_unit_tmp" "$TUNX_UNIT"; then
    cat "$tunx_unit_tmp" > "$TUNX_UNIT"
    unit_changed=1
  fi
  rm -f "$tunx_unit_tmp"
  systemctl daemon-reload
  systemctl enable tun-xray.service >/dev/null 2>&1 || true
  if [ "$conf_changed" -eq 1 ] || [ "$unit_changed" -eq 1 ] \
     || ! systemctl is-active --quiet tun-xray.service; then
    systemctl restart tun-xray.service || echo "WARN: tun-xray failed to start."
    sleep 2
  fi

  # (d) point the app at the tunnel ONLY when it is really up and
  # carrying traffic: one lightweight egress check THROUGH the
  # proxy; the country it reports is what the Admin card shows.
  if systemctl is-active --quiet tun-xray.service; then
    tunx_trace="$(curl -fsS --max-time 25 --proxy "http://127.0.0.1:${TUNX_PORT}" \
                  https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)"
    tunx_country="$(printf '%s' "$tunx_trace" | grep -E '^loc=' | head -1 | cut -d= -f2 | tr -d ' \r' | cut -c1-2)"
    if [ -n "$tunx_trace" ] && [ -n "$tunx_country" ]; then
      tunx_env_set VT_TUNNEL_PROXY "http://127.0.0.1:${TUNX_PORT}"
      tunx_env_set VT_TUNNEL_COUNTRY "$tunx_country"
      tunx_env_set VT_TUNNEL_CONF_SHA "$conf_sha"
      echo "VPN tunnel: live on 127.0.0.1:${TUNX_PORT} — egress country ${tunx_country}; Gemini/OpenAI will use it."
    else
      tunx_clear_pointers
      echo "WARN: tunnel egress check failed (node down?) — tunnel pointers cleared; Gemini/OpenAI stay direct."
    fi
  else
    tunx_clear_pointers
    echo "WARN: tun-xray is not active; tunnel pointers cleared (Gemini/OpenAI stay direct)."
  fi
  exit 0
)
tunx_setup || echo "WARN: VPN tunnel setup hit an error — continuing the translator deploy."

# --- 13b. Myanmar fonts -------------------------------------------------------------------------
# The repo ships the fonts as .ttf.b64 (the push channel is text
# only). The app service runs as an unprivileged system user and
# CANNOT write into $APP_DIR, so the runtime decode in the app
# silently failed and subtitles rendered as tofu boxes. Decode
# HERE, as root, and register the fonts system-wide as well, so
# libass finds the families via fontconfig even if fontsdir is
# ignored by the local ffmpeg/libass build.
python3 - "$APP_DIR" <<'PYEOF'
import base64
import sys
from pathlib import Path

fd = Path(sys.argv[1]) / "assets" / "fonts"
if fd.exists():
    for b in fd.glob("*.ttf.b64"):
        t = b.with_suffix("")
        if not t.exists() or t.stat().st_size < 10_000:
            t.write_bytes(base64.b64decode(b.read_text()))
    print("Fonts materialized:",
          sorted(p.name for p in fd.glob("*.ttf")))
PYEOF
mkdir -p /usr/local/share/fonts/vt
cp -f "$APP_DIR"/assets/fonts/*.ttf /usr/local/share/fonts/vt/ 2>/dev/null || true
if command -v fc-cache >/dev/null 2>&1; then
  fc-cache -f >/dev/null 2>&1 || true
  echo "System fonts registered (fc-cache done)."
fi

# --- 14. start + health checks ----------------------------------------------------------------
systemctl restart "$SERVICE_NAME"
echo "Waiting for the app on http://127.0.0.1:${INTERNAL_PORT}/ ..."
app_ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${INTERNAL_PORT}/" >/dev/null 2>&1; then
    app_ok=1
    break
  fi
  sleep 2
done
if [ "$app_ok" -ne 1 ]; then
  echo "ERROR: the app did not answer on 127.0.0.1:${INTERNAL_PORT} within ~60s."
  exit 1
fi
echo "App OK on 127.0.0.1:${INTERNAL_PORT}."

code_noauth="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PUBLIC_PORT}/")"
if [ "$FLIP" -eq 1 ]; then
  if [ "$code_noauth" != "200" ]; then
    echo "ERROR: flipped site without auth returned HTTP $code_noauth (expected 200)."
    exit 1
  fi
  echo "Caddy OK: wall down, site public on :${PUBLIC_PORT} (accounts enforced)."
else
  if [ "$code_noauth" != "401" ]; then
    echo "ERROR: Caddy without auth returned HTTP $code_noauth (expected 401)."
    exit 1
  fi
  code_auth="$(curl -s -o /dev/null -w '%{http_code}' -u "${AUTH_USER}:${AUTH_PW}" "http://127.0.0.1:${PUBLIC_PORT}/")"
  if [ "$code_auth" != "200" ]; then
    echo "ERROR: Caddy with auth returned HTTP $code_auth (expected 200)."
    exit 1
  fi
  echo "Caddy OK: no-auth -> 401, with-auth -> 200 on :${PUBLIC_PORT}."
fi

trap - ERR
echo "TRANSLATOR_DEPLOY_DONE"
# The password is printed exactly here (and stored root-only in
# /root/translator-auth.txt) so the operator can hand it to the user.
echo "TRANSLATOR_AUTH $AUTH_USER $AUTH_PW"
