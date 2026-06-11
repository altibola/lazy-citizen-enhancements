# lazy-citizen-enhancements

[![Update community translations](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/check-translations.yml/badge.svg?branch=main)](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/check-translations.yml)
[![Translate enhancement texts](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/translate-enhancements.yml/badge.svg?branch=main)](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/translate-enhancements.yml)
[![Build on Windows](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/build-on-windows.yml/badge.svg?branch=main)](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/build-on-windows.yml)
[![Promote build to main](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/promote-build.yml/badge.svg?branch=main)](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/promote-build.yml)

Generates enhanced `global.ini` localization files for Star Citizen by merging
community translations with auto-generated stat overlays (ships, weapons,
missions, components).

## Version status

<!-- VERSION-STATUS:START -->

_No version check has run yet — this table is filled automatically by the
pipeline and the **Update community translations** workflow (game build +
pinned upstream commit of every translation source, compared against
upstream HEAD)._

<!-- VERSION-STATUS:END -->

## Quickstart (players)

Open **PowerShell** and run:

```powershell
irm https://raw.githubusercontent.com/altibola/lazy-citizen-enhancements/main/install_translation.ps1 | iex
```

The installer finds your Star Citizen folder (current directory → RSI
Launcher logs → default path → asks), lets you pick the language and whether
you want the stats/enhancements translated too, downloads the right
`global.ini` and sets `g_language` in `user.cfg`. Run it from inside a
channel folder (e.g. `StarCitizen\PTU`) to target that channel; from
anywhere else it targets **LIVE**.

## Downloads

<!-- DOWNLOADS:START -->

_No builds published yet — the table is generated automatically after the first pipeline run (`versions_report.py`)._

<!-- DOWNLOADS:END -->

Install: copy the desired language folder to your Star Citizen installation —

```
StarCitizen/LIVE/data/Localization/<language>/global.ini
```

and confirm `user.cfg` contains:

```
g_language = portuguese_(brazil)   # adjust to your language
```

## Version guarantees

Every release records the exact versions it was built from:

- **[VERSIONS.md](VERSIONS.md)** — game build (P4CL), the pinned upstream
  commit of each community translation (with permalinks) and exactly which
  inputs the enhancement generator consumed, plus per-file output hashes.
- **`enhancements/version.json`** — machine-readable equivalent, used by the
  automation to detect upstream translation updates.
- **`enhancements/<lang>/enhancements/provenance.json`** — full per-language
  input/output chain with SHA-256 hashes.

## How it works

Two flows, by design:

### 1. Full build (local machine or hosted runner — produces a `build/{p4cl}` branch)

Generates everything from the game data. Needs game files: either a local
Star Citizen install (`Data.p4k`) or `--download` (RSI CDN, credentials
required). Runs on Windows **and** Linux — on Linux the unp4k/unforge DLL
builds (github.com/dolkensp/unp4k) are fetched at setup time and run via the
`dotnet` runtime. `./runall.sh` chains all steps; each one is also runnable
on its own (see Usage).

1. Extract the English `global.ini` + DataForge XMLs (from `Data.p4k`, or
   download `global.ini` + `Game2.dcb` from the CDN)
2. Download the community base translation for each language (commit pinned)
3. Generate stat overlays via Smart Citizen (ships, weapons, missions, …)
4. Translate the generated texts via glossary (`*_all` variants)
5. Merge base + enhancements → final `global.ini` per language
6. `create_pr.py` pushes branch **`build/{p4cl}`** and opens a PR to `main`.
   The PR is labeled with the environment (**LIVE / PTU / EPTU / HOTFIX /
   TECHPREVIEW**); PTU-family PRs carry a "merge only after LIVE" note.

### 2. Translation refresh (CI — no game files needed)

When an upstream community translation updates, the
**Update community translations** workflow re-runs only the
download + merge steps (`--skip-extract --skip-generate`), reusing the
enhancement INIs already on the branch. If nothing changed upstream, the run
produces no diff and commits nothing.

When a PTU build ships to LIVE, the **Promote build to main** workflow merges
the matching `build/{p4cl}` branch into `main`.

## Workflows (all on-demand — `workflow_dispatch`)

| Workflow | What it does |
|---|---|
| **Build on Windows (self-hosted)** | Step 1 (extract + generate + merge) on a self-hosted Windows runner with Star Citizen installed; optionally translates and pushes `build/{p4cl}` + PR. |
| **Update community translations** | Checks upstream repos for new commits; re-merges and commits when something changed. Run summary shows stored vs. upstream versions. |
| **Translate enhancement texts** | Applies the glossaries to the generated texts and rebuilds the `*_all*` variants. |
| **Promote build to main** | Opens a PR (or auto-merges) `build/{p4cl}` → `main` when that build is LIVE. |
| **Download build (hosted)** | Full pipeline on a GitHub-hosted runner (`ubuntu-latest` or `windows-latest`), via RSI CDN download — no game install needed. See [docs/download-runner.md](docs/download-runner.md). |

