#!/bin/bash
# runall.sh — full end-to-end build, all steps in order:
#
#   [1/3] run_pipeline.py          extract/download + generate + merge + manifests
#   [2/3] translate_enhancements.py  glossary translation → *_all* variants
#   [3/3] create_pr.py             branch build/{p4cl} + PR to main
#
# Every step remains granular and can be run on its own (locally or online):
#   ./run.sh / run_pipeline.py        — pipeline only (supports --skip-extract,
#                                       --skip-generate, --download, --lang)
#   ./translate.sh                    — glossary translation only
#   python create_pr.py               — branch + PR only
#
# All arguments are forwarded to run_pipeline.py, e.g.:
#   ./runall.sh --p4k "/e/StarCitizen/LIVE/Data.p4k"
#   ./runall.sh --download --channel PTU
#   ./runall.sh --skip-extract                 # reuse caches
#   ./runall.sh --p4k "..." --no-pr            # stop before branch/PR
#
# Flags handled here (not forwarded):
#   --no-pr     skip step 3 (branch + PR)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAMBA_ROOT="${SCRIPT_DIR}/.micromamba"

# Windows (Git Bash / MINGW) requires the .exe extension.
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

PY() { "$EXE" run -r "$MAMBA_ROOT" -n lce python "$@"; }

NO_PR=false
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-pr) NO_PR=true ;;
        *)       ARGS+=("$arg") ;;
    esac
done

echo "=== [1/3] Pipeline: extract/download + generate + merge ==="
PY "$SCRIPT_DIR/run_pipeline.py" "${ARGS[@]}"

echo ""
echo "=== [2/3] Glossary translation: *_all* variants ==="
PY "$SCRIPT_DIR/translate_enhancements.py"

if $NO_PR; then
    echo ""
    echo "--no-pr: skipping branch/PR. To publish later:  python create_pr.py"
else
    echo ""
    echo "=== [3/3] Branch build/{p4cl} + Pull Request ==="
    PY "$SCRIPT_DIR/create_pr.py"
fi

echo ""
echo "[runall] All steps completed."
