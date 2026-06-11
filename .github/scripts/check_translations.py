"""check_translations.py — CI helper: detect whether community translations changed.

Reads ``enhancements/version.json`` to find the last-known commit SHA for each
language's upstream repo.  Queries the GitHub API (unauthenticated, 60 req/h)
for the current HEAD commit of each branch.  If any differ, exits with
``changed=true`` in the GitHub Actions output file so the caller can decide
whether to re-run the pipeline.

Usage (from repo root):
    python .github/scripts/check_translations.py

Outputs (written to $GITHUB_OUTPUT when running in Actions):
    changed=true|false
    summary=<human-readable one-liner>

Also prints a table to stdout for the log.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_JSON = REPO_ROOT / "enhancements" / "version.json"

# Re-uses lang_sources.LANGUAGE_GITHUB_INFO without importing the module
# (the script must work in a bare Python 3.11 env with only stdlib).
_LANG_GITHUB = {
    "french":           {"owner": "Dymerz",     "repo": "StarCitizen-Localization", "branch": "main"},
    "spanish":          {"owner": "Dymerz",     "repo": "StarCitizen-Localization", "branch": "main"},
    "portuguese_br":    {"owner": "Dymerz",     "repo": "StarCitizen-Localization", "branch": "main"},
    "portuguese_br_alt":{"owner": "danielgmota","repo": "StarCitizen-Localization", "branch": "develop"},
}


def _gh_api(path: str) -> dict:
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lazy-citizen-enhancements/check-translations",
            "Accept": "application/vnd.github.v3+json",
            **({
                "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"
            } if "GITHUB_TOKEN" in os.environ else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


_commit_cache: dict[tuple[str, str, str], str | None] = {}


def current_commit(owner: str, repo: str, branch: str) -> str | None:
    """Return the current HEAD commit SHA for owner/repo@branch, or None on error.

    Cached per (owner, repo, branch): several languages share the same
    upstream repo, so this avoids redundant API calls (and rate-limit
    pressure on the unauthenticated 60 req/h budget).
    """
    key = (owner, repo, branch)
    if key in _commit_cache:
        return _commit_cache[key]
    result: str | None
    try:
        data = _gh_api(f"repos/{owner}/{repo}/commits/{branch}")
        result = data.get("sha", "")
    except urllib.error.HTTPError as exc:
        print(f"  [warn] GitHub API {exc.code} for {owner}/{repo}@{branch}: {exc.reason}",
              file=sys.stderr)
        result = None
    except Exception as exc:
        print(f"  [warn] GitHub API error for {owner}/{repo}@{branch}: {exc}", file=sys.stderr)
        result = None
    _commit_cache[key] = result
    return result


def load_stored_commits() -> dict[str, str]:
    """Read stored translation commit SHAs from version.json."""
    if not VERSION_JSON.exists():
        print(f"[info] {VERSION_JSON} not found — treating all translations as changed.")
        return {}
    try:
        data = json.loads(VERSION_JSON.read_text(encoding="utf-8"))
        return data.get("translations", {})
    except Exception as exc:
        print(f"[warn] Could not read version.json: {exc}", file=sys.stderr)
        return {}


def set_github_output(key: str, value: str) -> None:
    out_file = os.environ.get("GITHUB_OUTPUT")
    if out_file:
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"::set-output name={key}::{value}")  # legacy fallback


def write_step_summary(rows: list[tuple[str, str, str, str]]) -> None:
    """Append a versions table to $GITHUB_STEP_SUMMARY so the original
    (stored/processed) vs. current upstream translation versions are visible
    directly on the Actions run page.

    rows: (language, stored_sha, current_sha, status)
    """
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    with open(summary_file, "a", encoding="utf-8") as f:
        f.write("## Translation source versions\n\n")
        f.write("**Stored** = original commit pinned in `enhancements/version.json` "
                "(the version last fed to the enhancement pipeline). "
                "**Current** = upstream HEAD right now.\n\n")
        f.write("| Language | Stored (processed) | Current (upstream) | Status |\n")
        f.write("|---|---|---|---|\n")
        for lang, stored_sha, current_sha, status in rows:
            info = _LANG_GITHUB[lang]
            base = f"https://github.com/{info['owner']}/{info['repo']}"
            stored_cell = (f"[`{stored_sha[:7]}`]({base}/commit/{stored_sha})"
                           if stored_sha else "_(none)_")
            current_cell = (f"[`{current_sha[:7]}`]({base}/commit/{current_sha})"
                            if current_sha else "_(api error)_")
            f.write(f"| {lang} | {stored_cell} | {current_cell} | `{status}` |\n")
        f.write("\n")


def update_readme_status(rows: list[tuple[str, str, str, str]]) -> None:
    """Write the stored-vs-upstream comparison into the README status table
    (between the VERSION-STATUS markers) via versions_report. The workflow
    commits README.md, so the repo front page always shows the result of the
    latest verification.

    Skipped entirely when EVERY lookup failed: a fully-failed run verified
    nothing, so the README keeps its last good "Last verified" state."""
    import time
    if rows and all(status.startswith("!") for _, _, _, status in rows):
        print("[warn] All upstream lookups failed - README status table left untouched.",
              file=sys.stderr)
        return
    _STATUS_LABELS = {
        "=": "✅ up to date",
        "^": "⬆️ update available",
        "+": "🆕 new (not processed yet)",
        "!": "❗ upstream check failed",
    }
    try:
        sys.path.insert(0, str(REPO_ROOT))
        import versions_report
        vr_rows = []
        for lang, stored_sha, current_sha, status in rows:
            info = _LANG_GITHUB[lang]
            base = f"https://github.com/{info['owner']}/{info['repo']}"
            vr_rows.append({
                "label": f"{lang} — `{info['owner']}/{info['repo']}@{info['branch']}`",
                "stored": stored_sha,
                "current": current_sha,
                "status": _STATUS_LABELS.get(status[:1], status),
                "url": f"{base}/commit/{stored_sha}" if stored_sha else "",
            })
        checked_at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        if versions_report.update_readme_status(vr_rows, checked_at):
            print("[info] README version-status table refreshed.")
    except Exception as exc:
        print(f"[warn] Could not refresh README status table: {exc}", file=sys.stderr)


def main() -> int:
    stored = load_stored_commits()

    print(f"\n{'Language':<22} {'Stored SHA':<42} {'Current SHA':<42} {'Status'}")
    print("-" * 115)

    changed_langs: list[str] = []
    failed_langs: list[str] = []
    rows: list[tuple[str, str, str, str]] = []

    for lang, info in _LANG_GITHUB.items():
        stored_sha = stored.get(lang, "")
        current = current_commit(info["owner"], info["repo"], info["branch"])
        if current is None:
            status = "! api-error"
            failed_langs.append(lang)
        elif not stored_sha:
            status = "+ new"
            changed_langs.append(lang)
        elif current != stored_sha:
            status = "^ updated"
            changed_langs.append(lang)
        else:
            status = "= up-to-date"

        rows.append((lang, stored_sha, current or "", status))
        print(f"{lang:<22} {(stored_sha or '(none)'):<42} {(current or 'n/a'):<42} {status}")

    write_step_summary(rows)
    update_readme_status(rows)

    print()
    failed_note = (f" ({len(failed_langs)} source(s) could NOT be verified - API error: "
                   f"{', '.join(failed_langs)})" if failed_langs else "")
    if changed_langs:
        summary = f"{len(changed_langs)} language(s) updated: {', '.join(changed_langs)}{failed_note}"
        print(f"[result] {summary}")
        set_github_output("changed", "true")
        set_github_output("changed_langs", " ".join(changed_langs))
        set_github_output("summary", summary)
    elif failed_langs:
        # Verification failed — do NOT claim freshness, and do not trigger a run.
        summary = (f"Verification incomplete: could not reach upstream for "
                   f"{', '.join(failed_langs)}. No language confirmed as updated.")
        print(f"[result] {summary}")
        set_github_output("changed", "false")
        set_github_output("changed_langs", "")
        set_github_output("summary", summary)
    else:
        summary = "All translations up-to-date - no pipeline run needed."
        print(f"[result] {summary}")
        set_github_output("changed", "false")
        set_github_output("changed_langs", "")
        set_github_output("summary", summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
