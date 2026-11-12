#!/data/data/com.termux/files/usr/bin/env python
"""Search names, file names, contents and archives from one CLI.
Subcommands: ``names`` finds person names inside files, ``fast`` walks a tree quickly, ``find`` matches file names including archive members (zip, wheels, tarballs, 7z, zstd, brotli), ``grep`` is a ripgrep-like content search with regex/fixed-string/ignore-case options, ``info`` reports file-name statistics and similar-name groups and ``regex`` builds a regex from a name list.
Usage: pysearch {names,fast,find,grep,info,regex} [options] Positional arguments: names_file File with one name per line directory Root directory search_string pattern directories ...
paths ...
filename files ...
Subcommands: names Search person names inside files (exnames.py) fast Fast recursive search (fdrg.py) find Search filenames incl.
archives (pfind.py) grep Ripgrep-like content search info Filename stats + similar groups (pyfinfo.py) regex Emit regex from filename list (pygrex.py) strings Extract strings from binaries (stringr.py) Options: --extensions File extensions to scan -c, --content Search file contents instead of names -d, --directory -o, --output (kept for compatibility; unused) --exclude Exclude a dir name or glob (repeatable) --no-pause Disable SPACE/p pause hotkey -e, --regexp Pattern (alternative to positional) -i, --ignore-case -F, --fixed-strings -n, --line-number ...
plus 11 more; run ``pysearch --help`` for the full list.
Examples: $ pysearch grep 'TODO' -d src/ $ pysearch find '*.whl' ~/wheels Command-line script: run ``python pysearch.py`` or the ``pysearch`` command.
The module is importable as well."""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import fnmatch
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from typing import Any, Iterable, Iterator, Optional, Sequence
import zipfile

from dh import MAX_WORKERS, SKIP_DIRS

try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import brotli
except ImportError:
    brotli = None
try:
    import zstandard
except ImportError:
    zstandard = None
try:
    import keyboard
except ImportError:
    keyboard = None
TEXT_EXT_DEFAULT: set[str] = {
    ".txt",
    ".md",
    ".log",
    ".py",
    ".html",
    ".css",
    ".js",
    ".json",
    ".xml",
    ".yml",
    ".yaml",
}
BINARY_EXT_DEFAULT: set[str] = {".pyc", ".bak"}
ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar.gz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
    ".tar.7z",
    ".tar.br",
    ".tar",
    ".zip",
    ".whl",
    ".apk",
)
RESET: str = "\x1b[0m"
CYAN: str = "\x1b[5;96m"
RED: str = "\x1b[91m"


def is_binary(path: Path, sample: int = 1024) -> bool:
    try:
        with path.open("rb") as f:
            return b"\x00" in f.read(sample)
    except OSError:
        return True


def walk_files(
    roots: Sequence[Path],
    *,
    hidden: bool = False,
    follow_symlinks: bool = False,
    skip_dirs: Optional[set[str]] = None,
    max_size: Optional[int] = None,
    include_globs: Optional[Sequence[str]] = None,
    exclude_globs: Optional[Sequence[str]] = None,
    allowed_exts: Optional[set[str]] = None,
    blocked_exts: Optional[set[str]] = None,
) -> Iterator[Path]:
    skip = SKIP_DIRS if skip_dirs is None else skip_dirs
    blocked = BINARY_EXT_DEFAULT if blocked_exts is None else blocked_exts

    def ok(p: Path) -> bool:
        try:
            if not p.is_file():
                return False
            if p.is_symlink() and not follow_symlinks:
                return False
        except OSError:
            return False
        if not hidden and p.name.startswith("."):
            return False
        if max_size is not None:
            try:
                if p.stat().st_size > max_size:
                    return False
            except OSError:
                return False
        if allowed_exts is not None and p.suffix not in allowed_exts:
            return False
        if blocked and p.suffix in blocked:
            return False
        s = str(p)
        if include_globs and not any(fnmatch.fnmatch(s, g) or fnmatch.fnmatch(p.name, g) for g in include_globs):
            return False
        return not (exclude_globs and any(fnmatch.fnmatch(s, g) or fnmatch.fnmatch(p.name, g) for g in exclude_globs))

    for root in roots:
        root = root.resolve()
        if root.is_file():
            if ok(root):
                yield root
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
            dirnames[:] = [d for d in dirnames if d not in skip]
            if not hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            dp = Path(dirpath)
            for name in filenames:
                fp = dp / name
                if ok(fp):
                    yield fp


