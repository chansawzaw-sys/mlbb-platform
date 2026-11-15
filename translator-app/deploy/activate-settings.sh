#!/usr/bin/env bash
# activate-settings.sh — settings activation for the video translator
# (build 190; extracted from translator-bootstrap.sh §13d + §13e;
# build 191: tunnel engine swapped xray-core -> sing-box, Telegram
# section .env parsing hardened).
#
# Two translator features are driven by values the owner saves in
# Admin cards, NOT by code:
#   * Telegram Local API — TG_API_ID / TG_API_HASH in the app .env
#     (Admin card, build 184) -> a local telegram-bot-api server.
#   * VPN tunnel — the node link the owner saved in the app .env
#     (Admin card, build 189), rendered by the app into a sing-box
#     client config at $APP_DIR/tun-singbox.json -> a local
#     sing-box proxy used ONLY by the Gemini/OpenAI clients.
#     (Builds 189/190 rendered an xray config instead; xray-core
#     v26 refuses the plain vless nodes in the owner's reseller
#     subscription, sing-box accepts them — build 191. The
#     section re-renders the config from the saved link itself,
#     so a link saved under the old engine activates with no
#     owner re-save.)
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
# section's INPUT values (the parsed .env credential values / the
# saved tunnel link + rendered config) stored next to the app.
# When the marker matches, the section is a fast no-op (one echo).
# When a card save changes an input, the next tick (or a manual
# apply-update run) activates it — typically within ~15 minutes,
# with no code deploy involved. A section's marker advances only on
# a TERMINAL state (live, or cleanly not-configured, or a config
# the engine rejects outright — re-testing a deterministic parse
# failure every tick buys nothing); transient failures (download,
# unit start, egress) leave the marker stale so the next tick
# retries and the setup self-heals. Because the tunnel section can
# CHANGE its own input while it runs (it re-renders the config
# from the saved link), the marker is written from a sha recomputed
# AFTER the run, not the one that triggered it. Sections are
# independent: one section's failure never blocks the other's
# marker.
#
# FAIL-SAFE BY CONSTRUCTION (inherited from the bootstrap
# sections): every section is a `set +e` subshell behind a guard;
# nothing here can fail a deploy or an update run, and activation
# restarts ONLY its own units (tg-bot-api / tun-singbox) — never
# the translator app, never Caddy.
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
# Build 191 hardening: .env reading is a TOLERANT parse (optional
# quotes, spaces around "=", CRLF, last occurrence wins) — the
# build-190 grep reader flipped to "not configured" once when a
# card save rewrote the .env in a shape it did not expect, while
# the credentials were in fact present. And when a credential IS
# genuinely absent the section now only REPORTS which key is
# missing (names only, never values) and changes NOTHING: it never
# removes TG_API_BASE and never stops the unit. The off-switch is
# the app card's delete, which removes TG_API_BASE itself.
# Return codes (build 190): 0 = terminal (live / not configured /
# skipped), 1 = transient failure (download, unit start) — the
# activation marker stays stale and the next tick retries.
TGBA_VERSION='10.3'
TGBA_HOME="${TGBA_HOME:-/opt/tg-bot-api}"
TGBA_UNIT="${TGBA_UNIT:-/etc/systemd/system/tg-bot-api.service}"
TGBA_DL_BASE="${TGBA_DL_BASE:-https://github.com/SwissCore92/telegram-bot-api-bin/releases/download/v${TGBA_VERSION}}"
TGBA_PORT='8081'

