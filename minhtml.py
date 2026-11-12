#!/data/data/com.termux/files/usr/bin/env python
"""Recursively minify HTML files in place with minhtml."""

import argparse
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
import contextlib
from dataclasses import dataclass
from datetime import timedelta
import fnmatch
import glob as _glob
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import TypeAlias

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger

try:
    from concurrent.futures import InterpreterPoolExecutor as _IPE

    _HAS_IPE: bool = True
except ImportError:
    _IPE = None
    _HAS_IPE = False
try:
    from unidecode import unidecode as _translit

    _HAS_TRANSLIT: bool = True
except ImportError:
    try:
        from anyascii import anyascii as _translit

        _HAS_TRANSLIT = True
    except ImportError:
        _HAS_TRANSLIT = False
        _translit = None
JobTuple: TypeAlias = tuple[str, bool, str, tuple[str, ...], float, bool, str | None, bool, bool]
ResultTuple: TypeAlias = tuple[str, int, int, str | None, str | None, float]
DEFAULT_BASE_ARGS: tuple[str, ...] = (
    "--keep-html-and-head-opening-tags",
    "--preserve-brace-template-syntax",
    "--minify-css",
    "--minify-js",
)
GREEN: str = "\x1b[32m"
RED: str = "\x1b[31m"
YELLOW: str = "\x1b[33m"
DIM: str = "\x1b[2m"
RESET: str = "\x1b[0m"
DEFAULT_EXECUTOR: str = "interpreter"
MAX_SLUG_LEN: int = 200


@dataclass(frozen=True, slots=True)
class Job:
    path: str
    dry_run: bool
    minhtml_bin: str
    minhtml_args: tuple[str, ...]
    timeout: float
    strict: bool
    backup_suffix: str | None
    fsync: bool
    preserve_mtime: bool


@dataclass(frozen=True, slots=True)
class Result:
    path: str
    original: int
    new: int
    error: str | None
    skip_reason: str | None
    duration_ms: float


@dataclass(frozen=True, slots=True)
class Rename:
    src: str
    dst: str
    status: str


def _job_to_tuple(j: Job) -> JobTuple:
    return (
        j.path,
        j.dry_run,
        j.minhtml_bin,
        j.minhtml_args,
        j.timeout,
        j.strict,
        j.backup_suffix,
        j.fsync,
        j.preserve_mtime,
    )


def _tuple_to_job(t: JobTuple) -> Job:
    return Job(*t)


def _result_to_tuple(r: Result) -> ResultTuple:
    return (r.path, r.original, r.new, r.error, r.skip_reason, r.duration_ms)


def _tuple_to_result(t: ResultTuple) -> Result:
    return Result(*t)


def _worker_entry(job_tuple: JobTuple) -> ResultTuple:
    return _result_to_tuple(_worker(_tuple_to_job(job_tuple)))


def _slugify_stem(stem: str, lowercase: bool, transliterate: bool) -> str:
    if transliterate and _HAS_TRANSLIT and _translit is not None:
        with contextlib.suppress(Exception):
            stem = _translit(stem)
    stem = unicodedata.normalize("NFKD", stem)
    stem = "".join(c for c in stem if not unicodedata.combining(c))
    out: list[str] = []
    for ch in stem:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("_")
    s = re.sub(r"_+", "_", "".join(out)).strip("_")
    if lowercase:
        s = s.lower()
    if len(s) > MAX_SLUG_LEN:
        s = s[:MAX_SLUG_LEN].rstrip("_")
    return s or "file"


def _slugify_path(p: Path, lowercase: bool, transliterate: bool) -> Path:
    if not p.suffix:
        return p
    new_stem = _slugify_stem(p.stem, lowercase, transliterate)
    if new_stem == p.stem:
        return p
    return p.with_name(new_stem + p.suffix)


def _plan_renames(
    files: list[Path],
    slugify: bool,
    lowercase: bool,
    transliterate: bool,
) -> dict[Path, Path]:
    if not slugify:
        return {f: f for f in files}
    plans: dict[Path, Path] = {f: _slugify_path(f, lowercase, transliterate) for f in files}
    groups: dict[tuple[Path, str], list[Path]] = defaultdict(list)
    for f, t in plans.items():
        groups[(f.parent, t.name)].append(f)
    collisions: set[Path] = set()
    for (parent, name), srcs in groups.items():
        target = parent / name
        if len(srcs) > 1:
            names = ", ".join(sorted(s.name for s in srcs))
            logger.warning(f"slug collision -> {name}: {names}")
            collisions.update(srcs)
            continue
        src = srcs[0]
        if src != target and target.exists():
            logger.warning(f"slug target exists, skipping: {src.name} -> {name}")
            collisions.add(src)
    return {f: (f if f in collisions else plans[f]) for f in files}


