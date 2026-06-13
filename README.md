# lazy-citizen-enhancements

[![Update community translations](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/check-translations.yml/badge.svg?branch=main)](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/check-translations.yml)
[![Translate enhancement texts](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/translate-enhancements.yml/badge.svg?branch=main)](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/translate-enhancements.yml)
[![Promote build to main](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/promote-build.yml/badge.svg?branch=main)](https://github.com/altibola/lazy-citizen-enhancements/actions/workflows/promote-build.yml)

Generates enhanced `global.ini` localization files for Star Citizen by merging
community translations with auto-generated stat overlays (ships, weapons,
missions, components).

## Version status

<!-- VERSION-STATUS:START -->

_Last verified: **2026-06-13 13:01 UTC** — refreshed automatically by the pipeline and the **Update community translations** workflow._

| Source | Pinned (this repo) | Upstream HEAD | Status |
|---|---|---|---|
| Game build (P4CL) | `11952564` (LIVE) | — | — |
| french — `Dymerz/StarCitizen-Localization@main` | [`0ad8cda`](https://github.com/Dymerz/StarCitizen-Localization/commit/0ad8cdad1e790c30f88fbc4d188b44795533234a) | `0ad8cda` | ✅ up to date (pinned at build) |
| portuguese_br — `danielgmota/StarCitizen-Localization@develop` | [`bd0d6bf`](https://github.com/danielgmota/StarCitizen-Localization/commit/bd0d6bfe47a120a8437a9940021adb067ce2cd1d) | `bd0d6bf` | ✅ up to date (pinned at build) |
| spanish — `Dymerz/StarCitizen-Localization@main` | [`0ad8cda`](https://github.com/Dymerz/StarCitizen-Localization/commit/0ad8cdad1e790c30f88fbc4d188b44795533234a) | `0ad8cda` | ✅ up to date (pinned at build) |

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

Current build: **`11952564`** (LIVE) — this table is regenerated automatically by the pipeline (`versions_report.py`); see [VERSIONS.md](VERSIONS.md) for the full input/output version manifest.

| Language | Game build | Enhanced file |
|---|---|---|
| English | `11952564` (LIVE) | [global.ini](data/Localization/english/global.ini) |
| French (France) | `11952564` (LIVE) | [global.ini](data/Localization/french_%28france%29/global.ini) |
| French (France) — stats translated | `11952564` (LIVE) | [global.ini](data/Localization/french_%28france%29_all/global.ini) |
| Portuguese (Brazil) | `11952564` (LIVE) | [global.ini](data/Localization/portuguese_%28brazil%29/global.ini) |
| Portuguese (Brazil) — stats translated | `11952564` (LIVE) | [global.ini](data/Localization/portuguese_%28brazil%29_all/global.ini) |
| Spanish (Spain) | `11952564` (LIVE) | [global.ini](data/Localization/spanish_%28spain%29/global.ini) |
| Spanish (Spain) — stats translated | `11952564` (LIVE) | [global.ini](data/Localization/spanish_%28spain%29_all/global.ini) |

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

Three flows by design:

### 1. Full build local + online (recomendado)

Você roda localmente apenas o que precisa do Star Citizen instalado. O
GitHub Actions cuida do restante (traduções comunitárias, glossário, PR).

```
RSI Launcher instala o jogo
    └─ run-local.ps1           (extrai Data.p4k, gera enhancements, commita)
        └─ build-from-extraction.yml  (online: baixa traduções, cria PR)
```

1. RSI Launcher baixa e instala o patch normalmente.
2. `run-local.ps1` detecta o `Data.p4k` automaticamente, extrai e gera.
3. Commita e faz push de `enhancements/`.
4. Dispara o workflow **Build from local extraction** → traduz + cria PR.

### 2. Full build 100% online (requer credenciais RSI no GitHub)

O workflow **Download build (hosted)** autentica no CDN da RSI e faz tudo
sem instalação local. Ver [docs/download-runner.md](docs/download-runner.md).

### 3. Translation refresh (CI — sem arquivos de jogo)

Quando uma tradução comunitária muda upstream, o workflow
**Update community translations** reroda só o download + merge
(`--skip-extract --skip-generate`), reusando os `*_enhancements.ini` já no
branch. Se nada mudou, não commita nada.

Quando um build PTU vira LIVE, o workflow **Promote build to main** merge
o branch `build/{p4cl}` em `main`.

## Workflows (all on-demand — `workflow_dispatch`)

| Workflow | O que faz | Quando usar |
|---|---|---|
| **Build from local extraction** | Baixa traduções comunitárias e abre PR; reusa `enhancements/` já commitado localmente. | Após `run-local.ps1` + push |
| **Download build (hosted)** | Pipeline completo num runner GitHub via CDN da RSI. Requer `RSI_USERNAME`/`RSI_PASSWORD` nos Secrets. | Patch novo, sem instalação local |
| **Update community translations** | Verifica se as traduções upstream mudaram; remerge e commita quando sim. | Atualizações de tradução |
| **Translate enhancement texts** | Aplica os glossários e rebuilda as variantes `*_all*`. | Após editar glossários |
| **Promote build to main** | Abre PR (ou auto-merge) `build/{p4cl}` → `main` quando o build já está LIVE. | PTU → LIVE |

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
# Git Bash / Linux / macOS:
./bootstrap.sh        # cria .micromamba env e baixa Smart Citizen (uma vez)
```

## Uso — Windows (PowerShell nativo, sem Git Bash)

O ponto de entrada principal no Windows é `run-local.ps1`:

```powershell
# Auto-detecta Data.p4k do RSI Launcher, processa todas as línguas:
.\run-local.ps1

# Só PT-BR:
.\run-local.ps1 -Lang portuguese_br

# Caminho explícito (PTU, por exemplo):
.\run-local.ps1 -P4k "E:\StarCitizen\PTU\Data.p4k"

# Pipeline + criar PR automaticamente:
.\run-local.ps1 -Pr

# Reutilizar extração anterior (patch de tradução, sem re-extrair o p4k):
.\run-local.ps1 -SkipExtract

# Só remerge (mais rápido; útil quando apenas as traduções mudaram):
.\run-local.ps1 -SkipExtract -SkipGenerate
```

Após o push, dispare o workflow **Build from local extraction** no GitHub
para baixar as traduções comunitárias atualizadas e abrir o PR automaticamente.

## Uso — Linux / macOS / Git Bash

```bash
# Pipeline completo a partir do p4k local:
./run.sh --p4k "/path/to/StarCitizen/LIVE/Data.p4k"
./run.sh --p4k "..." --lang portuguese_br   # língua única

# Só remerge (CI mode):
python run_pipeline.py --skip-extract --skip-generate

# Tradução por glossário (*_all* variants):
./translate.sh

# Branch build/{p4cl} + PR:
python create_pr.py

# Tudo de uma vez:
./runall.sh --p4k "/path/to/StarCitizen/LIVE/Data.p4k"
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
