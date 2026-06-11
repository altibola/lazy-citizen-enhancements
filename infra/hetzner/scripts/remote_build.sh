#!/usr/bin/env bash
# remote_build.sh — Runs ON the Hetzner server (piped over SSH by run_build.sh).
#
# Clones the repo, runs the full pipeline with --download (RSI CDN), then
# pushes the build/{p4cl} branch and opens the PR via create_pr.py.
#
# Required environment (exported by the caller before piping this script):
#   GIT_REPO_URL     https clone URL (token embedded for private repos / push)
#   GIT_BRANCH       branch to check out before building (e.g. main)
#   RSI_USERNAME     RSI account login
#   RSI_PASSWORD     RSI account password
#   SC_CHANNEL       LIVE | PTU | EPTU
# Optional:
#   RSI_MFA_CODE     current MFA code (needed on first auth of a session)
#   GITHUB_TOKEN     used by create_pr.py to open the PR via API
#   GIT_USER_NAME / GIT_USER_EMAIL  committer identity (defaults below)

set -euo pipefail

: "${GIT_REPO_URL:?GIT_REPO_URL is required}"
: "${RSI_USERNAME:?RSI_USERNAME is required}"
: "${RSI_PASSWORD:?RSI_PASSWORD is required}"

SC_CHANNEL="${SC_CHANNEL:-LIVE}"
GIT_BRANCH="${GIT_BRANCH:-main}"
WORKDIR=/opt/lce

echo "=== [remote] LCE build — channel ${SC_CHANNEL} ==="

# ── 1. Clone ──────────────────────────────────────────────────────────────────
rm -rf "${WORKDIR}"
git clone --branch "${GIT_BRANCH}" "${GIT_REPO_URL}" "${WORKDIR}"
cd "${WORKDIR}"

git config user.name  "${GIT_USER_NAME:-lce-build-bot}"
git config user.email "${GIT_USER_EMAIL:-lce-build-bot@users.noreply.github.com}"

# ── 2. Python env ─────────────────────────────────────────────────────────────
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet lxml zstandard

# ── 3. Smart Citizen tools (generator + unforge.exe) ─────────────────────────
chmod +x setup_smart_citizen.sh
./setup_smart_citizen.sh

# mono/binfmt sanity — unforge.exe is a .NET Framework binary.
mono --version | head -1
chmod +x .smart-citizen/assets/unp4k/*.exe

# ── 4. Full pipeline: CDN download + generation + merge ──────────────────────
python run_pipeline.py \
  --download \
  --channel "${SC_CHANNEL}" \
  ${RSI_MFA_CODE:+--rsi-mfa-code "${RSI_MFA_CODE}"}
# RSI_USERNAME / RSI_PASSWORD are read from the environment by run_pipeline.

# ── 5. Glossary translation: *_all* variants ─────────────────────────────────
python translate_enhancements.py

# ── 6. Version manifest + branch + PR ─────────────────────────────────────────
python versions_report.py --stdout-only
python create_pr.py

echo "=== [remote] Build finished ==="
