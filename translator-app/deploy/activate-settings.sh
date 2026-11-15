#!/usr/bin/env bash
# activate-settings.sh — settings activation for the video translator
# (build 190; extracted from translator-bootstrap.sh §13d + §13e).
#
# Two translator features are driven by values the owner saves in
# Admin cards, NOT by code:
#   * Telegram Local API — TG_API_ID / TG_API_HASH in the app .env
#     (Admin card, build 184) -> a local telegram-bot-api server.
#   * VPN tunnel — the parsed node config at $APP_DIR/tun-xray.json
#     (Admin card, build 189) -> a local xray-core proxy used ONLY
#     by the Gemini/OpenAI clients.
# Until build 190 the setup sections for both lived inline in the
# bootstrap, and the bootstrap only ran when a CODE deploy happened:
# apply-update.sh exited at "Already up to date" on no-change ticks,
# so a card save sat inert until an unrelated code push came along
# (the owner hit this twice — the TG API activation, then the VPN
# node). This script is the fix: apply-update.sh runs it on EVERY
# invocation (changed code or not), and the bootstrap still runs it
# in place on full deploys.
#
# Pending detection is a per-section marker: a sha256 of the
# section's INPUT values (the .env credential lines / the tunnel
# config file) stored next to the app. When the marker matches,
# the section is a fast no-op (one echo). When a card save changes
# an input, the next tick (or a manual apply-update run) activates
# it — typically within ~15 minutes, with no code deploy involved.
# A section's marker advances only on a TERMINAL state (live, or
# cleanly not-configured, or a config xray rejects outright —
# re-testing a deterministic parse failure every tick buys
# nothing); transient failures (download, unit start, egress)
# leave the marker stale so the next tick retries and the setup
# self-heals. Sections are independent: one section's failure
# never blocks the other's marker.
#
# FAIL-SAFE BY CONSTRUCTION (inherited from the bootstrap
# sections): every section is a `set +e` subshell behind a guard;
# nothing here can fail a deploy or an update run, and activation
# restarts ONLY its own units (tg-bot-api / tun-xray) — never the
# translator app, never Caddy.
#
# Usage: bash activate-settings.sh
#   APP_DIR / APP_USER may be preset by the caller (the bootstrap
#   passes its own); defaults match the bootstrap's configuration.
# Exit code: 0 when every section is live-or-terminal, 1 when a
# section failed transiently (callers treat this as advisory).

set +e

APP_DIR="${APP_DIR:-/opt/video-translator}"
APP_USER="${APP_USER:-translator}"
TUN_STATUS="${TUN_STATUS:-$APP_DIR/tun-status.json}"
ACT_TG_MARKER="$APP_DIR/.settings-activated.tg.sha"
ACT_TUN_MARKER="$APP_DIR/.settings-activated.tun.sha"

# --- Telegram Local API (was bootstrap §13d, build 184) ----------------------
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
# When the credentials are ABSENT the section only clears TG_API_BASE
# (cloud default); a previously installed tg-bot-api unit is left
# installed and running but unused — removing it is a manual call.
# Return codes (build 190): 0 = terminal (live / not configured /
# skipped), 1 = transient failure (download, unit start) — the
# activation marker stays stale and the next tick retries.
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
      exit 1
    fi
    rm -rf "$tgba_dl"
  fi
  if [ ! -x "$tgba_bin" ]; then
    echo "WARN: Local Telegram API binary missing — skipping."
    exit 1
  fi

  mkdir -p "$TGBA_HOME/data"
  chown -R "$APP_USER:$APP_USER" "$TGBA_HOME/data" 2>/dev/null || true

  # One-time cloud -> local migration (Telegram's rule): the bot must
  # log OUT of the cloud Bot API server once before a local server may
  # receive its updates. Guarded by a marker file so it runs exactly
  # once, and failure-tolerant — an unconfirmed logOut is retried on
  # the next activation run, never blocking this one. NOTE: after a
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
  # up; the app reads TG_API_BASE live, so no translator restart is
  # needed for the pointer to take effect.
  if systemctl is-active --quiet tg-bot-api.service; then
    tgba_env_set_base "http://127.0.0.1:${TGBA_PORT}"
    echo "Local Telegram API: live on 127.0.0.1:${TGBA_PORT} — TG_API_BASE set for the app."
    exit 0
  fi
  echo "WARN: tg-bot-api is not active; TG_API_BASE left unchanged (bot stays on the cloud API)."
  exit 1
)

# --- VPN tunnel for Gemini/OpenAI (was bootstrap §13e, build 189) ------------
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
# Build 190 — diagnosability: every activation attempt leaves
# its verdict in $APP_DIR/tun-status.json (mode 644, read by
# pipeline/tunnel.py into the Admin card): ok flag, a failure
# kind (config-invalid / egress-failed / unit-failed), the
# node's protocol/transport/security triple, the exit country
# on success, and — on failure — ONE sanitized reason line.
# xray's raw errors embed node addresses, UUIDs and URLs, so
# the reason passes through tunx_sanitize() before it is
# stored: the file carries structural text only, no secrets
# by construction. When the config is ABSENT the section
# clears the app-side pointers, stops the unit and removes
# the status file; the binary is left installed but unused.
# Return codes: 0 = terminal (live / not configured / config
# rejected — deterministic), 1 = transient (download, unit,
# egress) so the next tick retries.
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

