"""Star Citizen Data.p4k extraction (global.ini + DataForge entity XMLs).

This file is a DERIVATIVE WORK adapted from Smart Citizen's
``src/utils/pak_extractor.py`` (Copyright Osiris DevWorks, Apache License 2.0)
and has been MODIFIED for lazy-citizen-enhancements: the i18n progress strings, the
DataForge diff-manifest snapshot, and the AppSettings/QSettings coupling were
removed so this runs as a plain CLI module with no GUI dependencies. See NOTICE.

It bundles and drives unp4k/unforge (MIT, github.com/dolkensp/unp4k) to:
  * extract the English ``global.ini`` from Data.p4k, and
  * extract + unforge the DataForge ``Game2.dcb`` into entity XMLs, keeping only
    the subtrees the enhancement generator reads.

Runtime requirement (unp4k/unforge are .NET programs):
  * Windows: .NET Framework 4.x (bundled unp4k.exe/unforge.exe are self-hosting)
  * Linux/macOS: the `dotnet` runtime — the platform DLL builds from
    github.com/dolkensp/unp4k are fetched by setup_tools.py and run as
    `dotnet unp4k.dll ...` / `dotnet unforge.cli.dll ...`.
"""
from __future__ import annotations

import gc
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import setup_tools

logger = logging.getLogger(__name__)

# Repo root (this file lives at the repo root).
_REPO_ROOT = Path(__file__).resolve().parent

# Smart Citizen checkout (cloned by setup_smart_citizen.sh).
SMART_CITIZEN_DIR = _REPO_ROOT / ".smart-citizen"

# Temporary files go here instead of the system temp (C:\...\AppData\Local\Temp).
_LOCAL_TMP = _REPO_ROOT / "out" / "tmp"


def _local_tmp() -> "tempfile.TemporaryDirectory[str]":
    """Return a TemporaryDirectory rooted at out/tmp (auto-cleaned on exit)."""
    _LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=_LOCAL_TMP)

# ``shutil.rmtree`` replaced ``onerror`` with ``onexc`` in Python 3.12. The
# pinned env is 3.11, but keep the version probe so the module also works if
# someone runs it under 3.12+ with their own interpreter.
_RMTREE_CB_KWARG = "onexc" if sys.version_info >= (3, 12) else "onerror"

# Path of global.ini inside the p4k archive (unp4k preserves directory structure).
_GLOBAL_INI_RELATIVE = Path("data/Localization/english/global.ini")

# Subtrees of unforge's ``libs/foundry/records/`` the enhancement generator
# actually reads. Everything else unforge produces is left in the temp dir and
# dropped when the with-block exits. Mirrors Smart Citizen's
# DATAFORGE_KEEP_SUBPATHS — keep in sync with the generator's read paths.
DATAFORGE_KEEP_SUBPATHS: tuple[str, ...] = (
    "entities/scitem",
    "entities/spaceships",
    "entities/missions",
    "entities/contracts",
    "entities/jobterminal",
    "contracts/contractgenerator",
    "contracts/contracttemplates",
    "crafting/blueprintrewards",
    "crafting/blueprints/crafting",
    "missionbroker/pu_missions",
    "ammoparams/vehicle",
    "ammoparams/fps",
    "reputation/rewards/missionrewards_reputation",
    "reputation/standings",
)


# ── unp4k / unforge resolution ──────────────────────────────────────────────
# Windows: the .exe bundled with the Smart Citizen checkout.
# Linux/macOS: the DLL builds downloaded by setup_tools.py (run via `dotnet`).
# Both live in .smart-citizen/assets/unp4k/ — setup_tools.get_binary_paths()
# picks the right pair for the current platform.

def resolve_unp4k_exe() -> Path:
    return setup_tools.get_binary_paths()[0]


def resolve_unforge_exe() -> Path:
    return setup_tools.get_binary_paths()[1]


