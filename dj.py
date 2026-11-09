#!/data/data/com.termux/files/usr/bin/env python
"""Delete junk files from directories that are not under version control.
Reads extra junk names from ``/sdcard/data/junk``, combines them with the built-in suffix list (``.pyc``, ``.tmp``, ``.log``, ``.bak``, ``.exe``, ``.dll`` ...) and removes matching files and caches.
Git metadata (``.gitignore``, ``.gitkeep``, ``.github``) is preserved and nothing is touched inside a repository unless the rules allow it.
Usage: dj [paths ...] Examples: $ dj ~/tmp Runs as a script (``python dj.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import shutil
import sys

from rignore import walk as _walk


JUNK_FILE = Path("/sdcard/data/junk")

JUNK_SUFFIXES = (
    ".pyc",
    ".tmp",
    ".log",
    ".bak",
    ".cache.json",
    ".exe",
    ".bat",
    ".dll",
    ".dylib",
    ".pyd",
    ".lnk",
    ".msi",
)

GIT_META_FILES = frozenset({".gitkeep", ".gitmodules", ".gitignore"})
GIT_META_DIRS = frozenset({".github"})


def load_junk_names(path: Path = JUNK_FILE) -> frozenset[str]:
    try:
        with path.open(encoding="utf-8") as fh:
            return frozenset(line.strip().lower() for line in fh if line.strip())
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return frozenset()


_git_cache: dict[Path, bool] = {}


def in_git_repo(path: Path) -> bool:
    chain: list[Path] = []
    result = False
    for parent in path.parents:
        cached = _git_cache.get(parent)
        if cached is not None:
            result = cached
            break
        chain.append(parent)
        if (parent / ".git").exists():
            result = True
            break
    for parent in chain:
        _git_cache[parent] = result
    return result


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def main() -> int:
    cwd = Path.cwd()
    junk_names = load_junk_names()
    removed_count = 0

    for path in _walk(cwd):
        if not path.exists() or path.is_symlink() or ".git" in path.parts:
            continue

        loname = path.name.lower()

        if loname in GIT_META_FILES or (loname in GIT_META_DIRS and path.is_dir()):
            if not in_git_repo(path):
                remove(path)
                removed_count += 1
                print(f"{path.name} removed")
            continue

        if path.is_file() and (loname.endswith(JUNK_SUFFIXES) or loname in junk_names):
            path.unlink()
            removed_count += 1
            print(f"{path.name} removed")
            continue

        if path.is_dir() and loname == "licenses" and "dist-info" in path.parent.name:
            shutil.rmtree(path)
            removed_count += 1
            print(f"{path.name} removed")
            continue

    if removed_count:
        print(f"\n{removed_count} item(s) removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
