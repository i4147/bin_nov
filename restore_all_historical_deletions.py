#!/data/data/com.termux/files/usr/bin/env python
"""Restore every file that was ever deleted in the repository history.
Reads ``git log --diff-filter=D`` to find the deletions and the commit that removed each file, checks out the version from the parent commit, stages it and finally commits everything with the message ``removed files``.
Usage: restore_all_historical_deletions Examples: $ restore_all_historical_deletions Runs as a script (``python restore_all_historical_deletions.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import subprocess
from typing import Any


def run_git_command(args: list[str]) -> str:
    try:
        result = subprocess.run(["git"] + args, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"❌ Git error executing {' '.join(e.cmd)}:\n{e.stderr.strip()}")
        return None


def main() -> None:
    repo_root = Path()
    if not (repo_root / ".git").exists() and not run_git_command(["rev-parse", "--is-inside-work-tree"]) == "true":
        print("❌ Error: Current directory is not a Git repository root.")
        return
    print("🔍 Analyzing repository history for all historical file deletions...")
    log_output = run_git_command(["log", "--diff-filter=D", "--pretty=format:%H", "--name-only"])
    if not log_output:
        print("🎉 No deleted files were found in this repository's entire history.")
        return
    deleted_files_map = {}
    current_commit_hash = None
    for line in log_output.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and " " not in line:
            current_commit_hash = line
        else:
            path = line
            if path not in deleted_files_map:
                deleted_files_map[path] = current_commit_hash
    files_to_restore = []
    for path_str, deletion_commit in deleted_files_map.items():
        if not Path(path_str).exists():
            files_to_restore.append((path_str, deletion_commit))
    if not files_to_restore:
        print("ℹ️  All historically deleted files are already active or restored in your workspace.")
        return
    print(f"⚠️  Found {len(files_to_restore)} historically deleted file(s) missing from your workspace.\n")
    restored_count = 0
    for path_str, deletion_commit in files_to_restore:
        print(f"🔄 Restoring: {path_str} (From commit prior to {deletion_commit[:8]})")
        try:
            run_git_command(["checkout", f"{deletion_commit}^", "--", path_str])
            run_git_command(["add", path_str])
            restored_count += 1
        except Exception:
            try:
                run_git_command(["checkout", deletion_commit, "--", path_str])
                run_git_command(["add", path_str])
                restored_count += 1
            except Exception as fallback_err:
                print(f"   ❌ Could not restore {path_str}: {fallback_err}")
    if restored_count > 0:
        print("\n💾 Committing restored files into the repository repository...")
        commit_output = run_git_command(["commit", "-m", "removed files"])
        print("\n📊 Git Commit Summary:")
        print(commit_output)
    else:
        print("\n❌ No files were successfully restored.")


if __name__ == "__main__":
    raise SystemExit(main())
