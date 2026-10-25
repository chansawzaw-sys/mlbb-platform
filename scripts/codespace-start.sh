#!/usr/bin/env bash
# Runs every time the Codespace starts: launch the app server in the
# background (idempotent — does nothing if it is already up).
# Data lives in ./.data inside the workspace, so it persists in the
# Codespace between stops and restarts.
export PATH="$HOME/.bun/bin:$PATH"
cd "$(dirname "$0")/.."
mkdir -p .data
if curl -s -m 2 http://localhost:3000/health >/dev/null 2>&1; then
  echo "MLBB server already running on port 3000."
  exit 0
fi
DATA_DIR="$PWD/.data" PORT=3000 nohup bun run server.ts > .data/server.log 2>&1 &
echo "MLBB server starting... (log: .data/server.log)"
for i in $(seq 1 30); do
  if curl -s -m 2 http://localhost:3000/health >/dev/null 2>&1; then
    echo "MLBB server is up on port 3000."
    exit 0
  fi
  sleep 1
done
echo "Server did not come up within 30s - see .data/server.log"
exit 1