tgba_env_get() {  # $1 = key -> value from the app .env ("" if absent)
  # Tolerant parse (build 191): whitespace around the key and the
  # "=", one pair of surrounding quotes, CRLF endings; the LAST
  # occurrence wins, matching the app's own readers.
  [ -f "$APP_DIR/.env" ] || return 0
  python3 - "$APP_DIR/.env" "$1" <<'PYEOF' 2>/dev/null || true
import sys

path, key = sys.argv[1], sys.argv[2]
val = ""
try:
    text = open(path, encoding="utf-8", errors="replace").read()
except Exception:
    text = ""
for raw in text.splitlines():
    line = raw.strip().lstrip("\ufeff")
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() != key:
        continue
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    val = v
print(val)
PYEOF
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
    # Genuinely absent (the tolerant parse above found nothing):
    # name the missing key(s) — names only — and change NOTHING
    # (build 191: this branch is non-destructive; it never
    # removes TG_API_BASE and never stops the unit).
    tgba_missing=""
    [ -z "$api_id" ] && tgba_missing="TG_API_ID"
    if [ -z "$api_hash" ]; then
      tgba_missing="${tgba_missing:+$tgba_missing + }TG_API_HASH"
    fi
    echo "Local Telegram API: not configured — missing ${tgba_missing} (cloud API, 50 MB sends)."
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
# Admin card, the app parses it into a client config at
# $APP_DIR/tun-singbox.json (chmod 600 — it carries the node
# credential). This section installs sing-box (pinned release +
# sha256 below, verified before anything is installed) and runs
# it as the tun-singbox unit with ONE loopback mixed (SOCKS +
# HTTP) proxy inbound on 127.0.0.1:18119. The app's Gemini/OpenAI
# clients — and nothing else in the system — pass that proxy
# explicitly per request (as http://127.0.0.1:18119, the exact
# build-189 contract), and only after this section has verified
# real egress through it: only then are VT_TUNNEL_PROXY /
# VT_TUNNEL_COUNTRY / VT_TUNNEL_CONF_SHA (the sha of the very
# config verified) written into the app .env, and the app
# re-checks that sha on every call, so a re-saved node is never
# trusted until it has been verified here.
#
# Build 191 — why sing-box: builds 189/190 ran xray-core, whose
# v26 hard-refuses VLESS nodes with no TLS/REALITY encryption
# pointing at public addresses. The owner's reseller
# subscription offers ONLY such nodes (verified live), so the
# tunnel could never come up. sing-box accepts plain nodes;
# everything else about the contract is unchanged. The section
# first re-renders the config from the saved link via the app
# itself (`pipeline.tunnel rerender`), so the xray-era state on
# disk (a tun-xray.json + a build-190 marker) activates on the
# first post-deploy run with NO owner re-save, and the retired
# tun-xray unit is stopped/disabled whenever this section runs.
#
# Diagnosability (build 190, kept): every activation attempt
# leaves its verdict in $APP_DIR/tun-status.json (mode 644, read
# by pipeline/tunnel.py into the Admin card): ok flag, a failure
# kind (config-invalid / egress-failed / unit-failed), the
# engine name, the node's protocol/transport/security triple,
# the exit country on success, and — on failure — ONE sanitized
# reason line. sing-box's raw errors embed node addresses,
# UUIDs and paths, so the reason passes through tun_sanitize()
# before it is stored: the file carries structural text only,
# no secrets by construction. When no config exists the section
# clears the app-side pointers, stops the unit and removes the
# status file; the binary is left installed but unused.
# Return codes: 0 = terminal (live / not configured / config
# rejected — deterministic), 1 = transient (download, unit,
# egress, render-path failure) so the next tick retries.
TUNSB_VERSION='1.14.4'
TUNSB_HOME="${TUNSB_HOME:-/opt/tun-singbox}"
TUNSB_UNIT="${TUNSB_UNIT:-/etc/systemd/system/tun-singbox.service}"
TUNSB_DL_BASE="${TUNSB_DL_BASE:-https://github.com/SagerNet/sing-box/releases/download/v${TUNSB_VERSION}}"
TUNSB_PORT='18119'
TUNSB_CONF='tun-singbox.json'
TUNSB_LEGACY_UNIT='tun-xray.service'

tuns_env_set() {  # $1 = key, $2 = value ("" removes the line)
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

tuns_clear_pointers() {
  tuns_env_set VT_TUNNEL_PROXY ""
  tuns_env_set VT_TUNNEL_COUNTRY ""
  tuns_env_set VT_TUNNEL_CONF_SHA ""
}

tun_sanitize() {  # stdin -> ONE sanitized structural line on stdout
  # Token-based scrubbing: a token is dropped when it looks like
  # a credential or an address — anything containing "://"
  # (URLs and share links), a UUID, an IPv4 address, a dotted
  # hostname (optionally with :port), a file path, or a long
  # opaque blob (keys / hashes / base64, >= 24 chars). The one
  # slash exception is a section prefix style ("infra/conf:"),
  # which is structural. Structural words ("failed", "to",
  # "build", "outbound", "config", "prohibited"...) survive
  # untouched. The result is a single line, capped at 200 chars.
  # The text travels via the environment, NOT stdin: the
  # heredoc below IS python's stdin (its script).
  local raw
  raw="$(cat)"
  TUN_SAN_INPUT="$raw" python3 - <<'PYEOF'
import os
import re

text = os.environ.get("TUN_SAN_INPUT", "")
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

tun_triple() {  # $1 = sing-box config path -> "proto transport security"
  python3 - "$1" <<'PYEOF'
import json
import sys

try:
    conf = json.load(open(sys.argv[1]))
    ob = (conf.get("outbounds") or [{}])[0]
    transport = ((ob.get("transport") or {}).get("type") or "tcp").lower()
    if transport == "http":
        transport = "h2"
    tls = ob.get("tls") or {}
    if (tls.get("reality") or {}).get("enabled"):
        security = "reality"
    elif tls.get("enabled"):
        security = "tls"
    else:
        security = "none"
    print(ob.get("type") or "", transport, security)
except Exception:
    print("", "", "")
PYEOF
}

tun_write_status() {  # $1 ok(1/0) $2 kind $3 reason $4 proto $5 transport $6 security $7 country
  python3 - "$TUN_STATUS" "$1" "$2" "$3" "$4" "$5" "$6" "$7" <<'PYEOF'
import json
import os
import sys
import time

path = sys.argv[1]
data = {"ok": sys.argv[2] == "1",
        "kind": sys.argv[3] or None,
        "reason": sys.argv[4],
        "engine": "sing-box",
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

tuns_rerender() {
  # Build 191: ask the app to re-render $APP_DIR/tun-singbox.json
  # from the link saved in .env (VT_TUNNEL_LINK_B64). Echoes one
  # token: RERENDER_OK / RERENDER_NOLINK / RERENDER_FAIL (the app
  # judged the saved input) / RERENDER_ERROR (the render path
  # itself failed — transient). The app's venv python is
  # preferred (it has `requests` for subscription URLs); the
  # module itself needs only the stdlib otherwise.
  if ! grep -Eq '^[[:space:]]*VT_TUNNEL_LINK_B64[[:space:]]*=' "$APP_DIR/.env" 2>/dev/null; then
    echo "RERENDER_NOLINK"
    return 0
  fi
  local py="$APP_DIR/.venv/bin/python" out
  [ -x "$py" ] || py="$(command -v python3 || true)"
  if [ -z "$py" ]; then
    echo "RERENDER_ERROR"
    return 0
  fi
  out="$(cd "$APP_DIR" 2>/dev/null && "$py" -m pipeline.tunnel rerender 2>/dev/null)"
  case "$out" in
    *RERENDER_OK*) echo "RERENDER_OK" ;;
    *RERENDER_NOLINK*) echo "RERENDER_NOLINK" ;;
    *RERENDER_FAIL*) echo "RERENDER_FAIL" ;;
    *) echo "RERENDER_ERROR" ;;
  esac
}

tuns_setup() (
  set +e  # contained to this subshell: nothing here may fail the deploy
  # The xray-era unit (builds 189/190) is retired: never let it
  # run alongside — or instead of — the sing-box unit.
  systemctl disable --now "$TUNSB_LEGACY_UNIT" >/dev/null 2>&1 || true

  # (0) re-render the config from the saved link (build 191), so
  # state saved under the old engine/selection activates as-is.
  case "$(tuns_rerender)" in
    RERENDER_FAIL)
      echo "WARN: the saved tunnel link could not be rendered into a config — tunnel pointers cleared (Gemini/OpenAI stay direct)."
      tun_write_status 0 config-invalid "the saved link could not be parsed into a sing-box config" "" "" "" ""
      tuns_clear_pointers
      systemctl stop tun-singbox.service >/dev/null 2>&1 || true
      exit 0 ;;
    RERENDER_ERROR)
      echo "WARN: tunnel re-render path failed (retried next run) — nothing changed."
      exit 1 ;;
  esac

  conf_src="$APP_DIR/$TUNSB_CONF"
  if [ ! -s "$conf_src" ] \
     || ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$conf_src" 2>/dev/null; then
    if grep -q '^VT_TUNNEL_PROXY=' "$APP_DIR/.env" 2>/dev/null; then
      tuns_clear_pointers
      echo "VPN tunnel: no config — tunnel pointers cleared (Gemini/OpenAI direct)."
    else
      echo "VPN tunnel: not configured (Gemini/OpenAI direct)."
    fi
    rm -f "$TUN_STATUS"
    systemctl disable --now tun-singbox.service >/dev/null 2>&1 || true
    exit 0
  fi

  # The chosen node's triple — recorded in every status write
  # below (it is what parser support gets extended from).
  read -r tuns_proto tuns_transport tuns_security <<< "$(tun_triple "$conf_src")"

  case "$(uname -m)" in
    x86_64|amd64)   tuns_arch='amd64'; tuns_sha='699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968' ;;
    aarch64|arm64)  tuns_arch='arm64'; tuns_sha='436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26' ;;
    *) echo "VPN tunnel: unsupported arch $(uname -m) — skipped."
       tun_write_status 0 unit-failed "unsupported architecture" "$tuns_proto" "$tuns_transport" "$tuns_security" ""
       exit 0 ;;
  esac
  tuns_asset="sing-box-${TUNSB_VERSION}-linux-${tuns_arch}.tar.gz"
  tuns_bin="$TUNSB_HOME/bin/sing-box"

  # (a) binary: download once per pinned version, sha256-verified.
  if [ ! -x "$tuns_bin" ] || [ "$(cat "$TUNSB_HOME/VERSION" 2>/dev/null)" != "$TUNSB_VERSION" ]; then
    tuns_dl="$(mktemp -d)"
    echo "VPN tunnel: downloading sing-box v${TUNSB_VERSION} (linux-${tuns_arch})..."
    if curl -fsSL --max-time 300 -o "$tuns_dl/$tuns_asset" "$TUNSB_DL_BASE/$tuns_asset" \
       && echo "${tuns_sha}  $tuns_dl/$tuns_asset" | sha256sum -c - >/dev/null 2>&1 \
       && tar -xzf "$tuns_dl/$tuns_asset" -C "$tuns_dl"; then
      mkdir -p "$TUNSB_HOME/bin"
      install -m 755 "$tuns_dl/sing-box-${TUNSB_VERSION}-linux-${tuns_arch}/sing-box" "$tuns_bin"
      echo "$TUNSB_VERSION" > "$TUNSB_HOME/VERSION"
      echo "VPN tunnel: sing-box v${TUNSB_VERSION} installed at $tuns_bin."
    else
      echo "WARN: sing-box download/verify failed — skipping (Gemini/OpenAI stay direct)."
      tun_write_status 0 unit-failed "sing-box download or checksum verification failed" "$tuns_proto" "$tuns_transport" "$tuns_security" ""
      rm -rf "$tuns_dl"
      exit 1
    fi
    rm -rf "$tuns_dl"
  fi
  if [ ! -x "$tuns_bin" ]; then
    echo "WARN: sing-box binary missing — skipping."
    tun_write_status 0 unit-failed "sing-box binary missing after the install attempt" "$tuns_proto" "$tuns_transport" "$tuns_security" ""
    exit 1
  fi

  mkdir -p "$TUNSB_HOME"
  chown "$APP_USER:$APP_USER" "$TUNSB_HOME" 2>/dev/null || true

  # (b) config: install the app's rendered config (it carries the
  # node credential, so 600), and let sing-box itself judge it
  # before anything is pointed at the tunnel. The verdict is
  # CAPTURED (build 190) and stored sanitized.
  conf_dst="$TUNSB_HOME/config.json"
  conf_sha="$(sha256sum "$conf_src" | cut -d' ' -f1)"
  conf_changed=0
  if [ ! -f "$conf_dst" ] || [ "$(sha256sum "$conf_dst" 2>/dev/null | cut -d' ' -f1)" != "$conf_sha" ]; then
    cp "$conf_src" "$conf_dst"
    conf_changed=1
  fi
  chmod 600 "$conf_dst" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$conf_dst" 2>/dev/null || true
  tuns_check_out="$("$tuns_bin" check -c "$conf_dst" 2>&1)"
  if [ $? -ne 0 ]; then
    # sing-box prints log-prefixed lines (FATAL[...] ...); the
    # failure line itself is the diagnosis, so select it (not a
    # tail window) before sanitizing.
    tuns_reason="$(printf '%s' "$tuns_check_out" | grep -iE 'fatal|fail|error' | tail -n 1 | tun_sanitize)"
    if [ -z "$tuns_reason" ]; then
      tuns_reason="$(printf '%s' "$tuns_check_out" | tail -n 1 | tun_sanitize)"
    fi
    [ -n "$tuns_reason" ] || tuns_reason="sing-box rejected the config (no detail captured)"
    echo "WARN: sing-box rejects the saved config — tunnel pointers cleared (Gemini/OpenAI stay direct). Reason: $tuns_reason"
    tun_write_status 0 config-invalid "$tuns_reason" "$tuns_proto" "$tuns_transport" "$tuns_security" ""
    tuns_clear_pointers
    systemctl stop tun-singbox.service >/dev/null 2>&1 || true
    exit 0
  fi

  # (c) systemd unit. Unlike 13d's unit it embeds NO secret (the
  # credential lives in the 600 config file), so default perms.
  unit_changed=0
  tuns_unit_tmp="$(mktemp)"
  cat > "$tuns_unit_tmp" <<EOF
