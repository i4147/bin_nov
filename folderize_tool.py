#!/data/data/com.termux/files/usr/bin/env python
"""Organize files into folders by rule, plus related directory checks.
Analysis / deduplication ------------------------ - ``foext.py`` (top-level files -> ``<ext>/``) and ``foldext.py`` (recursive, with stats) are one ``by-ext`` command; the originals moved immediately - the merged tool plans first and needs ``--apply`` (``--recursive`` selects the foldext behaviour).
- ``folderize.py`` (first letter / ``0-9`` folders, already had a ``--dry-run``) -> ``by-initial``.
- ``folderize_by_sim_names.py`` (Lua files grouped by normalized name) -> ``by-name --glob '*.lua'`` (glob is now configurable).
- ``foldimension.py`` (images grouped by pixel size, needs Pillow) -> ``by-image-size``.
- ``nosubdir.py`` does not folderize at all: it lists top-level dirs that have no subdirs but contain ``.py`` files -> ``leaf-dirs``.
- ``rename_by_parent.py`` (``README.pdf`` -> ``<parent>.pdf`` with a hand-rolled walker) -> ``rename-by-parent``.
Every mutating command defaults to a dry-run plan; pass ``--apply``.
Examples -------- python3 folderize_tool.py by-ext ~/Downloads --apply python3 folderize_tool.py by-initial .
--recursive python3 folderize_tool.py by-name .
--glob '*.png' --apply python3 folderize_tool.py by-image-size photos/ --apply python3 folderize_tool.py leaf-dirs .
--max-kb 100"""

import argparse
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
import re
import shutil
import sys

IMAGE_EXT: frozenset[str] = frozenset({
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
    ".ico",
})
UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")


def human_size(num: int) -> str:
    """Format a byte count like ``1.5 MB``."""
    size: float = float(num)
    for unit in UNITS:
        if abs(size) < 1024.0 or unit == UNITS[-1]:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def unique_destination(dest: Path) -> Path:
    """Return *dest* or ``name_N.suffix`` when it already exists."""
    if not dest.exists():
        return dest
    counter: int = 1
    while True:
        candidate: Path = dest.parent / f"{dest.stem}_{counter}{dest.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def gather(root: Path, recursive: bool) -> list[Path]:
    """Collect files under *root* (``.git`` always skipped)."""
    candidates = root.rglob("*") if recursive else root.iterdir()
    return sorted(p for p in candidates if p.is_file() and ".git" not in p.parts)


def execute_plan(plan: list[tuple[Path, Path]], apply: bool, cleanup: bool, root: Path) -> int:
    """Print/apply (src -> dst) moves; return how many were planned."""
    moved: int = 0
    for src, dst in plan:
        real_dst: Path = unique_destination(dst) if apply else dst
        print(f"{'move' if apply else 'plan'}: {src} -> {real_dst}")
        if apply:
            real_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(real_dst))
            moved += 1
    if apply and cleanup:
        for folder in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if folder.is_dir() and folder != root:
                try:
                    folder.rmdir()
                    print(f"removed empty dir: {folder}")
                except OSError:
                    pass
    if not apply and plan:
        print(f"Dry run: {len(plan)} move(s) planned. Re-run with --apply.")
    return moved


def initial_folder(filename: str) -> str:
    """First-letter folder name (``0-9`` for digits/symbols)."""
    first: str = filename[0].lower() if filename else ""
    return first if first.isalpha() else "0-9"


def ext_folder(path: Path) -> str:
    """Extension folder name without the dot."""
    return path.suffix.lower().lstrip(".") or "no_extension"


def cmd_by_ext(args: argparse.Namespace) -> int:
    """Move files into ``<ext>/`` folders (with per-extension stats)."""
    root: Path = args.root.resolve()
    files: list[Path] = gather(root, args.recursive)
    stats: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    plan: list[tuple[Path, Path]] = []
    for path in files:
        folder: str = ext_folder(path)
        count, total = stats[folder]
        stats[folder] = (count + 1, total + path.stat().st_size)
        dest: Path = root / folder / path.name
        if dest.resolve() != path.resolve():
            plan.append((path, dest))
    execute_plan(plan, args.apply, args.cleanup, root)
    for ext in sorted(stats):
        count, total = stats[ext]
        print(f"{ext:<15} : {count:4} file(s)  {human_size(total):>10}")
    return 0


def cmd_by_initial(args: argparse.Namespace) -> int:
    """Move files into first-letter folders (a/, b/, ..., 0-9/)."""
    root: Path = args.root.resolve()
    plan: list[tuple[Path, Path]] = []
    for path in gather(root, args.recursive):
        dest: Path = root / initial_folder(path.name) / path.name
        if dest.resolve() != path.resolve():
            plan.append((path, dest))
    execute_plan(plan, args.apply, args.cleanup, root)
    return 0


def normalize_name(path: Path, strip_suffixes: bool) -> str:
    """Group key: lowercase stem, no trailing ``_123``, dashes for junk."""
    name: str = path.stem if strip_suffixes else path.name
    name = re.sub(r"_\d+$", "", name).strip().lower()
    name = re.sub(r"[^a-z0-9_.-]+", "-", re.sub(r"\s+", "-", name))
    return re.sub(r"-+", "-", name).strip("-._") or "ungrouped"


