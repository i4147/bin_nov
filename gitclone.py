#!/data/data/com.termux/files/usr/bin/env python
"""Clone a git repository, fetching submodules when it has any.
Clones into the directory named after the repository (``--recursive`` is added when the remote advertises submodules), reports the default branch and exits with the status of ``git clone``.
Usage: gitclone URL [DESTINATION] Examples: $ gitclone https://github.com/user/repo.git Runs as a script (``python gitclone.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import re
import subprocess
import sys
from typing import Any


def _default_branch(url: str) -> str | None:
    result = subprocess.run(
        ["git", "ls-remote", "--symref", url, "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        m = re.match(r"^ref:\s+refs/heads/(\S+)\s+HEAD", line)
        if m:
            return m.group(1)
    return None


def _has_submodules(repo_dir: Path) -> bool:
    rc = subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "-e", "HEAD:.gitmodules"],
        capture_output=True,
    ).returncode
    return rc == 0


def git_clone2(*args: Any) -> int:
    if not args:
        print("Usage: g2 <repo-url> [target-dir]")
        return 1
    url = args[0]
    target = args[1] if len(args) > 1 else ""
    branch = _default_branch(url)
    if not branch:
        print(f"❌ Could not determine default branch for {url}", file=sys.stderr)
        return 1
    repo = Path(url.rstrip("/")).name
    repo = repo.removesuffix(".git")
    if not target:
        target = f"{repo}.git"
    print(f"🔍 Default branch: {branch}")
    print(f"📦 Cloning only '{branch}' (with submodules) from {url} into {target} ...")
    rc = subprocess.run([
        "git",
        "clone",
        "--single-branch",
        "--branch",
        branch,
        "--bare",
        "--recurse-submodules",
        url,
        target,
    ]).returncode
    if rc != 0:
        return rc
    target_path = Path(target)
    has_subs = _has_submodules(target_path)
    print("✅ Done!")
    print(f"   To update later: cd {target} && git fetch origin {branch}")
    if has_subs:
        print(f"   Update submodules: git submodule update --init --recursive --remote")
    return 0


if __name__ == "__main__":
    sys.exit(git_clone2(*sys.argv[1:]))
