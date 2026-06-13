"""lazy-citizen-enhancements — full extraction + enhancement pipeline.

From a Star Citizen ``Data.p4k`` this:
  1. Detects the game version from ``build_manifest.id`` (placed next to Data.p4k
     by the RSI Launcher) and creates a version-stamped output folder.
  2. Extracts the English ``global.ini`` (stat-tag source) — once per version.
  3. Extracts + unforges the DataForge entity XMLs — once per version.
  4. Per language: downloads the community base ``global.ini``, runs the
     enhancement generator, and merges base + enhancements into a final
     ``global.ini`` ready to drop into the game.

Outputs (all under ``out/<game-version>/``):
  base_en.ini                              — English base (shared)
  dataforge/raw/libs/...                   — DataForge cache (shared)
  <lang>/enhancements/base.ini             — downloaded target-language base
  <lang>/enhancements/*_enhancements.ini   — 8 generated enhancement INIs
  <lang>/global/global.ini                 — final merged result

Run via the isolated env: ``./run.sh --p4k "...\LIVE\Data.p4k"`` or ``./run-local.sh``.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import lang_sources
import pak_extract

REPO_ROOT = Path(__file__).resolve().parent.parent

# Smart Citizen checkout (cloned by setup_smart_citizen.sh).
SMART_CITIZEN_DIR = REPO_ROOT / ".smart-citizen"

# The 8 enhancement INIs the generator writes (used for the final merge).
ENHANCEMENT_FILES: tuple[str, ...] = (
    "ships_desc_enhancements.ini",
    "components_desc_enhancements.ini",
    "ship_weapons_desc_enhancements.ini",
    "fps_weapons_desc_enhancements.ini",
    "mission_rewards_enhancements.ini",
    "commodity_crafting_enhancements.ini",
    "journal_enhancements.ini",
    "missile_enhancements.ini",
)

logger = logging.getLogger("lazy_citizen_enhancements")


# ── helpers ──────────────────────────────────────────────────────────────────

def _autodetect_p4k() -> Path | None:
    """Find Data.p4k by reading the RSI Launcher log file.

    The launcher writes one JSON object per line to:
      %APPDATA%\\rsilauncher\\logs\\log.log

    Two log patterns contain the exact channel install path:
      [Launcher::launch] Launching Star Citizen LIVE from (E:\\...\\LIVE)
      [Installer] Delta update applied ... in E:\\...\\LIVE

    Strategy:
      1. Collect all candidate paths from both patterns.
      2. Return the most recent path for LIVE (preferred channel).
      3. Fall back to most recent path for any other channel.
    """
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None

    log_path = Path(appdata) / "rsilauncher" / "logs" / "log.log"
    if not log_path.exists():
        logger.debug(f"RSI Launcher log not found: {log_path}")
        return None

    # Regex patterns that extract the channel install path from log messages.
    # Applied directly on raw lines — avoids JSON parsing issues with
    # multi-line error entries that span several lines in the log file.
    # In the raw file, backslashes are JSON-escaped (\\), so we unescape them.
    _LAUNCH_RE = re.compile(
        r"\[Launcher::launch\] Launching Star Citizen \S+ from \((.+?)\)"
    )
    _INSTALL_RE = re.compile(
        r"\[Installer\] Delta update applied .+ in ([^\s\"]+)"
    )

    live_candidates: list[Path] = []
    other_candidates: list[Path] = []

    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                for pattern in (_LAUNCH_RE, _INSTALL_RE):
                    m = pattern.search(raw)
                    if m:
                        # Unescape JSON double-backslashes → single backslashes.
                        path_str = m.group(1).strip().replace("\\\\", "\\")
                        p = Path(path_str)
                        if p.name.upper() == "LIVE":
                            live_candidates.append(p)
                        else:
                            other_candidates.append(p)
                        break

    except OSError as exc:
        logger.debug(f"Could not read launcher log: {exc}")
        return None

    # Prefer LIVE; within each group use the most recent entry (last in log).
    for path in (*reversed(live_candidates), *reversed(other_candidates)):
        p4k = path / "Data.p4k"
        if p4k.exists():
            logger.info(f"Auto-detected Data.p4k from launcher log: {p4k}")
            return p4k

    return None


def _prompt_p4k() -> Path:
    """Ask the user for the Data.p4k path interactively."""
    print(
        "\nData.p4k not found automatically.\n"
        "Enter the full path to your Star Citizen Data.p4k, e.g.:\n"
        "  D:\\Roberts Space Industries\\StarCitizen\\LIVE\\Data.p4k\n"
    )
    while True:
        try:
            raw = input("Path to Data.p4k: ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1)
        if not raw:
            print("  (path cannot be empty, try again)")
            continue
        p = Path(raw)
        if p.exists():
            return p
        print(f"  File not found: {p}\n  Try again.")


def _detect_game_version(p4k_path: Path) -> str:
    """Return the P4 changelist number that identifies this game build.

    Reads ``RequestedP4ChangeNum`` from ``build_manifest.id`` placed next to
    Data.p4k by the RSI Launcher, e.g. ``11952564``.

    This number is the same component the launcher appends after the dot in its
    display version (``4.8.1-live.11952564``). It is monotonically increasing,
    unique per build, and the only piece of version information reliably present
    in a local installation without hitting the RSI CDN.

    Fallback chain if the manifest is absent or unreadable:
      1. Uppercased channel folder name (LIVE / PTU / EPTU).
      2. "unknown" — pass ``--game-version`` to override.
    """
    manifest = p4k_path.parent / "build_manifest.id"
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            inner = payload.get("Data", payload)  # handle both nested and flat layouts
            p4cl  = str(inner.get("RequestedP4ChangeNum", "")).strip()
            if p4cl:
                logger.info(f"Game build detected: P4CL {p4cl}")
                return p4cl
        except Exception as e:
            logger.warning(f"Could not parse build_manifest.id: {e}")

    channel = p4k_path.parent.name.upper()
    if channel:
        logger.warning(
            f"build_manifest.id unreadable — using channel name '{channel}' as version."
        )
        return channel

    logger.warning(
        "Could not determine game version — using 'unknown'. "
        "Pass --game-version to override."
    )
    return "unknown"


def _file_sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_github_commit(owner: str, repo: str, branch: str) -> str | None:
    """Return the tip commit SHA for {owner}/{repo}@{branch} via the GitHub API.

    Uses the public API (no auth needed for public repos, 60 req/h limit).
    Returns None on any error so callers can fall back gracefully.
    """
    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "lazy-citizen-enhancements/1.0",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("sha")
    except Exception as exc:
        logger.warning(f"GitHub API: could not resolve {owner}/{repo}@{branch}: {exc}")
        return None


def _download_with_source(
    url: str,
    dest: Path,
    gh_info: dict | None = None,
) -> dict:
    """Download *url* to *dest* and return a source-provenance dict.

    If *gh_info* is provided (keys: owner, repo, branch, path), resolves the
    current commit SHA via the GitHub API and constructs a permanent link that
    pins the exact tree state at download time.  The provenance dict is also
    written as ``<dest>.source.json`` alongside the downloaded file.

    Returned dict always contains at least ``url`` and ``sha256``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "lazy-citizen-enhancements/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    dest.write_bytes(raw)
    logger.info(f"Saved {len(raw):,} bytes -> {dest}")

    source: dict = {"url": url, "sha256": _file_sha256(dest)}

    if gh_info:
        commit = _resolve_github_commit(
            gh_info["owner"], gh_info["repo"], gh_info["branch"]
        )
        source.update({
            "owner":  gh_info["owner"],
            "repo":   gh_info["repo"],
            "branch": gh_info["branch"],
        })
        if commit:
            source["commit"] = commit
            source["permalink"] = (
                f"https://github.com/{gh_info['owner']}/{gh_info['repo']}"
                f"/blob/{commit}/{gh_info['path']}"
            )
        else:
            # Fallback: branch-based link (not pinned, but still navigable)
            source["permalink"] = (
                f"https://github.com/{gh_info['owner']}/{gh_info['repo']}"
                f"/blob/{gh_info['branch']}/{gh_info['path']}"
            )

        source_file = dest.with_name(dest.name + ".source.json")
        source_file.write_text(json.dumps(source, indent=2), encoding="utf-8")
        logger.info(f"Source provenance -> {source_file}")

    return source


