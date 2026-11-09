#!/data/data/com.termux/files/usr/bin/env python
"""Normalise the file and directory permissions of a tree.
Sets the directories to 0775 and the files to 0644 (configurable), adds the execute bit to the scripts (shebang, ``.sh``/``.so`` suffix or the ``bin``-style parent folders) and removes it elsewhere, with ``--no-preserve-existing-exec``, ``--dir-mode``/``--file-mode`` and the parallel/serial runners.
Usage: nch [PATH] [--dir-mode 0775] [--file-mode 0644] [--skip-dir NAME] Positional arguments: path Root path (default: current directory) mode Which pipeline to run (default: all) patterns ...
Glob patterns for deexec mode (default: *.py).
Options: --skip-dir Directory name to skip (repeatable) --exec-dir Parent dir name that marks files executable (repeatable) --suffix-exec File suffix that marks files executable (repeatable, e.g.
.sh) --no-preserve-existing-exec Also normalize already-executable files --no-binary-check Disable dh.is_binary() (falls back to shebang only) --dir-mode Octal mode for directories (default: 0775) --file-mode Octal mode for regular files (default: 0644) --exec-mode Octal mode for executable files (default: 0755) --keep-exec-parent Parent dir name whose files keep exec state in deexec mode (repeatable; default: bin, sbin) --dry-run Analyze but do not change anything ...
plus 3 more; run ``nch --help`` for the full list.
Examples: $ nch ~/bin --dry-run Changes the permissions of every file below the path; review the list before running it on a system tree."""

import argparse
from dataclasses import dataclass, field
import fnmatch
import multiprocessing as mp
import os
from pathlib import Path
import stat as st
import sys
import time
from typing import Iterator, Optional, Sequence

from dh import MAX_WORKERS, SKIP_DIRS


try:
    from tqdm import tqdm
except Exception:

    def tqdm(it, **_kwargs):
        return it


DIR_MODE: int = 0o775
FILE_MODE: int = 0o644
EXEC_MODE: int = 0o755
EXEC_BITS: int = st.S_IXUSR | st.S_IXGRP | st.S_IXOTH
DEFAULT_EXEC_DIRS: frozenset[str] = frozenset({
    "bin",
    "sbin",
    ".bin",
    "libexec",
    "scripts",
    "tools",
})
DEFAULT_SUFFIX_EXEC: tuple[str, ...] = (".sh", ".so")
DEFAULT_DEEXEC_PATTERNS: tuple[str, ...] = ("*.py",)
DEFAULT_KEEP_EXEC_PARENTS: frozenset[str] = frozenset({"bin", "sbin"})


def mode_of(p: Path) -> Optional[int]:
    try:
        return st.S_IMODE(p.stat().st_mode)
    except (OSError, PermissionError):
        return None


def is_symlink(p: Path) -> bool:
    try:
        return p.is_symlink()
    except OSError:
        return False


def has_shebang(p: Path) -> bool:
    try:
        with p.open("rb") as fh:
            return fh.readline().startswith(b"#!")
    except OSError:
        return False


def is_executable(p: Path) -> bool:
    try:
        return bool(p.stat().st_mode & EXEC_BITS)
    except OSError:
        return False


def parent_writable(p: Path) -> bool:
    try:
        parent = p.parent
        return parent.exists() and os.access(str(parent), os.W_OK)
    except (OSError, PermissionError):
        return False


def chmod_safe(p: Path, mode: int) -> bool:
    try:
        p.chmod(mode)
        return True
    except (OSError, PermissionError):
        return False


def chmod_add_x(p: Path) -> bool:
    try:
        base = p.stat().st_mode
    except OSError:
        return False
    bits = [st.S_IXUSR, st.S_IXGRP, st.S_IXOTH]
    for n in range(len(bits), 0, -1):
        new = base
        for b in bits[:n]:
            new |= b
        try:
            p.chmod(new)
            return True
        except OSError:
            continue
    return False


