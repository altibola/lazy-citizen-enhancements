# Download build on a hosted runner (`download-build.yml`)

On-demand GitHub Actions workflow that runs the **whole download build** on a
GitHub-hosted runner — **`ubuntu-latest` (default) or `windows-latest`**,
chosen at dispatch time. No Star Citizen install and no local `Data.p4k`
required. It replaces the former Hetzner-based build (the `infra/hetzner/`
Terraform setup and the *Build on Hetzner* workflow were removed).

It authenticates to RSI and pulls the inputs straight from the CDN
(`run_pipeline.py --download`):

```
RSI auth → global.ini + Game2.dcb (CDN) → unforge → generate → merge
         → [translate] → [branch build/{p4cl} + PR]
```

Platform handling:

- **Windows**: uses the `unp4k.exe`/`unforge.exe` bundled with the Smart
  Citizen checkout (.NET Framework 4.x ships with the hosted Windows image).
- **Linux**: `setup_tools.py` (run by `setup_smart_citizen.sh`) downloads the
  Linux DLL builds from [dolkensp/unp4k](https://github.com/dolkensp/unp4k/releases)
  and `pak_extract.py` runs them via the `dotnet` runtime. Current unp4k
  builds target **.NET 10**; the workflow installs that runtime when the
  image doesn't already have it.

## Credentials (GitHub Settings → Secrets and variables → Actions)

| Name           | Kind                   | Value                |
|----------------|------------------------|----------------------|
| `RSI_PASSWORD` | **Secret** (mandatory) | RSI account password |
| `RSI_USERNAME` | **Secret** *or* Variable | RSI account email  |

The password **must** be a Secret (encrypted, masked in logs). The username
is not strictly sensitive, so a plaintext repository Variable works — but a
Secret is also accepted and keeps it out of logs entirely; the workflow
checks `secrets.RSI_USERNAME` first and falls back to `vars.RSI_USERNAME`.
Both reach the pipeline as the `RSI_USERNAME` / `RSI_PASSWORD` environment
variables that `run_pipeline.py` reads.

If the RSI account has MFA enabled, pass the current 6-digit code in the
`rsi_mfa_code` dispatch input (TOTP codes are short-lived).

## How to run

Actions → **Download build (hosted)** → *Run workflow*. Inputs:
`os` (ubuntu-latest/windows-latest), `channel` (LIVE/PTU/EPTU), optional
`lang`, optional `rsi_mfa_code`, `translate` (default true), `push_branch`
(default true).

## Installing the workflow file

> [!IMPORTANT]
> The automation that produced this branch cannot commit files under
> `.github/workflows/` (the integration token lacks the GitHub `workflow`
> scope). Two manual steps remain, both needing an account/token with that
> scope:
>
> 1. **Add** the YAML below as `.github/workflows/download-build.yml`.
> 2. **Delete** `.github/workflows/build-on-hetzner.yml` (the Hetzner infra
>    it drove no longer exists on this branch). The `HCLOUD_TOKEN` repository
>    secret can be removed as well.

`workflow_dispatch` only becomes runnable once the file exists on the
repository's **default branch** (`main`).

```yaml
name: Download build (hosted)

# ON-DEMAND ONLY (workflow_dispatch) — runs the FULL download build on a
# GitHub-hosted runner, Linux (default) or Windows.
#
# No local game files needed: authenticates to RSI and pulls the inputs
# straight from the CDN (--download):
#
#   RSI auth → global.ini + Game2.dcb (CDN) → unforge → generate → merge
#            → [translate] → [branch build/{p4cl} + PR]
#
# Platform notes:
#   • windows-latest: unforge.exe is a .NET Framework 4.x executable; that
#     runtime ships with the hosted Windows image.
#   • ubuntu-latest: setup_tools.py fetches the Linux DLL builds of
#     unp4k/unforge (github.com/dolkensp/unp4k) and runs them via `dotnet`,
#     which is preinstalled on the hosted Ubuntu image (a fallback install
#     step below covers images without it).
#
# Credentials come from the repository's GitHub settings
# (Settings → Secrets and variables → Actions):
#   • Secret    RSI_PASSWORD   RSI account password — MUST be a Secret.
#   • Secret or Variable RSI_USERNAME — the account email. A Secret keeps it
#     out of logs entirely; a plaintext Variable also works (the expression
#     below checks the Secret first, then falls back to the Variable).
#
# If the RSI account has MFA enabled, pass the current 6-digit code in the
# rsi_mfa_code input when dispatching (TOTP codes are short-lived; the signed
# session is cached on the ephemeral runner only for this run).

on:
  workflow_dispatch:
    inputs:
      os:
        description: 'Runner OS'
        required: true
        default: 'ubuntu-latest'
        type: choice
        options: ['ubuntu-latest', 'windows-latest']
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
  group: download-build     # one download build at a time (shared caches)
  cancel-in-progress: false

jobs:
  download-build:
    runs-on: ${{ inputs.os }}
    timeout-minutes: 120

    permissions:
      contents: write
      pull-requests: write

    defaults:
      run:
        shell: bash          # native on Linux; Git Bash on the Windows image

    steps:
      # ── 1. Checkout (credentials persisted for the push in step 7) ──────────
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      # ── 2. Fail fast if the RSI credentials aren't configured ───────────────
      - name: Verify RSI credentials are configured
        env:
          RSI_USERNAME: ${{ secrets.RSI_USERNAME || vars.RSI_USERNAME }}
          RSI_PASSWORD: ${{ secrets.RSI_PASSWORD }}
        run: |
          if [ -z "$RSI_USERNAME" ]; then
            echo "::error::RSI_USERNAME is not set. Add it as a repository Secret (or Variable) under Settings → Secrets and variables → Actions."
            exit 1
          fi
          if [ -z "$RSI_PASSWORD" ]; then
            echo "::error::RSI_PASSWORD repository secret is not set (Settings → Secrets and variables → Actions → Secrets)."
            exit 1
          fi
          echo "RSI credentials present — proceeding."

      # ── 3. .NET runtime for unp4k/unforge (Linux only; no-op when present) ──
      # Current unp4k/unforge releases target .NET 10 — the version check
      # matters, not just the presence of `dotnet`.
      - name: Ensure dotnet 10 runtime (Linux)
        if: runner.os != 'Windows'
        run: |
          if command -v dotnet >/dev/null 2>&1 \
             && dotnet --list-runtimes 2>/dev/null | grep -q '^Microsoft\.NETCore\.App 10\.'; then
            echo "dotnet 10 runtime present."
          else
            echo "Installing .NET 10 runtime (required by current unp4k builds)..."
            curl -sSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh
            bash /tmp/dotnet-install.sh --channel 10.0 --runtime dotnet
            echo "DOTNET_ROOT=$HOME/.dotnet" >> "$GITHUB_ENV"
            echo "$HOME/.dotnet" >> "$GITHUB_PATH"
          fi

      # ── 4. Cache the micromamba env + Smart Citizen tools across runs ───────
      - name: Cache micromamba env + Smart Citizen
        uses: actions/cache@v4
        with:
          path: |
            .micromamba
            .smart-citizen
          key: lce-${{ inputs.os }}-${{ hashFiles('environment.yml', 'setup_smart_citizen.sh', 'setup_tools.py') }}
          restore-keys: |
            lce-${{ inputs.os }}-

      # ── 5. Bootstrap env (skipped when restored from cache) ─────────────────
      - name: Bootstrap micromamba env + Smart Citizen
        run: |
          if [ ! -f .micromamba/bin/micromamba ] && [ ! -f .micromamba/bin/micromamba.exe ]; then
            echo "No cached env — bootstrapping..."
            chmod +x bootstrap.sh
            ./bootstrap.sh
          else
            echo ".micromamba present (cache hit) — refreshing Smart Citizen only."
          fi
          chmod +x setup_smart_citizen.sh run.sh translate.sh runall.sh
          ./setup_smart_citizen.sh

      # ── 6. Pipeline: RSI CDN download + unforge + generate + merge ──────────
      - name: Run pipeline (RSI CDN download)
        env:
          PYTHONIOENCODING: utf-8
          RSI_USERNAME: ${{ secrets.RSI_USERNAME || vars.RSI_USERNAME }}
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

      # ── 7. Glossary translation (*_all* variants) ───────────────────────────
      - name: Translate enhancement texts
        if: inputs.translate == 'true'
        run: ./translate.sh
        env:
          PYTHONIOENCODING: utf-8

      # ── 8. Branch build/{p4cl} + PR ─────────────────────────────────────────
      - name: Push build branch and open PR
        if: inputs.push_branch == 'true'
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          PYBIN=.micromamba/envs/lce/bin/python
          [ -f .micromamba/envs/lce/python.exe ] && PYBIN=.micromamba/envs/lce/python.exe
          "$PYBIN" create_pr.py
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      # ── 9. Version manifest in the run summary ──────────────────────────────
      - name: Publish version manifest to run summary
        if: always()
        run: |
          if [ -f VERSIONS.md ]; then
            cat VERSIONS.md >> "$GITHUB_STEP_SUMMARY"
          fi
```

## Validation performed (in the automation sandbox, Linux)

- `setup_tools.py` downloaded the real Linux builds from the unp4k release
  (`unp4k-linux-x64-v4.0.87.zip`, `unforge-linux-x64-v4.0.87.zip`) — using the
  new API-less fallback (resolves the tag from the `releases/latest`
  redirect), since `api.github.com` was unreachable there.
- Both DLLs **execute** under `dotnet` (the .NET **10** runtime is required;
  .NET 8 refuses to load them — hence the workflow's version-checked install
  step).
- `run_pipeline.py --download` with `RSI_USERNAME`/`RSI_PASSWORD` env vars ran
  end-to-end up to the RSI sign-in network call (blocked only by the
  sandbox's egress policy; hosted runners have open egress).
- Workflow YAML validated (structure, inputs, per-step `if` conditions).
