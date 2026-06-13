#!/usr/bin/env python3
"""translate_enhancements.py — Translate generated enhancement texts to the
target language, producing fully-localized variants (e.g.
``data/Localization/portuguese_(brazil)_danielgmota_all/global.ini``).

The enhancement generator emits stat blocks with ENGLISH labels
(``Crew:``, ``Length:``, ``Insurance: ... min base``, ``>> Crafting:`` ...).
The community base translation is already in the target language, so only the
generated fragments need translating. This script does that with a
user-editable glossary — no machine-translation service involved.

Pipeline per configured language:

  1. Read  enhancements/{lang}/enhancements/*_enhancements.ini   (EN labels)
  2. Apply glossary rules        translations/glossaries/{g}.json
  3. Apply per-key overrides     translations/overrides/{g}.ini   (optional)
  4. Write translated INIs   →   enhancements/{lang}/enhancements_translated/
     (intermediate artifact — inspect or diff these)
  5. Merge with the language's base.ini → data/Localization/{sc_id}{suffix}/global.ini
  6. Report untranslated label candidates → translations/pending/{g}.json
     (intermediate artifact — copy entries into the glossary to translate them)

Customizing translations:
  - Edit translations/glossaries/{g}.json   → label-level translations (ordered,
    first match wins; longer rules should come first).
  - Edit translations/overrides/{g}.ini     → full-value replacement for specific
    INI keys (wins over glossary output entirely).
  - translations/pending/{g}.json lists what the glossary did NOT cover.

Usage:
    python translate_enhancements.py                 # all configured languages
    python translate_enhancements.py --lang portuguese_br_alt
    python translate_enhancements.py --check         # report only, write nothing
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

import lang_sources
from run_pipeline import ENHANCEMENT_FILES, _parse_ini_builtin, _write_ini_builtin

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENH_ROOT = REPO_ROOT / "enhancements"
TRANSLATIONS_DIR = REPO_ROOT / "translations"

# Which pipeline languages get a fully-translated variant and which glossary
# they use. The variant folder suffix is "_all" for every language.
ENHANCEMENT_TRANSLATIONS: dict[str, dict[str, str]] = {
    "french":            {"glossary": "fr",    "suffix": "_all"},
    "spanish":           {"glossary": "es",    "suffix": "_all"},
    "portuguese_br":     {"glossary": "pt_br", "suffix": "_all"},
}

# Label candidates the pending-report looks for: "Word(s):" after a literal
# \n (INI values store backslash-n, not newlines) or a "|" separator inside
# the generated stat block, plus unit suffixes.
_LABEL_RE = re.compile(r"(?:^|\\n|\|\s+)([A-Z][A-Za-z0-9 ./()-]{1,30}?):(?=\s)")
_UNIT_RE = re.compile(r"\b(min base|min express|items|types)\b")


def _load_glossary(name: str) -> tuple[list[tuple[str, str]], set[str]]:
    """Load ordered [search, replace] rules plus the 'ignore' term set.

    Longer searches are applied first so 'Armor HP:' wins over 'HP:'
    regardless of file order. 'ignore' lists terms reviewed and intentionally
    kept untranslated (units, acronyms) — they are excluded from the pending
    report.
    """
    path = TRANSLATIONS_DIR / "glossaries" / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Glossary not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    rules = [(str(s), str(t)) for s, t in data.get("rules", [])]
    rules.sort(key=lambda r: len(r[0]), reverse=True)
    ignore = {str(t) for t in data.get("ignore", [])}
    return rules, ignore


def _load_overrides(name: str) -> dict[str, str]:
    """Per-key full-value overrides (custom translations). Optional file."""
    path = TRANSLATIONS_DIR / "overrides" / f"{name}.ini"
    if not path.exists():
        return {}
    return _parse_ini_builtin(path)


def _apply_glossary(value: str, rules: list[tuple[str, str]]) -> str:
    for search, replace in rules:
        value = value.replace(search, replace)
    return value


# Generated-section markers, language-independent: the stats divider
# ("--- STATS ---" or its translation) and crafting lines ("<EM4>>>").
_SECTION_RE = re.compile(r"--- [^-]{3,40} ---|<EM4>>>")


def _find_untranslated(value: str) -> list[str]:
    """Heuristic: surviving English labels/units in the generated stat block.

    Called on the TRANSLATED value, so the markers may already be localized —
    _SECTION_RE matches both forms. Only the generated section is scanned;
    the description body is community-translated text and may legitimately
    contain "Word:" patterns.
    """
    m = _SECTION_RE.search(value)
    if not m:
        return []
    scan = value[m.start():]
    found: list[str] = []
    for lm in _LABEL_RE.finditer(scan):
        found.append(lm.group(1) + ":")
    for um in _UNIT_RE.finditer(scan):
        found.append(um.group(1))
    return found


def translate_language(language: str, cfg: dict[str, str], check_only: bool) -> dict:
    """Translate one language's enhancements. Returns a stats dict."""
    glossary_name = cfg["glossary"]
    suffix = cfg["suffix"]
    enh_dir = ENH_ROOT / language / "enhancements"
    out_dir = ENH_ROOT / language / "enhancements_translated"

    if not enh_dir.is_dir():
        logger.warning("[%s] no enhancements directory — skipping.", language)
        return {}

    rules, ignore = _load_glossary(glossary_name)
    overrides = _load_overrides(glossary_name)
    logger.info("[%s] glossary=%s (%d rules, %d overrides) suffix=%s",
                language, glossary_name, len(rules), len(overrides), suffix)

    # Already-translated labels (rule targets) can still be plain ASCII and
    # match the label regex — exclude them from the pending report, along
    # with intentionally-kept terms from the glossary's 'ignore' list.
    known = {t for _, t in rules} | ignore

    pending: Counter[str] = Counter()
    translated_all: dict[str, str] = {}
    files_written = 0

    for name in ENHANCEMENT_FILES:
        src = enh_dir / name
        if not src.exists():
            continue
        data = _parse_ini_builtin(src)
        out: dict[str, str] = {}
        for key, value in data.items():
            if key in overrides:
                out[key] = overrides[key]
            else:
                out[key] = _apply_glossary(value, rules)
                for term in _find_untranslated(out[key]):
                    if term not in known:
                        pending[term] += 1
        translated_all.update(out)
        if not check_only:
            out_dir.mkdir(parents=True, exist_ok=True)
            _write_ini_builtin(out_dir / name, out)
            files_written += 1

    # Build the variant from the pipeline's ALREADY-MERGED global.ini, only
    # swapping the enhancement keys for their translated values. Re-merging
    # base.ini here would diverge from the canonical product: the smart-citizen
    # parser normalizes ",P"-suffixed keys, the built-in one does not.
    merged_path = None
    sc_id = lang_sources.sc_language_id(language)
    canonical = REPO_ROOT / "data" / "Localization" / sc_id / "global.ini"
    if not canonical.exists():
        canonical = ENH_ROOT / language / "global" / "global.ini"
    if not check_only:
        if not canonical.exists():
            logger.warning("[%s] no merged global.ini found (run the pipeline "
                           "first) — variant not written.", language)
        else:
            merged = _parse_ini_builtin(canonical)
            replaced = sum(1 for k in translated_all if k in merged)
            merged.update(translated_all)
            variant_dir = REPO_ROOT / "data" / "Localization" / f"{sc_id}{suffix}"
            variant_dir.mkdir(parents=True, exist_ok=True)
            merged_path = variant_dir / "global.ini"
            _write_ini_builtin(merged_path, merged)
            logger.info("[%s] variant written: %s (%d keys, %d enhancement "
                        "keys swapped)", language, merged_path, len(merged),
                        replaced)

    return {
        "language": language,
        "glossary": glossary_name,
        "rules": len(rules),
        "overrides_applied": len(overrides),
        "keys_translated": len(translated_all),
        "files_written": files_written,
        "variant": str(merged_path) if merged_path else None,
        "pending": pending,
    }