def highlight(line: str, spans: Sequence[tuple[int, int]], enabled: bool) -> str:
    if not enabled or not spans:
        return line
    parts: list[str] = []
    last = 0
    for s, e in sorted(spans):
        parts.append(line[last:s])
        parts.append(f"{RED}{line[s:e]}{RESET}")
        last = e
    parts.append(line[last:])
    return "".join(parts)


def _load_names(path: Path) -> list[tuple[str, re.Pattern[str]]]:
    out: list[tuple[str, re.Pattern[str]]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            name = raw.strip()
            if not name:
                continue
            parts = name.split()
            if len(parts) >= 2:
                a = re.escape(parts[0][0].upper())
                b = re.escape(parts[-1][0].upper())
                pat = rf"{a}[\w\s\-r']+\s+{b}[\w\s\-']+"
            else:
                pat = re.escape(name[0].upper()) + r"[\w\s\-']+"
            out.append((name, re.compile(pat, re.IGNORECASE)))
    return out


def cmd_names(args: argparse.Namespace) -> int:
    names_file = Path(args.names_file)
    root = Path(args.directory)
    if not names_file.exists():
        print(f"Error: Names file not found at {names_file}", file=sys.stderr)
        return 1
    try:
        names = _load_names(names_file)
    except Exception as e:
        print(f"Error loading names file: {e}", file=sys.stderr)
        return 1
    if not names:
        return 0
    exts = set(args.extensions) if args.extensions else TEXT_EXT_DEFAULT
    results: dict[str, list[dict]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in exts:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"Could not read file {path}: {e}")
            continue
        for orig, rx in names:
            for m in rx.finditer(content):
                text = m.group(0).strip()
                parts = text.split()
                if len(parts) < 2:
                    continue
                want = orig.split()
                if parts[0][0].upper() != want[0][0].upper():
                    continue
                if parts[-1][0].upper() != want[-1][0].upper():
                    continue
                entry = {"file": str(path.relative_to(root)), "match": text}
                bucket = results.setdefault(orig, [])
                if entry not in bucket:
                    bucket.append(entry)
    if not results:
        print("No target names found in the specified files.")
        return 0
    print(f"Found names (from {names_file}):")
    for name, items in results.items():
        print(f"\n- {name}:")
        for it in items:
            print(f"  - File: {it['file']}, Match: '{it['match']}'")
    return 0


_pause_event: Optional[threading.Event] = None


def _fast_worker(job: tuple[str, str, bool]) -> list[tuple[str, Optional[int]]]:
    path_str, query, content_mode = job
    path = Path(path_str)
    out: list[tuple[str, Optional[int]]] = []
    if content_mode:
        try:
            with path.open(encoding="utf-8", errors="ignore") as f:
                for ln, line in enumerate(f, 1):
                    if query in line:
                        out.append((str(path), ln))
        except Exception:
            pass
    elif query.lower() in path.name.lower():
        out.append((str(path), None))
    return out


def _install_pause_hotkey() -> bool:
    if keyboard is None:
        print("'keyboard' not installed. Pause disabled.")
        return False
    global _pause_event
    _pause_event = threading.Event()
    _pause_event.set()

    def handler(ev: Any) -> None:
        if ev.name in {"space", "p"} and _pause_event.is_set():
            _pause_event.clear()
            print("PAUSED - press 'c' to continue...")
        elif ev.name == "c" and not _pause_event.is_set():
            _pause_event.set()
            print("RESUMED - searching...")

    keyboard.on_press(handler)
    return True


def cmd_fast(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not args.no_pause:
        _install_pause_hotkey()
    dir_excludes = {e for e in args.exclude if not any(c in e for c in "*?[]")}
    glob_excludes = {e for e in args.exclude if any(c in e for c in "*?[]")}
    print(f"Root: {root}")
    print(f"Mode: {'content' if args.content else 'filename'}")
    print(f"Excluded dirs: {sorted(dir_excludes)}")
    print(f"Excluded patterns: {sorted(glob_excludes)}")
    print("-" * 40)
    files = list(
        walk_files(
            [root],
            hidden=True,
            skip_dirs=(dir_excludes or SKIP_DIRS),
            exclude_globs=list(glob_excludes),
        )
    )
    print(f"Files queued: {len(files)}")
    total = 0
    jobs = [(str(f), args.search_string, args.content) for f in files]
    if MAX_WORKERS > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for res in pool.map(_fast_worker, jobs, chunksize=16):
                for path_str, line in res:
                    if line is not None:
                        print(f"[FOUND] {path_str} (Line: {line})")
                    else:
                        print(f"[FOUND] {path_str}")
                    total += 1
    else:
        for job in jobs:
            if _pause_event is not None:
                _pause_event.wait()
            for path_str, line in _fast_worker(job):
                if line is not None:
                    print(f"[FOUND] {path_str} (Line: {line})")
                else:
                    print(f"[FOUND] {path_str}")
                total += 1
    print(f"Total results: {total}")
    return 0


def _scan_archive(archive: Path, pattern: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    needle = pattern.lower()
    name = archive.name.lower()
    try:
        if archive.suffix in (".zip", ".whl", ".apk"):
            with zipfile.ZipFile(archive) as zf:
                for entry in zf.namelist():
                    if needle in entry.lower():
                        out.append((str(archive), entry))
        elif name.endswith((".tar.gz", ".tar")):
            with tarfile.open(archive, "r:*") as tf:
                for m in tf.getmembers():
                    if needle in m.name.lower():
                        out.append((str(archive), m.name))
        elif name.endswith(".tar.bz2"):
            with tarfile.open(archive, "r:bz2") as tf:
                for m in tf.getmembers():
                    if needle in m.name.lower():
                        out.append((str(archive), m.name))
        elif name.endswith(".tar.xz"):
            with tarfile.open(archive, "r:xz") as tf:
                for m in tf.getmembers():
                    if needle in m.name.lower():
                        out.append((str(archive), m.name))
        elif name.endswith(".tar.zst") and zstandard is not None:
            import tempfile

            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp:
                d = zstandard.ZstdDecompressor()
                with archive.open("rb") as f:
                    tmp.write(d.stream_reader(f).read())
                tmp_path = tmp.name
            try:
                with tarfile.open(tmp_path, "r") as tf:
                    for m in tf.getmembers():
                        if needle in m.name.lower():
                            out.append((str(archive), m.name))
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        elif name.endswith(".tar.7z") and py7zr is not None:
            with py7zr.SevenZipFile(archive, "r") as sz:
                for entry in sz.getnames():
                    if needle in entry.lower():
                        out.append((str(archive), entry))
        elif name.endswith(".tar.br") and brotli is not None:
            import tempfile

            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp:
                tmp.write(brotli.decompress(archive.read_bytes()))
                tmp_path = tmp.name
            try:
                with tarfile.open(tmp_path, "r") as tf:
                    for m in tf.getmembers():
                        if needle in m.name.lower():
                            out.append((str(archive), m.name))
            finally:
                Path(tmp_path).unlink(missing_ok=True)
    except Exception:
        pass
    return out


def _find_worker(job: tuple[str, str]) -> list[tuple[str, Optional[str]]]:
    path_str, pattern = job
    p = Path(path_str)
    results: list[tuple[str, Optional[str]]] = []
    if any(p.name.endswith(s) for s in ARCHIVE_SUFFIXES):
        for arc, entry in _scan_archive(p, pattern):
            results.append((arc, entry))
    if pattern.lower() in p.name.lower():
        results.append((str(p), None))
    return results


def cmd_find(args: argparse.Namespace) -> int:
    roots = [Path(d) for d in args.directories]
    files = list(walk_files(roots, hidden=True, allowed_exts=None, blocked_exts=set()))
    jobs = [(str(p), args.pattern) for p in files]
    total = 0

    def iter_results():
        if MAX_WORKERS > 1 and len(jobs) > 1:
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
                yield from pool.map(_find_worker, jobs, chunksize=32)
        else:
            for j in jobs:
                yield _find_worker(j)

    for res in iter_results():
        for path_str, entry in res:
            print(f"{path_str}:{entry}" if entry else path_str)
            total += 1
    return 0 if total else 1


def _grep_worker(
    job: tuple[str, str, bool, bool, bool],
) -> tuple[str, list[tuple[int, str, list[tuple[int, int]]]]]:
    path_str, pattern, ignore_case, fixed_strings, skip_binary = job
    path = Path(path_str)
    if skip_binary and is_binary(path):
        return str(path), []
    rx: Optional[re.Pattern[str]] = None
    if not fixed_strings:
        flags = re.MULTILINE
        if ignore_case:
            flags |= re.IGNORECASE
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            return str(path), []
    results: list[tuple[int, str, list[tuple[int, int]]]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for ln, raw in enumerate(f, 1):
                line = raw.rstrip("\n\r")
                spans: list[tuple[int, int]] = []
                if rx is not None:
                    spans = [m.span() for m in rx.finditer(line)]
                else:
                    hay = line.lower() if ignore_case else line
                    needle = pattern.lower() if ignore_case else pattern
                    pos = 0
                    while needle:
                        idx = hay.find(needle, pos)
                        if idx < 0:
                            break
                        spans.append((idx, idx + len(needle)))
                        pos = idx + max(1, len(needle))
                if spans:
                    results.append((ln, line, spans))
    except Exception:
        pass
    return str(path), results


def cmd_grep(args: argparse.Namespace) -> int:
    pattern = args.pattern_e or args.pattern
    if not pattern:
        print(
            "No pattern provided. Use positional PATTERN or -e PATTERN.",
            file=sys.stderr,
        )
        return 2
    if not args.fixed_strings:
        try:
            flags = re.MULTILINE | (re.IGNORECASE if args.ignore_case else 0)
            re.compile(pattern, flags)
        except re.error as e:
            print(f"Invalid regex: {e}", file=sys.stderr)
            return 2
    allowed = set(args.extensions) if args.extensions else None
    roots = [Path(p) for p in (args.paths or ["."])]
    files = list(
        walk_files(
            roots,
            hidden=args.hidden,
            max_size=args.max_filesize,
            include_globs=args.glob,
            exclude_globs=args.exclude,
            allowed_exts=allowed,
            blocked_exts=set(),
        )
    )
    use_color = (not args.no_color) and sys.stdout.isatty()
    jobs = [(str(p), pattern, args.ignore_case, args.fixed_strings, True) for p in files]

    def iter_results():
        if MAX_WORKERS > 1 and len(jobs) > 1:
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
                yield from pool.map(_grep_worker, jobs, chunksize=16)
        else:
            for j in jobs:
                yield _grep_worker(j)

    found_any = False
    try:
        for path_str, matches in iter_results():
            if not matches:
                continue
            found_any = True
            if args.files_with_matches:
                print(path_str)
            elif args.count:
                print(f"{path_str}:{len(matches)}")
            else:
                for ln, line, spans in matches:
                    shown = highlight(line, spans, use_color)
                    if args.line_number:
                        print(f"{CYAN}{path_str}{RESET}:{ln}:{shown}")
                    else:
                        print(f"{CYAN}{path_str}{RESET}:{shown}")
    except KeyboardInterrupt:
        print("\nSearch cancelled.", file=sys.stderr)
        return 130
    return 0 if found_any else 1


def _collect_stems(root: Path) -> Iterator[str]:
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != ".git":
                            stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.name).stem
        except (PermissionError, OSError):
            continue


def _edit_distance_bounded(a: str, b: str, bound: int) -> int:
    n, m = len(a), len(b)
    if abs(n - m) > bound:
        return bound + 1
    if n < m:
        a, b = b, a
        n, m = m, n
    INF = bound + 1
    prev = [INF] * (m + 1)
    for j in range(min(m, bound) + 1):
        prev[j] = j
    for i in range(1, n + 1):
        cur = [INF] * (m + 1)
        lo = max(0, i - bound)
        hi = min(m, i + bound)
        if lo == 0 and i <= bound:
            cur[0] = i
        for j in range(max(lo, 1), hi + 1):
            cur[j] = min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (a[i - 1] != b[j - 1]),
            )
        if min(cur[lo : hi + 1]) > bound:
            return bound + 1
        prev = cur
    return prev[m] if prev[m] <= bound else bound + 1


def _similar_groups(names: list[str], ratio: float = 0.8) -> list[list[str]]:
    n = len(names)
    used = [False] * n
    by_len: dict[int, list[int]] = {}
    for i, s in enumerate(names):
        by_len.setdefault(len(s), []).append(i)
    groups: list[list[str]] = []
    for i, a in enumerate(names):
        if used[i]:
            continue
        used[i] = True
        group = [a]
        la = len(a)
        lo = int(la * ratio * 0.9) or la
        lo = max(1, int(la * 0.8))
        hi = int(la * 1.25)
        cands: list[int] = []
        for L in range(lo, hi + 1):
            for j in by_len.get(L, ()):
                if not used[j]:
                    cands.append(j)
        for j in cands:
            b = names[j]
            bound = max(la, len(b)) * 2 // 10
            if _edit_distance_bounded(a, b, bound) <= bound:
                group.append(b)
                used[j] = True
        if len(group) > 1:
            groups.append(group)
    return groups


def cmd_info(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    counts = Counter(_collect_stems(root))
    for stem, c in counts.most_common(args.top):
        if c > args.min_count:
            print(f"{stem}: {c}")
    print("\n=== Similar Filename Groups ===")
    groups = _similar_groups(list(counts.keys()), ratio=args.ratio)
    if not groups:
        print("No similar groups found.")
    else:
        for i, g in enumerate(groups, 1):
            print(f"Group {i}: {', '.join(g)}")
    return 0


def cmd_regex(args: argparse.Namespace) -> int:
    src = Path(args.filename)
    if not src.exists():
        print(f"File not found: {src}", file=sys.stderr)
        return 1
    with src.open(encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]
    print("^(?:{})$".format("|".join(re.escape(l) for l in lines)))
    return 0


def _strings_worker(job: tuple[str, str]) -> Optional[tuple[str, str]]:
    path_str, _ = job
    path = Path(path_str)
    if not path.exists() or not is_binary(path):
        return None
    exe = shutil.which("strings")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, str(path)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except Exception:
        return None
    return path.name, out


def cmd_strings(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if args.files:
        files = [Path(f) for f in args.files if Path(f).is_file()]
    else:
        files = [f for f in Path.cwd().rglob("*") if f.is_file()]
    total = len(files)
    if not total:
        print("No files to process.")
        return 0
    if shutil.which("strings") is None:
        print("'strings' command not found in PATH.", file=sys.stderr)
        return 1
    jobs = [(str(f), str(output)) for f in files]

    def iter_results():
        if MAX_WORKERS > 1 and len(jobs) > 1:
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
                yield from pool.map(_strings_worker, jobs, chunksize=4)
        else:
            for j in jobs:
                yield _strings_worker(j)

    with output.open("a", encoding="utf-8") as fh:
        for i, res in enumerate(iter_results(), 1):
            if res is None:
                continue
            name, text = res
            print(f"[{i}/{total}] {name}")
            fh.write(f"\n# filename :{name}\n{text}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged_search.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("names", help="Search person names inside files (exnames.py)")
    p.add_argument("names_file", help="File with one name per line")
    p.add_argument("directory", nargs="?", default=".", help="Root directory")
    p.add_argument(
        "--extensions",
        nargs="*",
        default=sorted(TEXT_EXT_DEFAULT),
        help="File extensions to scan",
    )
    p.set_defaults(func=cmd_names)
    p = sub.add_parser("fast", help="Fast recursive search (fdrg.py)")
    p.add_argument("search_string")
    p.add_argument(
        "-c",
        "--content",
        action="store_true",
        help="Search file contents instead of names",
    )
    p.add_argument("-d", "--directory", default=".")
    p.add_argument("-o", "--output", default="output", help="(kept for compatibility; unused)")
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude a dir name or glob (repeatable)",
    )
    p.add_argument("--no-pause", action="store_true", help="Disable SPACE/p pause hotkey")
    p.set_defaults(func=cmd_fast)
    p = sub.add_parser("find", help="Search filenames incl. archives (pfind.py)")
    p.add_argument("pattern")
    p.add_argument("directories", nargs="*", default=["."])
    p.set_defaults(func=cmd_find)
    p = sub.add_parser("grep", help="ripgrep-like content search")
    p.add_argument("pattern", nargs="?")
    p.add_argument("paths", nargs="*", default=["."])
    p.add_argument("-e", "--regexp", dest="pattern_e", help="Pattern (alternative to positional)")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("-F", "--fixed-strings", action="store_true")
    p.add_argument("-n", "--line-number", dest="line_number", action="store_true", default=True)
    p.add_argument("--no-line-number", dest="line_number", action="store_false")
    p.add_argument("-l", "--files-with-matches", action="store_true")
    p.add_argument("-c", "--count", action="store_true")
    p.add_argument("--hidden", action="store_true")
    p.add_argument("-g", "--glob", action="append", help="Include glob (repeatable)")
    p.add_argument("-x", "--exclude", action="append", help="Exclude glob (repeatable)")
    p.add_argument("-m", "--max-filesize", type=int, default=10_000_000)
    p.add_argument("--extensions", nargs="*", help="Only scan these extensions (e.g. .txt .md)")
    p.add_argument("--no-color", action="store_true")
    p.set_defaults(func=cmd_grep)
    p = sub.add_parser("info", help="Filename stats + similar groups (pyfinfo.py)")
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument("-t", "--top", type=int, default=100)
    p.add_argument("--min-count", type=int, default=2)
    p.add_argument("-r", "--ratio", type=float, default=0.8)
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("regex", help="Emit regex from filename list (pygrex.py)")
    p.add_argument("filename")
    p.set_defaults(func=cmd_regex)
    p = sub.add_parser("strings", help="Extract strings from binaries (stringr.py)")
    p.add_argument("files", nargs="*")
    p.add_argument("-o", "--output", default="all_strings.txt")
    p.set_defaults(func=cmd_strings)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
