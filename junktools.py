#!/data/data/com.termux/files/usr/bin/env python
"""Filesystem cleanup toolkit for OS junk, caches, empty files and twins.
Subcommands: ``os-junk`` removes the Windows/macOS leftovers, ``darwin`` the ``.DS_Store``-style files, ``caches`` the ``__pycache__``/``.mypy_cache``/``.pyc`` items, ``empty-files``/``empty-dirs`` the empty entries (with the optional keypress window) and ``twins`` the files that share a stem with another extension.
Usage: junktools {os-junk,darwin,caches,empty-files,empty-dirs,twins} [options] Positional arguments: directory path Subcommands: os-junk Clean_os_files.py darwin Cleanup_darwin.py caches Cnn.py empty-files Find_empty.py / rmempty.py empty-dirs Pdmp.py / pydmp.py / pydmp2.py twins Twin_files.py / twinfiles.py Options: -a, --auto-remove -v, --verbose --delete Rmempty.py behavior --timeout Abort window in seconds --dry-run --exclude-mode `pdmp` uses the original exclusion sets --include-root Pdmp.py appended the root itself to the candidate list --ext1 --ext2 --remove Which extension to delete when a twin exists (default 2) ...
plus 2 more; run ``junktools --help`` for the full list.
Examples: $ junktools caches -a ~/projects $ junktools empty-dirs --dry-run Command-line script: run ``python junktools.py`` or the ``junktools`` command.
The module is importable as well."""

import argparse
import contextlib
import multiprocessing
import os
from pathlib import Path
import select
import shutil
import sys
from typing import Any, Iterable, Iterator, Optional, Sequence


_WIN_EXTS: set[str] = {".exe", ".dll", ".bat", ".com", ".msi", ".vbs", ".ps1"}
_MAC_EXTS: set[str] = {".dmg", ".app", ".ds_store", ".plist", ".pkg"}
_DARWIN_NAMES: set[str] = {
    ".DS_Store",
    ".AppleDouble",
    ".LSOverride",
    ".TemporaryItems",
    ".Spotlight-V100",
    ".Trashes",
}
_DARWIN_WILDCARDS: tuple[str, str] = ("._*", ".com.apple.*")
_WIN_NAMES: tuple[str, ...] = (
    "*.exe",
    "*.dll",
    "*.sys",
    "*.bat",
    "*.cmd",
    "*.com",
    "*.msi",
    "*.scr",
    "*.lnk",
    "Thumbs.db",
    "desktop.ini",
    "$RECYCLE.BIN",
)
_CACHE_SUFFIXES: tuple[str, str, str] = (".pyc", ".log", ".bak")
_CACHE_DIRS: set[str] = {"__pycache__", ".ruff_cache", ".mypy_cache"}
_PDMP_NAME_EXCLUDES: set[str] = {"tmp", "cache", "bin", ".git", "etc", "config", "var"}
_PDMP_PART_EXCLUDES: set[str] = {".git", "tmp", "etc", "var", "config"}


def _human_size(num: float) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def _walk_recursive(root: Path) -> Iterator[Path]:
    try:
        for entry in root.iterdir():
            yield entry
            if entry.is_dir() and not entry.is_symlink():
                yield from _walk_recursive(entry)
    except (OSError, PermissionError) as exc:
        print(f"Error accessing {root}: {exc}", file=sys.stderr)


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _remove_path(path: Path) -> tuple[str, int]:
    try:
        if path.is_file():
            size = path.stat().st_size
            path.unlink()
            return str(path), size
        if path.is_dir():
            total = 0
            for sub in _walk_recursive(path):
                if sub.is_file():
                    with contextlib.suppress(OSError):
                        total += sub.stat().st_size
            shutil.rmtree(path)
            return str(path), total
    except (OSError, PermissionError) as exc:
        print(f"Error deleting {path}: {exc}", file=sys.stderr)
    return str(path), 0


def cmd_os_junk(args: argparse.Namespace) -> int:
    base = Path.cwd()
    print(f"Scanning directory: {base}\n")
    win_low = {e.lower() for e in _WIN_EXTS}
    mac_low = {e.lower() for e in _MAC_EXTS}
    found: list[Path] = []
    for root, _dirs, files in os.walk(base):
        for fname in files:
            low = fname.lower()
            if low == ".ds_store":
                found.append(Path(root) / fname)
                continue
            suffix = Path(low).suffix
            if suffix in win_low or suffix in mac_low:
                found.append(Path(root) / fname)
    if not found:
        print("No Windows or macOS related files found.")
        return 0
    base_res = base.resolve()
    print(f"Found {len(found)} file(s):\n")
    for p in found:
        print(f"  {_safe_rel(p.resolve(), base_res)}")
    if args.auto_remove:
        print("\n" + "=" * 35)
        count = 0
        for p in found:
            try:
                os.remove(p)
                print(f"Deleted: {p}")
                count += 1
            except Exception as exc:
                print(f"Error deleting {p}: {exc}")
        print(f"\nDeleted {count} of {len(found)} files.")
    return 0


