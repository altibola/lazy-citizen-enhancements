#!/usr/bin/env bash
# provision.sh — Create the Hetzner build server with Terraform.
#
# Runs in Git Bash (Windows) and in Linux CI.
#
# Required environment:
#   HCLOUD_TOKEN      Hetzner Cloud API token
# Optional:
#   SSH_KEY_FILE      Private key path (default: ./.build-ssh-key, generated
#                     if missing — the matching .pub is passed to Terraform)
#   TF_VAR_server_name / TF_VAR_server_type / TF_VAR_location  overrides
#
# Usage (from repo root or anywhere):
#   HCLOUD_TOKEN=... infra/hetzner/scripts/provision.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

: "${HCLOUD_TOKEN:?HCLOUD_TOKEN is required (Hetzner Cloud API token)}"

SSH_KEY_FILE="${SSH_KEY_FILE:-${TF_DIR}/.build-ssh-key}"

# ── Ephemeral SSH key (generated once, gitignored) ────────────────────────────
if [ ! -f "${SSH_KEY_FILE}" ]; then
  echo "[provision] Generating SSH key: ${SSH_KEY_FILE}"
  ssh-keygen -t ed25519 -N "" -C "lce-build" -f "${SSH_KEY_FILE}"
fi

export TF_VAR_hcloud_token="${HCLOUD_TOKEN}"
TF_VAR_ssh_public_key="$(cat "${SSH_KEY_FILE}.pub")"
export TF_VAR_ssh_public_key

# ── Terraform ─────────────────────────────────────────────────────────────────
cd "${TF_DIR}"
terraform init -input=false
terraform apply -input=false -auto-approve

SERVER_IP="$(terraform output -raw server_ip)"
echo ""
echo "[provision] Build server up: ${SERVER_IP}"
echo "[provision] Next: scripts/run_build.sh"
