#!/usr/bin/env bash
# apply-update.sh — one-shot server updater for the video translator.
#
# Manual use (as root on the VPS):
#   wget raw.githubusercontent.com/chansawzaw-sys/mlbb-platform/main/translator-app/deploy/apply-update.sh
#   bash apply-update.sh
#
# It pulls the latest code from the local repo clone; if (and only if)
# something changed, it syntax-checks the Python, syncs the code into
# /opt/video-translator, optionally sets a new basic-auth password
# (typed interactively — never stored in this repo), and re-runs the
# full bootstrap (which restarts the app + Caddy and health-checks).
#
# With --auto it runs unattended (installed as a systemd timer by the
# bootstrap): no password prompt, no-op when nothing changed, and a
# lock so runs never overlap.
set -euo pipefail

REPO='/opt/mlbb-platform'
APP='/opt/video-translator'
AUTH_FILE='/root/translator-auth.txt'
AUTO=0
if [ "${1:-}" = "--auto" ]; then
  AUTO=1
  exec 9>/run/translator-autoupdate.lock
  flock -n 9 || exit 0
fi

cd "$REPO"
BEFORE="$(git rev-parse HEAD)"
git pull
AFTER="$(git rev-parse HEAD)"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "Already up to date ($AFTER)."
  exit 0
fi
echo "Updating $BEFORE -> $AFTER"

# Safety gate: never sync Python that does not compile.
if ! python3 -m py_compile translator-app/app.py translator-app/pipeline/*.py; then
  echo "ERROR: new code does not compile; staying on the old version."
  exit 1
fi

cp -r translator-app/. "$APP/"
echo "Code updated from GitHub."

if [ "$AUTO" -eq 0 ]; then
  echo "New translator password (just press Enter to keep the current one):"
  read -r NEWPW
  if [ -n "$NEWPW" ]; then
    printf 'password: %s\n' "$NEWPW" > "$AUTH_FILE"
    chmod 600 "$AUTH_FILE"
    echo "Password updated."
  fi
fi

bash "$APP/deploy/translator-bootstrap.sh"
echo "APPLY_UPDATE_DONE"
