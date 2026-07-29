#!/bin/bash
# bootstrap.sh — set up an isolated Python env for lazy-citizen-enhancements.
#
# Uses micromamba (a single self-contained binary) with a PROJECT-LOCAL root
# prefix, so nothing touches the global environment, PATH, or system packages.
# Deleting the project folder removes everything. Idempotent: safe to re-run.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"
MAMBA_ROOT="${PROJECT_ROOT}/.micromamba"
ENV_YML="${PROJECT_ROOT}/environment.yml"

# Detect OS and architecture up front (needed for both EXE name and download URL).
OS=$(uname -s)
ARCH=$(uname -m)

# Windows binaries require the .exe extension to be executed by Git Bash.
case "$OS" in
    MINGW*|MSYS*|CYGWIN*)
        EXE="${MAMBA_ROOT}/bin/micromamba.exe"
        ;;
    *)
        EXE="${MAMBA_ROOT}/bin/micromamba"
        ;;
esac

# Ensure the bin/ directory exists before curl tries to write into it.
mkdir -p "$(dirname "$EXE")"

if [ ! -f "$EXE" ]; then
    case "$OS" in
        Linux)
            if [ "$ARCH" = "x86_64" ]; then
                URL="https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-linux-64"
            elif [ "$ARCH" = "aarch64" ]; then
                URL="https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-linux-aarch64"
            else
                echo "Unsupported architecture: $ARCH"
                exit 1
            fi
            ;;
        Darwin)
            if [ "$ARCH" = "x86_64" ]; then
                URL="https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-osx-64"
            elif [ "$ARCH" = "arm64" ]; then
                URL="https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-osx-arm64"
            else
                echo "Unsupported architecture: $ARCH"
                exit 1
            fi
            ;;
        MINGW*|MSYS*|CYGWIN*)
            URL="https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-win-64.exe"
            ;;
        *)
            echo "Unsupported OS: $OS"
            exit 1
            ;;
    esac

    echo "Downloading micromamba for $OS/$ARCH..."
    echo "URL: $URL"

    if command -v curl &> /dev/null; then
        curl -L "$URL" -o "$EXE"
    elif command -v wget &> /dev/null; then
        wget "$URL" -O "$EXE"
    else
        echo "Error: Neither curl nor wget found. Please install one."
        exit 1
    fi

    chmod +x "$EXE"
else
    echo "micromamba already present: $EXE"
fi

# Project-local root prefix -> env lives at .micromamba/envs/lce
export MAMBA_ROOT_PREFIX="$MAMBA_ROOT"

echo "Creating/updating env 'lce' from environment.yml ..."
"$EXE" create -y -r "$MAMBA_ROOT" -n lce -f "$ENV_YML"

echo ""
echo "Setting up smart-citizen..."
SETUP_SCRIPT="${SCRIPT_DIR}/setup_smart_citizen.sh"
if [ -f "$SETUP_SCRIPT" ]; then
    chmod +x "$SETUP_SCRIPT"
    "$SETUP_SCRIPT"
else
    echo "Warning: setup_smart_citizen.sh not found"
    echo "Run it manually: chmod +x setup_smart_citizen.sh && ./setup_smart_citizen.sh"
fi

echo ""
echo "Environment ready. Run the pipeline with:"
echo "  ./src/runall.sh"
echo ""
