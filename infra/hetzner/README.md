# Hetzner build host

Ephemeral Hetzner Cloud server that runs the **full** pipeline — RSI CDN
download (`--download`), DataForge unforge (via mono), enhancement generation
and merge — then pushes the `build/{p4cl}` branch and opens the PR.

The server exists only for the duration of one build: **provision → build →
destroy**. Cost at the default `cpx31` (4 vCPU / 8 GB): ~0.03 EUR/h; a full
build typically stays well under one hour.

## Two ways to run

### A. GitHub Actions (recommended)

Workflow **Build on Hetzner** (`.github/workflows/build-on-hetzner.yml`),
`workflow_dispatch` only — no schedule.

Required repository secrets:

| Secret | Purpose |
|---|---|
| `HCLOUD_TOKEN` | Hetzner Cloud API token (project, read+write) |
| `RSI_USERNAME` | RSI account login |
| `RSI_PASSWORD` | RSI account password |

Dispatch inputs: `channel` (LIVE/PTU/EPTU), `rsi_mfa_code` (only if your
account has MFA — codes are short-lived, enter the current one), `branch`,
`server_type`, `location`. The server is **always destroyed**, even when the
build fails (`if: always()`).

### B. Locally from Git Bash

All scripts in `scripts/` are plain bash and run unchanged in Git Bash on
Windows (and on Linux). You need `terraform` and `ssh` on your `PATH`
(Git Bash already ships ssh; install Terraform from hashicorp.com).

```bash
cd infra/hetzner

# 1. Create the server (generates .build-ssh-key on first use)
HCLOUD_TOKEN=xxxx scripts/provision.sh

# 2. Run the build on it
RSI_USERNAME=you@mail.com \
RSI_PASSWORD=yyyy \
RSI_MFA_CODE=123456 \
SC_CHANNEL=PTU \
GITHUB_TOKEN=ghp_zzz \
scripts/run_build.sh

# 3. Tear it down (do not skip — the server bills hourly)
HCLOUD_TOKEN=xxxx scripts/destroy.sh
```

Notes for Git Bash:
- `run_build.sh` sets `MSYS_NO_PATHCONV=1` so MSYS does not rewrite remote
  paths like `/opt/lce`.
- Terraform state stays local in this directory (gitignored). In CI the state
  is ephemeral per run — the destroy step runs in the same job, so no remote
  state backend is needed.

## What's on the server (cloud-init)

- `python3` + venv (pipeline; `lxml`/`zstandard` installed per-run)
- `git` (repo + smart-citizen clone)
- `mono-complete` + `binfmt-support` — `unforge.exe`/`unp4k.exe` are .NET
  Framework binaries; binfmt registration lets `pak_extract.py` exec them
  directly on Linux.

## Version traceability

The build regenerates `VERSIONS.md` (game build, original community
translation commits, generator inputs/outputs) and `create_pr.py` commits it
to the `build/{p4cl}` branch. The Actions run summary also shows the manifest
after a successful build.

## Files

| File | Role |
|---|---|
| `main.tf` / `variables.tf` / `outputs.tf` | Terraform: server + SSH key + firewall |
| `cloud-init.yml` | Package bootstrap (python, mono, git) |
| `scripts/provision.sh` | `terraform init` + `apply` (generates SSH key) |
| `scripts/run_build.sh` | Waits for cloud-init, pipes `remote_build.sh` over SSH |
| `scripts/remote_build.sh` | Runs ON the server: clone → pipeline `--download` → PR |
| `scripts/destroy.sh` | `terraform destroy` |
