#!/usr/bin/env bash
# Runs once when the Codespace is created: install Bun + app deps.
set -e
export PATH="$HOME/.bun/bin:$PATH"
if ! command -v bun >/dev/null 2>&1; then
  npm install -g bun
fi
bun --version
bun install
echo "Setup complete."
