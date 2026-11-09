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

if [ "$AUTO" -eq 1 ]; then
  # Never restart the app out from under a running job: skip this tick
  # entirely (the pull is retried next tick, so nothing is lost).
  if python3 - "$APP" <<'PYEOF'
import json
import sys
import time
from pathlib import Path

app = Path(sys.argv[1])
for p in app.glob("jobs/*/job.json"):
    try:
        j = json.loads(p.read_text())
    except Exception:
        continue
    age = time.time() - p.stat().st_mtime
    # Running jobs heartbeat their file, so 25 min of freshness is a
    # live job; a QUEUED job has no heartbeat, so give the queue a
    # longer window before an update may restart over it.
    if j.get("status") == "running" and age < 25 * 60:
        sys.exit(0)  # an active job exists
    if j.get("status") == "queued" and age < 120 * 60:
        sys.exit(0)  # a queued job is waiting
sys.exit(1)
PYEOF
  then
    echo "A job is running; deferring the update to the next tick."
    exit 0
  fi
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

timeout --kill-after=10 600 bash "$APP/deploy/translator-bootstrap.sh" || { echo "BOOTSTRAP FAILED OR TIMED OUT"; exit 1; }
echo "APPLY_UPDATE_DONE"
