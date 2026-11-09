#!/data/data/com.termux/files/usr/bin/env python
"""Commit the current directory and push it to GitHub with one command.
Initializes a repository when needed, updates submodules, stages everything, commits with an ``Auto-commit: <timestamp>`` message and then pushes to the existing ``origin`` — creating a new public GitHub repository with ``gh repo create`` or forking the upstream when the current user does not own it.
Usage: gagc Examples: $ gagc Runs as a script (``python gagc.py``) in the directory it should act on; the module can also be imported."""

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys

from git import Repo


def run_command(command: list[str]) -> subprocess.CompletedProcess:
    print(f"Running: {' '.join(command)}")
    return subprocess.run(command, capture_output=True, text=True, check=True)


def auto_git_workflow() -> None:
    repo_path = os.getcwd()
    repo_name = Path(repo_path).name

    try:
        repo = Repo(repo_path)
        print(f"--- Detected repository: {repo_name} ---")
    except Exception:
        print("Not a git repository. Initializing...")
        repo = Repo.init(repo_path)
        repo.git.add(A=True)
        repo.index.commit(f"Initial commit: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if repo.submodules:
        print("Updating submodules...")
        repo.git.submodule("update", "--init", "--recursive")

    repo.git.add(A=True)
    commit_msg = f"Auto-commit: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    repo.index.commit(commit_msg)
    print(f"Committed changes with message: '{commit_msg}'")

    remotes = repo.remotes
    if not remotes:
        print("No remote configured. Creating new repository on GitHub...")
        try:
            run_command(["gh", "repo", "create", repo_name, "--public", "--source=.", "--push"])
        except subprocess.CalledProcessError:
            new_name = input("Repository exists. Enter a unique name for the new repo: ")
            run_command(["gh", "repo", "create", new_name, "--public", "--source=.", "--push"])
    else:
        origin = remotes[0].url
        user = subprocess.check_output(["gh", "api", "user", "--jq", ".login"]).decode().strip()

        if user not in origin:
            print("Not the owner. Forking repository...")
            run_command(["gh", "repo", "fork", "--remote", "true"])
            repo.git.push("fork", repo.active_branch)
        else:
            print("Pushing to existing remote...")
            repo.remotes.origin.push()

    print("Workflow completed successfully.")


if __name__ == "__main__":
    try:
        auto_git_workflow()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
