"""Language source map for lazy-citizen-enhancements.

Central extension point for the multi-language goal: map each internal language
name to the community ``global.ini`` download URL used as that language's base,
and to the Star Citizen localization folder id.

The English base is NOT listed here — it is extracted from the user's own
Data.p4k (see pak_extract.extract_global_ini), because the enhancement generator
needs the English text for stat tags / entity names / standings regardless of
the target language.
"""

# Internal language name → structured GitHub source info.
# Used to resolve the exact commit SHA at download time and build a permanent
# link (permalink) that pins the tree state we actually downloaded.
# ``url`` is derived automatically; add entries here when adding new languages.
LANGUAGE_GITHUB_INFO: dict[str, dict] = {
    "french": {
        "owner": "Dymerz",
        "repo":  "StarCitizen-Localization",
        "branch": "main",
        "branches": {"LIVE": "main", "PTU": "ptu", "EPTU": "ptu"},
        "path": "data/Localization/french_(france)/global.ini",
    },
    "spanish": {
        "owner": "Thord82",
        "repo":  "Star_citizen_ES",
        "branch": "main",
        "branches": {"LIVE": "main", "PTU": "ptu", "EPTU": "ptu"},
        "path": "global.ini",
    },
    "portuguese_br": {
        "owner": "danielgmota",
        "repo":  "StarCitizen-Localization",
        "branch": "develop",
        "branches": {"LIVE": "develop", "PTU": "develop", "EPTU": "develop"},
        "path": "data/Localization/portuguese_(brazil)/global.ini",
    },
    "portuguese_br_dymerz": {
        "owner": "Dymerz",
        "repo":  "StarCitizen-Localization",
        "branch": "main",
        "branches": {"LIVE": "main", "PTU": "ptu", "EPTU": "ptu"},
        "path": "data/Localization/portuguese_(brazil)/global.ini",
    },
    "italian": {
        "owner": "Dymerz",
        "repo":  "StarCitizen-Localization",
        "branch": "main",
        "branches": {"LIVE": "main", "PTU": "ptu", "EPTU": "ptu"},
        "path": "data/Localization/italian_(italy)/global.ini",
    },
}

# Internal language name → community global.ini download URL.
# Derived from LANGUAGE_GITHUB_INFO so there is a single source of truth.
LANGUAGE_SOURCES: dict[str, str] = {
    lang: (
        f"https://raw.githubusercontent.com/{info['owner']}/{info['repo']}"
        f"/{info['branch']}/{info['path']}"
    )
    for lang, info in LANGUAGE_GITHUB_INFO.items()
}

# Internal language name → Star Citizen localization folder / g_language id.
# Used when placing the final global.ini under data/Localization/<id>/.
SC_LANGUAGE_IDS: dict[str, str] = {
    "english": "english",
    "french": "french_(france)",
    "spanish": "spanish_(spain)",
    "portuguese_br": "portuguese_(brazil)",
    "portuguese_br_dymerz": "portuguese_(brazil)_dymerz",
    "italian": "italian_(italy)",
}

# Default language when none is specified (for single-language mode)
DEFAULT_LANGUAGE = "portuguese_br"


def available_languages() -> list[str]:
    """Languages that have a download source configured."""
    return sorted(LANGUAGE_GITHUB_INFO)


def github_info(language: str, environment: str = "LIVE") -> dict | None:
    """Return the GitHub source dict for *language*, adjusted for *environment*."""
    info = LANGUAGE_GITHUB_INFO.get(language)
    if not info:
        return None
    res = dict(info)
    branches = res.get("branches")
    if isinstance(branches, dict):
        res["branch"] = branches.get(environment.upper(), res.get("branch", "main"))
    return res


def language_url(language: str, environment: str = "LIVE") -> str:
    """Return the base.ini download URL for *language* in *environment* or raise KeyError."""
    info = github_info(language, environment)
    if not info:
        raise KeyError(
            f"No source URL configured for language {language!r}. "
            f"Known: {', '.join(available_languages()) or '(none)'}"
        )
    return (
        f"https://raw.githubusercontent.com/{info['owner']}/{info['repo']}"
        f"/{info['branch']}/{info['path']}"
    )


def sc_language_id(language: str) -> str:
    """Return the SC localization folder id for *language* (falls back to itself)."""
    return SC_LANGUAGE_IDS.get(language, language)