def chmod_remove_x(p: Path) -> bool:
    try:
        base = p.stat().st_mode
    except OSError:
        return False
    new_mode = base & ~EXEC_BITS
    try:
        p.chmod(new_mode)
        return True
    except OSError:
        return False


@dataclass
class Config:
    root: Path
    exec_dirs: frozenset[str] = DEFAULT_EXEC_DIRS
    suffix_exec: tuple[str, ...] = DEFAULT_SUFFIX_EXEC
    preserve_existing_exec: bool = True
    use_binary_check: bool = True
    dir_mode: int = DIR_MODE
    file_mode: int = FILE_MODE
    exec_mode: int = EXEC_MODE
    deexec_patterns: tuple[str, ...] = DEFAULT_DEEXEC_PATTERNS
    keep_exec_parents: frozenset[str] = DEFAULT_KEEP_EXEC_PARENTS

    def is_skipped(self, p: Path) -> bool:
        return any(part in self.skip_dirs for part in p.parts)

    def in_exec_dir(self, p: Path) -> bool:
        return any(part in self.exec_dirs for part in p.parts)

    def looks_executable_special(self, p: Path) -> bool:
        if p.suffix in self.suffix_exec:
            return True
        if has_shebang(p):
            return True
        if self.use_binary_check and _HAVE_DH and _is_binary(p):
            return True
        return bool(self.in_exec_dir(p))

    def matches_deexec(self, p: Path) -> bool:
        name = p.name
        return any(fnmatch.fnmatch(name, pat) for pat in self.deexec_patterns)

    def keeps_exec_by_parent(self, p: Path) -> bool:
        try:
            return p.parent.name in self.keep_exec_parents
        except (OSError, IndexError):
            return False


def walk_items(cfg: Config, include_dirs: bool = True, include_files: bool = True) -> Iterator[tuple[str, Path]]:
    for dirpath, dirnames, filenames in os.walk(str(cfg.root), topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in cfg.skip_dirs]
        base = Path(dirpath)
        if include_dirs:
            yield ("dir", base)
        if include_files:
            for name in filenames:
                p = base / name
                if is_symlink(p):
                    continue
                yield ("file", p)


@dataclass
class Decision:
    kind: str
    path: Path
    current: Optional[int]
    target: Optional[int]
    reason: str = ""


def decide(cfg: Config, kind: str, path: Path) -> Decision:
    if kind == "dir":
        cur = mode_of(path)
        if cur is None:
            return Decision("error", path, None, None, "stat failed")
        if cur == cfg.dir_mode:
            return Decision("skip_correct", path, cur, cfg.dir_mode)
        return Decision("change", path, cur, cfg.dir_mode)
    if is_executable(path):
        if cfg.preserve_existing_exec:
            return Decision("skip_exec", path, mode_of(path), None, "already executable")
        cur = mode_of(path) or 0
        return Decision("change", path, cur, cfg.exec_mode)
    cur = mode_of(path)
    if cur is None:
        return Decision("error", path, None, None, "stat failed")
    if cfg.looks_executable_special(path):
        target = cfg.exec_mode
    else:
        target = cfg.file_mode
    if cur == target:
        return Decision("skip_correct", path, cur, target)
    return Decision("change", path, cur, target)


@dataclass
class Stats:
    total: int = 0
    dirs_changed: int = 0
    files_changed: int = 0
    files_made_exec: int = 0
    files_deexeced: int = 0
    files_kept_exec: int = 0
    skipped: int = 0
    errors: int = 0
    permission_errors: int = 0
    other_errors: int = 0
    messages: list[str] = field(default_factory=list)

    def merge(self, other: "Stats") -> None:
        for f in (
            "total",
            "dirs_changed",
            "files_changed",
            "files_made_exec",
            "files_deexeced",
            "files_kept_exec",
            "skipped",
            "errors",
            "permission_errors",
            "other_errors",
        ):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.messages.extend(other.messages)


