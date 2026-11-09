#!/data/data/com.termux/files/usr/bin/env python
"""Directory and file listing toolkit.
Subcommands: ``stems`` prints the file stems truncated at the first dash, ``dirs`` the directory names with a ``-`` prefix and ``ls`` a listing with colours, icons, sizes, git status, a tree mode and a JSON output, with sorting and filter options.
Usage: list_dir {stems,dirs,ls} [options] [directory] Positional arguments: directory paths ...
Subcommands: stems Print file stems, truncated at the first '-' dirs Print directory names prefixed with '-' ls List directory contents Options: --sep Separator (default: 3 spaces) -R, --recursive Recurse into subdirectories (files only) --tree Print a tree -l, --long Long listing --json JSON output --git Annotate entries with git status flags --icons Prefix icons -a, --all Include dotfiles --exclude-cache Skip .git/__pycache__/.mypy_cache/.ruff_cache --sort-by ...
plus 6 more; run ``list_dir --help`` for the full list.
Examples: $ list_dir ls -l $ list_dir ls --tree --icons ~/bin Command-line script: run ``python list_dir.py`` or the ``list_dir`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
import datetime as dt
import json as jsonlib
import os
from pathlib import Path
import re
import stat as statlib
import subprocess
import sys
from typing import Sequence


DEFAULT_EXCLUDES: frozenset[str] = frozenset({".mypy_cache", ".ruff_cache", ".git", "__pycache__"})
ICON_IMAGE_EXTS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
ICON_CODE_EXTS: frozenset[str] = frozenset({"py", "sh"})
ICON_ARCHIVE_EXTS: frozenset[str] = frozenset({"zip", "tar", "gz", "bz2", "xz"})
RESET: str = "\x1b[0m"
C_DIR: str = "\x1b[1;34m"
C_LINK: str = "\x1b[36m"
C_EXEC: str = "\x1b[1;32m"
ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class LsOptions:
    recursive: bool = False
    sort_by: str = "name"
    reverse: bool = False
    files_first: bool = False
    dirs_first: bool = False
    exclude_cache: bool = False
    all_: bool = False
    color: bool = True
    layout: str = "name-first"
    long: bool = False
    json: bool = False
    git: bool = False
    icons: bool = False
    time_format: str = "%H:%M"


class Entry:
    __slots__ = ("git", "link_target", "name", "path", "st")

    def __init__(
        self,
        path: str,
        name: str,
        st: os.stat_result,
        link_target: str | None = None,
        git: str | None = None,
    ) -> None:
        self.path = path
        self.name = name
        self.st = st
        self.link_target = link_target
        self.git = git


def human_size(size: int) -> str:
    size = abs(int(size))
    if size < 1024:
        return f"{size} B"
    units = ("K", "M", "G", "T", "P")
    i = -1
    v = float(size)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if v < 10:
        s = f"{v:.1f}".removesuffix(".0")
    else:
        s = f"{int(v)}"
    return f"{s} {units[i]}B"


def dir_size(root: Path) -> int:
    total = 0
    seen: set[tuple[int, int]] = set()
    stack: list[Path] = [root]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    m = st.st_mode
                    if statlib.S_ISLNK(m):
                        total += st.st_size
                    elif statlib.S_ISREG(m):
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                        total += st.st_size
                    elif statlib.S_ISDIR(m):
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return total


def effective_size(e: Entry) -> int:
    if statlib.S_ISDIR(e.st.st_mode):
        return dir_size(Path(e.path))
    return e.st.st_size


def colorize(text: str, st_mode: int, enabled: bool) -> str:
    if not enabled:
        return text
    if statlib.S_ISDIR(st_mode):
        return f"{C_DIR}{text}{RESET}"
    if statlib.S_ISLNK(st_mode):
        return f"{C_LINK}{text}{RESET}"
    if st_mode & statlib.S_IXUSR:
        return f"{C_EXEC}{text}{RESET}"
    return text


def visible_len(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def format_time(ts: float, fmt: str) -> str:
    return dt.datetime.fromtimestamp(ts).strftime(fmt)


def icon_for(e: Entry) -> str:
    m = e.st.st_mode
    if statlib.S_ISDIR(m):
        return "\U0001f4c1"
    if statlib.S_ISLNK(m):
        return "\U0001f517"
    name = e.name.lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in ICON_IMAGE_EXTS:
        return "\U0001f5bc"
    if ext in ICON_CODE_EXTS:
        return "\U0001f40d"
    if ext in ICON_ARCHIVE_EXTS:
        return "\U0001f4e6"
    return "\U0001f4c4"


def resolve_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty()


def git_status_map(directory: Path) -> dict[str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "status", "--porcelain=v2", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return {}
    result: dict[str, str] = {}
    for chunk in proc.stdout.split(b"\x00"):
        if not chunk.startswith(b"1 "):
            continue
        parts = chunk.split(b" ")
        if len(parts) < 8:
            continue
        xy = parts[1].decode("utf-8", errors="ignore")
        name = parts[-1].decode("utf-8", errors="ignore")
        result[name] = xy
    return result


def list_entries(directory: Path, opts: LsOptions) -> list[Entry]:
    try:
        raw = list(directory.iterdir())
    except (PermissionError, OSError) as exc:
        print(f"cannot open '{directory}': {exc}", file=sys.stderr)
        return []
    git_map = git_status_map(directory) if opts.git else {}
    entries: list[Entry] = []
    for p in raw:
        name = p.name
        if not opts.all_ and name.startswith("."):
            continue
        if opts.exclude_cache and name in DEFAULT_EXCLUDES:
            continue
        try:
            st = p.lstat()
        except (FileNotFoundError, OSError):
            continue
        link_target: str | None = None
        if statlib.S_ISLNK(st.st_mode):
            try:
                link_target = os.readlink(p)
            except OSError:
                link_target = None
        entries.append(Entry(str(p), name, st, link_target, git_map.get(name)))
    return entries


def sort_entries(entries: list[Entry], opts: LsOptions) -> list[Entry]:
    keyfuncs = {
        "name": lambda e: e.name.lower(),
        "size": lambda e: e.st.st_size,
        "mtime": lambda e: e.st.st_mtime,
        "ctime": lambda e: e.st.st_ctime,
    }
    key = keyfuncs[opts.sort_by]
    result = sorted(entries, key=key, reverse=opts.reverse)
    if opts.files_first:
        result = sorted(result, key=lambda e: statlib.S_ISDIR(e.st.st_mode))
    elif opts.dirs_first:
        result = sorted(result, key=lambda e: not statlib.S_ISDIR(e.st.st_mode))
    return result


def render_flat(entries: list[Entry], opts: LsOptions) -> None:
    if not entries:
        return
    rows: list[tuple[str, str, str, str]] = []
    for e in entries:
        name = e.name
        if opts.icons:
            name = f"{icon_for(e)} {name}"
        colored = colorize(name, e.st.st_mode, opts.color)
        size_str = human_size(effective_size(e))
        ts = e.st.st_ctime if opts.sort_by == "ctime" else e.st.st_mtime
        time_str = format_time(ts, opts.time_format)
        git_str = f" [{e.git}]" if e.git else ""
        rows.append((colored, size_str, time_str, git_str))
    name_w = max(visible_len(r[0]) for r in rows)
    size_w = max(len(r[1]) for r in rows)
    for colored, size_str, time_str, git_str in rows:
        pad = " " * (name_w - visible_len(colored))
        if opts.layout == "size-first":
            print(f"{size_str.rjust(size_w)}  {colored}{pad}  {time_str}{git_str}")
        else:
            print(f"{colored}{pad}  {size_str.rjust(size_w)}  {time_str}{git_str}")


def render_long(entries: list[Entry], opts: LsOptions) -> None:
    pwd = None
    grp = None
    try:
        import grp as _grp
        import pwd as _pwd

        pwd = _pwd
        grp = _grp
    except ImportError:
        pass
    for e in entries:
        st = e.st
        mode = statlib.filemode(st.st_mode)
        nlink = st.st_nlink
        if pwd is not None:
            try:
                uname = pwd.getpwuid(st.st_uid).pw_name
            except (KeyError, OSError):
                uname = str(st.st_uid)
        else:
            uname = str(st.st_uid)
        if grp is not None:
            try:
                gname = grp.getgrgid(st.st_gid).gr_name
            except (KeyError, OSError):
                gname = str(st.st_gid)
        else:
            gname = str(st.st_gid)
        size_str = human_size(st.st_size)
        ts = st.st_ctime if opts.sort_by == "ctime" else st.st_mtime
        time_str = format_time(ts, opts.time_format)
        name = e.name
        if opts.icons:
            name = f"{icon_for(e)} {name}"
        name = colorize(name, st.st_mode, opts.color)
        if e.link_target:
            name += f" -> {e.link_target}"
        suffix = f" {e.git}" if e.git else ""
        print(f"{mode} {nlink:>2} {uname:<8} {gname:<8} {size_str:>8} {time_str} {name}{suffix}")


def render_json(entries: list[Entry], opts: LsOptions) -> None:
    out = []
    for e in entries:
        m = e.st.st_mode
        if statlib.S_ISDIR(m):
            kind = "dir"
        elif statlib.S_ISLNK(m):
            kind = "link"
        else:
            kind = "file"
        out.append({
            "name": e.name,
            "size": e.st.st_size,
            "mode": statlib.filemode(m),
            "mtime": e.st.st_mtime,
            "git": e.git,
            "type": kind,
        })
    print(jsonlib.dumps(out, indent=2))


def dispatch_render(entries: list[Entry], opts: LsOptions) -> None:
    if opts.json:
        render_json(entries, opts)
    elif opts.long:
        render_long(entries, opts)
    else:
        render_flat(entries, opts)


def render_tree(
    directory: Path,
    opts: LsOptions,
    prefix: str = "",
    is_root: bool = True,
) -> None:
    if is_root:
        print(f"{directory}")
    entries = list_entries(directory, opts)
    entries.sort(key=lambda e: (not statlib.S_ISDIR(e.st.st_mode), e.name.lower()))
    for i, e in enumerate(entries):
        last = i == len(entries) - 1
        connector = "\u2514\u2500\u2500 " if last else "\u251c\u2500\u2500 "
        name = e.name
        if opts.icons:
            name = f"{icon_for(e)} {name}"
        name = colorize(name, e.st.st_mode, opts.color)
        print(prefix + connector + name)
        if statlib.S_ISDIR(e.st.st_mode) and not statlib.S_ISLNK(e.st.st_mode):
            child_prefix = prefix + ("    " if last else "\u2502   ")
            render_tree(Path(e.path), opts, child_prefix, is_root=False)


def recursive_ls(root: Path, opts: LsOptions) -> None:
    all_entries: list[Entry] = []
    try:
        iterator = root.rglob("*")
    except OSError:
        return
    for p in iterator:
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if opts.exclude_cache and any(part in DEFAULT_EXCLUDES for part in rel_parts):
            continue
        if not opts.all_ and p.name.startswith("."):
            continue
        try:
            st = p.lstat()
        except (FileNotFoundError, OSError):
            continue
        if statlib.S_ISDIR(st.st_mode):
            continue
        link: str | None = None
        if statlib.S_ISLNK(st.st_mode):
            try:
                link = os.readlink(p)
            except OSError:
                link = None
        all_entries.append(Entry(str(p), p.name, st, link))
    all_entries = sort_entries(all_entries, opts)
    dispatch_render(all_entries, opts)


def run_stems(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    try:
        entries = list(root.glob("*"))
    except OSError as exc:
        print(f"cannot list '{root}': {exc}", file=sys.stderr)
        return 2
    names: list[str] = []
    for p in entries:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        stem = p.stem
        idx = stem.find("-")
        if idx >= 0:
            stem = stem[:idx]
        names.append(stem)
    sys.stdout.write(args.sep.join(names))
    if names:
        sys.stdout.write(args.sep)
    return 0


def run_dirs(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    try:
        entries = list(root.glob("*"))
    except OSError as exc:
        print(f"cannot list '{root}': {exc}", file=sys.stderr)
        return 2
    for p in entries:
        try:
            if p.is_dir():
                print(f"-{p.name}")
        except OSError:
            continue
    return 0


def run_ls(args: argparse.Namespace) -> int:
    opts = LsOptions(
        recursive=args.recursive,
        sort_by=args.sort_by,
        reverse=args.reverse,
        files_first=args.files_first,
        dirs_first=args.dirs_first,
        exclude_cache=args.exclude_cache,
        all_=args.all,
        color=resolve_color(args.color),
        layout=args.layout,
        long=args.long,
        json=args.json,
        git=args.git,
        icons=args.icons,
        time_format=args.time_format,
    )
    paths = [Path(p) for p in (args.paths or ["."])]
    multiple = len(paths) > 1
    exit_code = 0
    for i, path in enumerate(paths):
        if multiple:
            if i > 0:
                print()
            print(f"{path}:")
        if not path.exists() and not path.is_symlink():
            print(
                f"cannot access '{path}': No such file or directory",
                file=sys.stderr,
            )
            exit_code = 2
            continue
        try:
            is_single_file = path.is_file() or path.is_symlink()
        except OSError:
            is_single_file = False
        if is_single_file:
            try:
                st = path.lstat()
            except (FileNotFoundError, OSError):
                continue
            link: str | None = None
            if statlib.S_ISLNK(st.st_mode):
                try:
                    link = os.readlink(path)
                except OSError:
                    link = None
            dispatch_render([Entry(str(path), path.name, st, link)], opts)
            continue
        if args.tree:
            render_tree(path, opts)
        elif opts.recursive:
            recursive_ls(path, opts)
        else:
            dispatch_render(sort_entries(list_entries(path, opts), opts), opts)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="listpy",
        description="Unified directory and file listing toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  listpy stems\n"
            "  listpy dirs /tmp\n"
            "  listpy ls -R --sort-by mtime --exclude-cache\n"
            "  listpy ls --long --all --git\n"
            "  listpy ls --tree --icons\n"
            "  listpy ls --json src/\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_stems = sub.add_parser("stems", help="Print file stems, truncated at the first '-'")
    p_stems.add_argument("directory", nargs="?", default=".")
    p_stems.add_argument("--sep", default="   ", help="Separator (default: 3 spaces)")
    p_dirs = sub.add_parser("dirs", help="Print directory names prefixed with '-'")
    p_dirs.add_argument("directory", nargs="?", default=".")
    p_ls = sub.add_parser("ls", help="List directory contents")
    p_ls.add_argument("paths", nargs="*", default=None)
    p_ls.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help="Recurse into subdirectories (files only)",
    )
    p_ls.add_argument("--tree", action="store_true", help="Print a tree")
    p_ls.add_argument("-l", "--long", action="store_true", help="Long listing")
    p_ls.add_argument("--json", action="store_true", help="JSON output")
    p_ls.add_argument(
        "--git",
        action="store_true",
        help="Annotate entries with git status flags",
    )
    p_ls.add_argument("--icons", action="store_true", help="Prefix icons")
    p_ls.add_argument("-a", "--all", action="store_true", help="Include dotfiles")
    p_ls.add_argument(
        "--exclude-cache",
        action="store_true",
        help="Skip .git/__pycache__/.mypy_cache/.ruff_cache",
    )
    p_ls.add_argument(
        "--sort-by",
        choices=("name", "size", "mtime", "ctime"),
        default="name",
    )
    p_ls.add_argument("-r", "--reverse", action="store_true")
    p_ls.add_argument("--files-first", action="store_true")
    p_ls.add_argument("--dirs-first", action="store_true")
    p_ls.add_argument(
        "--layout",
        choices=("name-first", "size-first"),
        default="name-first",
    )
    p_ls.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
    )
    p_ls.add_argument("--time-format", default="%H:%M")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "stems":
        return run_stems(args)
    if args.command == "dirs":
        return run_dirs(args)
    if args.command == "ls":
        return run_ls(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
