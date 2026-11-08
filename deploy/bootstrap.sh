#!/usr/bin/env bash
# MLBB KING — one-shot VPS bootstrap for the relaunch.
#
# Target: fresh Ubuntu 24.04 VPS (root web console), e.g.:
#   curl -fsSL https://raw.githubusercontent.com/chansawzaw-sys/mlbb-platform/main/deploy/bootstrap.sh | bash
#
# What it does (idempotent — safe to re-run):
#   1. Installs curl/git/ufw/docker.io and starts Docker.
#   2. Creates a 2 GB swapfile if no swap is active.
#   3. Firewall: allow 22/80/443, enable ufw.
#   4. Installs the operator SSH public key for root (appends, never removes).
#   5. Clones (or fast-forward pulls) the public app repo to /opt/mlbb-platform.
#   6. Builds the app image from docker/Dockerfile.railway, tagged mlbb:latest.
#   7. Runs container "mlbb" with host /srv/mlbb/data mounted at /data,
#      host port 8080 -> container port 3000 (app PORT; DATA_DIR=/data is
#      already set by the Dockerfile ENV, so it is not repeated here).
#   8. Polls http://localhost:8080/health for up to ~60s and prints it.
#
# Deliberately NOT done here: no domain/HTTPS/reverse proxy (domain attach
# comes later), no app source edits, no secrets printed.
# Full output is logged to /root/bootstrap.log. Success ends with
# BOOTSTRAP_DONE; any failure prints BOOTSTRAP_FAILED.

set -euo pipefail

LOG_FILE=/root/bootstrap.log
SSH_PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKs2bC3BNlE4P4VB8pgO2IgTZcYhNepFwMx431nA7DNg hatch'
REPO_URL='https://github.com/chansawzaw-sys/mlbb-platform.git'
REPO_DIR='/opt/mlbb-platform'
DATA_DIR_HOST='/srv/mlbb/data'
IMAGE_TAG='mlbb:latest'
CONTAINER_NAME='mlbb'
HOST_PORT='8080'
CONTAINER_PORT='3000'

# --- logging + failure trap -------------------------------------------------
mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1

on_error() {
  echo "BOOTSTRAP_FAILED (exit=$?, line=$1)"
  echo "--- last container logs (if any) ---"
  docker logs --tail 50 "$CONTAINER_NAME" 2>/dev/null || true
  echo "--- full log: $LOG_FILE ---"
}
trap 'on_error $LINENO' ERR

echo "=== MLBB bootstrap starting: $(date -Is) ==="

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: this script must run as root."
  exit 1
fi

# --- 1. packages + docker ---------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl git ufw docker.io
systemctl enable docker || true
systemctl start docker || service docker start
docker --version

# --- 2. swap (2 GB) -----------------------------------------------------------
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

# --- 3. firewall --------------------------------------------------------------
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status || true

# --- 4. operator SSH key for root ----------------------------------------------
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
if ! grep -qxF "$SSH_PUBKEY" /root/.ssh/authorized_keys; then
  echo "$SSH_PUBKEY" >> /root/.ssh/authorized_keys
fi
echo "SSH key installed for root."

# --- 5. app source --------------------------------------------------------------
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$REPO_DIR"
fi

# --- 6. build image ---------------------------------------------------------------
# Dockerfile.railway: FROM oven/bun:1, COPY . ., bun install, decodes the
# *.webp.b64 assets at build time, ENV DATA_DIR=/data, EXPOSE 3000,
# CMD bun run server.ts. The server reads PORT at runtime (default 3000).
docker build -f "$REPO_DIR/docker/Dockerfile.railway" -t "$IMAGE_TAG" "$REPO_DIR"

# --- 7. run container ----------------------------------------------------------------
mkdir -p "$DATA_DIR_HOST"
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  docker rm -f "$CONTAINER_NAME"
fi
# PORT is passed explicitly because the Dockerfile does not set it (it relies
# on the platform injecting it); DATA_DIR is NOT passed — the Dockerfile
# already sets ENV DATA_DIR=/data.
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -e PORT="$CONTAINER_PORT" \
  -v "$DATA_DIR_HOST":/data \
  -p "$HOST_PORT":"$CONTAINER_PORT" \
  "$IMAGE_TAG"

# --- 8. health check -------------------------------------------------------------------
echo "Waiting for http://localhost:${HOST_PORT}/health ..."
healthy=0
for _ in $(seq 1 30); do
  if body="$(curl -fsS "http://localhost:${HOST_PORT}/health" 2>/dev/null)"; then
    echo "HEALTH: $body"
    healthy=1
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo "ERROR: /health did not return ok within ~60s."
  exit 1
fi

trap - ERR
echo "BOOTSTRAP_DONE"