def apply_one(cfg: Config, kind: str, path: Path) -> Stats:
    s = Stats(total=1)
    try:
        if cfg.is_skipped(path):
            s.skipped += 1
            return s
        d = decide(cfg, kind, path)
        if d.kind == "skip_exec":
            s.skipped += 1
            return s
        if d.kind == "skip_correct":
            return s
        if d.kind == "error":
            s.errors += 1
            s.other_errors += 1
            s.messages.append(f"[ERR] {path}: {d.reason}")
            return s
        if not parent_writable(path):
            s.errors += 1
            s.permission_errors += 1
            s.messages.append(f"[PERM] {path}: parent not writable")
            return s
        if not chmod_safe(path, d.target):
            s.errors += 1
            s.permission_errors += 1
            s.messages.append(f"[PERM] {path}: chmod failed")
            return s
        old, new = d.current or 0, d.target or 0
        rel = str(path)
        if len(rel) > 80:
            rel = "..." + rel[-77:]
        if kind == "dir":
            s.dirs_changed += 1
            s.messages.append(f"[DIR]  {rel} {oct(old)}->{oct(new)}")
        elif new == cfg.exec_mode and old != new and not is_executable(path):
            s.files_made_exec += 1
            s.messages.append(f"[EXEC] {rel} {oct(old)}->{oct(new)}")
        else:
            s.files_changed += 1
            s.messages.append(f"[FILE] {rel} {oct(old)}->{oct(new)}")
    except Exception as exc:
        s.errors += 1
        s.other_errors += 1
        s.messages.append(f"[ERR]  {path}: {type(exc).__name__}: {exc}")
    return s


def run_addx(cfg: Config, *, dry_run: bool, verbose: bool) -> Stats:
    s = Stats()
    items = list(walk_items(cfg))
    for kind, path in tqdm(items, desc="addx", unit="items"):
        s.total += 1
        if cfg.is_skipped(path):
            s.skipped += 1
            continue
        if kind == "dir":
            cur = mode_of(path)
            if cur is None:
                s.errors += 1
                continue
            if cur != cfg.dir_mode and not dry_run:
                if chmod_safe(path, cfg.dir_mode):
                    s.dirs_changed += 1
                    if verbose:
                        s.messages.append(f"[DIR]  {path} {oct(cur)}->{oct(cfg.dir_mode)}")
                else:
                    s.errors += 1
            continue
        if is_executable(path):
            s.skipped += 1
            continue
        if not cfg.looks_executable_special(path):
            s.skipped += 1
            continue
        if dry_run:
            s.files_made_exec += 1
            continue
        if chmod_add_x(path):
            s.files_made_exec += 1
            if verbose:
                s.messages.append(f"[EXEC+] {path}")
        else:
            s.errors += 1
            s.permission_errors += 1
    return s


def run_deexec(cfg: Config, *, dry_run: bool, verbose: bool) -> Stats:
    s = Stats()
    items = list(walk_items(cfg, include_dirs=False, include_files=True))
    for kind, path in tqdm(items, desc="deexec", unit="items"):
        s.total += 1
        if cfg.is_skipped(path):
            s.skipped += 1
            continue
        if not cfg.matches_deexec(path):
            s.skipped += 1
            continue
        if cfg.keeps_exec_by_parent(path):
            s.files_kept_exec += 1
            if verbose:
                s.messages.append(f"[KEEP] {path} (parent={path.parent.name})")
            continue
        cur = mode_of(path)
        if cur is None:
            s.errors += 1
            s.other_errors += 1
            s.messages.append(f"[ERR]  {path}: stat failed")
            continue
        if not (cur & EXEC_BITS):
            s.skipped += 1
            continue
        target = cur & ~EXEC_BITS
        if dry_run:
            s.files_deexeced += 1
            if verbose:
                s.messages.append(f"[DEEXEC*] {path} {oct(cur)}->{oct(target)}")
            continue
        if chmod_remove_x(path):
            s.files_deexeced += 1
            if verbose:
                s.messages.append(f"[DEEXEC] {path} {oct(cur)}->{oct(target)}")
        else:
            s.errors += 1
            s.permission_errors += 1
            s.messages.append(f"[PERM] {path}: chmod -x failed")
    return s


