#!/data/data/com.termux/files/usr/bin/env python
"""Create and push a ``<month>_<year>`` branch in ``~/bin``.
Builds the branch name from the current month, checks whether it exists locally or on the remote, and otherwise fetches, updates ``main``, branches off it and pushes the new branch with upstream tracking before switching back to ``main``.
Usage: create_monthly_branch Examples: $ create_monthly_branch The repository path (``~/bin``) and base branch (``main``) are module constants; edit them for other layouts."""

from datetime import datetime
from pathlib import Path
import subprocess
import sys


REPO_PATH = Path.home() / "bin"
MAIN_BRANCH: str = "main"


def get_branch_name() -> str:
    now = datetime.now()
    return f"{now.strftime('%B').lower()}_{now.year}"


def branch_exists(branch_name: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "branch", "--list", branch_name],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
        )
        if branch_name in result.stdout:
            return True
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch_name],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
        )
        return f"refs/heads/{branch_name}" in result.stdout
    except Exception:
        return False


def create_monthly_branch() -> bool:
    branch_name = get_branch_name()
    print(f"Creating branch for {branch_name}...")
    if branch_exists(branch_name):
        print(f"✓ Branch '{branch_name}' already exists")
        return True
    try:
        subprocess.run(["git", "fetch", "--all"], cwd=REPO_PATH, check=True, capture_output=True)
        subprocess.run(
            ["git", "checkout", MAIN_BRANCH],
            cwd=REPO_PATH,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "pull"], cwd=REPO_PATH, check=True, capture_output=True)
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=REPO_PATH,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=REPO_PATH,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", MAIN_BRANCH],
            cwd=REPO_PATH,
            check=True,
            capture_output=True,
        )
        print(f"✓ Created and pushed branch: {branch_name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ Error: {e.stderr.decode() if e.stderr else str(e)}")
        return False


if __name__ == "__main__":
    if not (REPO_PATH / ".git").exists():
        print(f"Error: {REPO_PATH} is not a Git repository")
        sys.exit(1)
    success = create_monthly_branch()
    sys.exit(0 if success else 1)
