#!/bin/bash
# setup_smart_citizen.sh — Clone or update the smart-citizen repository.
#
# Smart Citizen provides the enhancement generator and merger used by this
# pipeline. It is fetched at setup time; the Python code references it via
# the SMART_CITIZEN_DIR variable (.smart-citizen/).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMART_CITIZEN_DIR="${SCRIPT_DIR}/.smart-citizen"
SMART_CITIZEN_REPO="https://github.com/Osiris-DevWorks/smart-citizen.git"
SMART_CITIZEN_BRANCH="main"

echo "Setting up smart-citizen..."

if [ ! -d "$SMART_CITIZEN_DIR" ]; then
    echo "Cloning smart-citizen..."
    git clone --depth 1 --branch "$SMART_CITIZEN_BRANCH" "$SMART_CITIZEN_REPO" "$SMART_CITIZEN_DIR"
else
    echo "Updating smart-citizen..."
    (cd "$SMART_CITIZEN_DIR" && git fetch origin "$SMART_CITIZEN_BRANCH" && git checkout "origin/$SMART_CITIZEN_BRANCH")
fi

echo ""
echo "✓ smart-citizen ready at: $SMART_CITIZEN_DIR"
echo ""

# Fetch the unp4k/unforge builds for this platform (github.com/dolkensp/unp4k).
# No-op on Windows (the .exes are bundled with Smart Citizen) and on re-runs.
# On Linux/macOS this downloads the DLL builds, which run via `dotnet`.
echo "Ensuring unp4k/unforge binaries for this platform..."
if command -v python3 &> /dev/null; then
    PYBIN=python3
else
    PYBIN=python
fi
"$PYBIN" "${SCRIPT_DIR}/setup_tools.py"
echo ""
