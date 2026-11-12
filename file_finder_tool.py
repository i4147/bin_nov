#!/data/data/com.termux/files/usr/bin/env python
"""Find/list files by name pattern, prefix, extension, or script-ness.
Analysis / deduplication ------------------------ - ``lkk.py`` (name substring, top level, skip symlinks unless ``-s``) and ``list_files_starting_with.py`` (prefix) -> ``find --name`` / ``find --prefix`` (add ``-r`` to recurse, off by default as before).
- ``bkk.py`` listed *and unconditionally deleted* every ``*.bak``: now ``find --name .bak --files-only [-r]`` lists them; deletion needs the explicit ``--delete`` flag (use ``--dry-run`` first).
- ``extinfo.py`` (count+size of one extension) and ``extz.py`` (count of every extension) -> ``ext-stats [EXT]`` (always shows sizes too).
- ``find_scripts.py`` (extension-less Python scripts via private ``dh`` helpers) -> ``scripts`` using a shebang/content sniff.
Examples -------- python3 file_finder_tool.py find --name test -r python3 file_finder_tool.py find --prefix IMG_ ~/Pictures python3 file_finder_tool.py find --name .bak -r --delete --dry-run python3 file_finder_tool.py ext-stats python3 file_finder_tool.py scripts ~/bin
"""

import argparse
from collections import Counter
from collections.abc import Iterator, Sequence
import fnmatch
from pathlib import Path
import sys

UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(num: int) -> str:
    """Format a byte count like ``1.5 MB``."""
    size: float = float(num)
    for unit in UNITS:
        if abs(size) < 1024.0 or unit == UNITS[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} PB"


def walk(root: Path, recursive: bool, follow_symlinks: bool) -> Iterator[Path]:
    """Yield entries under *root*, skipping ``.git`` and (optionally) symlinks."""
    if recursive:
        stack: list[Path] = [root]
        while stack:
            current: Path = stack.pop()
            try:
                entries: list[Path] = sorted(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name == ".git":
                    continue
                if entry.is_symlink() and not follow_symlinks:
                    yield entry
                    continue
                if entry.is_dir():
                    stack.append(entry)
                yield entry
    else:
        try:
            yield from sorted(root.iterdir())
        except OSError:
            return


def name_matches(path: Path, name: str | None, prefix: str | None, glob: str | None) -> bool:
    """True when the filename satisfies all given criteria."""
    if name is not None and name not in path.name:
        return False
    if prefix is not None and not path.name.startswith(prefix):
        return False
    return not (glob is not None and not fnmatch.fnmatch(path.name, glob))


def display(path: Path, root: Path, show_size: bool) -> str:
    """Format one result line (symlink target / size included on request)."""
    label: str = str(path.relative_to(root)) if root in path.parents or path.parent == root else str(path)
    extras: list[str] = []
    if path.is_symlink():
        extras.append(f"-> {path.resolve()}")
    if show_size and path.is_file():
        extras.append(human_size(path.stat().st_size))
    return f"  - {label}" + (f"  ({', '.join(extras)})" if extras else "")


def cmd_find(args: argparse.Namespace) -> int:
    """List (or delete) entries matching name/prefix/glob criteria."""
    if not (args.name or args.prefix or args.glob):
        print("Error: give at least one of --name, --prefix, --glob.", file=sys.stderr)
        return 2
    root: Path = args.root
    found: list[Path] = []
    for path in walk(root, args.recursive, args.follow_symlinks):
        if path.is_symlink() and not args.follow_symlinks and args.skip_symlinks:
            continue
        if args.files_only and not path.is_file():
            continue
        if name_matches(path, args.name, args.prefix, args.glob):
            found.append(path)
    for path in sorted(found):
        print(display(path, root, args.size))
    print(f"# {len(found)} match(es) under {root}", file=sys.stderr)
    if args.delete:
        for path in found:
            if not path.is_file():
                continue
            if args.dry_run:
                print(f"would delete: {path}")
            else:
                path.unlink()
                print(f"deleted: {path}")
    return 0


def cmd_ext_stats(args: argparse.Namespace) -> int:
    """Count files (and total size) per extension, or for one extension."""
    root: Path = args.root
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    for path in walk(root, True, False):
        if not path.is_file():
            continue
        ext: str = path.suffix.lower() or ".no_ext"
        counts[ext] += 1
        try:
            sizes[ext] += path.stat().st_size
        except OSError:
            continue
    if args.extension:
        ext = ("." + args.extension.lstrip(".")).lower()
        print(f"Total number of {ext} files: {counts.get(ext, 0)}")
        print(f"Total size of {ext} files: {human_size(sizes.get(ext, 0))}")
        return 0
    if not counts:
        print("No files found.")
        return 0
    width: int = max(len(ext) for ext in counts)
    print("extensions found:")
    for ext, count in counts.most_common():
        print(f" {ext:<{width}}  {count:>6} files  {human_size(sizes[ext]):>10}")
    return 0


def looks_like_python_script(path: Path) -> bool:
    """Sniff an extension-less file for a python shebang or python code."""
    try:
        head: str = path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return False
    first: str = head.splitlines()[0] if head else ""
    if first.startswith("#!"):
        return "python" in first.lower()
    return any(token in head for token in ("if __name__ ==", "def ", "import ", "from "))


def cmd_scripts(args: argparse.Namespace) -> int:
    """Find Python scripts that lack the ``.py`` extension."""
    root: Path = args.root
    found: list[Path] = [
        path
        for path in walk(root, args.recursive, False)
        if path.is_file() and not path.suffix and looks_like_python_script(path)
    ]
    if found:
        print("Python scripts without extension (relative paths):")
        for path in sorted(found):
            print(path.relative_to(root) if root in path.parents else path)
    else:
        print("No Python scripts without extension found.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Find files by name/prefix/extension/script.")
    sub = parser.add_subparsers(dest="command", required=True)
    fi = sub.add_parser("find", help="List entries matching name criteria")
    fi.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    fi.add_argument("--name", default=None, help="Substring required in the filename")
    fi.add_argument("--prefix", default=None, help="Filename must start with this")
    fi.add_argument("--glob", default=None, help="fnmatch pattern, e.g. '*.bak'")
    fi.add_argument("-r", "--recursive", action="store_true", help="Recurse (default: top level)")
    fi.add_argument("--files-only", action="store_true")
    fi.add_argument("--size", action="store_true", help="Show file sizes")
    fi.add_argument("-s", "--follow-symlinks", action="store_true", help="Include symlinks (lkk -s)")
    fi.add_argument("--skip-symlinks", action="store_true", default=True, help=argparse.SUPPRESS)
    fi.add_argument("--delete", action="store_true", help="Delete matching FILES (dangerous)")
    fi.add_argument("--dry-run", action="store_true", help="With --delete, only report")
    fi.set_defaults(handler=cmd_find)
    es = sub.add_parser("ext-stats", help="File counts/sizes per extension")
    es.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    es.add_argument("extension", nargs="?", default=None, help="Only this extension (e.g. py)")
    es.set_defaults(handler=cmd_ext_stats)
    sc = sub.add_parser("scripts", help="Find extension-less Python scripts")
    sc.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    sc.add_argument("-r", "--recursive", action="store_true", default=True)
    sc.set_defaults(handler=cmd_scripts)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