def run_parallel(cfg: Config, items: list[tuple[str, Path]], jobs: int) -> Stats:
    jobs = max(1, min(jobs, os.cpu_count() or 1, 32))
    chunk = max(1000, len(items) // (jobs * 10) or 1)

    def _work(pair):
        return apply_one(cfg, *pair)

    total = Stats()
    with mp.Pool(processes=jobs) as pool, tqdm(total=len(items), desc="parallel", unit="items") as bar:
        for st_ in pool.imap_unordered(_work, items, chunksize=chunk):
            total.merge(st_)
            bar.update(1)
    return total


def run_serial(cfg: Config, items: list[tuple[str, Path]], verbose: bool) -> Stats:
    total = Stats()
    for kind, path in tqdm(items, desc="scanning", unit="items"):
        total.merge(apply_one(cfg, kind, path))
    return total


def report(s: Stats, elapsed: float, verbose: bool) -> None:
    bar = "=" * 44
    print(f"\n{bar}\n📊 PERMISSION NORMALIZATION SUMMARY\n{'-' * 44}")
    print(f"⏱️  Time elapsed: {elapsed:.2f}s")
    print(f"📁 Total items processed: {s.total}")
    print(f"⏭️  Skipped: {s.skipped}")
    print("-" * 44)
    print(f"✓  Directories changed: {s.dirs_changed}")
    print(f"✓  Files normalized:     {s.files_changed}")
    print(f"✓  Files made executable:{s.files_made_exec}")
    if s.files_deexeced:
        print(f"✓  Files de-execed:      {s.files_deexeced}")
    if s.files_kept_exec:
        print(f"🔒 Files kept executable (bin/sbin): {s.files_kept_exec}")
    print(f"❌ Total errors:          {s.errors}")
    if s.permission_errors:
        print(f"   └─ Permission errors: {s.permission_errors}")
    if s.other_errors:
        print(f"   └─ Other errors:      {s.other_errors}")
    print(bar)
    if s.permission_errors:
        print("\n💡 Tip: permission errors can be fixed by:")
        print("  - Running with appropriate privileges (sudo/root)")
        print("  - Changing ownership of files")
        print("  - Running chmod on problematic directories first")
    if verbose and s.messages:
        print("\n📝 DETAILED CHANGES\n" + "-" * 44)
        buckets = {
            "[DIR]": [],
            "[EXEC]": [],
            "[EXEC+]": [],
            "[FILE]": [],
            "[DEEXEC]": [],
            "[DEEXEC*]": [],
            "[KEEP]": [],
            "[PERM]": [],
            "[ERR]": [],
        }
        for m in s.messages:
            for key in buckets:
                if m.startswith(key):
                    buckets[key].append(m)
                    break
        for key, label in (
            ("[DIR]", "Directory changes"),
            ("[EXEC]", "Files made executable"),
            ("[EXEC+]", "Files made executable (chained)"),
            ("[FILE]", "Files normalized"),
            ("[DEEXEC]", "Files de-execed"),
            ("[DEEXEC*]", "Files to de-exec (dry-run)"),
            ("[KEEP]", "Files kept executable (bin/sbin)"),
            ("[PERM]", "Permission errors"),
            ("[ERR]", "Errors"),
        ):
            if buckets[key]:
                print(f"\n{label}:")
                for m in buckets[key][:50]:
                    print(f"  {m}")
                if len(buckets[key]) > 50:
                    print(f"  ... and {len(buckets[key]) - 50} more")


def show_examples(cfg: Config, items: list[tuple[str, Path]], per_bucket: int = 5) -> None:
    buckets: dict[str, list[Path]] = {
        "dirs": [],
        "make_exec": [],
        "normalize": [],
        "skip_exec": [],
        "correct": [],
        "errors": [],
    }
    for kind, path in items:
        d = decide(cfg, kind, path)
        if d.kind == "skip_exec":
            buckets["skip_exec"].append(path)
        elif d.kind == "skip_correct":
            buckets["correct"].append(path)
        elif d.kind == "error":
            buckets["errors"].append(path)
        elif d.kind == "change":
            if kind == "dir":
                buckets["dirs"].append(path)
            elif d.target == cfg.exec_mode:
                buckets["make_exec"].append(path)
            else:
                buckets["normalize"].append(path)

    def dump(label: str, key: str) -> None:
        rows = buckets[key]
        if not rows:
            return
        print(f"\n{label} ({len(rows)}):")
        for p in rows[:per_bucket]:
            print(f"  {p}")
        if len(rows) > per_bucket:
            print(f"  ... and {len(rows) - per_bucket} more")

    dump("Directories to change", "dirs")
    dump("Files to make executable", "make_exec")
    dump("Files to normalize to 0644", "normalize")
    dump("Already-executable files (skipped)", "skip_exec")
    dump("Errors during analysis", "errors")


def parse_mode(s: str) -> int:
    try:
        return int(s, 8)
    except ValueError:
        msg = f"invalid octal mode: {s!r}"
        raise argparse.ArgumentTypeError(msg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chmodnorm.py",
        description="Normalize file and directory permissions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Modes:\n"
            "  all    - dirs + files (default)\n"
            "  dirs   - dirs only\n"
            "  files  - files only\n"
            "  addx   - chained x-bit addition (nchmod.py)\n"
            "  deexec - strip x bits from files matching patterns\n"
            "           (files under a parent named bin/ or sbin/ are kept)\n"
            "\n"
            "Examples:\n"
            "  chmodnorm.py deexec                     # strip x from *.py under CWD\n"
            "  chmodnorm.py deexec '*.py' '*.sh'       # strip x from *.py and *.sh\n"
            "  chmodnorm.py deexec --dry-run -v        # preview without changing\n"
        ),
    )
    p.add_argument("path", nargs="?", default=".", help="root path (default: current directory)")
    p.add_argument(
        "--skip-dir",
        action="append",
        default=None,
        help="directory name to skip (repeatable)",
    )
    p.add_argument(
        "--exec-dir",
        action="append",
        default=None,
        help="parent dir name that marks files executable (repeatable)",
    )
    p.add_argument(
        "--suffix-exec",
        action="append",
        default=None,
        help="file suffix that marks files executable (repeatable, e.g. .sh)",
    )
    p.add_argument(
        "--no-preserve-existing-exec",
        action="store_true",
        help="also normalize already-executable files",
    )
    p.add_argument(
        "--no-binary-check",
        action="store_true",
        help="disable dh.is_binary() (falls back to shebang only)",
    )
    p.add_argument(
        "--dir-mode",
        type=parse_mode,
        default=oct(DIR_MODE),
        help="octal mode for directories (default: 0775)",
    )
    p.add_argument(
        "--file-mode",
        type=parse_mode,
        default=oct(FILE_MODE),
        help="octal mode for regular files (default: 0644)",
    )
    p.add_argument(
        "--exec-mode",
        type=parse_mode,
        default=oct(EXEC_MODE),
        help="octal mode for executable files (default: 0755)",
    )
    p.add_argument(
        "--keep-exec-parent",
        action="append",
        default=None,
        help=("parent dir name whose files keep exec state in deexec mode (repeatable; default: bin, sbin)"),
    )
    p.add_argument(
        "mode",
        nargs="?",
        default="all",
        choices=["all", "dirs", "files", "addx", "deexec"],
        help="which pipeline to run (default: all)",
    )
    p.add_argument(
        "patterns",
        nargs="*",
        default=None,
        help="glob patterns for deexec mode (default: *.py).",
    )
    p.add_argument("--dry-run", action="store_true", help="analyze but do not change anything")
    p.add_argument(
        "--show-examples",
        action="store_true",
        help="print examples of items that would change",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="print detailed change log")
    p.add_argument("--parallel", action="store_true", help="use multiprocessing (chmodi.py style)")
    return p


