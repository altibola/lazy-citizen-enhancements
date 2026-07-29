#!/usr/bin/env python3
"""create_pr.py — Automatiza commits de novas versões e abertura de Pull Requests.

Este script:
  1. Detecta a versão mais recente gerada no diretório out/.
  2. Verifica se há arquivos modificados ou não rastreados no Git (data/ e out/).
  3. Cria um branch de versão (ex: version-11952564).
  4. Realiza o commit das alterações das traduções e estatísticas geradas.
  5. Realiza o push do novo branch para a origem.
  6. Tenta abrir um Pull Request (PR) automaticamente:
     - Usando o CLI 'gh' (se disponível e autenticado).
     - Usando a API REST do GitHub (se GITHUB_TOKEN estiver nas variáveis de ambiente).
     - Imprimindo uma URL direta de comparação para abertura do PR com um clique.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.request
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Executa um comando Git e retorna o resultado."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return subprocess.run(
        ["git"] + args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=check,
        env=env
    )


def get_current_branch() -> str:
    """Retorna o nome do branch atual."""
    res = run_git(["branch", "--show-current"])
    return res.stdout.strip() or "live"


def infer_version_and_environment() -> tuple[str | None, str | None]:
    """Retorna a versão e o ambiente a partir de enhancements/version.json."""
    for base in (REPO_ROOT / "enhancements", REPO_ROOT / "out" / "enhancements"):
        version_file = base / "version.json"
        if version_file.exists():
            try:
                data = json.loads(version_file.read_text(encoding="utf-8"))
                return data.get("version"), data.get("environment")
            except Exception:
                pass
    return None, None


def parse_repo_info() -> tuple[str, str] | None:
    """Obtém o proprietário (owner) e repositório (repo) das configurações do git remote origin."""
    try:
        res = run_git(["remote", "get-url", "origin"])
        url = res.stdout.strip()
        # Regex para lidar com formatos HTTPS e SSH do GitHub:
        # Ex: https://github.com/altibola/lazy-citizen-enhancements.git
        # Ex: git@github.com:altibola/lazy-citizen-enhancements.git
        match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
        if match:
            return match.group(1), match.group(2)
    except Exception:
        pass
    return None


def gh_cli_available() -> bool:
    """Verifica se o CLI 'gh' está instalado e autenticado."""
    try:
        res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False


def create_github_pr_api(owner: str, repo: str, head_branch: str, base_branch: str, title: str, body: str, token: str) -> bool:
    """Cria um Pull Request usando a API do GitHub via urllib."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    data = json.dumps({
        "title": title,
        "head": head_branch,
        "base": base_branch,
        "body": body,
        "draft": False
    }).encode("utf-8")
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "LCE-PR-Automator/1.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            res_data = json.loads(resp.read().decode())
            print(f"\n✓ Pull Request criado via API do GitHub: {res_data.get('html_url')}")
            return True
    except Exception as e:
        print(f"\n⚠ Falha ao criar Pull Request via API do GitHub: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="create_pr",
        description="Automatiza commit e Pull Request de novas traduções/estatísticas do Star Citizen."
    )
    parser.add_argument("--game-version", default=None,
                        help="Override da versão do jogo (detectado automaticamente se omitido).")
    parser.add_argument("--branch-prefix", default="build/",
                        help="Prefixo do branch do Git (default: 'build/').")
    parser.add_argument("--base-branch", default=None,
                        help="Branch base para o PR (default: 'main' para LIVE, 'main' para PTU também).")
    parser.add_argument("--force", action="store_true",
                        help="Força a criação do PR mesmo se não houver alterações locais (ex: para re-push).")
    
    args = parser.parse_args()
    
    # 1. Determina a versão do jogo e o ambiente
    game_version = args.game_version
    environment = "LIVE"
    
    version_json, env_json = infer_version_and_environment()
    if version_json:
        if not game_version:
            game_version = version_json
        environment = env_json or "LIVE"
        
    if not game_version:
        print("Erro: Não foi possível inferir a versão do jogo a partir de version.json.")
        print("Certifique-se de executar o pipeline './run.sh' ou passar '--game-version <versão>' manualmente.")
        return 1
        
    print(f"Versão do jogo identificada: {game_version}")
    print(f"Ambiente identificado: {environment}")
    
    # 2. Verifica alterações locais
    status_res = run_git(["status", "--porcelain"])
    changes = status_res.stdout.strip()
    
    if not changes and not args.force:
        print("Nenhuma alteração local detectada para commitar.")
        return 0
        
    # 3. Determina o branch base e o novo branch
    # Convention: build/{p4cl} for every build (PTU or LIVE).
    # PRs always target "main" — for PTU builds that means "ready for review
    # when promoted"; for LIVE re-runs, it merges directly.
    base_branch = args.base_branch or "main"
    new_branch = f"{args.branch_prefix}{game_version}"
    
    print(f"Branch base: {base_branch}")
    print(f"Novo branch: {new_branch}")
    
    # 4. Cria/Troca para o branch da versão
    print(f"\nCriando/Mudando para o branch '{new_branch}'...")
    # Força a recriação do branch a partir do ponto atual mantendo arquivos não-comitados
    run_git(["checkout", "-B", new_branch])
        
    # 5. Adiciona e commita as alterações
    print("Commitando modificações...")
    run_git(["add", ".gitignore", "README.md"], check=False)
    if (REPO_ROOT / "VERSIONS.md").exists():
        # Manifesto de versões: originais das traduções + inputs do gerador
        run_git(["add", "VERSIONS.md"], check=False)
    if (REPO_ROOT / "data").exists():
        run_git(["add", "data/"], check=False)
    if (REPO_ROOT / "enhancements").exists():
        # Git adicionará apenas os arquivos não ignorados (base_en.ini, version.json e enhancements)
        run_git(["add", "enhancements/"], check=False)
    if (REPO_ROOT / "translations").exists():
        # Glossários, overrides e relatório de pendências da tradução dos melhoramentos
        run_git(["add", "translations/"], check=False)

    commit_msg = f"Update translations and stats for Star Citizen {game_version}"
    run_git(["commit", "-m", commit_msg], check=False)
    
    # 6. Realiza o push para a origem
    print(f"Realizando push para origin/{new_branch}...")
    push_res = run_git(["push", "-u", "origin", new_branch], check=False)
    if push_res.returncode != 0:
        # Se falhar porque a branch divergiu, tenta force push seguro
        push_res = run_git(["push", "--force-with-lease", "origin", new_branch], check=False)
    if push_res.returncode != 0:
        print(f"Erro ao empurrar o branch: {push_res.stderr}")
        return 1
        
    # 7. Abre o Pull Request
    repo_info = parse_repo_info()
    env_label = environment.upper()
    ptu_note = (
        "\n\n> ⚠️ This branch was generated from **PTU**. "
        "Merge to `main` only after this build ships to **LIVE**."
        if env_label not in ("LIVE",) else ""
    )
    pr_title = f"[{env_label}] Translations + stats for build {game_version}"
    pr_body = (
        f"Automated PR with community translations and auto-generated stat "
        f"enhancements for Star Citizen build `{game_version}` ({env_label})."
        f"{ptu_note}\n\n"
        f"Generated by **lazy-citizen-enhancements**."
    )
    
    # Tentativa 1: GitHub CLI (gh)
    if gh_cli_available():
        print("\nTentando criar Pull Request usando GitHub CLI...")
        pr_cmd = [
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_body,
            "--base", base_branch,
            "--head", new_branch
        ]
        res = subprocess.run(pr_cmd, capture_output=True, text=True)
        if res.returncode == 0:
            print(res.stdout.strip())
            return 0
        else:
            print(f"CLI gh retornou erro: {res.stderr.strip()}")
            
    # Tentativa 2: API do GitHub (com GITHUB_TOKEN)
    token = os.environ.get("GITHUB_TOKEN")
    if token and repo_info:
        owner, repo = repo_info
        print("\nGITHUB_TOKEN encontrado. Tentando criar Pull Request via API do GitHub...")
        if create_github_pr_api(owner, repo, new_branch, base_branch, pr_title, pr_body, token):
            return 0
            
    # Tentativa 3 (Fallback): Gerar URL de Comparação
    if repo_info:
        owner, repo = repo_info
        comparison_url = f"https://github.com/{owner}/{repo}/compare/{base_branch}...{new_branch}?expand=1"
        print("\n" + "=" * 80)
        print("Branch enviado com sucesso!")
        print("Para abrir o Pull Request manualmente, acesse o link abaixo:")
        print(comparison_url)
        print("=" * 80 + "\n")
    else:
        print(f"\nBranch '{new_branch}' enviado com sucesso!")
        print("Não foi possível gerar a URL de comparação (repositório remoto não identificado).")
        
    return 0


if __name__ == "__main__":
    sys.exit(main())
