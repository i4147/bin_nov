#!/data/data/com.termux/files/usr/bin/env python
"""Format Python files (black, yapf, autopep8, ruff and more)."""


import argparse
from concurrent.futures import (
    ProcessPoolExecutor,
    TimeoutError as FuturesTimeout,
)
import difflib
import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable

from dh import MAX_WORKERS, SKIP_DIRS, atomic_write
from loguru import logger


BACKENDS: tuple[str, ...] = ("black", "yapf", "autopep8", "ruff", "isort", "autoflake")
DEFAULT_LINE_LENGTH: int = 120
DEFAULT_TARGET: str = "3.12"
DEFAULT_EXTS: tuple[str, ...] = (".py", ".pyi", ".pyw")


Result = dict[str, Any]
Job = tuple[str, list[str], dict[str, Any], bool, bool, bool, str, int]
Emit = Callable[[int, Result], None]


def _setup_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<level>{level: <8}</level> | <cyan>{message}</cyan>",
        colorize=sys.stderr.isatty() and not os.environ.get("NO_COLOR"),
    )


def parse_size(s: str | None) -> int | None:
    if not s:
        return None
    t = s.upper().strip()
    for unit, mult in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024), ("B", 1)):
        if t.endswith(unit):
            return int(float(t[: -len(unit)]) * mult)
    return int(t)