def cmd_by_name(args: argparse.Namespace) -> int:
    """Group files into folders named by their normalized filename."""
    root: Path = args.root.resolve()
    self_path: Path = Path(__file__).resolve()
    files: list[Path] = [p for p in root.rglob(args.glob) if p.is_file() and p.resolve() != self_path]
    groups: dict[str, list[Path]] = defaultdict(list)
    for path in files:
        groups[normalize_name(path, True)].append(path)
    if not groups:
        print(f"No files matching {args.glob} found.")
        return 0
    plan: list[tuple[Path, Path]] = [
        (src, root / group / src.name)
        for group, paths in sorted(groups.items())
        for src in sorted(paths)
        if (root / group / src.name).resolve() != src.resolve()
    ]
    execute_plan(plan, args.apply, False, root)
    return 0


def cmd_by_image_size(args: argparse.Namespace) -> int:
    """Group images into ``<W>x<H>/`` folders (needs Pillow)."""
    try:
        from PIL import Image
    except ImportError:
        print("Error: needs Pillow (pip install Pillow).", file=sys.stderr)
        return 1
    root: Path = args.root.resolve()
    plan: list[tuple[Path, Path]] = []
    for path in gather(root, True):
        if path.suffix.lower() not in IMAGE_EXT:
            continue
        try:
            with Image.open(path) as img:
                width, height = img.size
        except Exception as exc:
            print(f"warning: skipping {path}: {exc}")
            continue
        dest: Path = root / f"{width}x{height}" / path.name
        if dest.resolve() != path.resolve():
            plan.append((path, dest))
    execute_plan(plan, args.apply, False, root)
    return 0


def is_leaf_python_dir(path: Path, max_kb: float | None) -> bool:
    """True for dirs with .py files, no subdirs, optionally small enough."""
    try:
        children: list[Path] = list(path.iterdir())
    except OSError:
        return False
    if any(child.is_dir() for child in children):
        return False
    py_files: list[Path] = [c for c in children if c.is_file() and c.suffix == ".py"]
    if not py_files:
        return False
    if max_kb is not None:
        total: int = sum(c.stat().st_size for c in children if c.is_file())
        if total / 1024 > max_kb:
            return False
    return True


def cmd_leaf_dirs(args: argparse.Namespace) -> int:
    """List top-level dirs without subdirs that contain .py files."""
    root: Path = args.root
    dirs: list[Path] = sorted(p for p in root.iterdir() if p.is_dir())
    hits: list[str] = [p.name for p in dirs if is_leaf_python_dir(p, args.max_kb)]
    if hits:
        note: str = f" (max {args.max_kb}KB)" if args.max_kb else ""
        print(f"Directories without subdirs containing .py files{note}:")
        for name in hits:
            print(f"  - {name}")
        print(f"\nTotal: {len(hits)}")
    else:
        print("No matching directories found.")
    return 0


def cmd_rename_by_parent(args: argparse.Namespace) -> int:
    """Rename matching files to ``<parent-dir-name><suffix>``."""
    root: Path = args.root.resolve()
    renamed: int = 0
    for path in gather(root, True):
        if not path.name.endswith(args.match):
            continue
        dest: Path = path.parent / f"{path.parent.name}{path.suffix}"
        if dest == path or dest.exists():
            continue
        print(f"{'rename' if args.apply else 'plan'}: {path} -> {dest}")
        if args.apply:
            path.rename(dest)
        renamed += 1
    if not args.apply and renamed:
        print(f"Dry run: {renamed} rename(s) planned. Re-run with --apply.")
    return 0


def add_common(child: argparse.ArgumentParser, recursive_default: bool) -> None:
    """Shared --apply/--recursive/--cleanup options for movers."""
    child.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    child.add_argument("--apply", action="store_true", help="Actually move (default: dry-run plan)")
    child.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        default=recursive_default,
        help=f"Collect recursively (default: {recursive_default})",
    )
    child.add_argument("--cleanup", action="store_true", help="Remove emptied dirs after moving")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Organize files into folders by rule.")
    sub = parser.add_subparsers(dest="command", required=True)
    be = sub.add_parser("by-ext", help="Group into <ext>/ folders")
    add_common(be, recursive_default=False)
    be.set_defaults(handler=cmd_by_ext)
    bi = sub.add_parser("by-initial", help="Group into first-letter folders")
    add_common(bi, recursive_default=True)
    bi.set_defaults(handler=cmd_by_initial)
    bn = sub.add_parser("by-name", help="Group by normalized filename")
    bn.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    bn.add_argument("--glob", default="*.lua", help="File pattern (original: *.lua)")
    bn.add_argument("--apply", action="store_true")
    bn.set_defaults(handler=cmd_by_name)
    bs = sub.add_parser("by-image-size", help="Group images into <W>x<H>/ folders")
    bs.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    bs.add_argument("--apply", action="store_true")
    bs.set_defaults(handler=cmd_by_image_size)
    ld = sub.add_parser("leaf-dirs", help="Leaf dirs containing .py files (nosubdir)")
    ld.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    ld.add_argument("-s", "--max-kb", type=float, default=None, help="Max dir size in KB")
    ld.set_defaults(handler=cmd_leaf_dirs)
    rp = sub.add_parser("rename-by-parent", help="README.pdf -> <parent>.pdf")
    rp.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    rp.add_argument("--match", default="README.pdf", help="Filename suffix to match")
    rp.add_argument("--apply", action="store_true")
    rp.set_defaults(handler=cmd_rename_by_parent)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
