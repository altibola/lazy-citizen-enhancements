#!/bin/bash
# runall.sh — full end-to-end local build, all steps in order:
#
#   [1/4] run_pipeline.py            extract + generate + merge + manifests
#   [2/4] npx validate               local INI key syntax validation
#   [3/4] translate_enhancements.py  glossary translation → *_all* variants
#   [4/4] create_pr.py               branch build/{p4cl} + PR to main
#
# Every step remains granular and can be run on its own:
#   python src/run_pipeline.py        — pipeline only (supports --skip-extract, --lang)
#   ./src/translate.sh                — glossary translation only
#   python src/create_pr.py           — branch + PR only
#
# All arguments are forwarded to run_pipeline.py, e.g.:
#   ./src/runall.sh --p4k "/e/StarCitizen/LIVE/Data.p4k"
#   ./src/runall.sh --skip-extract                 # reuse caches
#   ./src/runall.sh --p4k "..." --no-pr            # stop before branch/PR
#
# Flags handled here (not forwarded):
#   --no-pr     skip step 4 (branch + PR)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"
MAMBA_ROOT="${PROJECT_ROOT}/.micromamba"

# Windows (Git Bash / MINGW) requires the .exe extension.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) EXE="${MAMBA_ROOT}/bin/micromamba.exe" ;;
    *)                     EXE="${MAMBA_ROOT}/bin/micromamba" ;;
esac

if [ ! -f "$EXE" ]; then
    echo "Error: micromamba not found at $EXE"
    echo "Run ./src/bootstrap.sh first."
    exit 1
fi

export MAMBA_ROOT_PREFIX="$MAMBA_ROOT"

PY() { "$EXE" run -r "$MAMBA_ROOT" -n lce python "$@"; }

NO_PR=false
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-pr) NO_PR=true ;;
        *)       ARGS+=("$arg") ;;
    esac
done

echo "=== [1/4] Pipeline: extract + generate + merge ==="
PY "$SCRIPT_DIR/run_pipeline.py" "${ARGS[@]}"

echo ""
echo "=== [2/4] Validando arquivos INI com npx (micromamba) ==="
for l in "portuguese_(brazil)" "french_(france)" "spanish_(spain)"; do
    if [ -f "data/Localization/$l/global.ini" ]; then
        echo "  Validando data/Localization/$l/global.ini..."
        "$EXE" run -r "$MAMBA_ROOT" -n lce npx -y @dymerz/starcitizen-ini-utils validate --fail-on-error --reference-type local --local-path "data/Localization/english/global.ini" "data/Localization/$l/global.ini" || true
    fi
    if [ -f "data/Localization/${l}_all/global.ini" ]; then
        echo "  Validando data/Localization/${l}_all/global.ini..."
        "$EXE" run -r "$MAMBA_ROOT" -n lce npx -y @dymerz/starcitizen-ini-utils validate --fail-on-error --reference-type local --local-path "data/Localization/english/global.ini" "data/Localization/${l}_all/global.ini" || true
    fi
done
echo "✓ Validação concluída!"

echo ""
echo "=== [3/4] Glossary translation: *_all* variants ==="
PY "$SCRIPT_DIR/translate_enhancements.py"

if $NO_PR; then
    echo ""
    echo "--no-pr: skipping branch/PR. To publish later:  python src/create_pr.py"
else
    echo ""
    echo "=== [4/4] Branch build/{p4cl} + Pull Request ==="
    PY "$SCRIPT_DIR/create_pr.py"
fi

echo ""
echo "[runall] All steps completed."
