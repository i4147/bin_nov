#!/data/data/com.termux/files/usr/bin/env python
"""Report, delete or symlink the duplicate files of a directory.
Subcommands: ``report`` lists the groups, ``delete`` keeps one copy and removes the rest, ``symlink`` moves the master copies to a stash and links the others, and ``restore`` reverses that from the manifest.
Hashing is xxhash (with a quick pre-filter), the keeper is selectable and ``--json``/``--dry-run`` help before acting.
Usage: duptools {report,delete,symlink,restore} [options] Subcommands: report Show duplicate groups (no changes) delete Delete duplicates, keep one per group symlink Move master copies to a stash and symlink the rest restore Reverse the symlink operation from a manifest Options: -d, --directory Directory to scan (default: .) -r, --recursive --no-recursive Do not scan recursively --algorithm Hash algorithm (default: xxhash; falls back to blake2b) --min-size Skip files smaller than N bytes (default: 1) --chunk-size --follow-symlinks Follow symlinks when scanning --quick-hash Use dupfx.py's head+tail quick-hash pre-filter --exclude Directory names to skip (default: .git __pycache__ ...) --json Export the found groups to a JSON file ...
plus 7 more; run ``duptools --help`` for the full list.
Examples: $ duptools report -d ~/photos --json dupes.json Command-line script: run ``python duptools.py`` or the ``duptools`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Optional, Sequence

from dh import MAX_WORKERS

try:
    import xxhash

    _HAS_XXHASH = True
except ImportError:
    _HAS_XXHASH = False
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None
try:
    import ppdeep

    _HAS_PPDEEP = True
except ImportError:
    _HAS_PPDEEP = False
try:
    from xorhash import get_xorhash

    _HAS_XORHASH = True
except ImportError:
    _HAS_XORHASH = False
CHUNK_SIZE: int = 8192
BIG_CHUNK_SIZE: int = 32768
QUICK_HEAD: int = 4096
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
)
DEFAULT_SYMLINK_MANIFEST = Path.home() / ".symlink_backup.json"
DEFAULT_STASH_DIR = Path.home() / "dups"


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def _new_hasher(algorithm: str):
    if algorithm == "xxhash" and _HAS_XXHASH:
        return xxhash.xxh64()
    if algorithm in {"blake2b", "xxhash"}:
        return hashlib.blake2b(digest_size=16)
    if algorithm == "sha256":
        return hashlib.sha256()
    if algorithm == "md5":
        return hashlib.md5()
    if algorithm == "xorhash":
        return _XorHasher()
    if algorithm == "ppdeep":
        return _PPDeepHasher()
    msg = f"Unknown algorithm: {algorithm!r}"
    raise ValueError(msg)


class _XorHasher:
    __slots__ = ("_acc",)

    def __init__(self) -> None:
        self._acc = 0

    def update(self, data: bytes) -> None:
        for i in range(0, len(data) - 7, 8):
            word = int.from_bytes(data[i : i + 8], "little")
            self._acc ^= word

    def hexdigest(self) -> str:
        return f"{self._acc:016x}"


class _PPDeepHasher:
    __slots__ = ("_buf", "_h")

    def __init__(self) -> None:
        self._h = hashlib.blake2b(digest_size=8)
        self._buf = bytearray()

    def update(self, data: bytes) -> None:
        if len(self._buf) < 4096:
            self._buf.extend(data[: 4096 - len(self._buf)])
        self._h.update(data)

    def hexdigest(self) -> str:
        if _HAS_PPDEEP:
            return self._h.hexdigest()
        return self._h.hexdigest()


def hash_file(path: Path, algorithm: str = "xxhash", chunk_size: int = BIG_CHUNK_SIZE) -> Optional[str]:
    if algorithm == "ppdeep" and _HAS_PPDEEP:
        try:
            return ppdeep.hash_from_file(str(path))
        except Exception as e:
            warn(f"ppdeep failed on {path}: {e}")
            return None
    try:
        if not path.stat().st_size:
            return ""
    except OSError:
        return None
    h = _new_hasher(algorithm)
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def quick_hash(path: Path, head: int = QUICK_HEAD) -> Optional[str]:
    try:
        size = path.stat().st_size
        h = hashlib.blake2b(digest_size=8)
        with path.open("rb") as f:
            h.update(f.read(head))
            if size > head * 2:
                f.seek(max(size - head, 0))
                h.update(f.read(head))
            elif size > head:
                h.update(f.read())
        return h.hexdigest()
    except OSError:
        return None


def collect_files(
    root: Path,
    *,
    recursive: bool,
    follow_symlinks: bool,
    min_size: int,
    excludes: Iterable[str],
) -> list[Path]:
    exclude_set = set(excludes)
    iterator = root.rglob("*") if recursive else root.iterdir()
    out: list[Path] = []
    for p in iterator:
        if any(part in exclude_set for part in p.parts):
            continue
        if p.is_symlink() and not follow_symlinks:
            continue
        if not p.is_file():
            continue
        try:
            if p.stat().st_size < min_size:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def _hash_worker(args: tuple[Path, str, int]) -> tuple[Path, Optional[str]]:
    path, algorithm, chunk_size = args
    return path, hash_file(path, algorithm, chunk_size)


def _quick_worker(path: Path) -> tuple[Path, Optional[str]]:
    return path, quick_hash(path)


def find_duplicates(
    files: list[Path],
    *,
    algorithm: str,
    workers: int,
    quick_first: bool,
    chunk_size: int,
) -> dict[str, list[Path]]:
    by_size: dict[int, list[Path]] = defaultdict(list)
    for p in files:
        try:
            by_size[p.stat().st_size].append(p)
        except OSError:
            continue
    candidates = [p for group in by_size.values() if len(group) > 1 for p in group]
    if not candidates:
        return {}
    if quick_first:
        by_quick: dict[str, list[Path]] = defaultdict(list)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for path, h in ex.map(_quick_worker, candidates):
                if h:
                    by_quick[h].append(path)
        candidates = [p for g in by_quick.values() if len(g) > 1 for p in g]
        if not candidates:
            return {}
    by_hash: dict[str, list[Path]] = defaultdict(list)
    jobs = [(p, algorithm, chunk_size) for p in candidates]
    if workers <= 1:
        results = (_hash_worker(j) for j in jobs)
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        results = pool.map(_hash_worker, jobs)
    try:
        if tqdm is not None:
            results = tqdm(results, total=len(jobs), desc="Hashing", unit="file")
        for path, h in results:
            if h is not None:
                by_hash[h].append(path)
    finally:
        if workers > 1:
            pool.shutdown(wait=True)
    return {h: g for h, g in by_hash.items() if len(g) > 1}


def select_keeper(group: list[Path], policy: str) -> Path:
    if not group:
        msg = "empty group"
        raise ValueError(msg)
    if policy == "first":
        return min(group, key=str)
    if policy == "oldest":
        return min(group, key=lambda p: p.stat().st_mtime)
    if policy == "newest":
        return max(group, key=lambda p: p.stat().st_mtime)
    if policy == "shortest-name":
        return min(group, key=lambda p: (len(str(p)), str(p)))
    return min(group, key=str)


def _trash_available() -> bool:
    return shutil.which("gio") is not None


def _delete_file(path: Path, use_trash: bool) -> bool:
    try:
        if use_trash and _trash_available():
            subprocess.run(["gio", "trash", str(path)], check=True)
        else:
            path.unlink()
        return True
    except (OSError, subprocess.CalledProcessError) as e:
        warn(f"could not delete {path}: {e}")
        return False


def cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        err(f"{root} is not a directory")
        return 1
    files = collect_files(
        root,
        recursive=args.recursive,
        follow_symlinks=args.follow_symlinks,
        min_size=args.min_size,
        excludes=args.exclude,
    )
    info(f"scanning {len(files)} file(s) under {root}")
    groups = find_duplicates(
        files,
        algorithm=args.algorithm,
        workers=MAX_WORKERS,
        quick_first=args.quick_hash,
        chunk_size=args.chunk_size,
    )
    if not groups:
        print("No duplicates found.")
        return 0
    wasted = 0
    print(f"\nFound {len(groups)} duplicate group(s):")
    for i, (h, group) in enumerate(sorted(groups.items()), 1):
        try:
            size = group[0].stat().st_size
        except OSError:
            size = 0
        wasted += size * (len(group) - 1)
        print(f"\nGroup {i} (hash={h[:16]}..., {len(group)} files, {size} bytes each):")
        for p in sorted(group):
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            print(f"  • {rel}")
    print(f"\nTotal recoverable space: {wasted:,} bytes ({wasted / 1024 / 1024:.2f} MB)")
    if args.json:
        out = {h: [str(p) for p in group] for h, group in groups.items()}
        try:
            Path(args.json).write_text(
                json.dumps(out, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            info(f"Results exported to {args.json}")
        except OSError as e:
            err(f"could not write {args.json}: {e}")
            return 1
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        err(f"{root} is not a directory")
        return 1
    files = collect_files(
        root,
        recursive=args.recursive,
        follow_symlinks=args.follow_symlinks,
        min_size=args.min_size,
        excludes=args.exclude,
    )
    info(f"scanning {len(files)} file(s) under {root}")
    groups = find_duplicates(
        files,
        algorithm=args.algorithm,
        workers=MAX_WORKERS,
        quick_first=args.quick_hash,
        chunk_size=args.chunk_size,
    )
    if not groups:
        print("No duplicates found.")
        return 0
    total_groups = len(groups)
    total_dups = sum(len(g) - 1 for g in groups.values())
    info(f"{total_groups} group(s), {total_dups} duplicate file(s) to remove")
    use_trash = args.trash if args.trash is not None else _trash_available()
    deleted = 0
    freed = 0
    for group in groups.values():
        keeper = select_keeper(group, args.keep)
        for p in group:
            if p == keeper:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            if args.dry_run:
                print(f"[DRY RUN] Would delete: {p} ({size:,} bytes)")
                deleted += 1
                freed += size
                continue
            if _delete_file(p, use_trash):
                deleted += 1
                freed += size
                print(f"Deleted: {p}")
    print(
        f"\n{'[DRY RUN] ' if args.dry_run else ''}"
        f"Removed {deleted} file(s), freed {freed:,} bytes "
        f"({freed / 1024 / 1024:.2f} MB)"
    )
    return 0


@dataclass(slots=True)
class SymlinkOp:
    symlink: str
    target: str
    size: int


def cmd_symlink(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        err(f"{root} is not a directory")
        return 1
    stash = Path(args.stash_dir).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser()
    files = collect_files(
        root,
        recursive=args.recursive,
        follow_symlinks=False,
        min_size=args.min_size,
        excludes=args.exclude,
    )
    info(f"scanning {len(files)} file(s) under {root}")
    groups = find_duplicates(
        files,
        algorithm=args.algorithm,
        workers=MAX_WORKERS,
        quick_first=args.quick_hash,
        chunk_size=args.chunk_size,
    )
    if not groups:
        print("No duplicates found.")
        return 0
    if not args.dry_run:
        stash.mkdir(parents=True, exist_ok=True)
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    operations: list[dict] = manifest.get("operations", [])
    stash_map: dict[str, dict] = manifest.get("stash", {})
    info(f"{len(groups)} duplicate group(s)")
    for h, group in groups.items():
        keeper = select_keeper(group, args.prefer)
        try:
            size = keeper.stat().st_size
        except OSError:
            continue
        stashed_name = f"{h[:16]}__{keeper.name}"
        stashed_path = stash / stashed_name
        if args.dry_run:
            print(f"[DRY RUN] move {keeper} -> {stashed_path}")
        elif not stashed_path.exists():
            try:
                shutil.move(str(keeper), str(stashed_path))
                print(f"moved: {keeper} -> {stashed_path}")
            except OSError as e:
                warn(f"could not move {keeper}: {e}")
                continue
        elif keeper.exists():
            try:
                keeper.unlink()
                print(f"removed original file: {keeper}")
            except OSError as e:
                warn(f"could not remove {keeper}: {e}")
        for p in group:
            if p == keeper:
                continue
            if p.is_symlink():
                continue
            target_resolved = stashed_path.resolve()
            if args.dry_run:
                print(f"[DRY RUN] symlink {p} -> {target_resolved}")
                continue
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.symlink_to(target_resolved)
                print(f"symlinked: {p} -> {target_resolved}")
            except OSError as e:
                warn(f"could not symlink {p}: {e}")
                continue
            operations.append({
                "symlink": str(p),
                "target": str(target_resolved),
                "size": size,
            })
        stash_map[str(stashed_path)] = {
            "hash": h,
            "originals": [str(p) for p in group],
        }
    if args.dry_run:
        print("dry-run complete; no changes written.")
        return 0
    manifest["operations"] = operations
    manifest["stash"] = stash_map
    manifest["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        info(f"manifest written to {manifest_path}")
    except OSError as e:
        err(f"could not write manifest: {e}")
        return 1
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser()
    if not manifest_path.exists():
        err(f"manifest not found: {manifest_path}")
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        err(f"could not read manifest: {e}")
        return 1
    stash_map: dict = manifest.get("stash", {})
    operations: list[dict] = manifest.get("operations", [])
    restored = 0
    for op in operations:
        link = Path(op["symlink"])
        target = Path(op["target"])
        if not target.exists():
            warn(f"target missing: {target}")
            continue
        if link.is_symlink():
            try:
                if args.dry_run:
                    print(f"[DRY RUN] restore {target} -> {link}")
                else:
                    link.unlink()
                    shutil.copy2(target, link)
                    print(f"restored: {link}")
                restored += 1
            except OSError as e:
                warn(f"could not restore {link}: {e}")
    if not args.dry_run:
        for stash_path in stash_map:
            with contextlib.suppress(OSError):
                Path(stash_path).unlink()
        backup = manifest_path.with_suffix(manifest_path.suffix + f".restored.{int(time.time())}")
        try:
            manifest_path.rename(backup)
            info(f"manifest renamed to {backup}")
        except OSError:
            pass
    print(f"\nRestored {restored} symlink(s).")
    return 0


def _add_scan_args(p: argparse.ArgumentParser, *, recursive_default: bool) -> None:
    p.add_argument("-d", "--directory", default=".", help="Directory to scan (default: .)")
    p.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        default=recursive_default,
        help=f"Scan recursively (default: {recursive_default})",
    )
    p.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="Do not scan recursively",
    )
    p.add_argument(
        "--algorithm",
        choices=("xxhash", "blake2b", "sha256", "md5", "xorhash", "ppdeep"),
        default="xxhash",
        help="Hash algorithm (default: xxhash; falls back to blake2b)",
    )
    p.add_argument(
        "--min-size",
        type=int,
        default=1,
        help="Skip files smaller than N bytes (default: 1)",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=BIG_CHUNK_SIZE,
        help=f"Read size in bytes (default: {BIG_CHUNK_SIZE})",
    )
    p.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks when scanning")
    p.add_argument(
        "--quick-hash",
        action="store_true",
        help="Use dupfx.py's head+tail quick-hash pre-filter",
    )
    p.add_argument(
        "--exclude",
        nargs="+",
        default=list(DEFAULT_EXCLUDES),
        help="Directory names to skip (default: .git __pycache__ ...)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dupe_tool.py",
        description="Find, delete, or symlink duplicate files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original-script mapping:\n"
            "  dupf.py       ->  report\n"
            "  findupy.py    ->  report --algorithm sha256 --json out.json\n"
            "  xordup.py     ->  report --algorithm xorhash\n"
            "  dupefix.py    ->  delete\n"
            "  dupfx.py      ->  delete --keep newest --quick-hash\n"
            "  fsimz.py      ->  delete --algorithm ppdeep\n"
            "  dedupsym.py   ->  symlink --stash-dir ~/dups\n"
            "  symdups.py    ->  symlink  /  restore\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("report", help="Show duplicate groups (no changes)")
    _add_scan_args(p, recursive_default=True)
    p.add_argument("--json", metavar="PATH", help="Export the found groups to a JSON file")
    p.set_defaults(func=cmd_report)
    p = sub.add_parser("delete", help="Delete duplicates, keep one per group")
    _add_scan_args(p, recursive_default=True)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without deleting",
    )
    p.add_argument(
        "--keep",
        choices=("first", "oldest", "newest"),
        default="oldest",
        help="Which file to keep in each group (default: oldest)",
    )
    p.add_argument(
        "--trash",
        dest="trash",
        action="store_true",
        default=None,
        help="Move to trash via 'gio' instead of unlink",
    )
    p.add_argument(
        "--no-trash",
        dest="trash",
        action="store_false",
        help="Always unlink (never use gio trash)",
    )
    p.set_defaults(func=cmd_delete)
    p = sub.add_parser("symlink", help="Move master copies to a stash and symlink the rest")
    _add_scan_args(p, recursive_default=True)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making changes",
    )
    p.add_argument(
        "--stash-dir",
        default=str(DEFAULT_STASH_DIR),
        help=f"Directory to store master copies (default: {DEFAULT_STASH_DIR})",
    )
    p.add_argument(
        "--manifest",
        default=str(DEFAULT_SYMLINK_MANIFEST),
        help=f"Manifest path (default: {DEFAULT_SYMLINK_MANIFEST})",
    )
    p.add_argument(
        "--prefer",
        choices=("first", "oldest", "newest", "shortest-name"),
        default="shortest-name",
        help="Which file becomes the master (default: shortest-name)",
    )
    p.set_defaults(func=cmd_symlink)
    p = sub.add_parser("restore", help="Reverse the symlink operation from a manifest")
    p.add_argument(
        "--manifest",
        default=str(DEFAULT_SYMLINK_MANIFEST),
        help=f"Manifest path (default: {DEFAULT_SYMLINK_MANIFEST})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making changes",
    )
    p.set_defaults(func=cmd_restore)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
