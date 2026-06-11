#!/bin/bash
# translate.sh — re-translate the enhancement texts after editing the
# glossaries (translations/glossaries/*.json) or the per-key overrides
# (translations/overrides/*.ini).
#
# Runs translate_enhancements.py inside the isolated micromamba env and
# regenerates:
#   enhancements/<lang>/enhancements_translated/*.ini   (intermediate INIs)
#   data/Localization/<id>_all*/global.ini              (final variants)
#   translations/pending/<glossary>.json                (coverage report)
#   VERSIONS.md + README downloads table
#
# Usage (Git Bash / Linux):
#   ./translate.sh                        # all configured languages
#   ./translate.sh --lang portuguese_br_alt
#   ./translate.sh --check                # coverage report only, writes nothing
#   ./translate.sh --commit               # also commit + push the results

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

DO_COMMIT=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--commit" ]; then
        DO_COMMIT=true
    else
        ARGS+=("$arg")
    fi
done

"$EXE" run -r "$MAMBA_ROOT" -n lce python "$SCRIPT_DIR/translate_enhancements.py" "${ARGS[@]}"

if $DO_COMMIT; then
    echo ""
    echo "=== Commitando variantes traduzidas ==="
    cd "$SCRIPT_DIR"
    git add data/ enhancements/ translations/ VERSIONS.md README.md
    if git diff --staged --quiet; then
        echo "Nada mudou — nenhum commit necessário."
    else
        git commit -m "chore(i18n): re-translate enhancement texts (glossary update)"
        git push
    fi
else
    echo ""
    echo "Para commitar os resultados:  ./translate.sh --commit"
    echo "Ou no CI: workflow 'Translate enhancement texts' (workflow_dispatch)."
fi
