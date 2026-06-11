#!/usr/bin/env bash
# run_build.sh — Execute the pipeline on the provisioned Hetzner server.
#
# Runs in Git Bash (Windows) and in Linux CI. Waits for cloud-init to finish,
# then pipes remote_build.sh over SSH with the build environment.
#
# Required environment:
#   RSI_USERNAME, RSI_PASSWORD    RSI account credentials
# Optional:
#   SC_CHANNEL        LIVE | PTU | EPTU         (default: LIVE)
#   RSI_MFA_CODE      current MFA code, if your account requires it
#   GITHUB_TOKEN      lets create_pr.py open the PR via API
#   GIT_REPO_URL      override clone URL (default: origin of this checkout,
#                     with GITHUB_TOKEN embedded when provided)
#   GIT_BRANCH        branch to build from (default: main)
#   SSH_KEY_FILE      private key path (default: ../.build-ssh-key)
#
# Usage:
#   RSI_USERNAME=... RSI_PASSWORD=... infra/hetzner/scripts/run_build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${TF_DIR}/../.." && pwd)"

: "${RSI_USERNAME:?RSI_USERNAME is required}"
: "${RSI_PASSWORD:?RSI_PASSWORD is required}"

SC_CHANNEL="${SC_CHANNEL:-LIVE}"
GIT_BRANCH="${GIT_BRANCH:-main}"
SSH_KEY_FILE="${SSH_KEY_FILE:-${TF_DIR}/.build-ssh-key}"

# ── Resolve server IP from terraform state ────────────────────────────────────
SERVER_IP="$(cd "${TF_DIR}" && terraform output -raw server_ip)"
echo "[run_build] Server: ${SERVER_IP}"

# ── Resolve repo URL (embed token for push when provided) ─────────────────────
if [ -z "${GIT_REPO_URL:-}" ]; then
  ORIGIN_URL="$(cd "${REPO_ROOT}" && git remote get-url origin)"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    # https://github.com/owner/repo.git → token-authenticated form
    GIT_REPO_URL="${ORIGIN_URL/https:\/\/github.com\//https://x-access-token:${GITHUB_TOKEN}@github.com/}"
  else
    GIT_REPO_URL="${ORIGIN_URL}"
  fi
fi

# Git Bash: stop MSYS from rewriting remote paths like /opt/lce.
export MSYS_NO_PATHCONV=1

SSH_OPTS=(
  -i "${SSH_KEY_FILE}"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10
  -o LogLevel=ERROR
)

# ── Wait for SSH + cloud-init ─────────────────────────────────────────────────
echo "[run_build] Waiting for SSH..."
for i in $(seq 1 30); do
  if ssh "${SSH_OPTS[@]}" "root@${SERVER_IP}" true 2>/dev/null; then
    break
  fi
  [ "$i" = 30 ] && { echo "[run_build] SSH never came up"; exit 1; }
  sleep 5
done

echo "[run_build] Waiting for cloud-init (package install)..."
ssh "${SSH_OPTS[@]}" "root@${SERVER_IP}" "cloud-init status --wait" || true
ssh "${SSH_OPTS[@]}" "root@${SERVER_IP}" \
  "test -f /var/lib/cloud/instance/lce-ready" \
  || { echo "[run_build] cloud-init did not finish cleanly"; exit 1; }

# ── Run the build remotely ────────────────────────────────────────────────────
# Env is passed inline on the remote command line via a here-doc wrapper so
# secrets never land in a file on the server.
echo "[run_build] Starting remote build (channel=${SC_CHANNEL})..."
ssh "${SSH_OPTS[@]}" "root@${SERVER_IP}" \
  "GIT_REPO_URL='${GIT_REPO_URL}' \
   GIT_BRANCH='${GIT_BRANCH}' \
   RSI_USERNAME='${RSI_USERNAME}' \
   RSI_PASSWORD='${RSI_PASSWORD}' \
   RSI_MFA_CODE='${RSI_MFA_CODE:-}' \
   SC_CHANNEL='${SC_CHANNEL}' \
   GITHUB_TOKEN='${GITHUB_TOKEN:-}' \
   bash -s" < "${SCRIPT_DIR}/remote_build.sh"

echo "[run_build] Done. Remember to destroy the server: scripts/destroy.sh"
