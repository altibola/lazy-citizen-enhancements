"""Download and verify unp4k/unforge binaries for the current platform.

Called by bootstrap.sh and setup_smart_citizen.sh. Safe to re-run — skips
download if binaries are already present and executable.

Usage:
    python setup_tools.py [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import platform
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger("setup_tools")

_REPO_ROOT = Path(__file__).resolve().parent
_SMART_CITIZEN_DIR = _REPO_ROOT / ".smart-citizen"
_TOOLS_DIR = _SMART_CITIZEN_DIR / "assets" / "unp4k"

_GITHUB_API = "https://api.github.com/repos/dolkensp/unp4k/releases/latest"

# Maps (os, arch) → (unp4k zip name prefix, unforge zip name prefix, binary names inside)
# Linux builds are framework-dependent DLLs that need `dotnet` to run.
# Windows builds are self-contained single-file executables.
_PLATFORM_MAP: dict[str, dict] = {
    # (sys.platform, machine)
    "linux-x86_64":  {"os": "linux", "arch": "x64"},
    "linux-aarch64": {"os": "linux", "arch": "arm64"},
    "linux-armv7l":  {"os": "linux", "arch": "arm64"},  # best approximation
    "win32-AMD64":   {"os": "win",   "arch": "x64"},
    "win32-x86":     {"os": "win",   "arch": "x86"},
    "darwin-x86_64": {"os": "linux", "arch": "x64"},    # macOS: use linux DLLs via dotnet
    "darwin-arm64":  {"os": "linux", "arch": "arm64"},
}


def _platform_key() -> str:
    return f"{sys.platform}-{platform.machine()}"


def _get_platform_info() -> dict:
    key = _platform_key()
    info = _PLATFORM_MAP.get(key)
    if info is None:
        # Fallback
        if sys.platform == "win32":
            info = {"os": "win", "arch": "x64"}
        else:
            info = {"os": "linux", "arch": "x64"}
        logger.warning("Unknown platform %r — defaulting to %s/%s", key, info["os"], info["arch"])
    return info


def is_windows() -> bool:
    return sys.platform == "win32"


def get_binary_paths() -> tuple[Path, Path]:
    """Return (unp4k_path, unforge_path) for the current platform.

    On Windows: .exe files.
    On Linux/macOS: .dll files (run via `dotnet`).
    """
    if is_windows():
        return _TOOLS_DIR / "unp4k.exe", _TOOLS_DIR / "unforge.exe"
    else:
        return _TOOLS_DIR / "unp4k.dll", _TOOLS_DIR / "unforge.cli.dll"


def make_invocation(binary: Path, *args: str) -> list[str]:
    """Build the subprocess argv to invoke *binary* with *args*.

    On Windows: [str(binary), *args]
    On Linux: ["dotnet", str(binary), *args]
    """
    if is_windows():
        return [str(binary), *args]
    else:
        return ["dotnet", str(binary), *args]


def _fetch_release_info() -> dict:
    logger.info("Fetching latest unp4k release info from GitHub...")
    with urllib.request.urlopen(_GITHUB_API, timeout=30) as resp:
        return json.loads(resp.read())


def _download_zip(url: str, label: str) -> bytes:
    logger.info("Downloading %s...", label)
    with urllib.request.urlopen(url, timeout=300) as resp:
        data = resp.read()
    logger.info("  %d bytes downloaded", len(data))
    return data


def _extract_zip_to(data: bytes, dest_dir: Path) -> list[str]:
    """Extract zip bytes into dest_dir, returning list of extracted names."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    names = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            # Flatten structure: only take the basename
            base = Path(member.filename).name
            if not base or base.endswith("/"):
                continue
            dest = dest_dir / base
            dest.write_bytes(zf.read(member))
            # Restore executable permission on Unix
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if unix_mode & 0o111:
                dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            names.append(base)
            logger.debug("  extracted: %s", base)
    return names


def _check_dotnet() -> bool:
    """Return True if `dotnet` is available on PATH."""
    import shutil
    return shutil.which("dotnet") is not None


def ensure_tools(force: bool = False) -> tuple[Path, Path]:
    """Download unp4k + unforge binaries if not already present.

    Returns (unp4k_path, unforge_path).
    """
    unp4k_path, unforge_path = get_binary_paths()

    if not force and unp4k_path.exists() and unforge_path.exists():
        logger.info("Tools already present: %s, %s", unp4k_path.name, unforge_path.name)
        if not is_windows() and not _check_dotnet():
            logger.warning(
                "unp4k/unforge require .NET — install it with:\n"
                "  # Ubuntu/Debian:\n"
                "  wget https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 10.0\n"
                "  # Or: sudo apt install -y dotnet-runtime-10.0"
            )
        return unp4k_path, unforge_path

    info = _get_platform_info()
    os_tag   = info["os"]
    arch_tag = info["arch"]

    release = _fetch_release_info()
    version = release["tag_name"]
    assets  = {a["name"]: a["browser_download_url"] for a in release["assets"]}

    _TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    for tool, dll_name in [("unp4k", "unp4k.dll"), ("unforge", "unforge.cli.dll")]:
        zip_name = f"{tool}-{os_tag}-{arch_tag}-{version}.zip"
        if zip_name not in assets:
            # Try suite as fallback
            zip_name = f"{tool}-suite-{os_tag}-{arch_tag}-{version}.zip"
        if zip_name not in assets:
            raise RuntimeError(
                f"No release asset found for {tool} ({os_tag}/{arch_tag}).\n"
                f"Available: {sorted(assets)}"
            )
        url = assets[zip_name]
        data = _download_zip(url, zip_name)
        extracted = _extract_zip_to(data, _TOOLS_DIR)
        logger.info("Extracted %s: %s", zip_name, ", ".join(extracted))

    if not is_windows() and not _check_dotnet():
        logger.warning(
            "\n"
            "⚠  .NET runtime not found — required to run unp4k/unforge on Linux.\n"
            "   Install with:\n"
            "     wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh\n"
            "     bash dotnet-install.sh --channel 10.0\n"
            "     echo 'export PATH=$PATH:$HOME/.dotnet' >> ~/.bashrc\n"
            "     source ~/.bashrc\n"
            "   Or via package manager (Ubuntu 24.04+):\n"
            "     sudo apt install -y dotnet-runtime-10.0\n"
        )

    logger.info("✓ Tools ready: %s, %s", unp4k_path.name, unforge_path.name)
    return unp4k_path, unforge_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Download unp4k/unforge for this platform.")
    parser.add_argument("--force", action="store_true", help="Re-download even if already present.")
    args = parser.parse_args()
    try:
        unp4k, unforge = ensure_tools(force=args.force)
        print(f"\n✓ unp4k:   {unp4k}")
        print(f"✓ unforge: {unforge}")
        if not is_windows():
            print("\nTo run: dotnet <binary> <args>")
    except Exception as exc:
        print(f"\n✗ Error: {exc}", file=sys.stderr)
        sys.exit(1)