def _worker(job: Job) -> Result:
    path = Path(job.path)
    t0 = time.perf_counter()

    def done(err: str | None = None, skip: str | None = None, orig: int = 0, new: int = 0) -> Result:
        return Result(str(path), orig, new, err, skip, (time.perf_counter() - t0) * 1000.0)

    try:
        if path.is_symlink():
            return done(err="unresolved symlink")
        st = path.stat()
        orig_data = path.read_bytes()
        orig_size = len(orig_data)
        if not orig_data.lstrip(b"\xef\xbb\xbf \t\r\n\v\f"):
            return done(skip="empty", orig=orig_size, new=orig_size)
        mtime_before = st.st_mtime_ns
        fd, tmp_name = tempfile.mkstemp(prefix=".minhtml-", suffix=".tmp", dir=str(path.parent))
        os.close(fd)
        tmp_file: Path | None = Path(tmp_name)
        try:
            try:
                proc = subprocess.run(
                    ["min-html", *job.minhtml_args, "-o", str(tmp_file), str(path)],
                    capture_output=True,
                    timeout=job.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return done(err=f"timeout after {job.timeout:g}s", orig=orig_size, new=orig_size)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace").strip()[:400]
                return done(err=f"minhtml exit {proc.returncode}: {err}", orig=orig_size, new=orig_size)
            new_data = tmp_file.read_bytes()
            new_size = len(new_data)
            if new_data == orig_data:
                return done(skip="unchanged", orig=orig_size, new=orig_size)
            if new_size > orig_size:
                if job.strict:
                    return done(err=f"grew {orig_size}->{new_size}", orig=orig_size, new=new_size)
                return done(skip="grown", orig=orig_size, new=new_size)
            st2 = path.stat()
            if st2.st_mtime_ns != mtime_before or st2.st_size != orig_size:
                return done(err="modified during run", orig=orig_size, new=orig_size)
            if job.dry_run:
                return done(orig=orig_size, new=new_size)
            if job.backup_suffix:
                backup = path.with_name(path.name + job.backup_suffix)
                try:
                    shutil.copy2(path, backup)
                except OSError as e:
                    return done(err=f"backup failed: {e}", orig=orig_size, new=orig_size)
            if job.fsync:
                with open(tmp_file, "rb") as f:
                    os.fsync(f.fileno())
            try:
                os.chmod(tmp_file, stat.S_IMODE(st.st_mode))
                os.replace(tmp_file, path)
            except OSError:
                path.write_bytes(new_data)
                tmp_file.unlink(missing_ok=True)
            tmp_file = None
            if job.preserve_mtime:
                with contextlib.suppress(OSError):
                    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            return done(orig=orig_size, new=new_size)
        finally:
            if tmp_file is not None:
                with contextlib.suppress(OSError):
                    tmp_file.unlink(missing_ok=True)
    except Exception as e:
        return done(err=f"{type(e).__name__}: {e}")


def _make_executor(kind: str, workers: int) -> Executor:
    if kind == "interpreter":
        if not _HAS_IPE or _IPE is None:
            msg = "InterpreterPoolExecutor unavailable (Python 3.14+ required); pass --executor=process"
            raise RuntimeError(msg)
        return _IPE(max_workers=workers)
    if kind == "process":
        return ProcessPoolExecutor(max_workers=workers)
    msg = f"unknown executor: {kind!r}"
    raise ValueError(msg)


def _resolve_minhtml_args(args: argparse.Namespace) -> tuple[str, ...]:
    base = list(DEFAULT_BASE_ARGS)
    if args.no_preserve_braces:
        base.remove("--preserve-brace-template-syntax")
    if args.no_minify_css:
        base.remove("--minify-css")
    if args.no_minify_js:
        base.remove("--minify-js")
    return tuple(base + list(args.minhtml_arg))


def _parse_duration(s: str) -> timedelta:
    s = s.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if len(s) < 2 or s[-1] not in units:
        msg = f"bad duration: {s!r} (expected e.g. 30m, 2h, 1d)"
        raise ValueError(msg)
    try:
        n = int(s[:-1])
    except ValueError:
        msg = f"bad duration: {s!r}"
        raise ValueError(msg) from None
    if n < 0:
        msg = f"bad duration: {s!r}"
        raise ValueError(msg)
    return timedelta(seconds=n * units[s[-1]])


def _matches_any(rel: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def _expand_arg(raw: str) -> list[Path]:
    p = Path(raw).expanduser()
    m = _glob.glob(p, recursive=True)
    if m:
        return [Path(x) for x in m]
    return [Path(p)]


def _collect(args: argparse.Namespace, cutoff: float | None) -> list[Path]:
    exclude_dirs: set[str] = set()
    if not args.no_default_excludes:
        exclude_dirs.update(SKIP_DIRS)
    exclude_dirs.update(args.exclude_dir)
    seen: set[Path] = set()
    out: list[Path] = []

    def accept(p: Path, base: Path, explicit: bool) -> bool:
        if not p.is_file():
            return False
        if not explicit:
            if not p.match(args.pattern):
                return False
            try:
                rel = str(p.relative_to(base))
            except ValueError:
                rel = p.name
            if args.include and not _matches_any(rel, args.include):
                return False
            if args.exclude and _matches_any(rel, args.exclude):
                return False
        try:
            st = p.stat()
        except OSError:
            return False
        if st.st_size < args.min_size:
            return False
        if args.max_size and st.st_size > args.max_size:
            return False
        return not (cutoff is not None and st.st_mtime < cutoff)

    for raw in args.paths:
        for entry in _expand_arg(raw):
            if entry.is_file():
                try:
                    rp = entry.resolve(strict=True)
                except (OSError, RuntimeError):
                    logger.warning(f"cannot resolve: {entry}")
                    continue
                if accept(rp, rp.parent, explicit=True) and rp not in seen:
                    seen.add(rp)
                    out.append(rp)
            elif entry.is_dir():
                base = entry.resolve()
                for dirpath, dirnames, filenames in os.walk(base, followlinks=args.follow_symlinks):
                    dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
                    for fn in filenames:
                        p = Path(dirpath) / fn
                        if p.is_symlink():
                            try:
                                rp = p.resolve(strict=True)
                            except (OSError, RuntimeError):
                                continue
                            try:
                                rp.relative_to(base)
                            except ValueError:
                                continue
                            p = rp
                        if accept(p, base, explicit=False):
                            key = p.resolve(strict=False)
                            if key not in seen:
                                seen.add(key)
                                out.append(p)
            else:
                logger.warning(f"input not found: {raw}")
    out.sort(key=lambda x: x.name)
    return out


def _apply_renames(
    files: list[Path],
    rename_plans: dict[Path, Path],
    dry_run: bool,
    use_color: bool,
) -> tuple[list[Path], list[Rename]]:
    final: list[Path] = []
    log: list[Rename] = []
    for src in files:
        dst = rename_plans.get(src, src)
        if src == dst:
            final.append(src)
            continue
        if dry_run:
            logger.info(f"would rename: {src.name} -> {dst.name}")
            log.append(Rename(str(src), str(dst), "would-rename"))
            final.append(src)
            continue
        try:
            src.rename(dst)
            logger.info(f"renamed: {src.name} -> {dst.name}")
            log.append(Rename(str(src), str(dst), "renamed"))
            final.append(dst)
        except OSError as e:
            logger.error(f"rename failed: {src.name} -> {dst.name}: {e}")
            log.append(Rename(str(src), str(dst), "error"))
            final.append(src)
    return final, log


def _colorize(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{RESET}" if enabled else text


def _human_bytes(n: int) -> str:
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a < 1024:
        return f"{sign}{a} B"
    v = float(a)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        v /= 1024.0
        if v < 1024.0:
            return f"{sign}{v:.2f} {unit}"
    return f"{sign}{v:.2f} EiB"


def _format_result(r: Result, idx: int, total: int, fn_w: int, bytes_w: int, cw: int, use_color: bool) -> str:
    name = Path(r.path).name
    if len(name) > fn_w:
        name = name[: max(1, fn_w - 1)] + "\u2026"
    counter = f"[{idx:>{cw}}/{total}]"
    if r.error is not None:
        value, color = "ERR", RED
    elif r.skip_reason == "unchanged":
        value, color = "0", DIM
    elif r.skip_reason == "empty":
        value, color = "-", DIM
    elif r.skip_reason == "grown":
        value, color = str(r.original - r.new), RED
    elif r.skip_reason is not None:
        value, color = r.skip_reason[:bytes_w], DIM
    else:
        delta = r.original - r.new
        if delta > 0:
            value, color = str(delta), GREEN
        elif delta < 0:
            value, color = str(delta), RED
        else:
            value, color = "0", DIM
    value_col = _colorize(value, color, use_color)
    pad = " " * max(0, bytes_w - len(value))
    return f"{counter}   {name:<{fn_w}}  {pad}{value_col}"


def _print_top(results: list[Result], n: int, use_color: bool) -> None:
    changed = [r for r in results if r.error is None and r.skip_reason is None and r.new < r.original]
    if not changed:
        return
    changed.sort(key=lambda r: r.original - r.new, reverse=True)
    print()
    print(f"Top {min(n, len(changed))} by savings:")
    for r in changed[:n]:
        delta = r.original - r.new
        pct = (delta / r.original * 100.0) if r.original else 0.0
        d = _colorize(f"{delta}", GREEN, use_color)
        print(f"  {Path(r.path).name:<40}  {d:>10}  ({pct:.1f}%)")


def _emit_json(results: list[Result], renames: list[Rename], summary: dict, dry_run: bool) -> None:
    payload = {
        "version": __version__,
        "dry_run": dry_run,
        "renames": [{"from": r.src, "to": r.dst, "status": r.status} for r in renames],
        "files": [
            {
                "path": r.path,
                "original": r.original,
                "new": r.new,
                "delta": (r.original - r.new) if r.error is None else None,
                "error": r.error,
                "skip_reason": r.skip_reason,
                "duration_ms": round(r.duration_ms, 2),
            }
            for r in results
        ],
        "summary": summary,
    }
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="minhtml-all",
        description="Recursively minify HTML files in-place with minhtml. "
        "Accepts any mix of files, directories, and globs; "
        "with no arguments, processes '.' recursively.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("paths", nargs="*", metavar="PATH", help="files, directories, or globs (default: .)")
    g = p.add_argument_group("selection")
    g.add_argument("--pattern", default="*.html", help="glob pattern (default: *.html)")
    g.add_argument(
        "--include", action="append", default=[], metavar="GLOB", help="only relpaths matching GLOB (repeatable)"
    )
    g.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB", help="exclude relpaths matching GLOB (repeatable)"
    )
    g.add_argument(
        "--exclude-dir", action="append", default=[], metavar="NAME", help="exclude dirs with this name (repeatable)"
    )
    g.add_argument("--no-default-excludes", action="store_true", help="disable built-in dir excludes")
    g.add_argument("--min-size", type=int, default=0, metavar="BYTES", help="skip files smaller than this (default: 0)")
    g.add_argument("--max-size", type=int, default=0, metavar="BYTES", help="skip files larger than this (0=unlimited)")
    g.add_argument("--newer-than", metavar="DUR", default=None, help="only files newer than this, e.g. 1d, 2h, 30m")
    g.add_argument("--follow-symlinks", action="store_true", help="follow directory symlinks during walk")
    g = p.add_argument_group("rename / slugify")
    g.add_argument(
        "--slugify",
        action="store_true",
        help="normalize filenames: transliterate non-ASCII (fa/ar/ko/ja/zh), collapse punctuation/spaces to '_'",
    )
    g.add_argument("--slug-lowercase", action="store_true", help="lowercase slugified stems")
    g.add_argument(
        "--no-transliterate", action="store_true", help="do not transliterate non-ASCII chars (replace with '_')"
    )
    g = p.add_argument_group("writing")
    g.add_argument("--dry-run", action="store_true", help="do not write changes")
    g.add_argument("--check", action="store_true", help="like --dry-run, exit 1 if any file would change")
    g.add_argument("--strict", action="store_true", help="treat size growth as error")
    g.add_argument(
        "--backup",
        nargs="?",
        const=".bak",
        default=None,
        metavar="SUFFIX",
        help="write backup before replace (default suffix: .bak)",
    )
    g.add_argument("--fsync", action="store_true", help="fsync temp file before rename")
    g.add_argument("--preserve-mtime", action="store_true", help="restore original mtime after replace")
    g.add_argument("--timeout", type=float, default=60.0, metavar="SEC", help="per-file minhtml timeout (default: 60)")
    g = p.add_argument_group("minhtml")
    g.add_argument("--minhtml-bin", default="min-html", help="minhtml binary name/path (default: minhtml)")
    g.add_argument(
        "--minhtml-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="extra argument passed to minhtml (repeatable)",
    )
    g.add_argument("--no-minify-css", action="store_true")
    g.add_argument("--no-minify-js", action="store_true")
    g.add_argument("--no-preserve-braces", action="store_true")
    g = p.add_argument_group("execution")
    g.add_argument("--chunksize", type=int, default=4, help="executor chunksize (0=auto, default: 4)")
    g.add_argument(
        "--executor",
        choices=("interpreter", "process"),
        default=DEFAULT_EXECUTOR,
        help=f"executor backend (default: {DEFAULT_EXECUTOR})",
    )
    g = p.add_argument_group("output")
    g.add_argument("--json", action="store_true", help="emit JSON to stdout")
    g.add_argument("--top", type=int, default=0, metavar="N", help="show top N savings (0=off)")
    g.add_argument("--no-color", action="store_true")
    g.add_argument("--log-file", type=Path, default=None)
    g.add_argument("-q", "--quiet", action="store_true", help="suppress per-file lines")
    g.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO", format="<level>{level: <8}</level> | {message}")
    if args.log_file is not None:
        logger.add(args.log_file, level="DEBUG", rotation="10 MB")
    if not args.paths:
        args.paths = ["."]
    if shutil.which("min-html") is None:
        logger.error(f"minhtml binary not found on PATH: {args.minhtml_bin!r}")
        return 2
    if args.executor == "interpreter" and not _HAS_IPE:
        logger.warning("InterpreterPoolExecutor unavailable; using ProcessPoolExecutor")
        args.executor = "process"
    if args.slugify and not args.no_transliterate and not _HAS_TRANSLIT:
        logger.warning(
            "neither 'unidecode' nor 'anyascii' is installed; "
            "non-ASCII chars will be replaced with '_'. "
            "Install one: pip install unidecode"
        )
    cutoff: float | None = None
    if args.newer_than is not None:
        try:
            cutoff = time.time() - _parse_duration(args.newer_than).total_seconds()
        except ValueError as e:
            logger.error(str(e))
            return 2
    files = _collect(args, cutoff)
    if not files:
        logger.warning(f"no files matched inputs: {args.paths}")
        return 0
    try:
        vproc = subprocess.run(["min-html", "--version"], capture_output=True, timeout=5, check=False)
        minhtml_ver = (vproc.stdout or vproc.stderr).decode("utf-8", "replace").strip() or "(unknown)"
    except Exception:
        minhtml_ver = "(unknown)"
    workers = MAX_WORKERS if MAX_WORKERS > 0 else (os.cpu_count() or 4)
    total = len(files)
    chunksize = args.chunksize if args.chunksize > 0 else max(1, total // (workers * 4))
    dry_run = args.dry_run or args.check
    minhtml_args = _resolve_minhtml_args(args)
    use_color = (
        not args.no_color
        and "NO_COLOR" not in os.environ
        and (os.environ.get("CLICOLOR_FORCE", "") not in ("", "0") or sys.stdout.isatty())
    )
    if args.slugify:
        rename_plans = _plan_renames(
            files,
            True,
            args.slug_lowercase,
            not args.no_transliterate,
        )
        files, renames = _apply_renames(files, rename_plans, dry_run, use_color)
    else:
        renames = []
    logger.info(
        f"minhtml={args.minhtml_bin} {minhtml_ver} | files={total} "
        f"executor={args.executor} workers={workers} chunksize={chunksize} "
        f"dry_run={dry_run} slugify={args.slugify} "
        f"renames={sum(1 for r in renames if r.status in ('renamed', 'would-rename'))} "
        f"backup={args.backup} strict={args.strict} timeout={args.timeout:g}s"
    )
    jobs: list[Job] = [
        Job(
            path=str(p),
            dry_run=dry_run,
            minhtml_bin=args.minhtml_bin,
            minhtml_args=minhtml_args,
            timeout=args.timeout,
            strict=args.strict,
            backup_suffix=args.backup,
            fsync=args.fsync,
            preserve_mtime=args.preserve_mtime,
        )
        for p in files
    ]
    term_w = shutil.get_terminal_size(fallback=(120, 24)).columns
    cw = len(str(total))
    counter_w = len(f"[{total}/{total}]")
    max_orig = max((p.stat().st_size for p in files), default=0)
    bytes_w = max(3, len(str(max_orig)))
    fn_w = min(max(len(p.name) for p in files), max(20, term_w - counter_w - 3 - 2 - bytes_w))
    try:
        executor = _make_executor(args.executor, workers)
    except Exception as e:
        logger.error(f"cannot create executor: {e}")
        return 2
    interrupted = {"v": False}

    def _on_sigterm(signum: int, frame: object) -> None:
        interrupted["v"] = True
        raise KeyboardInterrupt

    prev_handler = None
    if hasattr(signal, "SIGTERM"):
        try:
            prev_handler = signal.signal(signal.SIGTERM, _on_sigterm)
        except (ValueError, OSError):
            prev_handler = None
    results: list[Result] = []
    error_count = 0
    exit_code = 0
    job_tuples: list[JobTuple] = [_job_to_tuple(j) for j in jobs]
    try:
        with executor:
            it = executor.map(_worker_entry, job_tuples, chunksize=chunksize)
            for idx, r_raw in enumerate(it, start=1):
                r = _tuple_to_result(r_raw)
                results.append(r)
                if r.error is not None:
                    error_count += 1
                    logger.error(f"{Path(r.path).name}: {r.error}")
                if not args.quiet and not args.json:
                    print(_format_result(r, idx, total, fn_w, bytes_w, cw, use_color), file=sys.stdout, flush=True)
                if interrupted["v"]:
                    break
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return 130
    except Exception as e:
        logger.exception(f"executor failed: {e}")
        return 1
    finally:
        if prev_handler is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, prev_handler)
    ok = [r for r in results if r.error is None and r.skip_reason != "empty"]
    total_orig = sum(r.original for r in ok)
    freed = sum(max(0, r.original - r.new) for r in ok if r.new <= r.original)
    changed = sum(1 for r in ok if r.skip_reason is None and r.new < r.original)
    grown = sum(1 for r in ok if r.new > r.original)
    unchanged = sum(1 for r in ok if r.skip_reason == "unchanged")
    skipped = sum(1 for r in ok if r.skip_reason == "empty")
    pct = (freed / total_orig * 100.0) if total_orig else 0.0
    renamed = sum(1 for r in renames if r.status == "renamed")
    would_rename = sum(1 for r in renames if r.status == "would-rename")
    ext_rollup: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in ok:
        ext = Path(r.path).suffix.lower() or "(none)"
        ext_rollup[ext][0] += 1
        ext_rollup[ext][1] += r.original
        if r.new < r.original:
            ext_rollup[ext][2] += r.original - r.new
    if args.json:
        _emit_json(
            results,
            renames,
            {
                "files_total": total,
                "bytes_original": total_orig,
                "bytes_freed": freed,
                "ratio_pct": round(pct, 4),
                "changed": changed,
                "unchanged": unchanged,
                "grown": grown,
                "skipped_empty": skipped,
                "errors": error_count,
                "renamed": renamed,
                "would_rename": would_rename,
                "by_extension": {
                    k: {"files": v[0], "original": v[1], "freed": v[2]} for k, v in sorted(ext_rollup.items())
                },
            },
            dry_run,
        )
    else:
        verb = "would free" if dry_run else "freed"
        summary = (
            f"{verb} {freed} bytes ({_human_bytes(freed)}, {pct:.2f}%) | "
            f"changed={changed} unchanged={unchanged} grown={grown} "
            f"skipped={skipped} errors={error_count}"
        )
        if args.slugify:
            rlabel = "would-rename" if dry_run else "renamed"
            summary += f" {rlabel}={would_rename if dry_run else renamed}"
        if freed > 0 and use_color:
            summary = f"{GREEN}{summary}{RESET}"
        print(file=sys.stdout)
        print(summary, file=sys.stdout)
        if args.verbose and ext_rollup:
            print("by extension:", file=sys.stdout)
            for ext, (cnt, orig, fr) in sorted(ext_rollup.items()):
                print(f"  {ext:<10}  files={cnt:<4}  orig={orig:<10}  freed={fr}", file=sys.stdout)
        if args.top > 0:
            _print_top(results, args.top, use_color)
    if error_count:
        logger.warning(f"{error_count} file(s) failed; see errors above")
    if error_count > 0:
        exit_code = 1
    if args.check and (changed > 0 or would_rename > 0):
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