# Platform-appropriate hint for the "unforge produced nothing" diagnostic.
_DOTNET_HINT = (
    "This typically means .NET Framework 4.x isn't installed or is blocked "
    "by antivirus. Install the latest .NET Framework runtime from Microsoft."
    if sys.platform == "win32" else
    "This typically means the `dotnet` runtime isn't installed (current unp4k "
    "builds need .NET 10). Install it with your package manager (e.g. "
    "`sudo apt install -y dotnet-runtime-10.0`) or via "
    "https://dot.net/v1/dotnet-install.sh (--channel 10.0)."
)


def _get_subprocess_kwargs() -> dict:
    """Subprocess kwargs that suppress the console window on Windows."""
    kwargs: dict = {"capture_output": True, "text": True}
    if sys.platform == "win32":
        # CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


# ── robust rmtree (Windows / OneDrive lock survival) ────────────────────────

def _robust_rmtree(path: Path, attempts: int = 6) -> None:
    """Delete *path* recursively, surviving transient Windows file locks.

    Handles read-only bits left by unforge, ghost handles from the just-exited
    child process / Defender / Search Indexer / OneDrive, and mid-delete
    directory re-walks. Silently succeeds if *path* doesn't exist; raises the
    last error if every attempt fails.
    """
    if not path.exists():
        return

    def _on_error(func, target, *_):
        # Compatible with both 3.11 onerror(func, path, excinfo) and 3.12+
        # onexc(func, path, exc) callback signatures.
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        func(target)

    last_err: Exception | None = None
    for i in range(attempts):
        try:
            gc.collect()  # drop any lingering file handles we own
            shutil.rmtree(path, **{_RMTREE_CB_KWARG: _on_error})
            return
        except OSError as e:
            last_err = e
            delay = min(0.2 * (2 ** i), 3.0)  # 0.2,0.4,0.8,1.5,3.0s, ceiling ~6s
            logger.warning(
                f"rmtree {path} attempt {i + 1}/{attempts} failed ({e}); "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)

    raise last_err if last_err else OSError(f"Failed to remove {path}")


def _copy_filtered_records(src_libs: Path, dst_libs: Path) -> tuple[int, int]:
    """Copy only the generator's required subtrees from *src_libs* → *dst_libs*.

    Both paths point at the ``libs/`` directory unforge writes (containing
    ``foundry/records/<subtree>/...``). Returns ``(copied, skipped)`` — subpaths
    present and copied vs. not shipped in this game build (normal: subtrees come
    and go between patches; the generator guards each read).
    """
    records_src = src_libs / "foundry" / "records"
    records_dst = dst_libs / "foundry" / "records"

    if not records_src.exists():
        raise FileNotFoundError(
            f"unforge output missing expected 'foundry/records/' layout at {records_src}"
        )

    records_dst.mkdir(parents=True, exist_ok=True)

    copied = skipped = 0
    for rel in DATAFORGE_KEEP_SUBPATHS:
        src = records_src / rel
        dst = records_dst / rel
        if not src.exists():
            logger.debug(f"DataForge keep-path not in this build, skipping: {rel}")
            skipped += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        copied += 1

    return copied, skipped


# ── public extraction API ───────────────────────────────────────────────────