def _darwin_match(path: Path, patterns: Sequence[str]) -> bool:
    name = path.name
    for pat in patterns:
        if pat.startswith("*"):
            if name.endswith(pat[1:]):
                return True
        elif pat.startswith("._"):
            if name.startswith("._"):
                return True
        elif pat.startswith(".") and pat != ".DS_Store":
            if name == pat:
                return True
        elif name == pat or name.startswith(pat.split("*")[0]):
            return True
    return False


def cmd_darwin(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        return 1
    print(f"Scanning {root} for Darwin/Windows files...\n")
    patterns = tuple(_DARWIN_NAMES) + tuple(_DARWIN_WILDCARDS) + tuple(_WIN_NAMES)
    targets = [p for p in _walk_recursive(root) if _darwin_match(p, patterns)]
    if not targets:
        print("No matching files found.")
        return 0
    print(f"Found {len(targets)} file(s) to remove\n")
    details: list[tuple[str, int]] = []
    for p in targets:
        print(f"Removing: {p}")
        details.append(_remove_path(p))
    total = sum(size for _, size in details)
    removed = len(details)
    print("\n" + "=" * 40)
    print("REMOVAL REPORT")
    print("-" * 40)
    print(f"Files removed: {removed}")
    print(f"Total disk space freed: {_human_size(total)} ({total} bytes)")
    print("-" * 40)
    if args.verbose:
        print("Removed files:")
        for p, size in details:
            print(f"  {p} ({size} bytes)")
    return 0


def _cache_candidates(root: Path) -> Iterator[Path]:
    try:
        for entry in root.iterdir():
            if entry.is_file() and any(entry.name.endswith(s) for s in _CACHE_SUFFIXES):
                yield entry
            elif entry.is_dir() and not entry.is_symlink():
                if entry.name in _CACHE_DIRS:
                    if entry.parent.name == "site-packages":
                        print(f"not allowed: {entry}")
                        continue
                    yield entry
                else:
                    yield from _cache_candidates(entry)
    except PermissionError:
        return


def _remove_one(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
            print(f"Removed file: {path.name}")
        elif path.is_dir():
            shutil.rmtree(path)
            print(f"Removed directory: {_safe_rel(path, Path.cwd())}")
    except Exception as exc:
        print(f"Failed to remove {path}: {exc}")


def cmd_caches(_args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    targets = list(_cache_candidates(root))
    if not targets:
        return 0
    with multiprocessing.Pool() as pool:
        pool.map(_remove_one, targets)
    return 0


def _iter_empty_files(root: Path, exclude: set[str]) -> Iterator[Path]:
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            if name in exclude:
                continue
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            try:
                if p.is_file() and p.stat().st_size == 0:
                    yield p
            except OSError:
                continue


def _wait_for_key(timeout: int) -> bool:
    if timeout <= 0:
        return False
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return False
    if ready:
        with contextlib.suppress(Exception):
            sys.stdin.readline()
        return True
    return False


def cmd_empty_files(args: argparse.Namespace) -> int:
    root = Path.cwd()
    exclude: set[str] = set()
    if args.delete:
        exclude.add("__init__.py")
    else:
        exclude.update({"__init__.py", "py.typed"})
    files = list(_iter_empty_files(root, exclude))
    if not files:
        print("no empty files found")
        return 0
    print(f"{len(files)} empty files found.")
    for p in files:
        print(f"- {_safe_rel(p, root)}")
    if not args.delete:
        return 0
    if args.timeout > 0:
        print(f"Press any key within {args.timeout} seconds to abort.")
        if _wait_for_key(args.timeout):
            print("Aborted by user.")
            return 1
    removed = failed = 0
    for p in files:
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except Exception as exc:
            failed += 1
            print(f"Failed to remove {p}: {exc}", file=sys.stderr)
    print(f"Deleted: {removed}, Failed: {failed}")
    return 0


def _is_excluded_dir(path: Path, root: Path, name_ex: set[str], part_ex: set[str]) -> bool:
    if path.name in name_ex:
        return True
    try:
        parts = path.relative_to(root).parts
        if any(part in part_ex for part in parts):
            return True
    except ValueError:
        pass
    return bool(path.name.startswith("mc") and path.parent.name == "tmp")


def cmd_empty_dirs(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(
            f"Error: The provided path '{root}' is not a valid directory.",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print("--- DRY RUN MODE (no changes will be made) ---")
    if args.exclude_mode == "pdmp":
        name_ex = set(_PDMP_NAME_EXCLUDES)
        part_ex = set(_PDMP_PART_EXCLUDES)
    else:
        name_ex = set()
        part_ex = set()
    dirs = [p for p in root.rglob("*") if p.is_dir()]
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    if args.exclude_mode == "pdmp" and args.include_root:
        dirs.append(root)
    removed = 0
    removed_list: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        if (name_ex or part_ex) and _is_excluded_dir(d, root, name_ex, part_ex):
            if args.verbose:
                print(f"Skipping excluded directory: {_safe_rel(d, root)}")
            continue
        try:
            if not any(d.iterdir()):
                if args.verbose:
                    print(f"Empty directory found: {_safe_rel(d, root)}")
                if args.dry_run:
                    print(f"  (Dry Run) Would remove: {_safe_rel(d, root)}")
                    removed += 1
                    removed_list.append(d)
                else:
                    d.rmdir()
                    removed += 1
                    removed_list.append(d)
                    if args.verbose:
                        print(f"--> Removed: {_safe_rel(d, root)}")
        except PermissionError:
            print(f"[ERROR] Permission denied for: {_safe_rel(d, root)}", file=sys.stderr)
        except OSError as exc:
            print(
                f"[ERROR] Could not process {_safe_rel(d, root)}: {exc}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"[ERROR] An unexpected error occurred with {_safe_rel(d, root)}: {exc}",
                file=sys.stderr,
            )
    if removed > 0:
        verb = "Would have removed" if args.dry_run else "removed"
        print(f"{verb} {removed} empty directories:")
        for d in sorted(removed_list):
            print(f"- {_safe_rel(d, root)}")
    else:
        print("No empty dir.")
    return 0


def cmd_twins(args: argparse.Namespace) -> int:
    root = Path.cwd()
    ext1 = args.ext1 if args.ext1.startswith(".") else "." + args.ext1
    ext2 = args.ext2 if args.ext2.startswith(".") else "." + args.ext2
    if args.interactive:
        if not sys.stdin.isatty():
            print("--interactive requires a TTY", file=sys.stderr)
            return 1
        print(f"ext 1 : {ext1}")
        print(f"ext 2 : {ext2}")
        try:
            choice = input("remove which one: 1 or 2: ").strip()
        except EOFError:
            return 1
        if choice not in ("1", "2"):
            print("Invalid choice")
            return 1
        remove_ext = ext1 if choice == "1" else ext2
    else:
        remove_ext = ext2 if args.remove == "2" else ext1
    checked = removed = 0
    for candidate in root.rglob(f"*{ext1}"):
        if not candidate.is_file() or candidate.suffix != ext1:
            continue
        twin = candidate.with_suffix(ext2)
        if not twin.exists():
            continue
        checked += 1
        if remove_ext == ext1:
            target, keeper = candidate, twin
        else:
            target, keeper = twin, candidate
        if args.apply:
            try:
                target.unlink()
                print(f"[REMOVED] {target} (kept {keeper})")
                removed += 1
            except Exception as exc:
                print(f"[ERROR] Could not remove {target}: {exc}")
        else:
            print(f"[DRY RUN] Would remove {target} (kept {keeper})")
    print("\n--- Summary ---")
    print(f"Checked: {checked}")
    if args.apply:
        print(f"Removed: {removed}")
    else:
        print("Dry run only. No files removed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merged filesystem cleanup toolkit")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("os-junk", help="clean_os_files.py")
    p.add_argument("-a", "--auto-remove", action="store_true")
    p.set_defaults(func=cmd_os_junk)
    p = sub.add_parser("darwin", help="cleanup_darwin.py")
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_darwin)
    p = sub.add_parser("caches", help="cnn.py")
    p.set_defaults(func=cmd_caches)
    p = sub.add_parser("empty-files", help="find_empty.py / rmempty.py")
    p.add_argument("--delete", action="store_true", help="rmempty.py behavior")
    p.add_argument("--timeout", type=int, default=0, help="abort window in seconds")
    p.set_defaults(func=cmd_empty_files)
    p = sub.add_parser("empty-dirs", help="pdmp.py / pydmp.py / pydmp2.py")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--exclude-mode",
        choices=["none", "pdmp"],
        default="none",
        help="`pdmp` uses the original exclusion sets",
    )
    p.add_argument(
        "--include-root",
        action="store_true",
        help="pdmp.py appended the root itself to the candidate list",
    )
    p.set_defaults(func=cmd_empty_dirs)
    p = sub.add_parser("twins", help="twin_files.py / twinfiles.py")
    p.add_argument("--ext1", default=".json")
    p.add_argument("--ext2", default=".txt")
    p.add_argument(
        "--remove",
        choices=["1", "2"],
        default="2",
        help="Which extension to delete when a twin exists (default 2)",
    )
    p.add_argument("--apply", action="store_true", help="Actually delete files")
    p.add_argument("--interactive", action="store_true", help="Prompt for choice")
    p.set_defaults(func=cmd_twins)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
