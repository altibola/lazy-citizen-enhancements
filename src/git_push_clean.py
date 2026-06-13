#!/usr/bin/env python3
"""git_push_clean.py — Push commits to origin while squashing history to a single parentless commit.

Ensures the remote branch is always a single commit reflecting the local state,
with no prior history.
"""
from __future__ import annotations

import subprocess
import sys

def run_git(args: list[str]) -> str:
    res = subprocess.run(["git"] + args, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        print(f"Git command failed: git {' '.join(args)}", file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        sys.exit(res.returncode)
    return res.stdout.strip()

def main() -> int:
    # 1. Check if there are any commits
    try:
        msg = run_git(["log", "-1", "--pretty=%B"])
    except Exception:
        print("Error: No commits found or not in a git repository.", file=sys.stderr)
        return 1

    # 2. Get current branch name
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        print("Error: Detached HEAD state. Please switch to a branch first.", file=sys.stderr)
        return 1

    # 3. Create a parentless commit representing the current tree
    # git commit-tree HEAD^{tree} -m "<message>"
    tree_sha = run_git(["rev-parse", "HEAD^{tree}"])
    new_commit = run_git(["commit-tree", tree_sha, "-m", msg])

    # 4. Hard reset current branch to the parentless commit
    run_git(["reset", "--hard", new_commit])

    # 5. Force push to remote
    print(f"Force-pushing clean (parentless) branch '{branch}' to origin...")
    # Pass any additional command line arguments to git push
    push_args = ["push", "-f", "origin", branch] + sys.argv[1:]
    # Remove duplicates or conflicts if any
    unique_push_args = []
    for arg in push_args:
        if arg not in unique_push_args:
            unique_push_args.append(arg)
    run_git(unique_push_args)
    print("Push completed successfully!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
