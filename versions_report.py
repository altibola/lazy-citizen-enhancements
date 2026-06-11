#!/usr/bin/env python3
"""versions_report.py — Generate VERSIONS.md: a human-readable version manifest.

Makes explicit, at the repository root, the full input/output version chain:

  1. Game build (P4CL + environment) the pipeline ran against.
  2. The ORIGINAL community translation each language came from
     (upstream repo, branch, commit permalink, file sha256).
  3. What was fed INTO the enhancement generator (base_en.ini from the game
     build + the original base.ini) and which enhancement files it produced.

Data sources (already written by run_pipeline.py):
  - enhancements/version.json                              (build + commit SHAs)
  - enhancements/{lang}/enhancements/provenance.json       (full per-language chain)
  - enhancements/{lang}/enhancements/base.ini.source.json  (original source pin)

Stdlib-only — runs in bare CI Python and in Git Bash.

Usage:
    python versions_report.py                    # writes VERSIONS.md, prints it
    python versions_report.py --github-summary   # also appends to $GITHUB_STEP_SUMMARY
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ENH_DIR = REPO_ROOT / "enhancements"
DATA_DIR = REPO_ROOT / "data" / "Localization"
OUTPUT_MD = REPO_ROOT / "VERSIONS.md"
README_MD = REPO_ROOT / "README.md"

# Markers delimiting the auto-generated README sections.
README_START = "<!-- DOWNLOADS:START -->"
README_END = "<!-- DOWNLOADS:END -->"
STATUS_START = "<!-- VERSION-STATUS:START -->"
STATUS_END = "<!-- VERSION-STATUS:END -->"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _short(sha: str, n: int = 7) -> str:
    return sha[:n] if sha else "?"


def _display_language(folder: str) -> str:
    """data/Localization folder name → human-readable label.

    'portuguese_(brazil)_danielgmota'      → 'Portuguese (Brazil) — danielgmota'
    'portuguese_(brazil)_danielgmota_all'  → 'Portuguese (Brazil) — danielgmota — stats translated'
    """
    name = folder
    note = ""
    if name.endswith("_all"):
        note = " — stats translated"
        name = name[: -len("_all")]
    parts = name.split("_")
    # Re-join "(brazil)"-style chunks with the language word before them.
    lang_words: list[str] = []
    source = ""
    for p in parts:
        if p.startswith("(") or not lang_words:
            lang_words.append(p)
        else:
            source = p
    lang = " ".join(w.capitalize() if not w.startswith("(") else
                    "(" + w[1:-1].capitalize() + ")" for w in lang_words)
    label = lang + (f" — {source}" if source else "") + note
    return label


def build_downloads_table() -> str:
    """Markdown table: language | game build | link to the enhanced file.

    Lists every data/Localization/<folder>/global.ini currently in the repo,
    including the fully-translated *_all* variants.
    """
    version = _load_json(ENH_DIR / "version.json")
    game_build = version.get("version", "unknown")
    environment = version.get("environment", "unknown")

    lines: list[str] = []
    lines.append(README_START)
    lines.append("")
    lines.append(f"Current build: **`{game_build}`** ({environment}) — "
                 "this table is regenerated automatically by the pipeline "
                 "(`versions_report.py`); see [VERSIONS.md](VERSIONS.md) for the "
                 "full input/output version manifest.")
    lines.append("")
    lines.append("| Language | Game build | Enhanced file |")
    lines.append("|---|---|---|")

    if DATA_DIR.is_dir():
        for folder in sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir()):
            ini = DATA_DIR / folder / "global.ini"
            if not ini.exists():
                continue
            href = urllib.parse.quote(f"data/Localization/{folder}/global.ini")
            lines.append(
                f"| {_display_language(folder)} "
                f"| `{game_build}` ({environment}) "
                f"| [global.ini]({href}) |"
            )
    lines.append("")
    lines.append(README_END)
    return "\n".join(lines)


def _replace_readme_block(start_marker: str, end_marker: str, content: str) -> bool:
    """Replace the README block between two markers (markers included in
    *content*). Returns False when the README or markers are missing."""
    if not README_MD.exists():
        return False
    text = README_MD.read_text(encoding="utf-8")
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 or end < 0:
        return False
    new_text = text[:start] + content + text[end + len(end_marker):]
    if new_text != text:
        README_MD.write_text(new_text, encoding="utf-8")
    return True


def update_readme() -> bool:
    """Refresh the downloads table between the DOWNLOADS markers."""
    return _replace_readme_block(README_START, README_END, build_downloads_table())


def build_status_section(rows: list[dict] | None = None,
                         checked_at: str | None = None) -> str:
    """Markdown for the version-freshness table between the STATUS markers.

    rows: list of dicts with keys label, stored, current, status, url —
    produced by check_translations.py when it compares against upstream HEAD.
    When None (full pipeline run), rows are synthesized from version.json:
    everything was just downloaded, so the pins ARE upstream HEAD.
    """
    import time as _time
    version = _load_json(ENH_DIR / "version.json")
    game_build = version.get("version", "unknown")
    environment = version.get("environment", "unknown")
    checked_at = checked_at or _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime())

    if rows is None:
        rows = []
        try:
            import lang_sources
            gh = lang_sources.LANGUAGE_GITHUB_INFO
        except Exception:
            gh = {}
        for lang, sha in sorted(version.get("translations", {}).items()):
            info = gh.get(lang, {})
            url = (f"https://github.com/{info['owner']}/{info['repo']}/commit/{sha}"
                   if info else "")
            rows.append({
                "label": (f"{lang} — `{info['owner']}/{info['repo']}@{info['branch']}`"
                          if info else lang),
                "stored": sha, "current": sha,
                "status": "✅ up to date (pinned at build)", "url": url,
            })

    lines: list[str] = []
    lines.append(STATUS_START)
    lines.append("")
    lines.append(f"_Last verified: **{checked_at}** — refreshed automatically by "
                 "the pipeline and the **Update community translations** workflow._")
    lines.append("")
    lines.append("| Source | Pinned (this repo) | Upstream HEAD | Status |")
    lines.append("|---|---|---|---|")
    lines.append(f"| Game build (P4CL) | `{game_build}` ({environment}) | — | — |")
    for r in rows:
        stored = f"[`{r['stored'][:7]}`]({r['url']})" if r.get("stored") and r.get("url") \
                 else (f"`{r['stored'][:7]}`" if r.get("stored") else "_(none)_")
        current = f"`{r['current'][:7]}`" if r.get("current") else "_(n/a)_"
        lines.append(f"| {r['label']} | {stored} | {current} | {r['status']} |")
    lines.append("")
    lines.append(STATUS_END)
    return "\n".join(lines)


def update_readme_status(rows: list[dict] | None = None,
                         checked_at: str | None = None) -> bool:
    """Refresh the version-status table between the STATUS markers."""
    return _replace_readme_block(STATUS_START, STATUS_END,
                                 build_status_section(rows, checked_at))


def generate() -> str:
    """Write VERSIONS.md and refresh the README downloads + status tables.
    Entry point used by run_pipeline / translate_enhancements."""
    report = build_report()
    OUTPUT_MD.write_text(report, encoding="utf-8")
    update_readme()
    update_readme_status()
    return report


def build_report() -> str:
    version = _load_json(ENH_DIR / "version.json")
    game_build = version.get("version", "unknown")
    environment = version.get("environment", "unknown")

    lines: list[str] = []
    lines.append("# Versions")
    lines.append("")
    lines.append("> Auto-generated by the pipeline (`versions_report.py`) — do not edit manually.")
    lines.append("")

    # ── 1. Game build ─────────────────────────────────────────────────────────
    lines.append("## Game build")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Build (P4CL) | `{game_build}` |")
    lines.append(f"| Environment | `{environment}` |")

    # base_en.ini sha — same for all languages, grab from the first provenance found
    base_en_sha = ""
    for prov_path in sorted(ENH_DIR.glob("*/enhancements/provenance.json")):
        prov = _load_json(prov_path)
        base_en_sha = prov.get("inputs", {}).get("base_en.ini", {}).get("sha256", "")
        if base_en_sha:
            break
    if base_en_sha:
        lines.append(f"| `base_en.ini` sha256 | `{base_en_sha}` |")
    lines.append("")

    # ── 2. Original community translations ───────────────────────────────────
    lines.append("## Original community translations (pipeline inputs)")
    lines.append("")
    lines.append("The unmodified upstream files each language was built from.")
    lines.append("")
    lines.append("| Language | Upstream source | Pinned commit | Original `base.ini` sha256 |")
    lines.append("|---|---|---|---|")

    langs = sorted(p.name for p in ENH_DIR.iterdir()
                   if p.is_dir() and (p / "enhancements").is_dir())
    per_lang: dict[str, dict] = {}
    for lang in langs:
        src = _load_json(ENH_DIR / lang / "enhancements" / "base.ini.source.json")
        per_lang[lang] = src
        if not src:
            lines.append(f"| {lang} | _(no source pin found)_ | — | — |")
            continue
        if src.get("origin") == "game":
            lines.append(f"| {lang} | _Star Citizen (game build — `base_en.ini`)_ "
                         f"| — | `{src.get('sha256', '?')}` |")
            continue
        repo = f"{src.get('owner', '?')}/{src.get('repo', '?')}@{src.get('branch', '?')}"
        commit = src.get("commit", "")
        permalink = src.get("permalink", "")
        commit_cell = f"[`{_short(commit)}`]({permalink})" if permalink else f"`{_short(commit)}`"
        lines.append(f"| {lang} | `{repo}` | {commit_cell} | `{src.get('sha256', '?')}` |")
    lines.append("")

    # ── 3. Inputs handed to the enhancement generator ────────────────────────
    lines.append("## Enhancement generation (per-language input -> output)")
    lines.append("")
    lines.append("What was passed to the generator/merger and what it produced.")
    lines.append("")

    for lang in langs:
        prov = _load_json(ENH_DIR / lang / "enhancements" / "provenance.json")
        if not prov:
            lines.append(f"### {lang}")
            lines.append("")
            lines.append("_(no provenance.json found)_")
            lines.append("")
            continue

        lines.append(f"### {lang}")
        lines.append("")
        lines.append(f"- Generated at: `{prov.get('generated_at', '?')}` (build `{prov.get('build', '?')}`)")
        inputs = prov.get("inputs", {})
        be = inputs.get("base_en.ini", {})
        bi = inputs.get("base.ini", {})
        lines.append(f"- Input `base_en.ini` (game build): sha256 `{be.get('sha256', '?')}`")
        if bi.get("origin") == "game":
            lines.append(
                f"- Input `base.ini`: the game's own English base (no community "
                f"translation) — sha256 `{bi.get('sha256', '?')}`"
            )
        else:
            commit = bi.get("commit", "")
            lines.append(
                f"- Input `base.ini` (original translation): "
                f"`{bi.get('owner', '?')}/{bi.get('repo', '?')}@{_short(commit)}` "
                f"— sha256 `{bi.get('sha256', '?')}`"
            )
        outputs = prov.get("outputs", {})
        if outputs:
            lines.append(f"- Outputs ({len(outputs)} files):")
            for name in sorted(outputs):
                sha = outputs[name].get("sha256", "?")
                lines.append(f"  - `{name}` — sha256 `{sha}`")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate VERSIONS.md version manifest.")
    parser.add_argument("--github-summary", action="store_true",
                        help="Also append the report to $GITHUB_STEP_SUMMARY (CI).")
    parser.add_argument("--stdout-only", action="store_true",
                        help="Print only; do not write VERSIONS.md.")
    args = parser.parse_args()

    report = build_report()

    if not args.stdout_only:
        OUTPUT_MD.write_text(report, encoding="utf-8")
        print(f"[versions_report] wrote {OUTPUT_MD}")
        if update_readme():
            print(f"[versions_report] refreshed downloads table in {README_MD}")

    if args.github_summary:
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(report)
            print("[versions_report] appended to GITHUB_STEP_SUMMARY")

    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
