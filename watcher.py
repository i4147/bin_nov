#!/data/data/com.termux/files/usr/bin/env python
"""Unified recursive file/folder watcher."""


import argparse
import contextlib
from pathlib import Path
import shutil
import sys
import threading
import time
import traceback

from dh import fsz
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


def tail_file(fname, n: int = 10) -> list[str]:
    try:
        with open(fname) as f:
            lines = f.readlines()
    except OSError as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        return []
    return lines[-n:] if lines else []


def parse_exts(spec: str | None) -> tuple[str, ...] | None:
    if not spec:
        return None
    parts = [p.strip().lower() for p in spec.split(",") if p.strip()]
    if not parts:
        return None
    return tuple(p if p.startswith(".") else "." + p for p in parts)


def path_matches_ext(path: Path, exts: tuple[str, ...] | None) -> bool:
    if exts is None:
        return True
    name = path.name.lower()
    return any(name.endswith(ext) for ext in exts)


class ChangeHandler(FileSystemEventHandler):
    def __init__(
        self,
        root: Path,
        *,
        display_root: Path,
        single_file: bool = False,
        copy_enabled: bool = False,
        dest_dir: Path | None = None,
        allowed_exts: tuple[str, ...] | None = None,
        excluded_exts: tuple[str, ...] | None = None,
        interval: float = 1.0,
        pattern: str | None = None,
        tail_n: int = 10,
        stop_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.root = root
        self.display_root = display_root
        self.single_file = single_file
        self.copy_enabled = copy_enabled
        self.dest_dir = dest_dir
        self.allowed_exts = allowed_exts
        self.excluded_exts = excluded_exts
        self.interval = interval
        self.pattern = pattern
        self.tail_n = tail_n
        self.stop_event = stop_event or threading.Event()
        self._pending: dict[Path, str] = {}
        self._last_flush = time.time()
        self._errors: list[str] = []

    def _rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.display_root).as_posix()
        except ValueError:
            return p.name

    def _should_process(self, src_path: Path) -> bool:
        if self.single_file and src_path != self.root:
            return False
        if src_path.exists() and src_path.is_dir():
            return False
        if self.allowed_exts is not None and not path_matches_ext(src_path, self.allowed_exts):
            return False
        return not (self.excluded_exts is not None and path_matches_ext(src_path, self.excluded_exts))

    def _safe_copy(self, src: Path, rel: str) -> None:
        try:
            dst = self.dest_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            self._errors.append(f"[copy-error] {src} -> {self.dest_dir / rel}\n{e}\n{traceback.format_exc()}")

    def _pattern_found(self, path: Path) -> bool:
        if not self.pattern:
            return False
        return self.pattern in "".join(tail_file(path, self.tail_n))

    def _queue(self, src_path: Path, reason: str) -> None:
        if not self._should_process(src_path):
            return
        self._pending[src_path] = reason
        if time.time() - self._last_flush >= self.interval:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            self._last_flush = time.time()
            return
        for src_path, reason in list(self._pending.items()):
            try:
                rel = self._rel(src_path)
                if src_path.exists() and src_path.is_file():
                    try:
                        size_str = fsz(src_path.stat().st_size)
                    except OSError:
                        size_str = "?"
                    print(f"-  /{rel} | {reason} | {size_str}")
                    if self.copy_enabled and self.dest_dir is not None:
                        self._safe_copy(src_path, rel)
                    if self.pattern and self._pattern_found(src_path):
                        print(f"\n✓ Pattern {self.pattern!r} detected! Exiting...\n")
                        self.stop_event.set()
                elif not src_path.exists():
                    print(f"-  /{rel} | {reason} | deleted")
                    if self.copy_enabled and self.dest_dir is not None:
                        dst_file = self.dest_dir / rel
                        if dst_file.exists():
                            try:
                                dst_file.unlink()
                                print(f"  → removed from destination: /{rel}")
                            except OSError as e:
                                self._errors.append(f"[delete-error] {dst_file}\n{e}")
            except Exception as e:
                self._errors.append(f"[processing-error] {src_path}\n{e}")
        self._pending.clear()
        self._last_flush = time.time()
        if self._errors:
            print("\n[errors]")
            for msg in self._errors:
                print(msg)
            print("-" * 40)
            self._errors.clear()

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._queue(Path(event.src_path), "create")

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._queue(Path(event.src_path), "change")

    def on_deleted(self, event) -> None:
        if not event.is_directory:
            self._queue(Path(event.src_path), "delete")

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._queue(Path(event.src_path), "moved-out")
            self._queue(Path(event.dest_path), "moved-in")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Unified recursive file/folder watcher.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "paths",
        nargs="*",
        help="Files or folders to watch (default: current working directory).",
    )
    p.add_argument(
        "-c",
        "--copy",
        action="store_true",
        help="Copy changed/created files to the destination folder.",
    )
    p.add_argument(
        "-d",
        "--dest",
        default=None,
        help="Destination folder for copies (default: ~/tmp/tgz).",
    )
    p.add_argument(
        "-e",
        "--extensions",
        default=None,
        help=(
            "Comma-separated allowlist of file extensions "
            "(e.g. 'svg,png' or '.tar.gz,.whl'). If omitted, all types match."
        ),
    )
    p.add_argument(
        "-x",
        "--exclude",
        default=None,
        help="Comma-separated list of file extensions to exclude.",
    )
    p.add_argument(
        "-i",
        "--interval",
        type=float,
        default=1.0,
        help="Batch/flush interval in seconds (default: 1.0).",
    )
    p.add_argument(
        "-p",
        "--pattern",
        default=None,
        help="Exit when this string appears in the tail of a changed file.",
    )
    p.add_argument(
        "-n",
        "--tail",
        type=int,
        default=10,
        help="Number of tail lines checked for --pattern (default: 10).",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="Watch only the top-level of each folder (no subdirectories).",
    )
    p.add_argument(
        "--initial-copy",
        action="store_true",
        help="Copy matching files that already exist at startup (wtmp.py behaviour).",
    )
    return p