### Self-hosted Windows runner (one-time setup)

On the machine with Star Citizen installed: register a runner under
*Settings → Actions → Runners → New self-hosted runner* (labels
`self-hosted, Windows`), make sure Git for Windows is installed, and set the
repository variable `SC_P4K_PATH` (*Settings → Secrets and variables →
Variables*) to the `Data.p4k` path, e.g.
`E:\starcitizen\StarCitizen\LIVE\Data.p4k`. The first run bootstraps
`.micromamba`/`.smart-citizen` into the runner workspace; later runs reuse
them and the DataForge cache.

## Translating the generated texts (`*_all` variants)

The stat blocks are generated with English labels. `translate_enhancements.py`
translates them with committed, user-editable glossaries — no external
translation service:

| File | Role |
|---|---|
| `translations/glossaries/<g>.json` | Label translations (`"Crew:" → "Tripulação:"`). Edit freely. |
| `translations/overrides/<g>.ini` | Full custom translation for specific keys — wins over the glossary. |
| `translations/pending/<g>.json` | *Generated*: terms the glossary didn't cover. Copy them into the glossary to translate. |
| `enhancements/<lang>/enhancements_translated/` | *Generated*: the post-glossary INIs (intermediate, inspectable). |
| `data/Localization/<id>_all/` | Final fully-localized variant. |

```bash
python translate_enhancements.py            # all configured languages
python translate_enhancements.py --check    # coverage report only
```

## Setup

```bash
./bootstrap.sh        # creates .micromamba env and fetches Smart Citizen (run once)
```

## Usage

### All steps at once

```bash
# Local install → pipeline → glossary translation → branch build/{p4cl} + PR:
./runall.sh --p4k "/path/to/StarCitizen/LIVE/Data.p4k"

# Same via RSI CDN (no local install):
./runall.sh --download --channel LIVE     # PTU/EPTU also accepted

# Everything except the branch/PR:
./runall.sh --p4k "..." --no-pr
```

### Granular steps (each runnable on its own, locally or online)

```bash
# 1. Pipeline only (extract/download + generate + merge):
./run.sh --p4k "/path/to/StarCitizen/LIVE/Data.p4k"
./run.sh --p4k "..." --lang portuguese_br            # single language
python run_pipeline.py --skip-extract --skip-generate # re-merge only (CI mode)

# 2. Glossary translation only (*_all* variants):
./translate.sh                # ./translate.sh --check for coverage report

# 3. Branch build/{p4cl} + PR only:
python create_pr.py
```

## Adding languages

1. Add entries to `LANGUAGE_GITHUB_INFO` and `SC_LANGUAGE_IDS` in
   [`lang_sources.py`](lang_sources.py).
2. (Optional) To get a fully-translated variant, add the language to
   `ENHANCEMENT_TRANSLATIONS` in
   [`translate_enhancements.py`](translate_enhancements.py) and create its
   glossary under `translations/glossaries/`.

## Projects used

| Project | Repository | Role |
|---|---|---|
| **Smart Citizen** | [Osiris-DevWorks/smart-citizen](https://github.com/Osiris-DevWorks/smart-citizen) | Enhancement engine (generator + merger), fetched at setup time |
| **Dymerz** | [Dymerz/StarCitizen-Localization](https://github.com/Dymerz/StarCitizen-Localization) | Base translations: French, Spanish, PT-BR (`main`) |
| **danielgmota** | [danielgmota/StarCitizen-Localization](https://github.com/danielgmota/StarCitizen-Localization) | Alternative PT-BR base translation (`develop`) |

See [NOTICE](NOTICE) for Smart Citizen attribution (Apache 2.0).

## Maintenance

```bash
./clean.sh            # remove out/, *.log, __pycache__
./clean.sh --deep     # also remove .smart-citizen/  → restore with ./setup_smart_citizen.sh
./clean.sh --full     # also remove .micromamba/     → restore with ./bootstrap.sh
```

## Requirements

- **Full build, local Windows**: .NET Framework 4.x (`unp4k.exe`/`unforge.exe`),
  Star Citizen install or RSI credentials for `--download`
- **Full build, local Linux/macOS**: `dotnet` runtime (unp4k/unforge DLL builds,
  fetched automatically at setup) + RSI credentials for `--download`
- **Full build, hosted runner**: nothing local — see
  [docs/download-runner.md](docs/download-runner.md)
- **Translation refresh (CI)**: plain Python 3.11, no game files

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).  
Unofficial community project; not affiliated with Cloud Imperium Games or RSI.
