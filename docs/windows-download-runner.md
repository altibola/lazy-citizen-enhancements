# Windows download runner (`download-on-windows.yml`)

On-demand GitHub Actions workflow that runs the **whole download build** on a
GitHub-hosted `windows-latest` runner — no Star Citizen install and no local
`Data.p4k` required.

It authenticates to RSI and pulls the inputs straight from the CDN
(`run_pipeline.py --download`):

```
RSI auth → global.ini + Game2.dcb (CDN) → unforge → generate → merge
         → [translate] → [branch build/{p4cl} + PR]
```

`windows-latest` is used on purpose: `unforge.exe` is a .NET Framework 4.x
executable and that runtime ships with the hosted Windows image, so the
DataForge step works with no extra setup. (The existing
`build-on-windows.yml` is the *self-hosted* variant that reuses a local game
install; this one is fully self-contained.)

## Credentials (read from GitHub repo settings)

Settings → Secrets and variables → Actions:

| Kind     | Name           | Value                | Why |
|----------|----------------|----------------------|-----|
| Variable | `RSI_USERNAME` | RSI account email    | non-secret → repository **Variable** |
| Secret   | `RSI_PASSWORD` | RSI account password | sensitive → repository **Secret** |

The username is exposed as a plaintext repository **Variable** and the password
as an encrypted **Secret** — a password must never be stored as a plaintext
Variable. Both reach the pipeline as the `RSI_USERNAME` / `RSI_PASSWORD`
environment variables that `run_pipeline.py` already reads.

If the RSI account has MFA enabled, pass the current 6-digit code in the
`rsi_mfa_code` dispatch input (TOTP codes are short-lived).

## How to run

Actions → **Download on Windows (hosted)** → *Run workflow*. Inputs:
`channel` (LIVE/PTU/EPTU), optional `lang`, optional `rsi_mfa_code`,
`translate` (default true), `push_branch` (default true).

## Installing the workflow file

> [!IMPORTANT]
> This automated branch could **not** commit a file under
> `.github/workflows/` because the integration token lacks the GitHub
> `workflow` scope (both `git push` and the GitHub API returned a
> permission error for that path only). Add the file with an account/token
> that has the `workflow` scope, by saving the YAML below as
> `.github/workflows/download-on-windows.yml`.

Note: `workflow_dispatch` only becomes runnable once this file exists on the
repository's **default branch** (`main`).