def write_pending_report(glossary_name: str, pending: Counter, check_only: bool) -> None:
    """Persist untranslated terms so the user can extend the glossary."""
    report = {
        "_comment": (
            "Terms found in generated enhancement texts that no glossary rule "
            "matched. To translate them, copy entries into "
            f"translations/glossaries/{glossary_name}.json as [\"term\", \"translation\"] "
            "rules and re-run translate_enhancements.py."
        ),
        "untranslated": [
            {"term": term, "occurrences": count}
            for term, count in sorted(pending.items(), key=lambda kv: -kv[1])
        ],
    }
    path = TRANSLATIONS_DIR / "pending" / f"{glossary_name}.json"
    if not check_only:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        logger.info("pending report: %s (%d terms)", path, len(pending))
    else:
        logger.info("[check] %d untranslated terms for glossary %s",
                    len(pending), glossary_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Translate generated enhancement texts via glossary rules.")
    parser.add_argument("--lang", action="append", default=None,
                        help="Language to translate (repeatable; default: all configured).")
    parser.add_argument("--check", action="store_true",
                        help="Analyze only — write no files, just report coverage.")
    args = parser.parse_args(argv)

    languages = args.lang or list(ENHANCEMENT_TRANSLATIONS)
    pending_by_glossary: dict[str, Counter] = {}
    results = []

    for language in languages:
        cfg = ENHANCEMENT_TRANSLATIONS.get(language)
        if not cfg:
            logger.warning("No enhancement-translation config for %r — skipping. "
                           "Known: %s", language, ", ".join(ENHANCEMENT_TRANSLATIONS))
            continue
        stats = translate_language(language, cfg, args.check)
        if stats:
            results.append(stats)
            pending_by_glossary.setdefault(cfg["glossary"], Counter()).update(
                stats["pending"])

    for glossary_name, pending in pending_by_glossary.items():
        write_pending_report(glossary_name, pending, args.check)

    # New *_all* variants change the downloads table — refresh README/VERSIONS.
    if not args.check and results:
        try:
            import versions_report
            versions_report.generate()
        except Exception as exc:
            logger.warning("Could not refresh VERSIONS.md/README: %s", exc)

    print()
    for r in results:
        print(f"[{r['language']}] {r['keys_translated']} keys translated, "
              f"{len(r['pending'])} distinct untranslated terms"
              + (f", variant: {r['variant']}" if r["variant"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