[Unit]
Description=VPN tunnel (sing-box) for the translator's Gemini/OpenAI (build 191)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$TUNSB_HOME
ExecStart=$tuns_bin run -c $conf_dst
Restart=on-failure
RestartSec=5
MemoryMax=512M
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  if [ ! -f "$TUNSB_UNIT" ] || ! cmp -s "$tuns_unit_tmp" "$TUNSB_UNIT"; then
    cat "$tuns_unit_tmp" > "$TUNSB_UNIT"
    unit_changed=1
  fi
  rm -f "$tuns_unit_tmp"
  systemctl daemon-reload
  systemctl enable tun-singbox.service >/dev/null 2>&1 || true
  if [ "$conf_changed" -eq 1 ] || [ "$unit_changed" -eq 1 ] \
     || ! systemctl is-active --quiet tun-singbox.service; then
    systemctl restart tun-singbox.service || echo "WARN: tun-singbox failed to start."
    sleep 2
  fi

  # (d) point the app at the tunnel ONLY when it is really up and
  # carrying traffic: one lightweight egress check THROUGH the
  # proxy (the mixed inbound speaks HTTP proxy, so the build-189
  # check is unchanged); the country it reports is what the
  # Admin card shows.
  if systemctl is-active --quiet tun-singbox.service; then
    tuns_trace="$(curl -fsS --max-time 25 --proxy "http://127.0.0.1:${TUNSB_PORT}" \
                  https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)"
    tuns_country="$(printf '%s' "$tuns_trace" | grep -E '^loc=' | head -1 | cut -d= -f2 | tr -d ' \r' | cut -c1-2)"
    if [ -n "$tuns_trace" ] && [ -n "$tuns_country" ]; then
      tuns_env_set VT_TUNNEL_PROXY "http://127.0.0.1:${TUNSB_PORT}"
      tuns_env_set VT_TUNNEL_COUNTRY "$tuns_country"
      tuns_env_set VT_TUNNEL_CONF_SHA "$conf_sha"
      tun_write_status 1 "" "" "$tuns_proto" "$tuns_transport" "$tuns_security" "$tuns_country"
      echo "VPN tunnel: live on 127.0.0.1:${TUNSB_PORT} — egress country ${tuns_country}; Gemini/OpenAI will use it."
      exit 0
    fi
    tuns_clear_pointers
    tun_write_status 0 egress-failed "no answer through the local proxy — node unreachable or subscription expired" "$tuns_proto" "$tuns_transport" "$tuns_security" ""
    echo "WARN: tunnel egress check failed (node down?) — tunnel pointers cleared; Gemini/OpenAI stay direct."
    exit 1
  fi
  tuns_reason="$(journalctl -u tun-singbox.service -n 5 --no-pager -o cat 2>/dev/null | tail -n 2 | tun_sanitize)"
  [ -n "$tuns_reason" ] || tuns_reason="service failed to start"
  tuns_clear_pointers
  tun_write_status 0 unit-failed "$tuns_reason" "$tuns_proto" "$tuns_transport" "$tuns_security" ""
  echo "WARN: tun-singbox is not active; tunnel pointers cleared (Gemini/OpenAI stay direct)."
  exit 1
)

