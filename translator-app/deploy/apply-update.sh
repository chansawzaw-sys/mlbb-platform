#!/usr/bin/env bash
# apply-update.sh — one-shot server updater for the video translator.
#
# Run as root on the VPS:
#   wget raw.githubusercontent.com/chansawzaw-sys/mlbb-platform/main/translator-app/deploy/apply-update.sh
#   bash apply-update.sh
#
# It pulls the latest code from the local repo clone, syncs it into
# /opt/video-translator, optionally sets a new basic-auth password
# (typed interactively — never stored in this repo), and re-runs the
# full bootstrap (which restarts the app + Caddy and health-checks).
set -euo pipefail

REPO='/opt/mlbb-platform'
APP='/opt/video-translator'
AUTH_FILE='/root/translator-auth.txt'

cd "$REPO"
git pull
cp -r translator-app/. "$APP/"
echo "Code updated from GitHub."

echo "New translator password (just press Enter to keep the current one):"
read -r NEWPW
if [ -n "$NEWPW" ]; then
  printf 'password: %s\n' "$NEWPW" > "$AUTH_FILE"
  chmod 600 "$AUTH_FILE"
  echo "Password updated."
fi

bash "$APP/deploy/translator-bootstrap.sh"
echo "APPLY_UPDATE_DONE"