tunx_sanitize() {  # stdin -> ONE sanitized structural line on stdout
  # Token-based scrubbing: a token is dropped when it looks like
  # a credential or an address — anything containing "://"
  # (URLs and share links), a UUID, an IPv4 address, a dotted
  # hostname (optionally with :port), a file path, or a long
  # opaque blob (keys / hashes / base64, >= 24 chars). The one
  # slash exception is xray's own section prefix style
  # ("infra/conf:"), which is structural. Structural words
  # ("failed", "to", "build", "outbound", "config",
  # "prohibited"...) survive untouched. The result is a
  # single line, capped at 200 chars.
  # The text travels via the environment, NOT stdin: the
  # heredoc below IS python's stdin (its script).
  local raw
  raw="$(cat)"
  TUNX_SAN_INPUT="$raw" python3 - <<'PYEOF'
import os
import re

text = os.environ.get("TUNX_SAN_INPUT", "")
text = re.sub(r"\x1b\[[0-9;]*m", " ", text)          # ANSI colours
text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
uuid = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ipv4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
dotted = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d+)?$")
blob = re.compile(r"^[A-Za-z0-9+/=_-]{24,}$")
kept = []
for tok in text.split():
    if "://" in tok or uuid.search(tok) or ipv4.search(tok):
        continue
    if "/" in tok and not re.match(r"^[a-z]+/[a-z]+:$", tok):
        continue  # paths / URL fragments; "infra/conf:" survives
    bare = tok.strip("[]()<>,;\"'")
    if dotted.match(bare) or blob.match(bare):
        continue
    kept.append(tok)
print(" ".join(kept)[:200].strip())
PYEOF
}

tunx_triple() {  # $1 = config path -> "proto transport security"
  python3 - "$1" <<'PYEOF'
import json
import sys

try:
    conf = json.load(open(sys.argv[1]))
    ob = (conf.get("outbounds") or [{}])[0]
    ss = ob.get("streamSettings") or {}
    print(ob.get("protocol") or "", ss.get("network") or "tcp",
          ss.get("security") or "none")
except Exception:
    print("", "", "")
PYEOF
}

