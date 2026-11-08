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
#      launcher binds the app to 127.0.0.1:8613 and adds a
#      one-job-at-a-time gate (the app itself starts a thread per job with
#      no serialization; two concurrent whisper+ffmpeg jobs can OOM a
#      2 GB box). TRANSLATOR_GATE=off in the unit disables the gate.
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
apt-get install -y curl git ufw file python3 python3-venv ffmpeg

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

# --- 8. launcher (serves the app unchanged + one-job-at-a-time gate) -----------------
mkdir -p "$DEPLOY_DIR"
cat > "$DEPLOY_DIR/serve.py" <<'PYEOF'
#!/usr/bin/env python3
"""Video Translator production launcher (deploy artifact — NOT an app file).

Runs the exact Flask app object from app.py through the app's own venv, the
same way run.sh does (.venv/bin/python + app), with two deliberate deltas:

1. Binds 127.0.0.1 instead of app.py's hardcoded 0.0.0.0, on the internal
   port (PORT env). Caddy is the only public door.
2. A one-job-at-a-time gate. app.py starts a background thread per job with
   no serialization; on a 1 vCPU / 2 GB box, two concurrent whisper+ffmpeg
   jobs can OOM the service. The gate rejects a new phase start
   (POST /api/jobs, POST /api/jobs/<id>/review) with HTTP 429 while another
   job is queued/running. Preview downloads, exports and bulk text input
   are not gated (they start no heavy work). Disable with
   TRANSLATOR_GATE=off.

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


def _busy_job(exclude_jid=None):
    now = time.time()
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
            return job
    return None


if os.environ.get("TRANSLATOR_GATE", "on") != "off":

    @appmod.app.before_request
    def _one_job_at_a_time():
        if request.method != "POST":
            return None
        path = request.path.rstrip("/")
        if path == "/api/jobs":
            busy = _busy_job()
        elif path.startswith("/api/jobs/") and path.endswith("/review"):
            busy = _busy_job(exclude_jid=path.split("/")[3])
        else:
            return None
        if busy is not None:
            return jsonify({
                "error": "နောက်ထပ် job တစ်ခု လုပ်နေဆဲပါ — အဲဒီ job ပြီးမှ နောက်တစ်ခု စပါ။ "
                         "(one job at a time on this server)"
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
ExecStart=$APP_DIR/.venv/bin/python $DEPLOY_DIR/serve.py
Restart=on-failure
RestartSec=3
# Sane caps for a 1 vCPU / 2 GB box shared with the MLBB container:
# whisper-small (int8) + one ffmpeg pass fit comfortably under 1.5 GB;
# 90% CPU leaves the MLBB app responsive while a job runs.
MemoryMax=1500M
CPUQuota=90%
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
cat > /etc/caddy/translator.caddy <<EOF
# === video-translator (managed by translator-bootstrap.sh) ===
# Trial setup: plain HTTP on the public port, basic auth, app on localhost.
#
# FUTURE DOMAIN / HTTPS: add a new site block in the main Caddyfile, e.g.
#   translator.example.com {
#       basic_auth { htetheather <same bcrypt hash as below> }
#       reverse_proxy 127.0.0.1:${INTERNAL_PORT}
#   }
# Caddy will provision HTTPS automatically once DNS points at this server;
# then this :${PUBLIC_PORT} block can be removed. Do NOT reuse ports
# 8080/80/443 here — those belong to the MLBB app.
:${PUBLIC_PORT} {
	basic_auth {
		$AUTH_USER $AUTH_HASH
	}
	reverse_proxy 127.0.0.1:${INTERNAL_PORT}
	encode gzip
}
# === end video-translator ===
EOF
if [ -n "$TLS_NAME" ]; then
  cat >> /etc/caddy/translator.caddy <<EOF

# HTTPS site — Caddy provisions a Let's Encrypt certificate automatically.
# A secure context is what makes the PWA fully installable. Same auth + app.
${TLS_NAME} {
	basic_auth {
		$AUTH_USER $AUTH_HASH
	}
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
echo "Caddy serving :${PUBLIC_PORT} -> 127.0.0.1:${INTERNAL_PORT} (basic auth on)."

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

trap - ERR
echo "TRANSLATOR_DEPLOY_DONE"
# The password is printed exactly here (and stored root-only in
# /root/translator-auth.txt) so the operator can hand it to the user.
echo "TRANSLATOR_AUTH $AUTH_USER $AUTH_PW"