# --- pending detection + driver ----------------------------------------------
# A section runs only when the sha of its INPUT values differs
# from the marker its last terminal run left behind. The inputs
# are the values the Admin cards own — never the pointers the
# sections themselves write (those would retrigger forever). The
# Telegram inputs are the PARSED credential values (build 191),
# so a formatting-only .env rewrite does not retrigger anything.

act_tg_inputs_sha() {
  { printf 'id=%s\n' "$(tgba_env_get TG_API_ID)"
    printf 'hash=%s\n' "$(tgba_env_get TG_API_HASH)"
    printf 'tok=%s\n' "$(tgba_env_get TELEGRAM_BOT_TOKEN)"
    printf 'tg-end\n'
  } | sha256sum | cut -d' ' -f1
}

act_tun_inputs_sha() {
  # Inputs: the saved link line (the card's value — a re-save with
  # a new node changes it) + the rendered sing-box config the
  # section consumes (its re-render output). Keyed on both so the
  # first build-191 run fires on the owner's build-190 state
  # (old marker, xray-era config) and re-renders + activates with
  # no re-save.
  { grep -E '^[[:space:]]*VT_TUNNEL_LINK_B64[[:space:]]*=' "$APP_DIR/.env" 2>/dev/null || true
    if [ -s "$APP_DIR/$TUNSB_CONF" ]; then
      sha256sum "$APP_DIR/$TUNSB_CONF" | cut -d' ' -f1
    else
      printf 'noconf\n'
    fi
  } | sha256sum | cut -d' ' -f1
}