```yaml
name: Download on Windows (hosted)

# ON-DEMAND ONLY (workflow_dispatch) — runs the FULL download build on a
# GitHub-hosted Windows runner (windows-latest).
#
# Unlike build-on-windows.yml (self-hosted, needs Star Citizen installed and
# a local Data.p4k), this target needs NO local game files. It authenticates
# to RSI and pulls the inputs straight from the CDN (--download):
#
#   RSI auth → global.ini + Game2.dcb (CDN) → unforge → generate → merge
#            → [translate] → [branch build/{p4cl} + PR]
#
# windows-latest is used on purpose: unforge.exe is a .NET Framework 4.x
# executable and that runtime ships with the hosted Windows image, so the
# DataForge step works out of the box with no extra setup.
#
# Credentials come from the repository's GitHub settings
# (Settings → Secrets and variables → Actions):
#   • Variable  RSI_USERNAME   RSI account email   (non-secret → repo Variable)
#   • Secret    RSI_PASSWORD   RSI account password (sensitive  → repo Secret)
#
# The username is a plaintext repository Variable and the password an encrypted
# Secret — a password must never be stored as a plaintext Variable.
#
# If the RSI account has MFA enabled, pass the current 6-digit code in the
# rsi_mfa_code input when dispatching (TOTP codes are short-lived; the signed
# session is cached on the ephemeral runner only for this run).

on:
  workflow_dispatch:
    inputs:
      channel:
        description: 'Game channel to download from'
        required: true
        default: 'LIVE'
        type: choice
        options: ['LIVE', 'PTU', 'EPTU']
      lang:
        description: 'Single language to process (default: all, incl. english)'
        required: false
        default: ''
      rsi_mfa_code:
        description: 'Current RSI MFA code (only if your account requires it)'
        required: false
        default: ''
      translate:
        description: 'Also run the glossary translation (*_all* variants)'
        required: false
        default: 'true'
        type: choice
        options: ['true', 'false']
      push_branch:
        description: 'Push branch build/{p4cl} and open the PR'
        required: false
        default: 'true'
        type: choice
        options: ['true', 'false']

concurrency:
  group: windows-download   # one download build at a time (shared caches)
  cancel-in-progress: false

jobs:
  download-build:
    runs-on: windows-latest
    timeout-minutes: 120

    permissions:
      contents: write
      pull-requests: write

    defaults:
      run:
        shell: bash          # Git Bash ships with the windows image — all repo scripts target it

    steps:
      # ── 1. Checkout (credentials persisted for the push in step 6) ──────────
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      # ── 2. Fail fast if the RSI credentials aren't configured ───────────────
      - name: Verify RSI credentials are configured
        env:
          RSI_USERNAME: ${{ vars.RSI_USERNAME }}
          RSI_PASSWORD: ${{ secrets.RSI_PASSWORD }}
        run: |
          if [ -z "$RSI_USERNAME" ]; then
            echo "::error::RSI_USERNAME repository variable is not set (Settings → Secrets and variables → Actions → Variables)."
            exit 1
          fi
          if [ -z "$RSI_PASSWORD" ]; then
            echo "::error::RSI_PASSWORD repository secret is not set (Settings → Secrets and variables → Actions → Secrets)."
            exit 1
          fi
          echo "RSI credentials present — proceeding."

      # ── 3. Cache the micromamba env + Smart Citizen tools across runs ───────
      - name: Cache micromamba env + Smart Citizen
        uses: actions/cache@v4
        with:
          path: |
            .micromamba
            .smart-citizen
          key: lce-win-${{ hashFiles('environment.yml', 'setup_smart_citizen.sh') }}
          restore-keys: |
            lce-win-

      # ── 4. Bootstrap env (skipped when restored from cache) ─────────────────
      - name: Bootstrap micromamba env + Smart Citizen
        run: |
          if [ ! -f .micromamba/bin/micromamba.exe ]; then
            echo "No cached env — bootstrapping..."
            chmod +x bootstrap.sh
            ./bootstrap.sh
          else
            echo ".micromamba present (cache hit) — refreshing Smart Citizen only."
          fi
          chmod +x setup_smart_citizen.sh run.sh translate.sh runall.sh
          ./setup_smart_citizen.sh

      # ── 5. Pipeline: RSI CDN download + unforge + generate + merge ──────────
      - name: Run pipeline (RSI CDN download)
        env:
          PYTHONIOENCODING: utf-8
          RSI_USERNAME: ${{ vars.RSI_USERNAME }}
          RSI_PASSWORD: ${{ secrets.RSI_PASSWORD }}
        run: |
          ARGS=(--download --channel "${{ inputs.channel }}")
          if [ -n "${{ inputs.lang }}" ]; then
            ARGS+=(--lang "${{ inputs.lang }}")
          fi
          if [ -n "${{ inputs.rsi_mfa_code }}" ]; then
            ARGS+=(--rsi-mfa-code "${{ inputs.rsi_mfa_code }}")
          fi
          ./run.sh "${ARGS[@]}"

      # ── 6. Glossary translation (*_all* variants) ───────────────────────────
      - name: Translate enhancement texts
        if: inputs.translate == 'true'
        run: ./translate.sh
        env:
          PYTHONIOENCODING: utf-8

      # ── 7. Branch build/{p4cl} + PR ─────────────────────────────────────────
      - name: Push build branch and open PR
        if: inputs.push_branch == 'true'
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          .micromamba/envs/lce/python.exe create_pr.py
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      # ── 8. Version manifest in the run summary ──────────────────────────────
      - name: Publish version manifest to run summary
        if: always()
        run: |
          if [ -f VERSIONS.md ]; then
            cat VERSIONS.md >> "$GITHUB_STEP_SUMMARY"
          fi
```

## Validation performed

- Workflow YAML parses and has the expected `workflow_dispatch` inputs, job,
  `runs-on: windows-latest`, and step list.
- The CLI flags / env vars the workflow relies on
  (`--download`, `--channel`, `--lang`, `--rsi-mfa-code`, and the
  `RSI_USERNAME` / `RSI_PASSWORD` env reads) all exist in `run_pipeline.py`.
- The RSI download module imports and its auth flow executes end-to-end up to
  the network call (verified with throwaway credentials). A full live run
  requires real RSI credentials and open egress to
  `robertsspaceindustries.com`, which a hosted runner provides.
