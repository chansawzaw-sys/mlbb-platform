#!/usr/bin/env bash
# Fight for Legendary — attach fightforlegendary.com to the MLBB app via Caddy.
#
# Run AFTER deploy/bootstrap.sh has pulled the latest code (this file arrives
# with that pull):
#   bash /opt/mlbb-platform/deploy/attach-domain.sh
#
# What it does (idempotent — safe to re-run):
#   1. Writes /etc/caddy/mlbb.caddy: apex domain -> reverse_proxy
#      127.0.0.1:8080 (the mlbb container), www -> permanent redirect to apex.
#      Caddy provisions the Let's Encrypt certificate automatically
#      (TLS-ALPN-01 on 443; port 80 on this box is NAT-redirected to the
#      translator, so HTTP-01 is unavailable by design).
#   2. Imports that file from /etc/caddy/Caddyfile (same pattern the
#      translator uses — the two apps' blocks coexist).
#   3. Validates the full Caddy config BEFORE reloading; on any failure it
#      restores the previous mlbb.caddy and leaves Caddy untouched.
#   4. Reloads Caddy and polls https://fightforlegendary.com/health for up
#      to ~90s (certificate issuance can take a little while).
#
# Success ends with ATTACH_DONE; a still-pending certificate ends with
# ATTACH_PENDING_CERT (Caddy keeps retrying in the background — re-check
# the URL in a few minutes). Any hard failure prints ATTACH_FAILED.

set -uo pipefail

DOMAIN='fightforlegendary.com'
APP_PORT='8080'
SITE_FILE='/etc/caddy/mlbb.caddy'
MAIN_FILE='/etc/caddy/Caddyfile'

echo "=== MLBB domain attach starting: $(date -Is) ==="

if [ "$(id -u)" -ne 0 ]; then
  echo "ATTACH_FAILED: this script must run as root."
  exit 1
fi

if ! command -v caddy >/dev/null 2>&1; then
  echo "ATTACH_FAILED: caddy is not installed on this server."
  exit 1
fi

mkdir -p /etc/caddy
if [ -f "$SITE_FILE" ]; then
  cp "$SITE_FILE" "$SITE_FILE.bak"
fi

cat > "$SITE_FILE" <<EOF
# === mlbb platform (managed by attach-domain.sh) ===
${DOMAIN} {
	reverse_proxy 127.0.0.1:${APP_PORT}
	encode gzip
}

www.${DOMAIN} {
	redir https://${DOMAIN}{uri} permanent
}
# === end mlbb platform ===
EOF

touch "$MAIN_FILE"
if ! grep -q 'mlbb\.caddy' "$MAIN_FILE"; then
  echo 'import /etc/caddy/mlbb.caddy' >> "$MAIN_FILE"
fi

if ! caddy validate --config "$MAIN_FILE" >/dev/null 2>&1; then
  echo "ATTACH_FAILED: caddy validate rejected the config; restoring previous site file."
  if [ -f "$SITE_FILE.bak" ]; then
    cp "$SITE_FILE.bak" "$SITE_FILE"
  else
    rm -f "$SITE_FILE"
  fi
  exit 1
fi

systemctl reload caddy 2>/dev/null || systemctl restart caddy
sleep 2
if ! systemctl is-active --quiet caddy; then
  echo "ATTACH_FAILED: caddy is not active after reload; restoring previous site file."
  if [ -f "$SITE_FILE.bak" ]; then
    cp "$SITE_FILE.bak" "$SITE_FILE"
  else
    rm -f "$SITE_FILE"
  fi
  systemctl restart caddy || true
  exit 1
fi

echo "Caddy reloaded. Waiting for https://${DOMAIN}/health ..."
healthy=0
for _ in $(seq 1 45); do
  if body="$(curl -fsS "https://${DOMAIN}/health" 2>/dev/null)"; then
    echo "HEALTH: $body"
    healthy=1
    break
  fi
  sleep 2
done

if [ "$healthy" -eq 1 ]; then
  echo "ATTACH_DONE"
else
  echo "ATTACH_PENDING_CERT: HTTPS not answering yet — Caddy keeps trying in the background."
  echo "Re-check https://${DOMAIN}/health in a few minutes."
fi
