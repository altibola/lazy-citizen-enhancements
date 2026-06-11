#!/bin/bash
# clean.sh — Remove generated files from the working directory.
#
# Levels:
#   (default)  out/, logs, __pycache__
#   --deep     + .smart-citizen/ and symlinks  (re-run: setup_smart_citizen.sh)
#   --full     + .micromamba/                  (re-run: bootstrap.sh)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEEP=false
FULL=false

for arg in "$@"; do
    case "$arg" in
        --deep) DEEP=true ;;
        --full) DEEP=true; FULL=true ;;
        --help|-h)
            echo "Usage: $0 [--deep] [--full]"
            echo ""
            echo "  (default)  Remove out/, *.log, __pycache__"
            echo "  --deep     Also remove .smart-citizen/ and symlinks"
            echo "             Restore with: ./setup_smart_citizen.sh"
            echo "  --full     Also remove .micromamba/ (Python env)"
            echo "             Restore with: ./bootstrap.sh"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Run '$0 --help' for usage."
            exit 1
            ;;
    esac
done

echo "Cleaning repository..."

# --- Default: outputs, logs, caches ---
rm -rf "${SCRIPT_DIR}/enhancements/dataforge"
rm -rf "${SCRIPT_DIR}/enhancements/tmp"
rm -rf "${SCRIPT_DIR}/tmp"
find "${SCRIPT_DIR}" -name "__pycache__" -not -path "*/.smart-citizen/*" -not -path "*/.micromamba/*" -exec rm -rf {} + 2>/dev/null || true
find "${SCRIPT_DIR}" -maxdepth 1 -name "*.log" -delete

echo "  ✓ enhancements/dataforge, enhancements/tmp, *.log, __pycache__ removed"

# --- Deep: smart-citizen checkout ---
if $DEEP; then
    rm -rf "${SCRIPT_DIR}/.smart-citizen"
    echo "  ✓ .smart-citizen/ removed"
    echo "    → Restore with: ./setup_smart_citizen.sh"
fi

# --- Full: Python environment ---
if $FULL; then
    rm -rf "${SCRIPT_DIR}/.micromamba"
    echo "  ✓ .micromamba/ removed"
    echo "    → Restore with: ./bootstrap.sh"
fi

echo ""
echo "Done."
