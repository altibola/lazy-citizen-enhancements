#!/bin/bash
# run.sh — run the pipeline inside the isolated micromamba env.
# Forwards all arguments to run_pipeline.py, e.g.:
#   ./run.sh --p4k "/path/to/StarCitizen/LIVE/Data.p4k"
#   ./run.sh --lang portuguese_br --workers 8
#   ./run.sh --skip-extract --all

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

RUN_PR=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--pr" ]; then
        RUN_PR=true
    else
        ARGS+=("$arg")
    fi
done

# Run pipeline in micromamba environment
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
        echo "Ou execute o pipeline passando a flag --pr, ex: ./run.sh --pr"
        echo ""
    fi
fi
exit $STATUS
