#!/data/data/com.termux/files/usr/bin/env python
"""Remove ``.github`` folders and ``.gitignore`` files from a directory that is not a git repository.
Aborts when the current directory is a repository, otherwise it walks the tree and deletes leftover git metadata files and folders, reporting every removal.
Usage: dj2 Examples: $ cd archived_project && dj2 Runs as a script (``python dj2.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import shutil


def is_git_repo_simple(folder_path: str | Path) -> bool:
    try:
        target_dir = Path(folder_path).expanduser().resolve()
        return target_dir.is_dir() and (target_dir / ".git").is_dir()
    except (PermissionError, OSError) as e:
        print(f"Error accessing path: {e}")
        return False


def clean_non_git_dir() -> None:
    current_dir = Path.cwd().resolve()

    if is_git_repo_simple(current_dir):
        print(f"Abort: '{current_dir}' is a Git repository. No files were deleted.")
        return

    print(f"Cleaning non-git directory: {current_dir}\n")

    try:
        for path in list(current_dir.rglob("*")):
            if path.is_dir() and path.name == ".github":
                try:
                    shutil.rmtree(path)
                    print(f"Removed directory: {path.relative_to(current_dir)}")
                except Exception as e:
                    print(f"Failed to remove directory {path}: {e}")

            elif path.is_file() and path.name == ".gitignore":
                try:
                    path.unlink()
                    print(f"Removed file:      {path.relative_to(current_dir)}")
                except Exception as e:
                    print(f"Failed to remove file {path}: {e}")

    except PermissionError as e:
        print(f"Permission error scanning directory: {e}")
    except Exception as e:
        print(f"An unexpected error occurred during cleanup: {e}")


if __name__ == "__main__":
    clean_non_git_dir()