def _color(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _excluded(path: Path, patterns: list[str]) -> bool:
    if not patterns:
        return False
    s, name = str(path), path.name
    return any(fnmatch.fnmatch(s, p) or fnmatch.fnmatch(name, p) for p in patterns)


def is_python_file(path: Path, include_exts: tuple[str, ...]) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    if path.suffix.lower() in include_exts:
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as fh:
            head = fh.readline(256)
    except OSError:
        return False
    return head.startswith(b"#!") and b"python" in head


def _load_gitignore_spec() -> Any:
    try:
        import pathspec
    except ImportError:
        return None
    gi = Path.cwd() / ".gitignore"
    if not gi.exists():
        return None
    try:
        return pathspec.PathSpec.from_lines("gitwildmatch", gi.read_text(encoding="utf-8").splitlines())
    except Exception as exc:
        logger.warning(f"failed to load .gitignore: {exc}")
        return None


def _git_root() -> Path:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning(f"git unavailable: {exc}")
        return Path.cwd()
    if p.returncode != 0:
        logger.warning(f"git rev-parse failed: {p.stderr.strip()}")
        return Path.cwd()
    return Path(p.stdout.strip())


def _git_paths(args: list[str]) -> set[Path]:
    try:
        p = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.warning(f"git unavailable: {exc}")
        return set()
    if p.returncode != 0:
        logger.warning(f"git error ({' '.join(args)}): {p.stderr.strip()}")
        return set()
    root = _git_root()
    out: set[Path] = set()
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.add((root / line).resolve())
        except OSError:
            continue
    return out


def _select_git_files(args: argparse.Namespace) -> set[Path] | None:
    if not (args.since or args.staged or args.changed):
        return None
    selected: set[Path] = set()
    if args.since:
        selected |= _git_paths(["diff", "--name-only", args.since])
    if args.staged:
        selected |= _git_paths(["diff", "--name-only", "--cached"])
    if args.changed:
        selected |= _git_paths(["diff", "--name-only", "HEAD"])
        selected |= _git_paths(["ls-files", "--others", "--exclude-standard"])
    return selected


def discover_files(
    roots: list[Path],
    *,
    recursive: bool,
    exclude_globs: list[str],
    include_exts: tuple[str, ...],
    skip_dirs: set[str],
    max_size: int | None,
    gitignore_spec: Any,
) -> list[Path]:
    found: list[Path] = []

    def gitignore_hit(p: Path, is_dir: bool) -> bool:
        if gitignore_spec is None:
            return False
        try:
            rel = p.relative_to(Path.cwd()).as_posix()
        except ValueError:
            rel = p.as_posix()
        return bool(gitignore_spec.match_file(rel + ("/" if is_dir else "")))

    def consider(p: Path) -> None:
        if p.is_symlink() or not is_python_file(p, include_exts):
            return
        if _excluded(p, exclude_globs) or gitignore_hit(p, False):
            return
        if max_size is not None:
            try:
                if p.stat().st_size > max_size:
                    return
            except OSError as exc:
                logger.warning(f"stat failed on {p}: {exc}")
                return
        found.append(p)

    for root in roots:
        if root.is_symlink():
            logger.debug(f"skip symlink: {root}")
            continue
        if not root.exists():
            logger.warning(f"skip missing: {root}")
            continue
        if root.is_file():
            consider(root)
            continue
        if not root.is_dir():
            continue
        stack: list[Path] = [root]
        while stack:
            cur = stack.pop()
            try:
                entries = sorted(cur.iterdir())
            except OSError as exc:
                logger.warning(f"cannot read dir {cur}: {exc}")
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if not recursive or entry.name in skip_dirs:
                        continue
                    if _excluded(entry, exclude_globs) or gitignore_hit(entry, True):
                        continue
                    stack.append(entry)
                else:
                    consider(entry)
    return found


def _format_black(src: str, opts: dict[str, Any]) -> str:
    import black

    targets: set[Any] = set()
    for tv in opts.get("target_versions") or [DEFAULT_TARGET]:
        v = getattr(black.TargetVersion, "PY" + str(tv).replace(".", ""), None)
        if v is not None:
            targets.add(v)
    mode = black.Mode(target_versions=targets or None, line_length=opts["line_length"])
    try:
        return black.format_file_contents(src, fast=False, mode=mode)
    except black.NothingChanged:
        return src


def _format_yapf(src: str, opts: dict[str, Any]) -> str:
    from yapf.yapflib.yapf_api import FormatCode

    out, _ = FormatCode(
        src,
        style_config={"based_on_style": "pep8", "column_limit": opts["line_length"]},
    )
    return out


def _format_autopep8(src: str, opts: dict[str, Any]) -> str:
    import autopep8

    return autopep8.fix_code(src, options={"max_line_length": opts["line_length"]})


def _format_ruff(src: str, opts: dict[str, Any]) -> str:
    try:
        import ruff
    except ImportError as exc:
        msg = "ruff is not installed"
        raise RuntimeError(msg) from exc
    for name in ("format_str", "format_code"):
        fn = getattr(ruff, name, None)
        if fn is not None:
            try:
                return fn(src, line_length=opts["line_length"])
            except TypeError:
                return fn(src)
    msg = "This ruff build does not expose a Python formatting API; use the ruff CLI or upgrade ruff."
    raise RuntimeError(msg)


def _format_isort(src: str, opts: dict[str, Any]) -> str:
    import isort

    kwargs: dict[str, Any] = {"line_length": opts["line_length"]}
    if opts.get("isort_profile"):
        kwargs["profile"] = opts["isort_profile"]
    return isort.code(src, **kwargs)


def _format_autoflake(src: str, opts: dict[str, Any]) -> str:
    import autoflake

    return autoflake.fix_code(
        src,
        remove_unused_imports=True,
        remove_all_unused_imports=opts.get("autoflake_remove_all", True),
    )


_FORMATTERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "black": _format_black,
    "yapf": _format_yapf,
    "autopep8": _format_autopep8,
    "ruff": _format_ruff,
    "isort": _format_isort,
    "autoflake": _format_autoflake,
}


def run_backends(src: str, backends: list[str], opts: dict[str, Any]) -> str:
    for b in backends:
        src = _FORMATTERS[b](src, opts)
    return src


