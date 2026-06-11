#!/bin/bash
# run-local.sh — Process using local Data.p4k (no download).
#
# REQUIRES: Existing Star Citizen installation with Data.p4k.
#   • Looks for Data.p4k at the default location or via --p4k argument
#   • No RSI authentication needed — faster, offline
#
# Usage:
#   ./run-local.sh [--p4k "/path/to/Data.p4k"] [--lang LANG] [--workers N] [--all]
#
# Default locations (checked in order):
#   1. ~/StarCitizen/LIVE/Data.p4k
#   2. ~/Games/Star Citizen/LIVE/Data.p4k
#   3. (specify manually with --p4k)
#
# Examples:
#   ./run-local.sh                                        # Auto-detect location
#   ./run-local.sh --lang portuguese_br                   # Portuguese (Brazil)
#   ./run-local.sh --p4k "D:/StarCitizen/LIVE/Data.p4k"   # Custom path
#   ./run-local.sh --all --workers 8                      # All languages, 8 threads

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAMBA_ROOT="${SCRIPT_DIR}/.micromamba"

case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) EXE="${MAMBA_ROOT}/bin/micromamba.exe" ;;
    *)                     EXE="${MAMBA_ROOT}/bin/micromamba" ;;
esac

if [ ! -f "$EXE" ]; then
    echo "Error: micromamba not found at $EXE"
    echo "Run ./bootstrap.sh first."
    exit 1
fi

export MAMBA_ROOT_PREFIX="$MAMBA_ROOT"

RUN_PR=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--pr" ]; then
        RUN_PR=true
    else
        ARGS+=("$arg")
    fi
done

# Run WITHOUT --download (uses local Data.p4k)
"$EXE" run -r "$MAMBA_ROOT" -n lce python "$SCRIPT_DIR/run_pipeline.py" "${ARGS[@]}"
STATUS=$?

if [ $STATUS -eq 0 ]; then
    if $RUN_PR; then
        echo ""
        echo "=== Iniciando automação de Pull Request ==="
        "$EXE" run -r "$MAMBA_ROOT" -n lce python "$SCRIPT_DIR/create_pr.py"
        STATUS=$?
    else
        echo ""
        echo "✓ Pipeline executado com sucesso!"
        echo "Para criar um branch e abrir um Pull Request com estas mudanças, rode:"
        echo "  python create_pr.py"
        echo "Ou execute o pipeline passando a flag --pr, ex: ./run-local.sh --pr"
        echo ""
    fi
fi
exit $STATUS
