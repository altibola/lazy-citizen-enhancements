#!/usr/bin/env bash
# destroy.sh — Tear down the Hetzner build server.
#
# Runs in Git Bash (Windows) and in Linux CI.
#
# Required environment:
#   HCLOUD_TOKEN      Hetzner Cloud API token
#
# Usage:
#   HCLOUD_TOKEN=... infra/hetzner/scripts/destroy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

: "${HCLOUD_TOKEN:?HCLOUD_TOKEN is required (Hetzner Cloud API token)}"

SSH_KEY_FILE="${SSH_KEY_FILE:-${TF_DIR}/.build-ssh-key}"

export TF_VAR_hcloud_token="${HCLOUD_TOKEN}"
# ssh_public_key is required by the module even on destroy.
if [ -f "${SSH_KEY_FILE}.pub" ]; then
  TF_VAR_ssh_public_key="$(cat "${SSH_KEY_FILE}.pub")"
else
  TF_VAR_ssh_public_key="ssh-ed25519 AAAA destroy-placeholder"
fi
export TF_VAR_ssh_public_key

cd "${TF_DIR}"
terraform destroy -input=false -auto-approve

echo "[destroy] Build server destroyed."