def process_file(job: Job) -> Result:
    path_str, backends, opts, do_diff, dry_run, do_backup, encoding, diff_context = job
    path = Path(path_str)
    result: Result = {
        "path": path_str,
        "old_size": 0,
        "new_size": 0,
        "changed": False,
        "diff": None,
        "error": None,
    }
    try:
        old_full = path.read_bytes()
        result["old_size"] = len(old_full)

        bom, body = b"", old_full
        if body.startswith(b"\xef\xbb\xbf"):
            body = body[3:]
            if not opts.get("strip_bom"):
                bom = b"\xef\xbb\xbf"

        try:
            src = body.decode(encoding)
        except UnicodeDecodeError as exc:
            result["error"] = f"decode: {exc}"
            result["new_size"] = result["old_size"]
            return result

        newline = "\r\n" if "\r\n" in src else "\n"
        work = src.replace("\r\n", "\n")
        work = run_backends(work, backends, opts)
        new_src = work.replace("\n", "\r\n") if newline == "\r\n" else work
        new_full = bom + new_src.encode(encoding)

        result["new_size"] = len(new_full)
        result["changed"] = new_full != old_full

        if do_diff and result["changed"]:
            result["diff"] = "".join(
                difflib.unified_diff(
                    src.splitlines(keepends=True),
                    new_src.splitlines(keepends=True),
                    fromfile=path_str,
                    tofile=path_str,
                    n=diff_context,
                )
            )

        if result["changed"] and not dry_run:
            if do_backup:
                atomic_write(path_str + ".bak", old_full)
            atomic_write(path_str, new_full)

    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _row(i: int, total: int, res: Result, term_width: int, relative: bool, color: bool) -> str:
    idx = f"[{i}/{total}]"
    if relative:
        try:
            name = str(Path(res["path"]).relative_to(Path.cwd()))
        except ValueError:
            name = res["path"]
    else:
        name = Path(res["path"]).name

    idx_w = len(f"[{total}/{total}]") + 1
    size_w = 10
    path_w = max(20, term_width - idx_w - size_w - 2)
    if len(name) > path_w:
        name = "…" + name[-(path_w - 1) :]

    if res["error"]:
        status = _color("ERROR".rjust(size_w), "31", color)
    else:
        delta = res["new_size"] - res["old_size"]
        status = _color(f"{delta:+d}".rjust(size_w), "32", color)

    return f"{idx:<{idx_w}} {name:<{path_w}} {status}"


def _summarize(results: list[Result]) -> dict[str, int]:
    changed = sum(1 for r in results if r["changed"] and not r["error"])
    errors = sum(1 for r in results if r["error"])
    delta = sum(r["new_size"] - r["old_size"] for r in results if not r["error"])
    return {
        "total": len(results),
        "changed": changed,
        "unchanged": len(results) - changed - errors,
        "errors": errors,
        "delta_bytes": delta,
    }


def _log_failure(res: Result) -> None:
    if res.get("error"):
        logger.error(f"{res['path']}: {res['error']}")


