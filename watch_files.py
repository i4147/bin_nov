#!/data/data/com.termux/files/usr/bin/env python
"""Watch files/directories for changes (stdlib polling, no deps needed).
Analysis / deduplication ------------------------ Six watchers did two jobs: - **Report changes**: ``watcher.py`` (watchfiles one-liner), ``dw.py`` and ``watch_file.py`` (mtime polling + tail), ``fwatcher.py`` (tail a Tor log until "100% (done)") -> ``watch`` plus ``tail``.
The original ``"boostraped 100%"`` typo is preserved as the default ``--until`` pattern set alongside the correct spellings.
- **Copy changed files somewhere**: ``dw -c``, ``where.py`` (watchdog, extension filters, debouncing) and ``wtmp.py`` (watchdog, archive suffixes, startup scan) -> ``watch --copy-to DIR`` with ``--extensions/--exclude-extensions``.
Polling was kept over watchfiles/watchdog so the tool needs zero third-party packages.
Examples -------- python3 file_watch_tool.py watch src/ python3 file_watch_tool.py watch .
--copy-to ~/tmp/tmp -e py,txt python3 file_watch_tool.py tail ~/.tor/tor.log --until "100% (done)" python3 file_watch_tool.py watch ~/Downloads --once # single scan"""

import argparse
from collections.abc import Sequence
from pathlib import Path
import shutil
import sys
import time


ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar.gz",
    ".whl",
    ".tar.xz",
    ".zip",
    ".tar.bz2",
    ".tgz",
    ".txz",
    ".tbz2",
)
DEFAULT_UNTIL: tuple[str, ...] = ("bootstrapped 100%", "boostraped 100%", "100% (done)")


def parse_extensions(spec: str | None) -> frozenset[str] | None:
    """Parse ``"py,.txt"`` into ``{'.py', '.txt'}`` (None = all)."""
    if not spec:
        return None
    return frozenset((p if p.startswith(".") else "." + p).lower() for p in spec.split(",") if p.strip())


def suffixes_of(path: Path) -> tuple[str, ...]:
    """All dotted suffix candidates, longest first (``.tar.gz``, ``.gz``)."""
    name: str = path.name.lower()
    return tuple(name[i:] for i, ch in enumerate(name) if ch == ".")


def matches(path: Path, allow: frozenset[str] | None, deny: frozenset[str] | None) -> bool:
    """Extension filter used for copy decisions."""
    suffixes: tuple[str, ...] = suffixes_of(path)
    if deny and any(s in deny for s in suffixes):
        return False
    return allow is None or any(s in allow for s in suffixes)


def snapshot(root: Path, recursive: bool) -> dict[Path, float]:
    """Map every file under *root* to its mtime."""
    state: dict[Path, float] = {}
    candidates = root.rglob("*") if recursive else root.iterdir()
    for path in candidates:
        try:
            if path.is_file():
                state[path] = path.stat().st_mtime
        except OSError:
            continue
    return state


def safe_copy(src: Path, dest_root: Path, root: Path, preserve: bool) -> Path | None:
    """Copy *src* into *dest_root*, optionally preserving relative dirs."""
    dest: Path = dest_root / (src.relative_to(root) if preserve and root in src.parents else Path(src.name))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest
    except OSError as exc:
        print(f"copy failed for {src}: {exc}", file=sys.stderr)
        return None