def extract_global_ini(p4k_path: Path, output_path: Path, unp4k_exe: Path) -> bool:
    """Extract the English global.ini from Data.p4k → *output_path*.

    Runs ``unp4k <p4k> global.ini`` into a temp dir, then copies the extracted
    ``data/Localization/english/global.ini`` to *output_path*.
    """
    if not unp4k_exe.exists():
        raise FileNotFoundError(f"unp4k binary not found at: {unp4k_exe}")
    if not p4k_path.exists():
        raise FileNotFoundError(f"Data.p4k not found at: {p4k_path}")

    with _local_tmp() as tmp_dir:
        logger.info(f"unp4k: extracting global.ini (cwd={tmp_dir})")
        result = subprocess.run(
            setup_tools.make_invocation(unp4k_exe, str(p4k_path), "global.ini"),
            cwd=tmp_dir, timeout=300, **_get_subprocess_kwargs()
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"unp4k exited with code {result.returncode}.\n\n"
                f"{result.stderr or result.stdout}"
            )

        extracted = Path(tmp_dir) / _GLOBAL_INI_RELATIVE
        if not extracted.exists():
            raise FileNotFoundError(
                f"unp4k ran but global.ini was not found at:\n{extracted}\n\n"
                f"stdout: {(result.stdout or '')[:500]}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(extracted), str(output_path))
        logger.info(f"Extracted global.ini → {output_path}")
    return True


def extract_dataforge(
    p4k_path: Path,
    unp4k_exe: Path,
    unforge_exe: Path,
    dataforge_cache_dir: Path,
) -> bool:
    """Extract DataForge entity XMLs from Data.p4k into *dataforge_cache_dir*.

    Pipeline: unp4k extracts Game2.dcb → unforge converts it to entity XMLs →
    the filtered keep-subtrees are copied to ``dataforge_cache_dir/raw/libs/``.
    The resulting layout matches what the generator expects:
    ``dataforge_cache_dir/raw/libs/foundry/records/<subtree>/...``.
    """
    for exe, name in [(unp4k_exe, "unp4k"), (unforge_exe, "unforge")]:
        if not exe.exists():
            raise FileNotFoundError(f"{name} binary not found at: {exe}")
    if not p4k_path.exists():
        raise FileNotFoundError(f"Data.p4k not found at: {p4k_path}")

    with _local_tmp() as tmp_dir:
        tmp = Path(tmp_dir)

        # Step 1: extract Game2.dcb
        logger.info("unp4k: extracting .dcb")
        result = subprocess.run(
            setup_tools.make_invocation(unp4k_exe, str(p4k_path), ".dcb"),
            cwd=tmp_dir, timeout=600, **_get_subprocess_kwargs()
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"unp4k failed (code {result.returncode}):\n"
                f"{result.stderr or result.stdout}"
            )
        del result
        gc.collect()
        time.sleep(0.1)

        dcb_candidates = list(tmp.glob("Data/Game*.dcb"))
        if not dcb_candidates:
            raise FileNotFoundError(
                "Game*.dcb not found in p4k output — check game install path."
            )
        dcb_path = dcb_candidates[0]
        logger.info(f"Found DCB: {dcb_path} ({dcb_path.stat().st_size / 1_048_576:.0f} MB)")

        # Step 2: unforge → entity XMLs
        logger.info(f"unforge: {dcb_path}")
        result = subprocess.run(
            setup_tools.make_invocation(unforge_exe, str(dcb_path)),
            timeout=1800, **_get_subprocess_kwargs()
        )
        _stdout = (result.stdout or "").strip()
        _stderr = (result.stderr or "").strip()
        if _stdout:
            logger.info(f"unforge stdout ({len(_stdout)} bytes, truncated): {_stdout[:2000]}")
        if _stderr:
            logger.info(f"unforge stderr ({len(_stderr)} bytes, truncated): {_stderr[:2000]}")
        if result.returncode != 0:
            raise RuntimeError(
                f"unforge failed (code {result.returncode}):\n"
                f"{_stderr or _stdout or '(no output)'}"
            )
        del result
        gc.collect()
        time.sleep(0.1)

        libs_dir = dcb_path.parent
        if not (libs_dir / "libs").exists():
            if _stdout or _stderr:
                diagnostic = (
                    f"\n\nunforge stdout:\n{_stdout[:1500] or '(empty)'}"
                    f"\n\nunforge stderr:\n{_stderr[:1500] or '(empty)'}"
                )
            else:
                # No output and no libs/ — classic missing-.NET signature.
                diagnostic = (
                    "\n\nNo output from unforge and no libs/ directory produced. "
                    + _DOTNET_HINT
                )
            raise FileNotFoundError(
                "unforge ran but libs/ directory was not created — unexpected output structure."
                + diagnostic
            )

        # Step 3: cache the filtered extraction
        gc.collect()
        time.sleep(0.1)
        if dataforge_cache_dir.exists():
            _robust_rmtree(dataforge_cache_dir)
        dataforge_cache_dir.mkdir(parents=True, exist_ok=True)

        raw_dir = dataforge_cache_dir / "raw"
        logger.info(f"Caching DataForge extraction → {raw_dir}")
        copied, skipped = _copy_filtered_records(libs_dir / "libs", raw_dir / "libs")
        logger.info(
            f"DataForge cache written: {copied}/{len(DATAFORGE_KEEP_SUBPATHS)} "
            f"keep-subpaths copied ({skipped} not present in this build)"
        )

        # Stamp the p4k mtime so the generator's _cached_lookup fingerprint
        # (which reads .p4k_mtime when present) is stable across re-runs.
        stamp = dataforge_cache_dir / ".p4k_mtime"
        stamp.write_text(str(p4k_path.stat().st_mtime))
        logger.info(f"DataForge cache written to {dataforge_cache_dir}")

    gc.collect()
    return True


def extract_dataforge_from_dcb(
    dcb_path: Path,
    unforge_exe: Path,
    dataforge_cache_dir: Path,
) -> bool:
    """Run unforge on an already-downloaded Game2.dcb and cache the result.

    Used by the --download mode where unp4k is not needed because the DCB
    was fetched directly from the RSI CDN. The output layout is identical
    to extract_dataforge() so the rest of the pipeline is unaffected.
    """
    if not unforge_exe.exists():
        raise FileNotFoundError(f"unforge binary not found at: {unforge_exe}")
    if not dcb_path.exists():
        raise FileNotFoundError(f"Game2.dcb not found at: {dcb_path}")

    with _local_tmp() as tmp_dir:
        tmp = Path(tmp_dir)
        # Copy the DCB into the temp dir so unforge writes libs/ alongside it.
        import shutil as _shutil
        tmp_dcb = tmp / dcb_path.name
        _shutil.copy2(dcb_path, tmp_dcb)

        logger.info(f"unforge: {tmp_dcb}")
        result = subprocess.run(
            setup_tools.make_invocation(unforge_exe, str(tmp_dcb)),
            timeout=1800, **_get_subprocess_kwargs()
        )
        _stdout = (result.stdout or "").strip()
        _stderr = (result.stderr or "").strip()
        if _stdout:
            logger.info(f"unforge stdout (truncated): {_stdout[:2000]}")
        if _stderr:
            logger.info(f"unforge stderr (truncated): {_stderr[:2000]}")
        if result.returncode != 0:
            raise RuntimeError(
                f"unforge failed (code {result.returncode}):\n"
                f"{_stderr or _stdout or '(no output)'}"
            )
        del result
        gc.collect()
        time.sleep(0.1)

        libs_dir = tmp_dcb.parent
        if not (libs_dir / "libs").exists():
            diagnostic = (
                "\n\nNo output from unforge and no libs/ directory produced. "
                + _DOTNET_HINT
            ) if not (_stdout or _stderr) else (
                f"\n\nunforge stdout:\n{_stdout[:1500] or '(empty)'}"
                f"\n\nunforge stderr:\n{_stderr[:1500] or '(empty)'}"
            )
            raise FileNotFoundError(
                "unforge ran but libs/ was not created." + diagnostic
            )

        gc.collect()
        time.sleep(0.1)
        if dataforge_cache_dir.exists():
            _robust_rmtree(dataforge_cache_dir)
        dataforge_cache_dir.mkdir(parents=True, exist_ok=True)

        raw_dir = dataforge_cache_dir / "raw"
        logger.info(f"Caching DataForge extraction -> {raw_dir}")
        copied, skipped = _copy_filtered_records(libs_dir / "libs", raw_dir / "libs")
        logger.info(
            f"DataForge cache written: {copied}/{len(DATAFORGE_KEEP_SUBPATHS)} "
            f"keep-subpaths copied ({skipped} not present in this build)"
        )

        stamp = dataforge_cache_dir / ".p4k_mtime"
        stamp.write_text(str(dcb_path.stat().st_mtime))
        logger.info(f"DataForge cache written to {dataforge_cache_dir}")

    gc.collect()
    return True
