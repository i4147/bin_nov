#!/data/data/com.termux/files/usr/bin/env python
"""Python 2 to 3 migration toolbox.
Subcommands: ``detect`` classifies the sources as Py2 or Py3, ``refactor`` applies the lib2to3 fixers (dry run by default, ``-a`` writes), ``fixprint`` rewrites the ``print x`` and ``except X, e:`` lines, ``run2to3`` shells out to the external ``2to3`` and ``strip-tag`` removes the ``Tag:py2-none-any`` lines from the WHEEL members of the archives.
Usage: py3migrate {detect,refactor,fixprint,run2to3,strip-tag} [options] [paths ...] Positional arguments: paths ...
Files or directories to process (default: current directory).
Subcommands: detect Detect Py2 vs Py3 in source files.
refactor Rewrite Py2 → Py3 using lib2to3 fixers (dry-run by default).
fixprint Line-based fixes for `print x` and (with --all) `except X, e:` etc.
run2to3 Shell out to the external `2to3 -w -n -f all` CLI (run223.py).
strip-tag Strip a `Tag:py2-none-any` line from WHEEL members in archives.
Options: -v, --verbose Enable debug logging.
--ext -a, --apply Write changes in place (default: dry-run).
--fixers 'auto' (discover from --fixer-module) or comma-separated list of fixer module names.
--fixer-module --print-function Pass ``{'print_function': True}`` to RefactoringTool (my2to3.py).
--diff-mode Diff style: 'compact' (f23.py), 'full' (2232.py), 'none'.
--max-diff-lines --context-chars --backup Write a ``.bak`` next to each modified file.
...
plus 7 more; run ``py3migrate --help`` for the full list.
Examples: $ py3migrate refactor src/ --diff-mode compact $ py3migrate fixprint -a src/ Command-line script: run ``python py3migrate.py`` or the ``py3migrate`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
import pkgutil
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterable, Sequence
import zipfile

from dh import MAX_WORKERS
from loguru import logger


log = logger
DEFAULT_EXT: tuple[str, ...] = (".py",)
DEFAULT_FIXER_MODULE: str = "lib2to3.fixes"
DEFAULT_MAX_DIFF_LINES: int = 50
DEFAULT_CONTEXT_CHARS: int = 80
DEFAULT_TAG: str = "Tag:py2-none-any"
DEFAULT_TARGETS: tuple[str, ...] = ("WHEEL",)
ARCHIVE_EXTS: tuple[str, ...] = (".zip", ".whl", ".tar.gz", ".tgz", ".tar")
FALLBACK_FIXERS: tuple[str, ...] = (
    "lib2to3.fixes.fix_apply",
    "lib2to3.fixes.fix_asserts",
    "lib2to3.fixes.fix_basestring",
    "lib2to3.fixes.fix_buffer",
    "lib2to3.fixes.fix_dict",
    "lib2to3.fixes.fix_except",
    "lib2to3.fixes.fix_exec",
    "lib2to3.fixes.fix_execfile",
    "lib2to3.fixes.fix_exitfunc",
    "lib2to3.fixes.fix_filter",
    "lib2to3.fixes.fix_funcattrs",
    "lib2to3.fixes.fix_future",
    "lib2to3.fixes.fix_getcwdu",
    "lib2to3.fixes.fix_has_key",
    "lib2to3.fixes.fix_idioms",
    "lib2to3.fixes.fix_import",
    "lib2to3.fixes.fix_imports",
    "lib2to3.fixes.fix_imports2",
    "lib2to3.fixes.fix_input",
    "lib2to3.fixes.fix_itertools",
    "lib2to3.fixes.fix_itertools_imports",
    "lib2to3.fixes.fix_long",
    "lib2to3.fixes.fix_map",
    "lib2to3.fixes.fix_metaclass",
    "lib2to3.fixes.fix_methodattrs",
    "lib2to3.fixes.fix_ne",
    "lib2to3.fixes.fix_next",
    "lib2to3.fixes.fix_nonzero",
    "lib2to3.fixes.fix_numliterals",
    "lib2to3.fixes.fix_operator",
    "lib2to3.fixes.fix_paren",
    "lib2to3.fixes.fix_print",
    "lib2to3.fixes.fix_raise",
    "lib2to3.fixes.fix_raw_input",
    "lib2to3.fixes.fix_reduce",
    "lib2to3.fixes.fix_reload",
    "lib2to3.fixes.fix_renames",
    "lib2to3.fixes.fix_repr",
    "lib2to3.fixes.fix_set_literal",
    "lib2to3.fixes.fix_standarderror",
    "lib2to3.fixes.fix_sys_exc",
    "lib2to3.fixes.fix_throw",
    "lib2to3.fixes.fix_tuple_params",
    "lib2to3.fixes.fix_types",
    "lib2to3.fixes.fix_unicode",
    "lib2to3.fixes.fix_urllib",
    "lib2to3.fixes.fix_ws_comma",
    "lib2to3.fixes.fix_xrange",
    "lib2to3.fixes.fix_xreadlines",
    "lib2to3.fixes.fix_zip",
)


def iter_files(
    paths: Iterable[Path],
    exts: Sequence[str] = DEFAULT_EXT,
    *,
    skip: frozenset[str] = frozenset(),
) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        p = Path(p)
        candidates: list[Path] = []
        if p.is_file():
            if p.suffix in exts:
                candidates.append(p)
        elif p.is_dir():
            for ext in exts:
                candidates.extend(p.rglob(f"*{ext}"))
        for f in candidates:
            if f.name in skip or f in seen:
                continue
            seen.add(f)
            out.append(f)
    return out


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")
    except OSError as e:
        msg = f"cannot read {path}: {e}"
        raise OSError(msg) from e


def _make_backup(path: Path) -> None:
    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def _write(path: Path, text: str, *, backup: bool) -> None:
    if backup:
        _make_backup(path)
    path.write_text(text, encoding="utf-8")


def discover_fixers(fixer_module: str = DEFAULT_FIXER_MODULE) -> list[str]:
    try:
        import importlib

        mod = importlib.import_module(fixer_module)
    except ImportError:
        return list(FALLBACK_FIXERS)
    return [
        name for _finder, name, is_pkg in pkgutil.iter_modules(mod.__path__, prefix=fixer_module + ".") if not is_pkg
    ]


def resolve_fixers(spec: str, fixer_module: str) -> list[str]:
    if spec == "auto":
        return discover_fixers(fixer_module)
    return [f.strip() for f in spec.split(",") if f.strip()]


def _import_refactoring_tool():
    try:
        from lib2to3.refactor import RefactoringTool
    except ImportError as e:
        msg = (
            "lib2to3 is not available in this Python (removed in 3.13). "
            "Use Python 3.12 or install `2to3` from PyPI for the CLI path."
        )
        raise RuntimeError(msg) from e
    return RefactoringTool


class _CapturingTool:
    @staticmethod
    def build(fixers: list[str], options: dict) -> tuple[object, list[str], list[str]]:
        base = _import_refactoring_tool()

        class _Tool(base):
            def __init__(self, *a: Any, **kw: Any) -> None:
                self.captured_errors: list[str] = []
                self.captured_output: list[str] = []
                super().__init__(*a, **kw)

            def log_error(self, msg: str, *args: Any, **kwargs: Any) -> None:
                self.captured_errors.append(msg % args if args else msg)

            def log_message(self, msg: str, *args: Any, **kwargs: Any) -> None:
                if args:
                    msg = msg % args
                self.captured_output.append(msg)

        tool = _Tool(fixers, options)
        return tool, tool.captured_errors, tool.captured_output


def describe_diff(
    original: str,
    new_text: str,
    mode: str,
    max_lines: int,
    ctx_chars: int,
) -> str:
    if mode == "none" or original == new_text:
        return ""
    old = original.splitlines()
    new = new_text.splitlines()
    chunks: list[str] = []
    changed = 0
    for i, (a, b) in enumerate(zip(old, new)):
        if a == b:
            continue
        changed += 1
        if len(chunks) >= max_lines:
            continue
        if mode == "full":
            chunks.append(f"  Line {i + 1}: {a[:ctx_chars]} -> {b[:ctx_chars]}")
        else:
            chunks.append(f"-{a[:ctx_chars]}")
            chunks.append(f"+{b[:ctx_chars]}")
    if len(old) != len(new):
        chunks.append(f"  (line count: {len(old)} -> {len(new)})")
    header = f"Changed {changed} line(s)"
    if not chunks:
        return header
    body = "\n".join(chunks)
    if changed > max_lines:
        body += f"\n  ... and {changed - max_lines} more change(s)"
    return f"{header}\n{body}"


def _refactor_file(
    path: Path,
    fixers: list[str],
    print_function: bool,
    apply: bool,
    diff_mode: str,
    max_diff_lines: int,
    ctx_chars: int,
    backup: bool,
    show_errors: bool,
) -> tuple[Path, bool, str]:
    try:
        original = read_text(path)
    except OSError as e:
        return (path, False, f"✗ {path.name}: {e}")
    options = {"print_function": True} if print_function else {}
    try:
        if show_errors:
            tool, captured_errors, captured_output = _CapturingTool.build(fixers, options)
        else:
            tool = _import_refactoring_tool()(fixers, options)
            captured_errors, captured_output = [], []
    except RuntimeError as e:
        return (path, False, f"✗ {path.name}: {e}")
    try:
        new_tree = tool.refactor_string(original, str(path))
    except SyntaxError as e:
        return (path, False, f"✗ {path.name}: syntax error: {e}")
    except Exception as e:
        return (path, False, f"✗ {path.name}: refactor error: {e}")
    new_text = str(new_tree)
    log_block = ""
    if show_errors and (captured_errors or captured_output):
        joined = "\n".join(f"  {line}" for line in (captured_errors + captured_output))
        log_block = f"\n  [refactor log]\n{joined}"
    if new_text == original:
        return (path, False, f"○ {path.name}: no changes needed{log_block}")
    diff = describe_diff(original, new_text, diff_mode, max_diff_lines, ctx_chars)
    detail = f"\n{diff}" if diff else ""
    if not apply:
        return (path, True, f"📝 {path.name}: would change{detail}{log_block}")
    _write(path, new_text, backup=backup)
    return (path, True, f"✓ {path.name}: changed{detail}{log_block}")


def detect_file(path: Path) -> tuple[Path, int | None, str]:
    try:
        text = read_text(path)
    except OSError as e:
        return (path, None, f"read error: {e}")
    py2_score = 0
    py3_score = 0
    reasons: list[str] = []
    try:
        tree = ast.parse(text)
        py3_score += 1
        reasons.append("Parses cleanly under Python 3 grammar.")
    except SyntaxError as e:
        return (path, 2, f"High (Python 3 syntax error: {e})")
    if "print " in text and "print(" not in text:
        py2_score += 2
        reasons.append("Uses `print` statement without parentheses.")
    if "__future__" in text and "print_function" in text:
        py3_score += 2
        reasons.append("Uses `from __future__ import print_function`.")
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
            py3_score += 3
            reasons.append("Uses async / await syntax.")
        if isinstance(node, ast.FunctionDef):
            for arg in node.args.args:
                if getattr(arg, "annotation", None) is not None:
                    py3_score += 2
                    reasons.append("Uses function argument annotations.")
                    break
    if py2_score > py3_score:
        version = 2
        confidence = "High" if py2_score - py3_score > 2 else "Medium"
    elif py3_score > py2_score:
        version = 3
        confidence = "High" if py3_score - py2_score > 2 else "Medium"
    else:
        version = 3
        confidence = "Low"
        reasons.append("No strong indicators; defaulting to Python 3.")
    return (path, version, f"{confidence}: {'; '.join(reasons)}")


def cmd_detect(args: argparse.Namespace) -> int:
    files = iter_files(args.paths, args.ext)
    if not files:
        print("No Python files found.")
        return 1
    print(f"Scanning {len(files)} file(s) with {MAX_WORKERS} worker(s).\n")
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for path, version, reason in pool.map(detect_file, files):
            if version is None:
                print(f"⚠️  {path.name}: {reason}")
            else:
                print(f"{path.name} → Python {version}\n  {reason}")
    return 0


def cmd_refactor(args: argparse.Namespace) -> int:
    fixers = resolve_fixers(args.fixers, args.fixer_module)
    if not fixers:
        log.error("No fixers selected.")
        return 1
    print(f"Using {len(fixers)} fixer(s).")
    if not args.apply:
        print("Mode: DRY RUN (pass --apply to write)\n")
    else:
        print("Mode: APPLY\n")
    files = iter_files(args.paths)
    if not files:
        print("No Python files found to process.")
        return 0
    worker = partial(
        _refactor_file,
        fixers=fixers,
        print_function=args.print_function,
        apply=args.apply,
        diff_mode=args.diff_mode,
        max_diff_lines=args.max_diff_lines,
        ctx_chars=args.context_chars,
        backup=args.backup,
        show_errors=args.show_errors,
    )
    changed = 0
    failed = 0
    print(f"Processing {len(files)} file(s) with {MAX_WORKERS} workers.\n")
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for i, (path, was_changed, msg) in enumerate(pool.map(worker, files), 1):
            if msg:
                print(f"[{i}/{len(files)}] {msg}")
            if was_changed:
                changed += 1
            if msg.startswith("✗"):
                failed += 1
    print("=" * 40)
    if args.apply:
        print(f"Updated {changed} file(s); {failed} failure(s).")
    else:
        print(f"Would update {changed} file(s); {failed} failure(s).")
    return 0 if failed == 0 else 1


PRINT_BARE_RE = re.compile(r"^(\s*)print\s*$")
PRINT_REDIRECT_RE = re.compile(r"^(\s*)print\s+>>\s*(\w+)\s*,\s*(.+?)(\s*#.*)?$")
PRINT_PLAIN_RE = re.compile(r"^(\s*)print\s+(?!\()(.+?)(\s*#.*)?$")
EXCEPT_RE = re.compile(r"^(\s*)except\s+(\S+)\s*,\s*(\S+)\s*:")


def _convert_print_line(line: str) -> tuple[str, bool]:
    if PRINT_BARE_RE.match(line):
        indent = line[: len(line) - len(line.lstrip())]
        return (f"{indent}print()\n", True)
    m = PRINT_REDIRECT_RE.match(line)
    if m:
        indent, stream, expr, _ = m.groups()
        return (f"{indent}print({expr}, file={stream})\n", True)
    m = PRINT_PLAIN_RE.match(line)
    if m:
        indent, expr, _ = m.groups()
        return (f"{indent}print({expr})\n", True)
    return (line, False)


def _convert_legacy_line(line: str) -> tuple[str, bool]:
    original = line
    line = line.replace("xrange(", "range(")
    line = line.replace("raw_input(", "input(")
    m = EXCEPT_RE.match(line.strip())
    if m:
        indent = line[: len(line) - len(line.lstrip())]
        exc, name = m.group(2), m.group(3)
        line = f"{indent}except {exc} as {name}:\n"
    return (line, line != original)


def fixprint_text(text: str, do_all: bool) -> tuple[str, bool]:
    out_lines: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        new_line, c1 = _convert_print_line(line)
        if do_all:
            new_line, c2 = _convert_legacy_line(new_line)
            c1 = c1 or c2
        out_lines.append(new_line)
        changed = changed or c1
    return ("".join(out_lines), changed)


def _fixprint_file(
    path: Path,
    do_all: bool,
    apply: bool,
    backup: bool,
    with_ruff: bool,
) -> tuple[Path, bool, str]:
    try:
        original = read_text(path)
    except OSError as e:
        return (path, False, f"✗ {path.name}: {e}")
    new_text, changed = fixprint_text(original, do_all)
    if not changed:
        return (path, False, f"○ {path.name}: no changes needed")
    if not apply:
        return (path, True, f"📝 {path.name}: would rewrite print/legacy syntax")
    _write(path, new_text, backup=backup)
    extra = ""
    if with_ruff:
        extra = _run_ruff_up010(path)
    return (path, True, f"✓ {path.name}: rewritten{extra}")


def _run_ruff_up010(path: Path) -> str:
    if shutil.which("ruff") is None:
        return " (ruff not found; skipped)"
    try:
        proc = subprocess.run(
            ["ruff", "check", "--fix", "--select", "UP010", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        return f" (ruff failed: {e})"
    if proc.returncode == 0:
        return " (+ ruff applied fixes)"
    return " (ruff reported remaining issues)"


def cmd_fixprint(args: argparse.Namespace) -> int:
    files = iter_files(args.paths)
    if not files:
        print("No Python files found to process.")
        return 0
    if not args.apply:
        print("Mode: DRY RUN (pass --apply to write)\n")
    else:
        print("Mode: APPLY\n")
    worker = partial(
        _fixprint_file,
        do_all=args.all,
        apply=args.apply,
        backup=args.backup,
        with_ruff=args.with_ruff,
    )
    changed = 0
    print(f"Processing {len(files)} file(s) with {MAX_WORKERS} workers.\n")
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for i, (path, was_changed, msg) in enumerate(pool.map(worker, files), 1):
            if msg:
                print(f"[{i}/{len(files)}] {msg}")
            if was_changed:
                changed += 1
    print("=" * 40)
    print(f"{'Updated' if args.apply else 'Would update'} {changed} file(s).")
    return 0


def _run_2to3_cli(path: Path) -> tuple[Path, bool, str]:
    if not path.is_file():
        return (path, False, f"✗ {path.name}: file not found")
    if shutil.which("2to3") is None:
        return (path, False, "✗ 2to3 CLI not found on PATH")
    try:
        subprocess.run(
            ["2to3", "-w", "-n", "-f", "all", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return (path, False, f"✗ {path.name}: {e.stderr.strip() or e}")
    return (path, True, f"✓ {path.name}: 2to3 CLI ran")


def cmd_run2to3(args: argparse.Namespace) -> int:
    files = iter_files(args.paths)
    if not files:
        print("No Python files found.")
        return 0
    print(f"Running external 2to3 on {len(files)} file(s).\n")
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for path, _changed, msg in pool.map(_run_2to3_cli, files):
            if msg:
                print(msg)
    return 0


def _strip_tag_from_text(text: str, tag: str) -> str:
    out = "\n".join(l for l in text.splitlines() if not l.startswith(tag))
    if text.endswith("\n"):
        out += "\n"
    return out


def _process_zip(path: Path, targets: frozenset[str], tag: str) -> bool:
    _tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    tmp = Path(tmp_name)
    try:
        changed = False
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if Path(info.filename).name in targets:
                    try:
                        txt = data.decode("utf-8", errors="ignore")
                        new_txt = _strip_tag_from_text(txt, tag)
                        if new_txt != txt:
                            data = new_txt.encode("utf-8")
                            changed = True
                    except Exception:
                        pass
                zout.writestr(info, data)
        if changed:
            shutil.move(str(tmp), str(path))
        return changed
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _process_tar(path: Path, targets: frozenset[str], tag: str) -> bool:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_p = Path(tmpdir)
        with tarfile.open(path, "r:*") as tin:
            tin.extractall(tmpdir_p,filter='data')
        changed = False
        for root, _dirs, files in tmpdir_p.walk():
            for name in files:
                if name not in targets:
                    continue
                f = root / name
                try:
                    txt = f.read_text(encoding="utf-8", errors="ignore")
                    new_txt = _strip_tag_from_text(txt, tag)
                    if new_txt != txt:
                        f.write_text(new_txt, encoding="utf-8")
                        changed = True
                except OSError:
                    pass
        if not changed:
            return False
        tmp_archive = Path(tempfile.mktemp(suffix=path.suffix))
        mode = "w:gz" if path.name.lower().endswith((".tar.gz", ".tgz")) else "w"
        with tarfile.open(tmp_archive, mode) as tout:
            for entry in sorted(tmpdir_p.rglob("*")):
                arcname = entry.relative_to(tmpdir_p)
                tout.add(entry, arcname=str(arcname), recursive=False)
        shutil.move(str(tmp_archive), str(path))
        return True


def _strip_tag_file(path: Path, targets: frozenset[str], tag: str) -> tuple[Path, bool, str]:
    low = path.name.lower()
    try:
        if low.endswith((".zip", ".whl")) or zipfile.is_zipfile(path):
            changed = _process_zip(path, targets, tag)
            return (
                path,
                changed,
                f"📦 {path.name}: {'stripped' if changed else 'no change'}",
            )
        if low.endswith((".tar.gz", ".tgz", ".tar")) or tarfile.is_tarfile(path):
            changed = _process_tar(path, targets, tag)
            return (
                path,
                changed,
                f"📦 {path.name}: {'stripped' if changed else 'no change'}",
            )
        if path.name in targets:
            txt = read_text(path)
            new_txt = _strip_tag_from_text(txt, tag)
            if new_txt != txt:
                path.write_text(new_txt, encoding="utf-8")
                return (path, True, f"✓ {path.name}: stripped")
            return (path, False, f"○ {path.name}: no change")
        return (path, False, "")
    except Exception as e:
        return (path, False, f"✗ {path.name}: {e}")


def cmd_strip_tag(args: argparse.Namespace) -> int:
    targets = frozenset(args.target)
    paths: list[Path] = []
    for p in args.paths:
        p = Path(p)
        if p.is_dir():
            if args.recursive:
                paths.extend(f for f in p.rglob("*") if f.is_file())
            else:
                paths.extend(f for f in p.iterdir() if f.is_file())
        else:
            paths.append(p)
    if not paths:
        print("Nothing to scan.")
        return 0
    worker = partial(_strip_tag_file, targets=targets, tag=args.tag)
    total = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for path, changed, msg in pool.map(worker, paths):
            if msg:
                print(msg)
            if changed:
                total += 1
    print("=" * 40)
    print(f"Modified {total} file(s).")
    return 0


def _add_common_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path.cwd()],
        help="Files or directories to process (default: current directory).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py3migrate",
        description=(
            "Unified Python 2 → 3 migration toolbox: detect version, "
            "refactor with lib2to3, fix print statements, run external "
            "2to3, and strip py2 tags from wheels."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("detect", help="Detect Py2 vs Py3 in source files.")
    _add_common_opts(d)
    d.add_argument(
        "--ext",
        nargs="+",
        default=list(DEFAULT_EXT),
        help=f"File extensions to scan (default: {list(DEFAULT_EXT)}).",
    )
    r = sub.add_parser(
        "refactor",
        help="Rewrite Py2 → Py3 using lib2to3 fixers (dry-run by default).",
    )
    _add_common_opts(r)
    r.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Write changes in place (default: dry-run).",
    )
    r.add_argument(
        "--fixers",
        default="auto",
        help=("'auto' (discover from --fixer-module) or comma-separated list of fixer module names."),
    )
    r.add_argument(
        "--fixer-module",
        default=DEFAULT_FIXER_MODULE,
        help=f"Module to scan for fixers (default: {DEFAULT_FIXER_MODULE}).",
    )
    r.add_argument(
        "--print-function",
        action="store_true",
        help="Pass ``{'print_function': True}`` to RefactoringTool (my2to3.py).",
    )
    r.add_argument(
        "--diff-mode",
        choices=["compact", "full", "none"],
        default="compact",
        help="Diff style: 'compact' (f23.py), 'full' (2232.py), 'none'.",
    )
    r.add_argument(
        "--max-diff-lines",
        type=int,
        default=DEFAULT_MAX_DIFF_LINES,
        help=f"Max diff lines to print per file (default: {DEFAULT_MAX_DIFF_LINES}).",
    )
    r.add_argument(
        "--context-chars",
        type=int,
        default=DEFAULT_CONTEXT_CHARS,
        help=f"Chars of context per diff line (default: {DEFAULT_CONTEXT_CHARS}).",
    )
    r.add_argument(
        "--backup",
        dest="backup",
        action="store_true",
        default=False,
        help="Write a ``.bak`` next to each modified file.",
    )
    r.add_argument(
        "--no-backup",
        dest="backup",
        action="store_false",
        help="Do not write backups (default).",
    )
    r.add_argument(
        "--show-errors",
        action="store_true",
        help="Capture and print the RefactoringTool log (2232.py).",
    )
    f = sub.add_parser(
        "fixprint",
        help="Line-based fixes for `print x` and (with --all) `except X, e:` etc.",
    )
    _add_common_opts(f)
    f.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Write changes in place (default: dry-run).",
    )
    f.add_argument(
        "--all",
        action="store_true",
        help="Also rewrite `except X, e:`, `xrange(`, `raw_input(` (f23.py -a).",
    )
    f.add_argument(
        "--with-ruff",
        action="store_true",
        help="After writing, run `ruff check --fix --select UP010` (2to3ruff.py).",
    )
    f.add_argument(
        "--backup",
        dest="backup",
        action="store_true",
        default=True,
        help="Write a ``.bak`` next to each modified file (default).",
    )
    f.add_argument(
        "--no-backup",
        dest="backup",
        action="store_false",
        help="Do not write backups (f23.py -f).",
    )
    r2 = sub.add_parser(
        "run2to3",
        help="Shell out to the external `2to3 -w -n -f all` CLI (run223.py).",
    )
    _add_common_opts(r2)
    s = sub.add_parser(
        "strip-tag",
        help="Strip a `Tag:py2-none-any` line from WHEEL members in archives.",
    )
    _add_common_opts(s)
    s.add_argument(
        "--tag",
        default=DEFAULT_TAG,
        help=f"Line prefix to strip (default: {DEFAULT_TAG!r}).",
    )
    s.add_argument(
        "--target",
        action="append",
        default=list(DEFAULT_TARGETS),
        help=(f"Filename to look for inside archives (repeatable, default: {list(DEFAULT_TARGETS)})."),
    )
    s.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        default=True,
        help="Only scan top level of any directory argument.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "detect": cmd_detect,
        "refactor": cmd_refactor,
        "fixprint": cmd_fixprint,
        "run2to3": cmd_run2to3,
        "strip-tag": cmd_strip_tag,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