def tail_lines(path: Path, count: int) -> list[str]:
    """Return the last *count* lines of a text file."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


def until_hit(lines: Sequence[str], patterns: Sequence[str]) -> bool:
    """True if any --until pattern appears in *lines*."""
    joined: str = "\n".join(lines).lower()
    return any(pattern.lower() in joined for pattern in patterns)


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch a directory (or single file) and report/copy changes."""
    root: Path = args.target
    if root.is_file():
        return watch_single_file(args)
    if not root.is_dir():
        print(f"Error: not a file or directory: {root}", file=sys.stderr)
        return 1
    allow: frozenset[str] | None = parse_extensions(args.extensions)
    deny: frozenset[str] | None = parse_extensions(args.exclude_extensions)
    state: dict[Path, float] = snapshot(root, args.recursive)
    print(f"Watching {root} ({len(state)} file(s)); Ctrl+C to stop.")
    if args.scan_first and args.copy_to is not None:
        for path in sorted(state):
            if matches(path, allow, deny):
                safe_copy(path, args.copy_to, root, args.preserve)
    if args.once:
        return 0
    try:
        while True:
            time.sleep(args.interval)
            current: dict[Path, float] = snapshot(root, args.recursive)
            for path in sorted(set(state) - set(current)):
                print(f"[DELETED]  {path.relative_to(root) if root in path.parents else path}")
            for path in sorted(current):
                old: float | None = state.get(path)
                if old is not None and current[path] <= old:
                    continue
                event: str = "CREATED" if old is None else "MODIFIED"
                print(f"[{event}] {path.relative_to(root) if root in path.parents else path}")
                if args.copy_to is not None and matches(path, allow, deny):
                    dest: Path | None = safe_copy(path, args.copy_to, root, args.preserve)
                    if dest is not None:
                        print(f"           copied -> {dest}")
                if args.tail:
                    for line in tail_lines(path, args.tail):
                        print(f"           {line}")
                if args.until and until_hit(tail_lines(path, args.tail or 10), args.until):
                    print("Stop pattern seen; exiting.")
                    return 0
                if args.run:
                    run_command(args.run, path)
            state = current
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    return 0


def watch_single_file(args: argparse.Namespace) -> int:
    """Watch one file's mtime, printing its tail on each change."""
    path: Path = args.target
    print(f"Watching {path}; Ctrl+C to stop.")
    patterns: Sequence[str] = args.until or DEFAULT_UNTIL
    shown: list[str] = tail_lines(path, args.tail or 10)
    for line in shown:
        print(line)
    last_seen: int = len(tail_lines(path, 1_000_000))
    if args.once:
        return 0
    try:
        while True:
            time.sleep(args.interval)
            all_lines: list[str] = tail_lines(path, 1_000_000)
            new: list[str] = all_lines[last_seen:]
            if new:
                for line in new:
                    print(line)
                last_seen = len(all_lines)
                if until_hit(new, patterns):
                    print("Stop pattern seen; exiting.")
                    return 0
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    """Follow a log file until a pattern appears (fwatcher behaviour)."""
    args.target = args.file
    args.until = args.until or list(DEFAULT_UNTIL)
    args.tail = 5
    args.once = False
    return watch_single_file(args)


def run_command(template: str, changed: Path) -> None:
    """Run a shell command template where ``{}`` is the changed path."""
    import subprocess

    subprocess.run(template.replace("{}", str(changed)), shell=True, check=False)


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Polling file/directory watcher (stdlib only).")
    sub = parser.add_subparsers(dest="command", required=True)
    wa = sub.add_parser("watch", help="Watch a file or directory for changes")
    wa.add_argument("target", nargs="?", type=Path, default=Path.cwd())
    wa.add_argument("-i", "--interval", type=float, default=1.0, help="Poll seconds (default: 1.0)")
    wa.add_argument("--no-recursive", dest="recursive", action="store_false", help="Top level only")
    wa.add_argument("-c", "--copy-to", type=Path, default=None, help="Copy changed files here")
    wa.add_argument("--preserve", action="store_true", help="Preserve relative dirs when copying")
    wa.add_argument("--scan-first", action="store_true", help="Copy existing matches at startup")
    wa.add_argument("-e", "--extensions", default=None, help="Allowlist, e.g. py,txt (copy only)")
    wa.add_argument("-x", "--exclude-extensions", default=None, help="Denylist, e.g. pyc,log")
    wa.add_argument("--tail", type=int, default=0, metavar="N", help="Print last N lines of changed files")
    wa.add_argument("--until", action="append", default=[], metavar="TEXT", help="Exit when seen in tail")
    wa.add_argument("--run", default=None, metavar="CMD", help="Run CMD on change ({} = path)")
    wa.add_argument("--once", action="store_true", help="Single scan (with --scan-first) then exit")
    wa.set_defaults(handler=cmd_watch, recursive=True)
    ta = sub.add_parser("tail", help="Follow a log until a pattern appears")
    ta.add_argument("file", nargs="?", type=Path, default=Path("~/.tor/tor.log").expanduser())
    ta.add_argument("-i", "--interval", type=float, default=0.5)
    ta.add_argument("--until", action="append", default=[], metavar="TEXT")
    ta.set_defaults(handler=cmd_tail)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
