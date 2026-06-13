#!/bin/bash
# clean.sh — Remove generated files from the working directory.
#
# Levels:
#   (default)  out/, logs, __pycache__
#   --deep     + .smart-citizen/ and symlinks  (re-run: setup_smart_citizen.sh)
#   --full     + .micromamba/                  (re-run: bootstrap.sh)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"
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
rm -rf "${PROJECT_ROOT}/enhancements/dataforge"
rm -rf "${PROJECT_ROOT}/enhancements/tmp"
rm -rf "${PROJECT_ROOT}/tmp"
find "${PROJECT_ROOT}" -name "__pycache__" -not -path "*/.smart-citizen/*" -not -path "*/.micromamba/*" -exec rm -rf {} + 2>/dev/null || true
find "${PROJECT_ROOT}" -maxdepth 1 -name "*.log" -delete

echo "  ✓ enhancements/dataforge, enhancements/tmp, *.log, __pycache__ removed"

# --- Deep: smart-citizen checkout ---
if $DEEP; then
    rm -rf "${PROJECT_ROOT}/.smart-citizen"
    echo "  ✓ .smart-citizen/ removed"
    echo "    → Restore with: ./setup_smart_citizen.sh"
fi

# --- Full: Python environment ---
if $FULL; then
    rm -rf "${PROJECT_ROOT}/.micromamba"
    echo "  ✓ .micromamba/ removed"
    echo "    → Restore with: ./bootstrap.sh"
fi

echo ""
echo "Done."
