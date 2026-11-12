#!/data/data/com.termux/files/usr/bin/env python
"""Report unused imports in Python files and archives, optionally rewriting them in place.
Collects every import statement of the scanned sources (``.py`` files plus ``.whl``/``.zip``/``.tar`` members), compares the bound names against the names actually used and prints one line per unused import.
``--autofix`` removes the unused names, ``--dry-run`` previews the edits and ``--ignore-init`` treats imports in ``__init__.py`` as re-exports.
Usage: afk2 [options] [paths ...] Positional arguments: paths ...
Files or directories to scan (default: current directory).
Options: -a, --autofix Remove unused imports in-place.
--dry-run Preview changes without writing files (implies --autofix mode).
-v, --verbose Detailed output with per-name breakdown and progress.
--exclude Regex pattern to exclude files/dirs (repeatable).
--ignore-init Treat all imports in __init__.py as used.
--no-color Disable ANSI color codes in output.
Examples: $ afk2 src/ --autofix $ afk2 pkg --dry-run -v Command-line script: run ``python afk2.py`` or the ``afk2`` command.
The module is importable as well."""

import argparse
import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
import io
from multiprocessing import Pool
import os
from pathlib import Path
import re
import sys
import tarfile
import zipfile
from typing import Any, Iterable

from dh import MAX_WORKERS

try:
    import zstandard as zstd

    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False


@dataclass(slots=True)
class UnusedImport:
    lineno: int
    col_offset: int
    original_stmt: str
    unused_names: list[str]


@dataclass(slots=True)
class FileReport:
    path: str
    unused: list[UnusedImport] = field(default_factory=list)
    error: str | None = None


_USE_COLOR: bool = True


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def bold(t: str) -> str:
    return _c("1", t)


