#!/data/data/com.termux/files/usr/bin/env python
"""Collect, flatten and move files and directories.
Subcommands: ``collect`` gathers every ``*.EXT`` into one folder, ``move-subdirs`` moves the top-level directories, ``flatten`` lifts the files out of the subdirectories, ``move-tests`` pulls the test folders out of ``site-packages``, ``move-binary`` collects the binaries (with SHA-256 dedup) and ``send`` copies files to a destination.
Every move is logged and ``--reverse`` undoes them.
Usage: file_mover_tool {collect,move-subdirs,flatten,move-tests,move-binary,send} [options] Positional arguments: extension E.g.
pdf, whl root source dest files ...
Subcommands: collect Gather *.EXT into one folder move-subdirs Move top-level subdirs SRC -> DST flatten Lift files out of subdirectories move-tests Move tests/ dirs and test files out move-binary Binary files -> binary/ send Send file(s) to a destination dir Options: --to Default: <root>/<ext>/ --move Move instead of copy (www.py) -n, --dry-run --rename-on-clash -r, --recursive All depths (original: 1 level) --remove-empty --source Default: site-packages --dir Base for --kind files --kind --out ...
plus 6 more; run ``file_mover_tool --help`` for the full list.
Examples: $ file_mover_tool collect pdf --root ~/docs $ file_mover_tool flatten ~/projects Command-line script: run ``python file_mover_tool.py`` or the ``file_mover_tool`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import shutil
import site
import sys


DEFAULT_TESTS_OUT: Path = Path.home() / "tmp" / "tests"
DEFAULT_MOVE_LOG: Path = Path.home() / "tmp" / "moved_files.json"
DEFAULT_EXCLUDED_PKGS: tuple[str, ...] = ("numpy", "pandas", "scipy", "numba")


def unique_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    counter: int = 1
    while True:
        candidate: Path = dest.parent / f"{dest.stem}_{counter}{dest.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def do_move(src: Path, dest: Path, copy: bool, dry_run: bool) -> Path:
    real: Path = unique_destination(dest) if not dry_run else dest
    verb: str = "copy" if copy else "move"
    if dry_run:
        print(f"would {verb}: {src} -> {real}")
        return real
    real.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        if src.is_dir():
            shutil.copytree(src, real)
        else:
            shutil.copy2(src, real)
    else:
        shutil.move(str(src), str(real))
    print(f"{verb}: {src} -> {real}")
    return real


def is_binary(path: Path) -> bool:
    try:
        return b"\x00" in path.read_bytes()[:8192]
    except OSError:
        return False


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(32768), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_collect(args: argparse.Namespace) -> int:
    ext: str = args.extension.lower().lstrip(".")
    root: Path = args.root.resolve()
    target: Path = (args.to or root / ext).resolve()
    count: int = 0
    for path in sorted(root.rglob(f"*.{ext}")):
        if path.is_file() and target not in path.parents:
            do_move(path, target / path.name, copy=not args.move, dry_run=args.dry_run)
            count += 1
    print(f"Total files {'moved' if args.move else 'copied'}: {count}")
    return 0


def cmd_move_subdirs(args: argparse.Namespace) -> int:
    source, dest = args.source.resolve(), args.dest.resolve()
    if not source.is_dir() or not dest.is_dir() or source == dest:
        print("Error: source and dest must be two different existing directories.", file=sys.stderr)
        return 1
    dest_names: set[str] = {p.name for p in dest.iterdir() if p.is_dir()}
    moved: int = 0
    skipped: int = 0
    for subdir in sorted(p for p in source.iterdir() if p.is_dir()):
        if subdir.name in dest_names and not args.rename_on_clash:
            print(f"SKIP {subdir.name} (already in destination)")
            skipped += 1
            continue
        do_move(subdir, dest / subdir.name, copy=False, dry_run=args.dry_run)
        moved += 1
    print(f"Moved: {moved}, skipped: {skipped}")
    return 0


def cmd_flatten(args: argparse.Namespace) -> int:
    root: Path = args.root.resolve()
    if args.recursive:
        files: list[Path] = [p for p in root.rglob("*") if p.is_file() and p.parent != root]
    else:
        files = [p for sub in root.iterdir() if sub.is_dir() for p in sub.iterdir() if p.is_file()]
    if not files:
        print("No files found in subdirectories.")
        return 0
    moved: int = 0
    for path in sorted(files):
        dest: Path = root / path.name
        if dest.exists() and not args.rename_on_clash:
            print(f"skip (exists): {path}")
            continue
        do_move(path, dest, copy=False, dry_run=args.dry_run)
        moved += 1
    if args.remove_empty and not args.dry_run:
        for folder in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if folder.is_dir() and folder != root:
                try:
                    folder.rmdir()
                    print(f"removed empty dir: {folder}")
                except OSError:
                    pass
    print(f"Moved {moved} file(s).")
    return 0


def site_packages_default() -> Path:
    candidates: list[str] = site.getsitepackages() + [site.getusersitepackages()]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path.home() / ".local" / "lib" / "python3.12" / "site-packages"


def is_test_file(path: Path) -> bool:
    return "_test" in path.stem or "test_" in path.stem


def cmd_move_tests(args: argparse.Namespace) -> int:
    if args.reverse:
        return reverse_moves(args.log, args.dry_run)
    source: Path = (args.source or site_packages_default()).resolve()
    excluded: tuple[str, ...] = tuple(args.exclude)
    mapping: dict[str, str] = {}
    targets: list[Path] = []
    if args.kind in ("dirs", "both"):
        targets += [p for p in source.rglob("tests") if p.is_dir() and not any(name in str(p) for name in excluded)]
    if args.kind in ("files", "both"):
        base: Path = args.dir.resolve() if args.kind == "files" else source
        targets += [p for p in base.rglob("*.py") if is_test_file(p)]
    if not targets:
        print("No test dirs/files found.")
        return 0
    for path in sorted(set(targets)):
        try:
            rel: Path = path.relative_to(source)
        except ValueError:
            rel = Path(path.name)
        real: Path = do_move(path, args.out / rel, copy=False, dry_run=args.dry_run)
        mapping[str(path)] = str(real)
    if not args.dry_run and mapping:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        previous: dict[str, str] = {}
        if args.log.is_file():
            loaded: object = json.loads(args.log.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = {str(k): str(v) for k, v in loaded.items()}
        previous.update(mapping)
        args.log.write_text(json.dumps(previous, indent=2) + "\n", encoding="utf-8")
        print(f"Log updated: {args.log} (undo with --reverse)")
    print(f"Processed {len(mapping)} item(s) -> {args.out}")
    return 0


def reverse_moves(log_path: Path, dry_run: bool) -> int:
    if not log_path.is_file():
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return 1
    loaded: object = json.loads(log_path.read_text(encoding="utf-8"))
    mapping: dict[str, str] = {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}
    restored: int = 0
    for original, moved in mapping.items():
        moved_path, original_path = Path(moved), Path(original)
        if moved_path.exists():
            do_move(moved_path, original_path, copy=False, dry_run=dry_run)
            restored += 1
    print(f"Restored {restored}/{len(mapping)} item(s).")
    return 0


def cmd_move_binary(args: argparse.Namespace) -> int:
    root: Path = args.root.resolve()
    target: Path = args.to or root / "binary"
    candidates = root.rglob("*") if args.recursive else root.iterdir()
    moved: int = 0
    for path in sorted(candidates):
        if path.is_file() and target not in path.parents and is_binary(path):
            do_move(path, target / path.name, copy=args.copy, dry_run=args.dry_run)
            moved += 1
    print(f"Binary files {'copied' if args.copy else 'moved'}: {moved}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    dest_dir: Path = args.to.expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    status: int = 0
    for src in args.files:
        if not src.is_file():
            print(f"skip (not a file): {src}", file=sys.stderr)
            status = 1
            continue
        dest: Path = dest_dir / src.name
        if dest.exists() and args.dedup and sha256_of(dest) == sha256_of(src):
            print(f"{src.name}: identical file already at destination (source kept)")
            continue
        if dest.exists() and not args.rename:
            print(f"{src.name}: target exists (use --rename for name_N)")
            status = 1
            continue
        do_move(src, dest, copy=args.copy, dry_run=args.dry_run)
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect/move files and directories.")
    sub = parser.add_subparsers(dest="command", required=True)
    co = sub.add_parser("collect", help="Gather *.EXT into one folder")
    co.add_argument("extension", help="e.g. pdf, whl")
    co.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    co.add_argument("--to", type=Path, default=None, help="Default: <root>/<ext>/")
    co.add_argument("--move", action="store_true", help="Move instead of copy (www.py)")
    co.add_argument("-n", "--dry-run", action="store_true")
    co.set_defaults(handler=cmd_collect)
    ms = sub.add_parser("move-subdirs", help="Move top-level subdirs SRC -> DST")
    ms.add_argument("source", type=Path)
    ms.add_argument("dest", type=Path)
    ms.add_argument("--rename-on-clash", action="store_true")
    ms.add_argument("-n", "--dry-run", action="store_true")
    ms.set_defaults(handler=cmd_move_subdirs)
    fl = sub.add_parser("flatten", help="Lift files out of subdirectories")
    fl.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    fl.add_argument("-r", "--recursive", action="store_true", help="All depths (original: 1 level)")
    fl.add_argument("--rename-on-clash", action="store_true")
    fl.add_argument("--remove-empty", action="store_true")
    fl.add_argument("-n", "--dry-run", action="store_true")
    fl.set_defaults(handler=cmd_flatten)
    mt = sub.add_parser("move-tests", help="Move tests/ dirs and test files out")
    mt.add_argument("--source", type=Path, default=None, help="Default: site-packages")
    mt.add_argument("--dir", type=Path, default=Path.cwd(), help="Base for --kind files")
    mt.add_argument("--kind", choices=("dirs", "files", "both"), default="dirs")
    mt.add_argument("--out", type=Path, default=DEFAULT_TESTS_OUT)
    mt.add_argument("--log", type=Path, default=DEFAULT_MOVE_LOG)
    mt.add_argument("--exclude", nargs="*", default=list(DEFAULT_EXCLUDED_PKGS))
    mt.add_argument("--reverse", action="store_true", help="Undo using the log")
    mt.add_argument("-n", "--dry-run", action="store_true")
    mt.set_defaults(handler=cmd_move_tests)
    mb = sub.add_parser("move-binary", help="Binary files -> binary/")
    mb.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    mb.add_argument("--to", type=Path, default=None)
    mb.add_argument("-r", "--recursive", action="store_true")
    mb.add_argument("--copy", action="store_true")
    mb.add_argument("-n", "--dry-run", action="store_true")
    mb.set_defaults(handler=cmd_move_binary)
    se = sub.add_parser("send", help="Send file(s) to a destination dir")
    se.add_argument("files", nargs="+", type=Path)
    se.add_argument("--to", type=Path, required=True, help="e.g. ~/sbin, ~/isaac/may/scripts, /sdcard/doc")
    se.add_argument("--copy", action="store_true", help="Copy instead of move (pycp)")
    se.add_argument("--dedup", action="store_true", help="Skip when SHA-256 identical (tobin)")
    se.add_argument("--rename", action="store_true", help="Use name_N on clashes (tosc)")
    se.add_argument("-n", "--dry-run", action="store_true")
    se.set_defaults(handler=cmd_send)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
