#!/data/data/com.termux/files/usr/bin/env python
import argparse
import fnmatch
import os
from pathlib import Path
import stat as st
import sys
import time
from typing import Iterator, Optional, Sequence
from dh import SKIP_DIRS

DIR_MODE: int = 0o775
FILE_MODE: int = 0o644
EXEC_MODE: int = 0o755
EXEC_BITS: int = st.S_IXUSR | st.S_IXGRP | st.S_IXOTH

RESET: str = "\033[0m"
GREEN: str = "\033[32m"
BLUE: str = "\033[34m"
YELLOW: str = "\033[33m"
RED: str = "\033[31m"
MAGENTA: str = "\033[35m"
CYAN: str = "\033[36m"

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


def chmod_safe(p: Path, mode: int) -> bool:
    try:
        p.chmod(mode)
        return True
    except (OSError, PermissionError):
        return False


def rel_str(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def convert_file_content(p: Path, dry_run: bool) -> tuple[bool, bool, bool, Optional[str]]:
    try:
        raw = p.read_bytes()
    except (OSError, PermissionError):
        return False, False, False, "read failed"

    content: str = ""
    encoding_fixed: bool = False

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        for enc in ("latin-1", "cp1252", "iso-8859-1"):
            try:
                content = raw.decode(enc)
                encoding_fixed = True
                break
            except UnicodeDecodeError:
                continue
        else:
            return False, False, False, "undecodable encoding"

    has_crlf = b"\r\n" in raw
    content_lf = content.replace("\r\n", "\n").replace("\r", "\n")
    crlf_fixed = has_crlf or (content_lf != content)

    tabs_fixed: bool = False
    new_lines = []
    for line in content_lf.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped and line.startswith("\t"):
            tabs_fixed = True
            indent_len = len(line) - len(stripped)
            spaces = "    " * indent_len
            new_lines.append(spaces + stripped)
        else:
            new_lines.append(line)

    new_content = "".join(new_lines)
    if new_content != content_lf:
        tabs_fixed = True

    if not encoding_fixed and not crlf_fixed and not tabs_fixed:
        return False, False, False, None

    if not dry_run:
        try:
            p.write_text(new_content, encoding="utf-8")
        except (OSError, PermissionError):
            return False, False, False, "write_failed"

    return encoding_fixed, crlf_fixed, tabs_fixed, None


class Config:
    def __init__(
        self,
        root: Path,
        exec_dirs: frozenset[str] = DEFAULT_EXEC_DIRS,
        skip_dirs: frozenset[str] = SKIP_DIRS,
        suffix_exec: tuple[str, ...] = DEFAULT_SUFFIX_EXEC,
        preserve_existing_exec: bool = True,
        dir_mode: int = DIR_MODE,
        file_mode: int = FILE_MODE,
        exec_mode: int = EXEC_MODE,
        deexec_patterns: tuple[str, ...] = DEFAULT_DEEXEC_PATTERNS,
        keep_exec_parents: frozenset[str] = DEFAULT_KEEP_EXEC_PARENTS,
    ) -> None:
        self.root: Path = root
        self.exec_dirs: frozenset[str] = exec_dirs
        self.skip_dirs: frozenset[str] = skip_dirs
        self.suffix_exec: tuple[str, ...] = tuple(s.lower() for s in suffix_exec)
        self.preserve_existing_exec: bool = preserve_existing_exec
        self.dir_mode: int = dir_mode
        self.file_mode: int = file_mode
        self.exec_mode: int = exec_mode
        self.deexec_patterns: tuple[str, ...] = deexec_patterns
        self.keep_exec_parents: frozenset[str] = keep_exec_parents

    def is_skipped(self, p: Path) -> bool:
        return any(part in self.skip_dirs for part in p.parts)

    def in_exec_dir(self, p: Path) -> bool:
        return any(part in self.exec_dirs for part in p.parts)

    def looks_executable(self, p: Path) -> bool:
        if p.suffix.lower() in self.suffix_exec:
            return True
        if has_shebang(p):
            return True
        return self.in_exec_dir(p)

    def matches_deexec(self, p: Path) -> bool:
        name = p.name
        return any(fnmatch.fnmatch(name, pat) for pat in self.deexec_patterns)

    def keeps_exec_by_parent(self, p: Path) -> bool:
        try:
            return p.parent.name in self.keep_exec_parents
        except (OSError, IndexError):
            return False


def walk_items(cfg: Config, include_dirs: bool = True, include_files: bool = True) -> Iterator[tuple[str, Path]]:
    stack = [cfg.root]
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as entries:
                for entry in entries:
                    if entry.name in cfg.skip_dirs:
                        continue
                    p = Path(entry.path)
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if include_dirs:
                            yield ("dir", p)
                        stack.append(p)
                    elif entry.is_file(follow_symlinks=False):
                        if include_files:
                            yield ("file", p)
        except (OSError, PermissionError):
            continue


class Stats:
    def __init__(self) -> None:
        self.total: int = 0
        self.dirs_changed: int = 0
        self.files_changed: int = 0
        self.files_made_exec: int = 0
        self.files_deexeced: int = 0
        self.dos2unix_fixed: int = 0
        self.encoding_fixed: int = 0
        self.tabs_fixed: int = 0
        self.skipped: int = 0
        self.errors: int = 0


def process_item(cfg: Config, kind: str, path: Path, mode: str, dry_run: bool) -> tuple[Stats, Optional[str]]:
    s = Stats()
    s.total = 1
    rp = rel_str(path, cfg.root)

    if cfg.is_skipped(path):
        s.skipped = 1
        return s, None

    if kind == "file":
        enc_fix, crlf_fix, tabs_fix, err = convert_file_content(path, dry_run)
        if err:
            s.errors = 1
            return s, f"{RED}[ERR]{RESET} {rp}: {err}"
        if enc_fix:
            s.encoding_fixed += 1
            print(f"{MAGENTA}[ENCODING]{RESET} {rp} -> utf-8")
        if crlf_fix:
            s.dos2unix_fixed += 1
            print(f"{YELLOW}[DOS2UNIX]{RESET} {rp}")
        if tabs_fix:
            s.tabs_fixed += 1
            print(f"{CYAN}[TABS->SPACES]{RESET} {rp}")

    cur = mode_of(path)
    if cur is None:
        s.errors = 1
        return s, f"{RED}[ERR]{RESET} {rp}: stat failed"

    if mode == "deexec":
        if not cfg.matches_deexec(path) or cfg.keeps_exec_by_parent(path) or not (cur & EXEC_BITS):
            s.skipped = 1
            return s, None

        target = cur & ~EXEC_BITS
        if not dry_run:
            if not chmod_safe(path, target):
                s.errors = 1
                return s, f"{RED}[ERR]{RESET} {rp}: chmod failed"
        s.files_deexeced = 1
        return s, f"{YELLOW}[DEEXEC]{RESET} {rp} {oct(cur)}->{oct(target)}"

    if kind == "dir":
        target = cfg.dir_mode
        if cur == target:
            return s, None
        if not dry_run:
            if not chmod_safe(path, target):
                s.errors = 1
                return s, f"{RED}[ERR]{RESET} {rp}: chmod failed"
        s.dirs_changed = 1
        return s, f"{BLUE}[DIR]{RESET} {rp} {oct(cur)}->{oct(target)}"

    if mode == "addx":
        if is_executable(path) or not cfg.looks_executable(path):
            s.skipped = 1
            return s, None
        target = cfg.exec_mode
        if not dry_run:
            if not chmod_safe(path, target):
                s.errors = 1
                return s, f"{RED}[ERR]{RESET} {rp}: chmod failed"
        s.files_made_exec = 1
        return s, f"{GREEN}[EXEC+]{RESET} {rp} {oct(cur)}->{oct(target)}"

    if kind == "file" and is_executable(path) and not cfg.looks_executable(path) and not cfg.keeps_exec_by_parent(path):
        target = cur & ~EXEC_BITS
        if not dry_run:
            if not chmod_safe(path, target):
                s.errors = 1
                return s, f"{RED}[ERR]{RESET} {rp}: chmod failed"
        s.files_deexeced = 1
        return s, f"{YELLOW}[DEEXEC-NONEXEC]{RESET} {rp} {oct(cur)}->{oct(target)}"

    if is_executable(path) and cfg.preserve_existing_exec:
        s.skipped = 1
        return s, None

    target = cfg.exec_mode if cfg.looks_executable(path) else cfg.file_mode
    if cur == target:
        return s, None

    if not dry_run:
        if not chmod_safe(path, target):
            s.errors = 1
            return s, f"{RED}[ERR]{RESET} {rp}: chmod failed"

    if target == cfg.exec_mode:
        s.files_made_exec = 1
        return s, f"{GREEN}[EXEC]{RESET} {rp} {oct(cur)}->{oct(target)}"
    else:
        s.files_changed = 1
        return s, f"{BLUE}[FILE]{RESET} {rp} {oct(cur)}->{oct(target)}"


def run_serial(cfg: Config, mode: str, dry_run: bool, include_dirs: bool, include_files: bool) -> Stats:
    total = Stats()
    gen = walk_items(cfg, include_dirs=include_dirs, include_files=include_files)
    for kind, path in gen:
        st_res, msg = process_item(cfg, kind, path, mode, dry_run)
        total.total += st_res.total
        total.dirs_changed += st_res.dirs_changed
        total.files_changed += st_res.files_changed
        total.files_made_exec += st_res.files_made_exec
        total.files_deexeced += st_res.files_deexeced
        total.dos2unix_fixed += st_res.dos2unix_fixed
        total.encoding_fixed += st_res.encoding_fixed
        total.tabs_fixed += st_res.tabs_fixed
        total.skipped += st_res.skipped
        total.errors += st_res.errors
        if msg:
            print(msg)
    return total


def report(s: Stats, elapsed: float) -> None:
    print(f"\n{'-' * 40}")
    print(f"Time elapsed: {elapsed:.2f}s")
    print(f"Total items: {s.total}")
    print(f"Skipped: {s.skipped}")
    print(f"Dirs changed: {s.dirs_changed}")
    print(f"Files changed: {s.files_changed}")
    print(f"Files made exec: {s.files_made_exec}")
    print(f"Files de-execed: {s.files_deexeced}")
    print(f"Line endings converted (dos2unix): {s.dos2unix_fixed}")
    print(f"Encodings normalized to UTF-8: {s.encoding_fixed}")
    print(f"Indentation tabs converted to spaces: {s.tabs_fixed}")
    print(f"Errors: {s.errors}")
    print(f"{'-' * 40}")


def parse_mode(s: str) -> int:
    try:
        return int(s, 8)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid octal mode: {s!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chmodnorm.py")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--skip-dir", action="append", default=None)
    p.add_argument("--exec-dir", action="append", default=None)
    p.add_argument("--suffix-exec", action="append", default=None)
    p.add_argument("--no-preserve-existing-exec", action="store_true")
    p.add_argument("--dir-mode", type=parse_mode, default=oct(DIR_MODE))
    p.add_argument("--file-mode", type=parse_mode, default=oct(FILE_MODE))
    p.add_argument("--exec-mode", type=parse_mode, default=oct(EXEC_MODE))
    p.add_argument("--keep-exec-parent", action="append", default=None)
    p.add_argument("mode", nargs="?", default="all", choices=["all", "dirs", "files", "addx", "deexec"])
    p.add_argument("patterns", nargs="*", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def build_config(args: argparse.Namespace) -> Config:
    skip = frozenset(args.skip_dir) if args.skip_dir else SKIP_DIRS
    execd = frozenset(args.exec_dir) if args.exec_dir else DEFAULT_EXEC_DIRS

    if args.suffix_exec:
        suffix = tuple(s.lower() for s in args.suffix_exec)
    else:
        suffix = DEFAULT_SUFFIX_EXEC

    patterns = tuple(args.patterns) if args.patterns else DEFAULT_DEEXEC_PATTERNS
    keep = frozenset(args.keep_exec_parent) if args.keep_exec_parent else DEFAULT_KEEP_EXEC_PARENTS
    return Config(
        root=Path(args.path).resolve(),
        skip_dirs=skip,
        exec_dirs=execd,
        suffix_exec=suffix,
        preserve_existing_exec=not args.no_preserve_existing_exec,
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
        print(f"Error: path does not exist: {cfg.root}", file=sys.stderr)
        return 1

    include_dirs = args.mode in ("all", "dirs", "addx")
    include_files = args.mode in ("all", "files", "addx", "deexec")

    t0 = time.time()
    s = run_serial(cfg, args.mode, args.dry_run, include_dirs, include_files)
    report(s, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