act_run_section() {  # $1 label, $2 marker file, $3 inputs-sha fn, $4 setup fn
  local label="$1" marker="$2" shafn="$3" fn="$4" sha
  sha="$("$shafn")"
  if [ -f "$marker" ] && [ "$(cat "$marker" 2>/dev/null)" = "$sha" ]; then
    echo "Settings activation [$label]: nothing pending."
    return 0
  fi
  echo "Settings activation [$label]: changes pending — activating..."
  if "$fn"; then
    # The marker records the inputs as they stand AFTER the run:
    # a section may have changed its own inputs while running
    # (the tunnel section re-renders its config from the link).
    printf '%s' "$("$shafn")" > "$marker" 2>/dev/null || true
    chmod 644 "$marker" 2>/dev/null || true
    echo "Settings activation [$label]: done."
    return 0
  fi
  echo "Settings activation [$label]: FAILED — the marker stays stale, the next tick retries."
  return 1
}

act_main() {
  local rc=0
  act_run_section telegram "$ACT_TG_MARKER" act_tg_inputs_sha tgba_setup || rc=1
  act_run_section tunnel "$ACT_TUN_MARKER" act_tun_inputs_sha tuns_setup || rc=1
  return $rc
}

# Run only when executed, not when sourced (the proof harness
# sources this file to reach tun_sanitize).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  act_main
  exit $?
fi