def build_config(args: argparse.Namespace) -> Config:
    skip = frozenset(args.skip_dir) if args.skip_dir else SKIP_DIRS
    execd = frozenset(args.exec_dir) if args.exec_dir else DEFAULT_EXEC_DIRS
    suffix = tuple(args.suffix_exec) if args.suffix_exec else DEFAULT_SUFFIX_EXEC
    if args.mode == "deexec" and args.patterns:
        patterns = tuple(args.patterns)
    else:
        patterns = DEFAULT_DEEXEC_PATTERNS
    if args.keep_exec_parent:
        keep = frozenset(args.keep_exec_parent)
    else:
        keep = DEFAULT_KEEP_EXEC_PARENTS
    return Config(
        root=Path(args.path).resolve(),
        skip_dirs=skip,
        exec_dirs=execd,
        suffix_exec=suffix,
        preserve_existing_exec=not args.no_preserve_existing_exec,
        use_binary_check=not args.no_binary_check,
        dir_mode=args.dir_mode,
        file_mode=args.file_mode,
        exec_mode=args.exec_mode,
        deexec_patterns=patterns,
        keep_exec_parents=keep,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = build_config(args)
    if not cfg.root.exists():
        print(f"❌ Error: path does not exist: {cfg.root}", file=sys.stderr)
        return 1
    print(f"🔍 Root:      {cfg.root}")
    print(f"🧭 Mode:      {args.mode}")
    print(f"🚫 Skip dirs: {', '.join(sorted(cfg.skip_dirs))}")
    print(f"📂 Exec dirs: {', '.join(sorted(cfg.exec_dirs))}")
    if args.mode == "deexec":
        print(f"🎯 Patterns:  {', '.join(cfg.deexec_patterns)}")
        print(f"🔒 Keep-exec parents: {', '.join(sorted(cfg.keep_exec_parents))}")
    if args.dry_run:
        print("🧪 DRY RUN — no changes will be applied\n")
    t0 = time.time()
    if args.mode == "addx":
        s = run_addx(cfg, dry_run=args.dry_run, verbose=args.verbose)
        report(s, time.time() - t0, verbose=args.verbose)
        print("\n✅ Done!")
        return 0
    if args.mode == "deexec":
        s = run_deexec(cfg, dry_run=args.dry_run, verbose=args.verbose)
        report(s, time.time() - t0, verbose=args.verbose)
        print("\n✅ Done!")
        return 0
    include_dirs = args.mode in ("all", "dirs")
    include_files = args.mode in ("all", "files")
    items = list(walk_items(cfg, include_dirs=include_dirs, include_files=include_files))
    if not items:
        print("⚠️  No items found to process.")
        return 0
    if args.show_examples:
        show_examples(cfg, items)
    if args.dry_run:
        s = Stats(total=len(items))
        for kind, path in items:
            d = decide(cfg, kind, path)
            if d.kind == "skip_exec":
                s.skipped += 1
            elif d.kind == "error":
                s.errors += 1
            elif d.kind == "change":
                if kind == "dir":
                    s.dirs_changed += 1
                elif d.target == cfg.exec_mode:
                    s.files_made_exec += 1
                else:
                    s.files_changed += 1
        report(s, time.time() - t0, verbose=args.verbose)
        print("\n🧪 Dry run — nothing changed.")
        return 0
    if args.parallel or MAX_WORKERS:
        jobs = MAX_WORKERS if MAX_WORKERS > 0 else (os.cpu_count() or 1)
        print(f"⚙️  Parallel workers: {jobs}")
        s = run_parallel(cfg, items, jobs)
    else:
        s = run_serial(cfg, items, verbose=args.verbose)
    report(s, time.time() - t0, verbose=args.verbose)
    print("\n✅ Done!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