def _run_jobs(jobs: list[Job], workers: int, timeout: float | None, emit: Emit) -> list[Result | None]:
    results: list[Result | None] = [None] * len(jobs)

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(process_file, j) for j in jobs]
            for i, fut in enumerate(futures):
                try:
                    res = fut.result(timeout=timeout)
                except FuturesTimeout:
                    err = f"timeout after {timeout}s"
                    res = {
                        "path": jobs[i][0],
                        "old_size": 0,
                        "new_size": 0,
                        "changed": False,
                        "diff": None,
                        "error": err,
                    }
                    logger.error(f"{jobs[i][0]}: {err}")
                except Exception as exc:
                    res = {
                        "path": jobs[i][0],
                        "old_size": 0,
                        "new_size": 0,
                        "changed": False,
                        "diff": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    logger.opt(exception=exc).error(f"worker crashed on {jobs[i][0]}")
                else:
                    _log_failure(res)
                results[i] = res
                emit(i, res)
    else:
        for i, job in enumerate(jobs):
            try:
                res = process_file(job)
            except Exception as exc:
                res = {
                    "path": job[0],
                    "old_size": 0,
                    "new_size": 0,
                    "changed": False,
                    "diff": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                logger.opt(exception=exc).error(f"unexpected failure on {job[0]}")
            else:
                _log_failure(res)
            results[i] = res
            emit(i, res)
    return results


def _handle_stdin(args: argparse.Namespace, backends: list[str], opts: dict[str, Any]) -> int:
    src = sys.stdin.read()
    newline = "\r\n" if "\r\n" in src else "\n"
    try:
        out = run_backends(src.replace("\r\n", "\n"), backends, opts)
    except Exception as exc:
        logger.opt(exception=exc).error("failed to format <stdin>")
        return 1
    if newline == "\r\n":
        out = out.replace("\n", "\r\n")

    if args.diff:
        sys.stdout.write(
            "".join(
                difflib.unified_diff(
                    src.splitlines(keepends=True),
                    out.splitlines(keepends=True),
                    fromfile="<stdin>",
                    tofile="<stdin>",
                    n=args.diff_context,
                )
            )
        )
    else:
        sys.stdout.write(out)
    return 1 if (args.check and out != src) else 0


def _list_backends() -> None:
    for name in BACKENDS:
        try:
            __import__(name)
            print(f"{name:<10} available")
        except ImportError:
            print(f"{name:<10} not installed")


def _print_versions() -> None:
    print(f"pyfmt {__version__}")
    print(f"python {sys.version.split()[0]}")
    for name in BACKENDS:
        try:
            mod = __import__(name)
            print(f"  {name:<10} {getattr(mod, '__version__', 'unknown')}")
        except ImportError:
            print(f"  {name:<10} (not installed)")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyfmt",
        description="Format Python files (default: black, target 3.12, line length 120).",
    )
    p.add_argument("paths", nargs="*", help="Files or directories (default: cwd).")

    g = p.add_argument_group("backend")
    g.add_argument("-b", "--backend", choices=BACKENDS, default="black")
    g.add_argument("--chain", default=None, help="Comma-separated backends run in order (overrides -b).")
    g.add_argument("--line-length", type=int, default=DEFAULT_LINE_LENGTH)
    g.add_argument("--target-version", default=DEFAULT_TARGET)
    g.add_argument("--isort-profile", default=None)
    g.add_argument("--autoflake-remove-all", action=argparse.BooleanOptionalAction, default=True)

    g = p.add_argument_group("discovery")
    g.add_argument("-r", "--recursive", action="store_true")
    g.add_argument("--exclude", default="")
    g.add_argument("--include-ext", default=",".join(DEFAULT_EXTS))
    g.add_argument("--max-size", default=None)
    g.add_argument("--respect-gitignore", action="store_true")
    g.add_argument("--no-skip-dirs", action="store_true")

    g = p.add_argument_group("git selection")
    g.add_argument(
        "--since", default=None, metavar="REF", help="Only files changed since REF (git diff --name-only REF)."
    )
    g.add_argument("--staged", action="store_true", help="Only staged files (git diff --name-only --cached).")
    g.add_argument("--changed", action="store_true", help="Only files changed vs HEAD plus untracked files.")

    g = p.add_argument_group("output / behavior")
    g.add_argument("--diff", action="store_true")
    g.add_argument("--diff-context", type=int, default=3)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--backup", action="store_true")
    g.add_argument("--strip-bom", action="store_true")
    g.add_argument("--encoding", default="utf-8")
    g.add_argument("--stdin", action="store_true")
    g.add_argument("--timeout", type=float, default=None)
    g.add_argument("--json", action="store_true")
    g.add_argument("--summary", action="store_true")
    g.add_argument("-q", "--quiet", action="store_true")
    g.add_argument("-v", "--verbose", action="store_true")
    g.add_argument("--no-color", action="store_true")
    g.add_argument("--relative-paths", action="store_true")

    g = p.add_argument_group("meta")
    g.add_argument("--version", action="store_true")
    g.add_argument("--list-backends", action="store_true")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    _setup_logging(args.verbose)

    if args.version:
        _print_versions()
        return 0
    if args.list_backends:
        _list_backends()
        return 0

    if args.chain:
        backends = [b.strip() for b in args.chain.split(",") if b.strip()]
        bad = [b for b in backends if b not in BACKENDS]
        if bad:
            logger.error(f"unknown backend(s) in --chain: {', '.join(bad)}")
            return 2
    else:
        backends = [args.backend]

    opts: dict[str, Any] = {
        "line_length": args.line_length,
        "target_versions": [v.strip() for v in args.target_version.split(",") if v.strip()],
        "isort_profile": args.isort_profile,
        "autoflake_remove_all": args.autoflake_remove_all,
        "strip_bom": args.strip_bom,
    }

    if args.stdin:
        return _handle_stdin(args, backends, opts)

    roots = [Path(x) for x in args.paths] if args.paths else [Path.cwd()]
    exclude_globs = [g.strip() for g in args.exclude.split(",") if g.strip()]
    include_exts: tuple[str, ...] = tuple(
        e.strip() if e.strip().startswith(".") else "." + e.strip() for e in args.include_ext.split(",") if e.strip()
    )
    skip_dirs = set() if args.no_skip_dirs else SKIP_DIRS
    max_size = parse_size(args.max_size)

    gitignore_spec: Any = None
    if args.respect_gitignore:
        gitignore_spec = _load_gitignore_spec()
        if gitignore_spec is None:
            logger.warning("--respect-gitignore ignored (install 'pathspec' and add .gitignore)")

    files = discover_files(
        roots,
        recursive=args.recursive,
        exclude_globs=exclude_globs,
        include_exts=include_exts,
        skip_dirs=skip_dirs,
        max_size=max_size,
        gitignore_spec=gitignore_spec,
    )

    git_filter = _select_git_files(args)
    if git_filter is not None:
        files = [f for f in files if f.resolve() in git_filter]

    if not files:
        if args.json and args.summary:
            json.dump({"files": [], "summary": _summarize([])}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            logger.info("No Python files found.")
        return 0

    total = len(files)

    if str(MAX_WORKERS).lower() == "auto":
        workers = min(os.cpu_count() or 4, total)
    else:
        try:
            workers = max(1, int(MAX_WORKERS))
        except ValueError:
            logger.error(f"invalid -j value: {MAX_WORKERS!r}")
            return 2

    if args.verbose and not args.json:
        logger.debug(
            f"backends={','.join(backends)} line_length={args.line_length} "
            f"target={','.join(opts['target_versions'])} workers={workers} "
            f"files={total}"
        )

    dry_run = args.dry_run or args.check
    jobs: list[Job] = [
        (str(f), backends, opts, args.diff, dry_run, args.backup, args.encoding, args.diff_context) for f in files
    ]

    color = not args.no_color and not os.environ.get("NO_COLOR") and sys.stdout.isatty()
    term_width = shutil.get_terminal_size((100, 24)).columns

    if args.json:

        def emit(i: int, res: Result) -> None:
            return None

    elif args.quiet:

        def emit(i: int, res: Result) -> None:
            if res["error"] or (args.check and res["changed"]):
                print(_row(i + 1, total, res, term_width, args.relative_paths, color))
                if res.get("diff"):
                    print(res["diff"])

    else:

        def emit(i: int, res: Result) -> None:
            print(_row(i + 1, total, res, term_width, args.relative_paths, color))
            if res.get("diff"):
                print(res["diff"])

    raw = _run_jobs(jobs, workers, args.timeout, emit)
    results: list[Result] = [r for r in raw if r is not None]

    if args.json:
        payload: dict[str, Any] = {
            "files": [
                {
                    "path": r["path"],
                    "old_size": r["old_size"],
                    "new_size": r["new_size"],
                    "delta": r["new_size"] - r["old_size"],
                    "changed": r["changed"],
                    "error": r["error"],
                    "diff": r["diff"] if args.diff else None,
                }
                for r in results
            ]
        }
        if args.summary:
            payload["summary"] = _summarize(results)
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.summary:
        s = _summarize(results)
        print()
        print(
            " | ".join([
                f"{s['total']} file{'s' if s['total'] != 1 else ''}",
                f"{s['changed']} changed",
                f"{s['unchanged']} unchanged",
                f"{s['errors']} error{'s' if s['errors'] != 1 else ''}",
                f"{s['delta_bytes']:+d} bytes",
            ])
        )

    if any(r["error"] for r in results):
        return 1
    if args.check and any(r["changed"] for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
