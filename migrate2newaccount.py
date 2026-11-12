#!/data/data/com.termux/files/usr/bin/env python
"""Re-publish every local repository under a new GitHub account.
For each repository below the current directory it stages and commits the pending changes, creates ``<NEW_USER>/<repo>`` as a private repository with ``gh repo create``, rewrites ``origin`` to the SSH URL of the new owner and pushes all branches and tags.
Usage: migrate2newaccount Examples: $ migrate2newaccount Runs as a script (``python migrate2newaccount.py``) in the directory it should act on; the module can also be imported.
"""

import os
from pathlib import Path
import subprocess

NEW_USER: str = "i4147"
IGNORE: set[str] = {"cpython", "neovim-source", ".git"}


def migrate() -> None:
    for repo in [d for d in os.listdir(".") if Path(d).is_dir() and d not in IGNORE]:
        os.chdir(repo)
        if not Path(".git").exists():
            os.chdir("..")
            continue
        subprocess.run(["git", "add", "."], capture_output=True)
        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if status.stdout.strip():
            subprocess.run(["git", "commit", "-m", "migration sync"], capture_output=True)
        subprocess.run(
            ["gh", "repo", "create", f"{NEW_USER}/{repo}", "--private"],
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                f"git@github.com:{NEW_USER}/{repo}.git",
            ],
            capture_output=True,
        )
        subprocess.run(["git", "push", "-u", "origin", "--all"], capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "--tags"], capture_output=True)
        os.chdir("..")


if __name__ == "__main__":
    migrate()