def do_initial_copy(
    targets: list[Path],
    allowed: tuple[str, ...] | None,
    excluded: tuple[str, ...] | None,
    dest_dir: Path,
) -> None:
    for t in targets:
        if t.is_file():
            paths = [t]
            base = t.parent
        else:
            paths = [p for p in t.rglob("*") if p.is_file()]
            base = t
        for path in paths:
            if not path_matches_ext(path, allowed):
                continue
            if excluded is not None and path_matches_ext(path, excluded):
                continue
            try:
                rel = path.relative_to(base)
            except ValueError:
                rel = Path(path.name)
            dst = dest_dir / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst)
                print(f"[initial] copied {path} -> {dst}")
            except OSError as e:
                print(f"[initial-copy-error] {path}: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_paths = args.paths or [str(Path.cwd())]
    targets: list[Path] = []
    for raw in raw_paths:
        p = Path(raw).expanduser()
        if not p.exists():
            print(f"Error: path does not exist: {p}", file=sys.stderr)
            return 2
        targets.append(p.resolve())
    allowed = parse_exts(args.extensions)
    excluded = parse_exts(args.exclude)
    interval = max(0.1, float(args.interval))
    dest_dir: Path | None = None
    if args.copy:
        dest_dir = Path(args.dest).expanduser().resolve() if args.dest else (Path.home() / "tmp" / "tgz")
        dest_dir.mkdir(parents=True, exist_ok=True)
    print(f"Watching ({'non-' if args.no_recursive else ''}recursive):")
    for t in targets:
        kind = "file" if t.is_file() else "dir"
        print(f"  [{kind}] {t}")
    if args.copy:
        print(f"Copy destination: {dest_dir}")
    else:
        print("Copy disabled (print only).")
    if allowed is not None:
        print(f"Allowed extensions: {sorted(allowed)}")
    if excluded is not None:
        print(f"Excluded extensions: {sorted(excluded)}")
    if args.pattern:
        print(f"Exit pattern: {args.pattern!r} (checked in last {args.tail} lines)")
    print(f"Flush interval: {interval}s")
    print("(Press Ctrl+C to exit)\n")
    if args.initial_copy and args.copy and dest_dir is not None:
        do_initial_copy(targets, allowed, excluded, dest_dir)
    stop_event = threading.Event()
    observer = Observer()
    handlers: list[ChangeHandler] = []
    for t in targets:
        single_file = t.is_file()
        if single_file:
            watch_dir = t.parent
            display_root = t
            recursive = False
        else:
            watch_dir = t
            display_root = t
            recursive = not args.no_recursive
        handler = ChangeHandler(
            root=t,
            display_root=display_root,
            single_file=single_file,
            copy_enabled=args.copy,
            dest_dir=dest_dir,
            allowed_exts=allowed,
            excluded_exts=excluded,
            interval=interval,
            pattern=args.pattern,
            tail_n=args.tail,
            stop_event=stop_event,
        )
        observer.schedule(handler, str(watch_dir), recursive=recursive)
        handlers.append(handler)
    observer.start()
    try:
        while not stop_event.is_set():
            time.sleep(interval)
            for h in handlers:
                h.flush()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for h in handlers:
            with contextlib.suppress(Exception):
                h.flush()
        observer.stop()
        observer.join()
        print("Watcher stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