tunx_write_status() {  # $1 ok(1/0) $2 kind $3 reason $4 proto $5 transport $6 security $7 country
  python3 - "$TUN_STATUS" "$1" "$2" "$3" "$4" "$5" "$6" "$7" <<'PYEOF'
import json
import os
import sys
import time

path = sys.argv[1]
data = {"ok": sys.argv[2] == "1",
        "kind": sys.argv[3] or None,
        "reason": sys.argv[4],
        "proto": sys.argv[5],
        "transport": sys.argv[6],
        "security": sys.argv[7],
        "country": sys.argv[8],
        "ts": int(time.time())}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PYEOF
  chown "$APP_USER:$APP_USER" "$TUN_STATUS" 2>/dev/null || true
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
    rm -f "$TUN_STATUS"
    systemctl disable --now tun-xray.service >/dev/null 2>&1 || true
    exit 0
  fi

  # The chosen node's triple — recorded in every status write
  # below (it is what parser support gets extended from).
  read -r tunx_proto tunx_transport tunx_security <<< "$(tunx_triple "$conf_src")"

  case "$(uname -m)" in
    x86_64|amd64)   tunx_arch='64';        tunx_sha='b651f7e1d9966ee2c1b1ee1bf8b52c72771824d3065dce96bbc8bcc19ed95f82' ;;
    aarch64|arm64)  tunx_arch='arm64-v8a'; tunx_sha='64ed6858982a7f5eb85d88b811089063bd4f17561f4c8f44e0f1dc8e6f6430be' ;;
    *) echo "VPN tunnel: unsupported arch $(uname -m) — skipped."
       tunx_write_status 0 unit-failed "unsupported architecture" "$tunx_proto" "$tunx_transport" "$tunx_security" ""
       exit 0 ;;
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
      tunx_write_status 0 unit-failed "xray download or checksum verification failed" "$tunx_proto" "$tunx_transport" "$tunx_security" ""
      rm -rf "$tunx_dl"
      exit 1
    fi
    rm -rf "$tunx_dl"
  fi
  if [ ! -x "$tunx_bin" ]; then
    echo "WARN: xray binary missing — skipping."
    tunx_write_status 0 unit-failed "xray binary missing after the install attempt" "$tunx_proto" "$tunx_transport" "$tunx_security" ""
    exit 1
  fi

  mkdir -p "$TUNX_HOME"
  chown "$APP_USER:$APP_USER" "$TUNX_HOME" 2>/dev/null || true

  # (b) config: install the app's rendered config (it carries the
  # node credential, so 600), and let xray itself judge it before
  # anything is pointed at the tunnel. Build 190: xray's verdict
  # is CAPTURED (it used to go to /dev/null, which made a rejected
  # node undebuggable from the card) and stored sanitized.
  conf_dst="$TUNX_HOME/config.json"
  conf_sha="$(sha256sum "$conf_src" | cut -d' ' -f1)"
  conf_changed=0
  if [ ! -f "$conf_dst" ] || [ "$(sha256sum "$conf_dst" 2>/dev/null | cut -d' ' -f1)" != "$conf_sha" ]; then
    cp "$conf_src" "$conf_dst"
    conf_changed=1
  fi
  chmod 600 "$conf_dst" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$conf_dst" 2>/dev/null || true
  tunx_test_out="$("$tunx_bin" run -test -config "$conf_dst" 2>&1)"
  if [ $? -ne 0 ]; then
    # xray prints a banner + a "Reading config" info line before
    # the verdict; the failure line itself is the diagnosis, so
    # select it (not a tail window) before sanitizing.
    tunx_reason="$(printf '%s' "$tunx_test_out" | grep -iE 'fail|error' | tail -n 1 | tunx_sanitize)"
    if [ -z "$tunx_reason" ]; then
      tunx_reason="$(printf '%s' "$tunx_test_out" | tail -n 1 | tunx_sanitize)"
    fi
    [ -n "$tunx_reason" ] || tunx_reason="xray rejected the config (no detail captured)"
    echo "WARN: xray rejects the saved config — tunnel pointers cleared (Gemini/OpenAI stay direct). Reason: $tunx_reason"
    tunx_write_status 0 config-invalid "$tunx_reason" "$tunx_proto" "$tunx_transport" "$tunx_security" ""
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
      tunx_write_status 1 "" "" "$tunx_proto" "$tunx_transport" "$tunx_security" "$tunx_country"
      echo "VPN tunnel: live on 127.0.0.1:${TUNX_PORT} — egress country ${tunx_country}; Gemini/OpenAI will use it."
      exit 0
    fi
    tunx_clear_pointers
    tunx_write_status 0 egress-failed "no answer through the local proxy — node unreachable or subscription expired" "$tunx_proto" "$tunx_transport" "$tunx_security" ""
    echo "WARN: tunnel egress check failed (node down?) — tunnel pointers cleared; Gemini/OpenAI stay direct."
    exit 1
  fi
  tunx_reason="$(journalctl -u tun-xray.service -n 5 --no-pager -o cat 2>/dev/null | tail -n 2 | tunx_sanitize)"
  [ -n "$tunx_reason" ] || tunx_reason="service failed to start"
  tunx_clear_pointers
  tunx_write_status 0 unit-failed "$tunx_reason" "$tunx_proto" "$tunx_transport" "$tunx_security" ""
  echo "WARN: tun-xray is not active; tunnel pointers cleared (Gemini/OpenAI stay direct)."
  exit 1
)

# --- pending detection + driver ----------------------------------------------
# A section runs only when the sha of its INPUT values differs
# from the marker its last terminal run left behind. The inputs
# are the values the Admin cards own — never the pointers the
# sections themselves write (those would retrigger forever).

act_tg_inputs_sha() {
  { grep -E '^(TG_API_ID|TG_API_HASH|TELEGRAM_BOT_TOKEN)=' "$APP_DIR/.env" 2>/dev/null || true
    printf 'tg-end\n'
  } | sha256sum | cut -d' ' -f1
}

act_tun_inputs_sha() {
  if [ -s "$APP_DIR/$TUNX_CONF" ]; then
    sha256sum "$APP_DIR/$TUNX_CONF" | cut -d' ' -f1
  else
    printf 'absent'
  fi
}

act_run_section() {  # $1 label, $2 marker file, $3 inputs sha, $4 setup function
  local label="$1" marker="$2" sha="$3" fn="$4"
  if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$sha" ]; then
    echo "Settings activation [$label]: nothing pending."
    return 0
  fi
  echo "Settings activation [$label]: changes pending — activating..."
  if "$fn"; then
    printf '%s' "$sha" > "$marker" 2>/dev/null || true
    chmod 644 "$marker" 2>/dev/null || true
    echo "Settings activation [$label]: done."
    return 0
  fi
  echo "Settings activation [$label]: FAILED — the marker stays stale, the next tick retries."
  return 1
}

act_main() {
  local rc=0
  act_run_section telegram "$ACT_TG_MARKER" "$(act_tg_inputs_sha)" tgba_setup || rc=1
  act_run_section tunnel "$ACT_TUN_MARKER" "$(act_tun_inputs_sha)" tunx_setup || rc=1
  return $rc
}

# Run only when executed, not when sourced (the proof harness
# sources this file to reach tunx_sanitize).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  act_main
  exit $?
fi