def _stub_pyqt6() -> None:
    """Inject a no-op PyQt6 stub so smart-citizen's GUI imports don't crash headlessly.

    smart-citizen's generate_enhancements_ini tries to import AppSettings
    (which pulls in PyQt6/QSettings) to resolve the default DataForge cache
    dir.  We always pass forge_dir explicitly, so the default is irrelevant —
    but the import crash blocks the whole module.  A minimal stub makes the
    import succeed without requiring a real Qt installation.
    """
    import types
    if "PyQt6" in sys.modules:
        return

    class _QSettings:
        def __init__(self, *a, **kw): pass
        def value(self, key, default=None, **kw): return default
        def setValue(self, *a, **kw): pass
        def contains(self, *a, **kw): return False
        def remove(self, *a, **kw): pass
        def beginGroup(self, *a, **kw): pass
        def endGroup(self, *a, **kw): pass
        def allKeys(self, *a, **kw): return []

    _qt_core = types.ModuleType("PyQt6.QtCore")
    _qt_core.QSettings = _QSettings  # type: ignore[attr-defined]

    _pyqt6 = types.ModuleType("PyQt6")
    sys.modules["PyQt6"] = _pyqt6
    sys.modules["PyQt6.QtCore"] = _qt_core


def _import_generator():
    _stub_pyqt6()
    for p in (str(SMART_CITIZEN_DIR), str(SMART_CITIZEN_DIR / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import generate_enhancements_ini as gen  # noqa: E402
    return gen


# ── per-language processing ──────────────────────────────────────────────────

def _parse_ini_builtin(path: Path) -> dict[str, str]:
    """Minimal INI parser: ``key=value`` lines, UTF-8, no sections.

    Used as a fallback when the Smart Citizen generator is not available
    (e.g. in CI / GitHub Actions with ``--skip-generate``).
    """
    result: dict[str, str] = {}
    # utf-8-sig: community base.ini files carry a BOM; plain utf-8 would leave
    # ﻿ glued to the first key.
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "=" in line and not line.startswith(";") and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k] = v
    return result


def _write_ini_builtin(path: Path, data: dict[str, str]) -> None:
    """Minimal INI writer: one ``key=value`` line per entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keys must be sorted for consistent diffs, and utf-8-sig is REQUIRED by the game engine.
    # We must explicitly force CRLF line endings regardless of the OS, because Star Citizen requires it.
    text = "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
    with path.open("w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)
    logger.info(f"Written {len(data):,} entries -> {path}")


def process_language(
    language: str,
    versioned_out: Path,
    english_base: Path,
    forge_dir: Path,
    workers: int,
    game_version: str,
    skip_generate: bool = False,
) -> str | None:
    """Download the target base, generate enhancements, merge, and write provenance.

    When *skip_generate* is True (or DataForge is absent), the enhancement
    generation step is skipped and any existing ``*_enhancements.ini`` files
    committed to the branch are reused as-is.  Only the base translation
    download and final merge are performed.  This is the mode used by the
    automated GitHub Actions translation-update workflow.
    """
    enh_dir = versioned_out / language / "enhancements"
    global_dir = versioned_out / language / "global"
    base_ini = enh_dir / "base.ini"

    logger.info(f"=== Language: {language} ===")

    # 1. Resolve the target-language base with source tracking.
    if language == "english":
        # English is the game's own base — no community translation download.
        # The enhanced English file is base_en.ini + generated enhancements.
        base_ini.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(english_base, base_ini)
        base_source = {
            "origin": "game",
            "description": "English global.ini extracted from the game build (base_en.ini)",
            "sha256": _file_sha256(base_ini),
        }
        (base_ini.parent / "base.ini.source.json").write_text(
            json.dumps(base_source, indent=2), encoding="utf-8")
    else:
        gh_info = lang_sources.github_info(language)
        base_source = _download_with_source(
            lang_sources.language_url(language), base_ini, gh_info)

    # 2. Enhancement generation — skip if requested or DataForge unavailable.
    has_dataforge = forge_dir and (forge_dir / "raw" / "libs").exists()
    if skip_generate:
        logger.info("--skip-generate: reusing existing enhancement INIs.")
    elif has_dataforge:
        gen = _import_generator()
        logger.info("Running enhancement generator...")
        gen.main(
            base_ini_path=base_ini,
            forge_dir=forge_dir,
            categories=None,
            max_workers=workers,
            english_base_ini_path=english_base,
        )
    else:
        logger.warning(
            f"DataForge cache missing or incomplete at {forge_dir}. "
            "Skipping enhancement generation; will reuse existing enhancement INIs."
        )

    # 3. Merge: base overlaid with enhancements.
    # Use smart-citizen's parser when available; fall back to built-in for CI.
    try:
        gen = _import_generator()
        from src.merger.ini_merger import merge_sources_by_hierarchy
        def _parse(p: Path) -> dict[str, str]: return gen.parse_ini(p)
        def _write(p: Path, d: dict[str, str]) -> None: _write_ini_builtin(p, d)
        def _merge(base: dict, enh: dict) -> dict:
            return merge_sources_by_hierarchy(
                {"global": base, "enhancements": enh}, ["global", "enhancements"]
            )
    except Exception:
        logger.debug("Smart Citizen generator unavailable; using built-in INI helpers.")
        def _parse(p: Path) -> dict[str, str]: return _parse_ini_builtin(p)
        def _write(p: Path, d: dict[str, str]) -> None: _write_ini_builtin(p, d)
        def _merge(base: dict, enh: dict) -> dict: return {**base, **enh}

    english_lang = _parse(english_base)
    if language == "english":
        base_lang = english_lang
    else:
        comm_lang = _parse(base_ini)
        # Overlay community translation on top of english base
        base_lang = {**english_lang, **comm_lang}
    enh: dict[str, str] = {}
    produced = 0
    for name in ENHANCEMENT_FILES:
        path = enh_dir / name
        if path.exists():
            enh.update(_parse(path))
            produced += 1
    logger.info(
        f"Merging: base={len(base_lang):,} keys, "
        f"enhancements={len(enh):,} keys from {produced}/{len(ENHANCEMENT_FILES)} files"
    )

    merged = _merge(base_lang, enh)

    out_global = global_dir / "global.ini"
    _write(out_global, merged)
    logger.info(f"[{language}] final global.ini: {len(merged):,} keys -> {out_global}")

    # Also write to Star Citizen directory structure (data/Localization/{sc_id}/)
    sc_id = lang_sources.sc_language_id(language)
    sc_dir = versioned_out / "data" / "Localization" / sc_id
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc_global = sc_dir / "global.ini"
    _write(sc_global, merged)
    logger.info(f"[{language}] also written to SC structure: {sc_global}")

    # 4. Provenance — SHA-256 of every input and generated output file.
    outputs: dict[str, dict] = {}
    for name in ENHANCEMENT_FILES:
        p = enh_dir / name
        if p.exists():
            outputs[name] = {"sha256": _file_sha256(p)}
    outputs["global.ini"] = {"sha256": _file_sha256(out_global)}

    provenance = {
        "build": game_version,
        "language": language,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "inputs": {
            "base_en.ini": {
                "sha256": _file_sha256(english_base),
                "path": str(english_base),
            },
            "base.ini": base_source,
        },
        "outputs": outputs,
    }
    prov_file = enh_dir / "provenance.json"
    prov_file.write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    logger.info(f"[{language}] provenance -> {prov_file}")

    print(
        f"\n[{language}] done:\n"
        f"  enhancements: {enh_dir}\n"
        f"  global.ini:   {out_global}  ({len(merged):,} keys)\n"
        f"  SC structure: {sc_global}\n"
        f"  provenance:   {prov_file}"
    )

    return base_source.get("commit")


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_pipeline",
        description=(
            "Extract Data.p4k and generate versioned, localized enhancement INIs. "
            "Output is placed under out/<game-version>/ so each game patch produces "
            "a distinct, traceable set of files."
        ),
    )
    parser.add_argument("--p4k", type=Path, default=None,
                        help="Path to Data.p4k (auto-detected if omitted).")
    parser.add_argument("--game-version", default=None,
                        help="Override the game version string used as the output "
                             "folder prefix (auto-detected from build_manifest.id "
                             "when omitted). Required with --skip-extract if you "
                             "want a specific version folder.")
    parser.add_argument("--display-version", default=None,
                        help="Override the public display version string (e.g. "
                             "'4.8.1'). If provided, the final version will be "
                             "formatted as '<display>-<env>-<p4cl>'.")
    parser.add_argument("--lang", action="append", default=None,
                        help=f"Language to process (repeatable; default: all). "
                             f"Known: {', '.join(lang_sources.available_languages())}.")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "enhancements",
                        help="Output root directory (default: ./enhancements). "
                             "Files are written directly under this folder.")
    parser.add_argument("--workers", type=int, default=6,
                        help="Parallel workers for the generator (default: 6).")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Reuse existing base_en.ini and dataforge cache. "
                             "Reads version/environment from version.json unless overridden.")
    parser.add_argument("--skip-generate", action="store_true",
                        help="Skip the DataForge-based enhancement generation step. "
                             "Re-downloads community translations and re-merges with the "
                             "existing *_enhancements.ini files already on disk/branch. "
                             "Useful in CI where DataForge is unavailable — only lxml is "
                             "required.")



    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve languages — default is English (enhanced game base, no community
    # translation involved) plus every configured community language.
    if args.lang:
        languages = args.lang
    else:
        languages = ["english"] + lang_sources.available_languages()
    for lang in languages:
        if lang != "english" and lang not in lang_sources.LANGUAGE_SOURCES:
            parser.error(
                f"unknown language {lang!r}; known: english, "
                f"{', '.join(lang_sources.available_languages())}"
            )

    out_dir: Path = args.out

    # ── Local extraction ──────────────────────────────────────────────────────
    if args.skip_extract:
        game_version_json, environment = _read_version_file(out_dir)
        game_version = args.game_version or game_version_json
        if not game_version:
            parser.error(
                "--skip-extract requires enhancements/version.json to exist or --game-version to be passed."
            )
        if not environment:
            environment = "LIVE"
            
        english_base = out_dir / "base_en.ini"
        forge_dir = out_dir / "dataforge"
        if not english_base.exists():
            parser.error(
                f"--skip-extract given but english base is missing at {english_base}."
            )
        logger.info(f"Skipping extraction; reusing files in {out_dir}")
    else:
        p4k = args.p4k or _autodetect_p4k()
        if p4k is None:
            if sys.stdin.isatty():
                p4k = _prompt_p4k()
            else:
                parser.error(
                    "Data.p4k not found automatically. "
                    "Pass --p4k <path> (e.g. ...\\StarCitizen\\LIVE\\Data.p4k)."
                )
        elif not p4k.exists():
            parser.error(f"Data.p4k does not exist: {p4k}")

        game_version = args.game_version or _detect_game_version(p4k)
        environment = _detect_environment(p4k)
        
        # Determine the user-friendly display version (e.g., 4.8.0-live-11952564)
        display_version = f"{environment.lower()}-{game_version}"
        if args.display_version:
            display_version = f"{args.display_version}-{environment.lower()}-{game_version}"
        else:
            manifest = p4k.parent / "build_manifest.id"
            if manifest.exists():
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    inner = payload.get("Data", payload)
                    branch = str(inner.get("Branch", "")).strip()
                    if branch:
                        if branch.startswith("sc-alpha-"):
                            branch = branch[len("sc-alpha-"):]
                        display_version = f"{branch}-{environment.lower()}-{game_version}"
                except Exception:
                    pass

        # Ensure output directory exists and write version.json
        out_dir.mkdir(parents=True, exist_ok=True)
        version_file = out_dir / "version.json"
        version_data = {
            "version": game_version,
            "display_version": display_version,
            "environment": environment
        }
        version_file.write_text(json.dumps(version_data, indent=2), encoding="utf-8")
        logger.info(f"Wrote version file: {version_file} -> {version_data}")

        english_base = out_dir / "base_en.ini"
        forge_dir = out_dir / "dataforge"

        unp4k_exe = pak_extract.resolve_unp4k_exe()
        unforge_exe = pak_extract.resolve_unforge_exe()

        print(f"\nGame version: {game_version}")
        print(f"Environment:  {environment}")
        print(f"Output root:  {out_dir}\n")

        logger.info("=== Shared extraction ===")
        pak_extract.extract_global_ini(p4k, english_base, unp4k_exe)
        pak_extract.extract_dataforge(p4k, unp4k_exe, unforge_exe, forge_dir)

    # Per-language generation + merge.
    skip_gen = getattr(args, "skip_generate", False)
    translation_commits = {}
    for lang in languages:
        commit_sha = process_language(lang, out_dir, english_base, forge_dir, args.workers, game_version,
                                      skip_generate=skip_gen)
        if commit_sha:
            translation_commits[lang] = commit_sha

    # Update version.json with the collected translation SHAs
    version_file = out_dir / "version.json"
    version_data = {}
    if version_file.exists():
        try:
            version_data = json.loads(version_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Merge (not replace) translation SHAs: a partial run (--lang) must not
    # wipe the commits recorded for languages it didn't process.
    merged_translations = dict(version_data.get("translations", {}))
    merged_translations.update(translation_commits)
    version_data.update({
        "version": game_version,
        "environment": environment,
        "translations": merged_translations
    })
    version_file.write_text(json.dumps(version_data, indent=2), encoding="utf-8")
    logger.info(f"Updated version file with translations: {version_file} -> {version_data}")

    # Move data folder to repository root after successful generation
    _move_data_to_repo_root(out_dir)

    # Regenerate VERSIONS.md + README downloads table — human-readable manifest
    # of original translation versions and what was fed to the generator.
    try:
        import versions_report
        versions_report.generate()
        logger.info(f"Wrote version manifest: {versions_report.OUTPUT_MD}")
    except Exception as exc:
        logger.warning(f"Could not generate VERSIONS.md: {exc}")

    print(f"\nAll done. Output: {out_dir}")
    return 0


def _read_version_file(out_root: Path) -> tuple[str | None, str | None]:
    """Read version and environment from version.json in out_root."""
    version_file = out_root / "version.json"
    if version_file.exists():
        try:
            data = json.loads(version_file.read_text(encoding="utf-8"))
            return data.get("version"), data.get("environment")
        except Exception as e:
            logger.warning(f"Could not parse version.json: {e}")
    return None, None


def _detect_environment(p4k_path: Path) -> str:
    """Determine the game environment from the p4k path.

    Known channels: LIVE, PTU, EPTU, HOTFIX, TECHPREVIEW.
    Order matters: EPTU must be checked before PTU ("EPTU" contains "PTU").
    """
    p4k_dir_name = p4k_path.parent.name.upper()
    if "EPTU" in p4k_dir_name:
        return "EPTU"
    elif "PTU" in p4k_dir_name:
        return "PTU"
    elif "TECH" in p4k_dir_name:        # TECH-PREVIEW / TECHPREVIEW
        return "TECHPREVIEW"
    elif "HOTFIX" in p4k_dir_name:
        return "HOTFIX"
    elif "LIVE" in p4k_dir_name:
        return "LIVE"
    else:
        return p4k_dir_name if p4k_dir_name.isalnum() else "LIVE"


def _move_data_to_repo_root(versioned_out: Path) -> None:
    """Move data/Localization folder to repository root after successful generation."""
    import shutil

    data_src = versioned_out / "data"
    if not data_src.exists():
        logger.warning(f"Data folder not found at {data_src}")
        return

    repo_root = REPO_ROOT
    data_dst = repo_root / "data"

    try:
        # Merge per-language: replace only the folders this run produced.
        # A wholesale delete would wipe languages not included in the run
        # (e.g. --lang english would erase every community language and the
        # *_all* variants).
        src_loc = data_src / "Localization"
        dst_loc = data_dst / "Localization"
        dst_loc.mkdir(parents=True, exist_ok=True)
        moved = []
        for lang_dir in sorted(p for p in src_loc.iterdir() if p.is_dir()):
            target = dst_loc / lang_dir.name
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(lang_dir), str(target))
            moved.append(lang_dir.name)
        shutil.rmtree(data_src, ignore_errors=True)
        logger.info(f"[OK] Updated data folders at {dst_loc}: {', '.join(moved)}")
        print(f"\n[OK] Data folders updated: {', '.join(moved)}")

    except Exception as e:
        logger.error(f"Failed to move data folder: {e}")
        print(f"\n[Warning] Could not move data folder to repository root: {e}")
        print(f"  Data is still available at: {data_src}")


if __name__ == "__main__":
    raise SystemExit(main())
