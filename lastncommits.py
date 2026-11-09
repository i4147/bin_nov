#!/data/data/com.termux/files/usr/bin/env python
"""List the files that were added during the last N commits.
Runs ``git log -n N --name-status --diff-filter=A`` in the current repository, skips symlinks and prints the paths of the created files.
Usage: lastncommits N Examples: $ lastncommits 10 Runs as a script (``python lastncommits.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import subprocess
import sys


def get_created_files(n_commits: int) -> list:
    try:
        cmd = [
            "git",
            "log",
            "-n",
            str(n_commits),
            "--pretty=format:",
            "--name-status",
            "--diff-filter=A",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        created_files = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parts = line.split("\t")
                if len(parts) >= 2:
                    path = parts[1]
                    path = Path(path)
                    if path.is_symlink():
                        continue
                    created_files.append(path)
        return created_files
    except subprocess.CalledProcessError as e:
        print(f"✗ Git command failed: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 list_commits.py <N>")
        print("       where N is the number of commits to analyze")
        print("\nExamples:")
        print("  python3 list_commits.py 5")
        print("  python3 list_commits.py 10")
        sys.exit(1)
    try:
        n_commits = int(sys.argv[1])
        if n_commits <= 0:
            print("✗ Error: Number of commits must be positive", file=sys.stderr)
            sys.exit(1)
        created_files = get_created_files(n_commits)
        if created_files:
            for path in created_files:
                print(path)
        else:
            print(
                f"No files created in the last {n_commits} commit(s)",
                file=sys.stderr,
            )
    except ValueError:
        print(f"✗ Error: '{sys.argv[1]}' is not a valid integer", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    raise SystemExit(main())