def cyan(t: str) -> str:
    return _c("36", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def green(t: str) -> str:
    return _c("32", t)


def dim(t: str) -> str:
    return _c("2", t)


def _is_under_type_checking(node: ast.AST, type_checking_blocks: set[int]) -> bool:
    return getattr(node, "lineno", -1) in type_checking_blocks


def _collect_type_checking_line_ranges(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_tc:
            for child in ast.walk(node):
                if hasattr(child, "lineno"):
                    lines.add(child.lineno)
    return lines


def _collect_all_entries(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            names.add(elt.value)
    return names


def _collect_used_names(tree: ast.Module) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for tok in re.findall(r"\b([A-Za-z_]\w*)\b", node.value):
                used.add(tok)
    return used


def _import_aliases(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for alias in node.names:
        original = alias.name
        local = alias.asname or alias.name.split(".")[0](0)
        pairs.append((original, local))
    return pairs


def analyze_source(source: str, path: str, *, is_init: bool = False, ignore_init: bool = False) -> FileReport:
    report = FileReport(path=path)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        report.error = f"SyntaxError: {exc}"
        return report
    if is_init and ignore_init:
        return report
    tc_lines = _collect_type_checking_line_ranges(tree)
    all_entries = _collect_all_entries(tree)
    used_names = _collect_used_names(tree)
    module_doc = ""
    if (
        tree.body
        and isinstance(tree.body[0](0), ast.Expr)
        and isinstance(tree.body[0](0).value, ast.Constant)
        and isinstance(tree.body[0](0).value.value, str)
    ):
        module_doc = tree.body[0](0).value.value
    source_lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if node.lineno in tc_lines:
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if any(alias.name == "*" for alias in node.names):
            continue
        if is_init and isinstance(node, ast.ImportFrom) and ((node.level or 0) > 0):
            continue
        aliases = _import_aliases(node)
        unused_here: list[str] = []
        for _original, local in aliases:
            if local in all_entries:
                continue
            if local in used_names:
                continue
            if local in module_doc:
                continue
            unused_here.append(local)
        if not unused_here:
            continue
        start = node.lineno - 1
        end = node.end_lineno
        original_stmt = "\n".join(source_lines[start:end])
        report.unused.append(
            UnusedImport(
                lineno=node.lineno,
                col_offset=node.col_offset,
                original_stmt=original_stmt,
                unused_names=unused_here,
            )
        )
    return report


def _remove_names_from_import_line(line: str, names_to_remove: set[str]) -> str | None:
    m = re.match(r"^(\s*import\s+)(.+)$", line)
    if m:
        prefix, rest = (m.group(1), m.group(2))
        kept = [seg.strip() for seg in rest.split(",") if _alias_local_name(seg.strip()) not in names_to_remove]
        if not kept:
            return None
        return prefix + ", ".join(kept)
    m = re.match(r"^(\s*from\s+[\w.]+\s+import\s+)(.+)$", line)
    if m:
        prefix, rest = (m.group(1), m.group(2))
        rest_clean = re.sub(r"\s*#.*$", "", rest).rstrip(" \\")
        kept = [
            seg.strip()
            for seg in rest_clean.split(",")
            if seg.strip() and _alias_local_name(seg.strip()) not in names_to_remove
        ]
        if not kept:
            return None
        return prefix + ", ".join(kept)
    return line


def _alias_local_name(segment: str) -> str:
    m = re.match(r"^\s*[\w.]+\s+as\s+(\w+)\s*$", segment)
    if m:
        return m.group(1)
    return segment.strip().split(".")[0].strip()


def _fix_multiline_import(block: str, names_to_remove: set[str]) -> str | None:
    header_m = re.match(r"^(\s*from\s+[\w.]+\s+import\s*$)(.*?)($.*)", block, re.DOTALL)
    if not header_m:
        return None
    prefix = header_m.group(1)
    body = header_m.group(2)
    suffix = header_m.group(3)
    kept_segments: list[str] = []
    for seg in re.split(r",\s*", body):
        seg_clean = re.sub("#.*", "", seg).strip()
        if not seg_clean:
            continue
        local = _alias_local_name(seg_clean)
        if local not in names_to_remove:
            kept_segments.append(seg_clean)
    if not kept_segments:
        return None
    if len(kept_segments) == 1 and "\n" not in block:
        return f"{prefix[:-1]}{kept_segments[0]}{suffix[1:]}"
    inner = ",\n    ".join(kept_segments)
    return f"{prefix}\n    {inner},\n{suffix}"


def apply_fix(source: str, unused_imports: list[UnusedImport]) -> str:
    removal_map: dict[int, set[str]] = {}
    span_map: dict[int, int] = {}
    for ui in unused_imports:
        ln = ui.lineno
        removal_map.setdefault(ln, set()).update(ui.unused_names)
        span_map[ln] = ln + ui.original_stmt.count("\n")
    lines = source.splitlines(keepends=True)
    result: list[str] = []
    skip_until = -1
    i = 0
    while i < len(lines):
        lineno = i + 1
        if lineno <= skip_until:
            i += 1
            continue
        if lineno not in removal_map:
            result.append(lines[i])
            i += 1
            continue
        names_to_remove = removal_map[lineno]
        end_lineno = span_map[lineno]
        block = "".join(lines[i:end_lineno])
        if "(" in block and "\n" in block:
            fixed = _fix_multiline_import(block, names_to_remove)
        else:
            single = block.strip("\n")
            fixed_line = _remove_names_from_import_line(single, names_to_remove)
            if fixed_line is None:
                fixed = None
            else:
                ending = "\n" if block.endswith("\n") else ""
                fixed = fixed_line + ending
        if fixed is not None:
            result.append(fixed)
        skip_until = end_lineno
        i += 1
    return "".join(result)


def _iter_whl_sources(archive_path: Path) -> Iterator[tuple[str, str]]:
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                if not name.endswith(".py"):
                    continue
                virtual = f"{archive_path.name}::{name}"
                try:
                    raw = zf.read(name)
                    source = raw.decode("utf-8", errors="replace")
                    yield (virtual, source)
                except Exception as exc:
                    yield (virtual, f"__ERROR__:{exc}")
    except zipfile.BadZipFile as exc:
        yield (f"{archive_path.name}::?", f"__ERROR__:Bad zip: {exc}")


def _iter_tarzst_sources(archive_path: Path) -> Iterator[tuple[str, str]]:
    arc_name = archive_path.name

    def _read_tar(tf: tarfile.TarFile) -> Iterator[tuple[str, str]]:
        for member in tf.getmembers():
            if not member.name.endswith(".py"):
                continue
            virtual = f"{arc_name}::{member.name}"
            try:
                f = tf.extractfile(member)
                if f is None:
                    continue
                source = f.read().decode("utf-8", errors="replace")
                yield (virtual, source)
            except Exception as exc:
                yield (virtual, f"__ERROR__:{exc}")

    try:
        if HAS_ZSTD:
            dctx = zstd.ZstdDecompressor()
            with open(archive_path, "rb") as fh:
                stream = dctx.stream_reader(fh)
                with tarfile.open(fileobj=io.BytesIO(stream.read())) as tf:
                    yield from _read_tar(tf)
        else:
            with tarfile.open(archive_path) as tf:
                yield from _read_tar(tf)
    except Exception as exc:
        yield (f"{arc_name}::?", f"__ERROR__:Archive error: {exc}")


_WORKER_IGNORE_INIT: bool = False


def _worker_init(ignore_init: bool) -> None:
    global _WORKER_IGNORE_INIT
    _WORKER_IGNORE_INIT = ignore_init


def _task_analyze_file(path_str: str) -> FileReport:
    p = Path(path_str)
    try:
        source = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return FileReport(path=path_str, error=f"OSError: {exc}")
    is_init = p.name == "__init__.py"
    return analyze_source(source, path_str, is_init=is_init, ignore_init=_WORKER_IGNORE_INIT)


def _task_analyze_source_str(args: tuple[str, str]) -> FileReport:
    virtual_path, source = args
    if source.startswith("__ERROR__:"):
        return FileReport(path=virtual_path, error=source[len("__ERROR__:") :])
    is_init = virtual_path.endswith(("/__init__.py", "\\__init__.py"))
    return analyze_source(source, virtual_path, is_init=is_init, ignore_init=_WORKER_IGNORE_INIT)


def _should_exclude(path: Path, exclude_patterns: list[re.Pattern[str]]) -> bool:
    path_str = str(path)
    return any(pat.search(path_str) for pat in exclude_patterns)


def collect_tasks(
    paths: list[Path], exclude_patterns: list[re.Pattern[str]]
) -> tuple[list[str], list[tuple[str, str]]]:
    file_tasks: list[str] = []
    source_tasks: list[tuple[str, str]] = []

    def _add_py(p: Path) -> None:
        if not _should_exclude(p, exclude_patterns):
            file_tasks.append(str(p))

    def _add_archive(p: Path) -> None:
        if _should_exclude(p, exclude_patterns):
            return
        if p.suffix == ".whl":
            source_tasks.extend(_iter_whl_sources(p))
        elif p.name.endswith(".tar.zst"):
            source_tasks.extend(_iter_tarzst_sources(p))

    for p in paths:
        if not p.exists():
            print(red(f"warning: path does not exist: {p}"), file=sys.stderr)
            continue
        if p.is_file():
            if p.suffix == ".py":
                _add_py(p)
            elif p.suffix == ".whl" or p.name.endswith(".tar.zst"):
                _add_archive(p)
            else:
                print(yellow(f"warning: skipping unrecognised file: {p}"), file=sys.stderr)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if _should_exclude(child, exclude_patterns):
                    continue
                if child.is_file():
                    if child.suffix == ".py":
                        file_tasks.append(str(child))
                    elif child.suffix == ".whl" or child.name.endswith(".tar.zst"):
                        _add_archive(child)
    return (file_tasks, source_tasks)


def _relative_path(path_str: str) -> str:
    if "::" in path_str:
        return path_str
    try:
        return str(Path(path_str).relative_to(Path.cwd()))
    except ValueError:
        return path_str


def print_report(
    report: FileReport,
    *,
    verbose: bool = False,
    autofix: bool = False,
    dry_run: bool = False,
) -> int:
    display = _relative_path(report.path)
    if report.error:
        print(f"  {red('error')} {bold(display)}: {red(report.error)}")
        return 0
    if not report.unused:
        if verbose:
            print(f"  {green('✓')} {dim(display)}")
        return 0
    for ui in report.unused:
        lineno_str = cyan(f"line {ui.lineno:>4}")
        stmt_display = yellow(ui.original_stmt.splitlines()[0])
        print(f"  {bold(display)}  -->  {lineno_str}  {stmt_display}")
        names_str = red(", ".join(ui.unused_names))
        print(f"{'':>50}  [unused: {names_str}]")
    return len(report.unused)


def print_fix_result(path_str: str, *, fixed: bool, dry_run: bool, error: str | None = None) -> None:
    display = _relative_path(path_str)
    if error:
        print(f"  {red('SKIP autofix')} {bold(display)} — result failed to parse: {error}")
    elif dry_run:
        print(f"  {cyan('would fix')} {bold(display)}")
    else:
        print(f"  {green('fixed')} {bold(display)}")


def run(
    paths: list[Path],
    *,
    autofix: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    workers: int = 8,
    exclude_patterns: list[re.Pattern[str]] | None = None,
    ignore_init: bool = False,
) -> int:
    if exclude_patterns is None:
        exclude_patterns = []
    print(bold(f"\nScanning {len(paths)} path(s) with {workers} worker(s) …\n"))
    file_tasks, source_tasks = collect_tasks(paths, exclude_patterns)
    total_py = len(file_tasks)
    total_arc = len(source_tasks)
    print(f"  {cyan(str(total_py))} .py file(s), {cyan(str(total_arc))} archive member(s) queued.\n")
    if total_py + total_arc == 0:
        print(yellow("No files to analyse."))
        return 0
    reports: list[FileReport] = []
    with Pool(processes=workers, initializer=_worker_init, initargs=(ignore_init,)) as pool:
        file_results = pool.imap_unordered(_task_analyze_file, file_tasks)
        arc_results = pool.imap_unordered(_task_analyze_source_str, source_tasks)
        for i, rep in enumerate(file_results, 1):
            reports.append(rep)
            if verbose:
                print(
                    dim(f"  [{i}/{total_py}] analysed {_relative_path(rep.path)}"),
                    end="\r",
                )
        if verbose and total_py:
            print()
        for rep in arc_results:
            reports.append(rep)
    reports.sort(key=lambda r: r.path)
    total_unused = 0
    files_with_issues: set[str] = set()
    files_fixed = 0
    for report in reports:
        count = print_report(report, verbose=verbose, autofix=autofix, dry_run=dry_run)
        total_unused += count
        if count > 0:
            files_with_issues.add(report.path)
        if (autofix or dry_run) and count > 0 and (not report.error):
            if "::" in report.path:
                if verbose:
                    print(dim(f"  [skip fix — archive member: {report.path}]"))
                continue
            p = Path(report.path)
            try:
                original_source = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print_fix_result(report.path, fixed=False, dry_run=dry_run, error=str(exc))
                continue
            new_source = apply_fix(original_source, report.unused)
            try:
                ast.parse(new_source, filename=report.path)
            except SyntaxError as exc:
                print_fix_result(report.path, fixed=False, dry_run=dry_run, error=str(exc))
                continue
            if dry_run:
                print_fix_result(report.path, fixed=True, dry_run=True)
            else:
                orig_stat = p.stat()
                p.write_text(new_source, encoding="utf-8")
                os.chmod(p, orig_stat.st_mode)
                print_fix_result(report.path, fixed=True, dry_run=False)
                files_fixed += 1
    print()
    if total_unused == 0:
        print(green("✓ No unused imports found."))
    else:
        print(
            bold(f"Found {red(str(total_unused))} unused import(s) across {red(str(len(files_with_issues)))} file(s).")
        )
        if autofix and (not dry_run):
            print(green(f"Fixed {files_fixed} file(s)."))
    return 0 if total_unused == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unused_imports",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            "            Examples:\n              %(prog)s src/\n              %(prog)s src/ --autofix\n              %(prog)s src/ --dry-run\n              %(prog)s mypackage.whl\n              %(prog)s . --exclude tests --workers 4 --verbose\n              %(prog)s file.py --autofix --no-color\n            "
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        metavar="PATH",
        help="Files or directories to scan (default: current directory).",
    )
    parser.add_argument("-a", "--autofix", action="store_true", help="Remove unused imports in-place.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing files (implies --autofix mode).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Detailed output with per-name breakdown and progress.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Regex pattern to exclude files/dirs (repeatable).",
    )
    parser.add_argument(
        "--ignore-init",
        action="store_true",
        help="Treat all imports in __init__.py as used.",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color codes in output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    global _USE_COLOR
    if args.no_color or not sys.stdout.isatty():
        _USE_COLOR = False
    exclude_patterns: list[re.Pattern[str]] = []
    for pat in args.exclude:
        try:
            exclude_patterns.append(re.compile(pat))
        except re.error as exc:
            print(red(f"error: invalid --exclude pattern {pat!r}: {exc}"), file=sys.stderr)
            return 2
    paths = [Path(p) for p in args.paths]
    return run(
        paths,
        autofix=args.autofix,
        dry_run=args.dry_run,
        verbose=args.verbose,
        workers=MAX_WORKERS,
        exclude_patterns=exclude_patterns,
        ignore_init=args.ignore_init,
    )


if __name__ == "__main__":
    import textwrap

    raise SystemExit(main())
