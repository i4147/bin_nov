#!/data/data/com.termux/files/usr/bin/env python
"""Detect and remove unused imports with the built-in analyzer or autoflake.
Similar to :doc:`afkco`, plus ``--autoflake`` to delegate the rewrite to the external autoflake tool with a ``--diff`` preview; archives (wheel, tar.zst) are analysed without being unpacked.
Usage: afkx [-a|--autoflake] [--dry-run] [paths ...] Positional arguments: paths ...
File or directory paths to analyze (default: current directory) Options: -a, --autofix Remove unused imports in-place --dry-run Preview changes without writing (implies --autofix) -v, --verbose Show files with zero issues --autoflake Use external autoflake instead of built-in AST analyzer -d, --diff With --autoflake, show unified diff --exclude Regex pattern to exclude files (repeatable) --no-color Disable ANSI colors Examples: $ afkx --autoflake --diff src/ Command-line script: run ``python afkx.py`` or the ``afkx`` command.
The module is importable as well."""

import argparse
import ast
import concurrent.futures
from dataclasses import dataclass, field
import difflib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Any, Callable, Iterable, Optional
import zipfile

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
    statement: str
    unused_names: list[str] = field(default_factory=list)
    path: str = ""


@dataclass(slots=True)
class FileResult:
    path: str
    unused_imports: list[UnusedImport] = field(default_factory=list)
    error: Optional[str] = None
    size: int = 0
    diff: str = ""
    fixed_source: str = ""

    @property
    def has_unused(self) -> bool:
        return bool(self.unused_imports) or bool(self.diff)


class Colors:
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    RESET = "\033[0m"

    @classmethod
    def disable(cls) -> None:
        for name in dir(cls):
            if not name.startswith("_") and name != "disable":
                setattr(cls, name, "")


class ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: dict[str, tuple[int, int, str]] = {}
        self.type_checking_imports: set[str] = set()
        self.future_imports: set[str] = set()
        self.all_exports: set[str] = set()
        self.star_imports: set[str] = set()
        self._in_type_checking = False

    def visit_If(self, node: ast.If) -> None:
        is_type_checking = (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
            isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
        )
        if is_type_checking:
            old = self._in_type_checking
            self._in_type_checking = True
            for stmt in node.body:
                self.visit(stmt)
            self._in_type_checking = old
            for stmt in node.orelse:
                self.visit(stmt)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if node.names and node.names[0].name == "*":
            self.star_imports.add("")
            self.generic_visit(node)
            return
        stmt = self._unparse(node)
        for alias in node.names:
            name = alias.asname or alias.name
            if self._in_type_checking:
                self.type_checking_imports.add(name)
            else:
                self.imports[name] = (node.lineno, node.col_offset, stmt)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "__future__":
            for alias in node.names:
                self.future_imports.add(alias.asname or alias.name)
            self.generic_visit(node)
            return
        if node.names and node.names[0].name == "*":
            self.star_imports.add(node.module or "")
            self.generic_visit(node)
            return
        stmt = self._unparse(node)
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            if self._in_type_checking:
                self.type_checking_imports.add(name)
            else:
                self.imports[name] = (node.lineno, node.col_offset, stmt)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id == "__all__"
                and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
            ):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        self.all_exports.add(elt.value)
        self.generic_visit(node)

    @staticmethod
    def _unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return "<import statement>"


class NameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.used_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            self.used_names.add(node.value.id)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            for match in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", node.value):
                self.used_names.add(match)
        self.generic_visit(node)


def analyze_source(source: str, path: str = "") -> tuple[list[UnusedImport], Optional[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], f"Syntax error: {exc}"
    except Exception as exc:
        return [], f"Parse error: {exc}"
    imports = ImportCollector()
    imports.visit(tree)
    names = NameCollector()
    names.visit(tree)
    used = names.used_names
    results: list[UnusedImport] = []
    seen_lines: set[int] = set()
    for name, (lineno, col, stmt) in imports.imports.items():
        if (
            name in imports.future_imports
            or name in imports.type_checking_imports
            or name in imports.all_exports
            or name in imports.star_imports
            or name in used
        ):
            continue
        if lineno not in seen_lines:
            results.append(
                UnusedImport(
                    lineno=lineno,
                    col_offset=col,
                    statement=stmt,
                    unused_names=[name],
                    path=path,
                )
            )
            seen_lines.add(lineno)
        else:
            for item in results:
                if item.lineno == lineno:
                    item.unused_names.append(name)
                    break
    return results, None


def read_py_file(path: str) -> FileResult:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        size = len(text.encode("utf-8"))
    except PermissionError:
        return FileResult(path=str(p), error="Permission denied")
    except Exception as exc:
        return FileResult(path=str(p), error=f"Read error: {exc}")
    unused, err = analyze_source(text, str(p))
    return FileResult(path=str(p), unused_imports=unused, error=err, size=size)


def extract_from_wheel(wheel_path: str) -> dict[str, str]:
    result = {}
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".py"):
                    try:
                        content = zf.read(name).decode("utf-8", errors="replace")
                        result[f"{Path(wheel_path).name}::{name}"] = content
                    except Exception:
                        pass
    except Exception:
        pass
    return result


def extract_from_tar_zst(archive_path: str) -> dict[str, str]:
    result = {}
    try:
        if HAS_ZSTD:
            with open(archive_path, "rb") as fh:
                dctx = zstd.ZstdDecompressor()
                with dctx.stream_reader(fh) as reader, tarfile.open(fileobj=reader, mode="r|") as tar:
                    _extract_tar_members(tar, Path(archive_path).name, result)
        else:
            with tarfile.open(archive_path, "r:*") as tar:
                _extract_tar_members(tar, Path(archive_path).name, result)
    except Exception:
        pass
    return result


def _extract_tar_members(tar: tarfile.TarFile, archive_name: str, out: dict) -> None:
    for member in tar:
        if member.isfile() and member.name.endswith(".py"):
            try:
                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read().decode("utf-8", errors="replace")
                out[f"{archive_name}::{member.name}"] = content
            except Exception:
                pass


def analyze_archive_member(item: tuple[str, str]) -> FileResult:
    path, source = item
    unused, err = analyze_source(source, path)
    return FileResult(
        path=path,
        unused_imports=unused,
        error=err,
        size=len(source.encode("utf-8")),
    )


def rewrite_imports(source: str, unused: list[UnusedImport]) -> tuple[str, bool]:
    lines = source.splitlines(keepends=True)
    by_line: dict[int, set[str]] = {}
    for item in unused:
        by_line.setdefault(item.lineno, set()).update(item.unused_names)
    for lineno in sorted(by_line.keys(), reverse=True):
        idx = lineno - 1
        if idx < 0 or idx >= len(lines):
            continue
        line = lines[idx]
        names_to_remove = by_line[lineno]
        try:
            tree = ast.parse(line)
            node = tree.body[0] if tree.body else None
        except Exception:
            continue
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        new_line = _rebuild_import_line(node, names_to_remove, line)
        if new_line is None:
            del lines[idx]
        else:
            lines[idx] = new_line if new_line.endswith("\n") else new_line + "\n"
    new_source = "".join(lines)
    try:
        ast.parse(new_source)
        return new_source, True
    except SyntaxError:
        return source, False


def _rebuild_import_line(node: ast.AST, remove: set[str], original: str) -> Optional[str]:
    indent = original[: len(original) - len(original.lstrip())]
    if isinstance(node, ast.Import):
        kept = [alias for alias in node.names if (alias.asname or alias.name.split(".")[0]) not in remove]
        if not kept:
            return None
        parts = [f"{a.name} as {a.asname}" if a.asname else a.name for a in kept]
        return f"{indent}import {', '.join(parts)}"
    if isinstance(node, ast.ImportFrom):
        kept = [alias for alias in node.names if (alias.asname or alias.name) not in remove]
        if not kept:
            return None
        module = node.module or ""
        dots = "." * node.level if node.level else ""
        parts = [f"{a.name} as {a.asname}" if a.asname else a.name for a in kept]
        return f"{indent}from {dots}{module} import {', '.join(parts)}"
    return original


def autofix_file(path: str, unused: list[UnusedImport], dry_run: bool = False) -> tuple[bool, Optional[str]]:
    if "::" in path:
        return False, "Cannot autofix inside packed archives"
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return False, f"Read error: {exc}"
    new_source, ok = rewrite_imports(source, unused)
    if not ok:
        return False, "Result failed to parse"
    if dry_run:
        return True, None
    try:
        Path(path).write_text(new_source, encoding="utf-8")
        return True, None
    except Exception as exc:
        return False, f"Write error: {exc}"


AUTOFLAKE_CMD: list[str] = ["autoflake", "--remove-all-unused-imports", "--ignore-init-module-imports"]


def run_autoflake_on_file(path: str) -> FileResult:
    try:
        original = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return FileResult(path=path, error=f"Read error: {exc}")
    try:
        proc = subprocess.run(
            [*AUTOFLAKE_CMD, path],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return FileResult(path=path, error="autoflake not installed")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        return FileResult(path=path, error=msg or f"autoflake exited {proc.returncode}")
    fixed = proc.stdout
    if fixed == original:
        return FileResult(path=path)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return FileResult(path=path, diff=diff, fixed_source=fixed)


def run_autoflake_on_source(item: tuple[str, str]) -> FileResult:
    path, source = item
    try:
        proc = subprocess.run(
            [*AUTOFLAKE_CMD, "-"],
            input=source,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return FileResult(path=path, error="autoflake not installed")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        return FileResult(path=path, error=msg or f"autoflake exited {proc.returncode}")
    fixed = proc.stdout
    if fixed == source:
        return FileResult(path=path)
    diff = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return FileResult(path=path, diff=diff, fixed_source=fixed)


def collect_targets(paths: list[str], exclude_patterns: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    py_files: list[str] = []
    archive_members: list[tuple[str, str]] = []
    exclude_re = [re.compile(p) for p in exclude_patterns]

    def excluded(p: str) -> bool:
        return any(rx.search(p) for rx in exclude_re)

    for raw in paths:
        p = Path(raw)
        if not p.exists():
            print(f"⚠ Path not found: {raw}", file=sys.stderr)
            continue
        if p.is_file():
            _handle_single_file(p, excluded, py_files, archive_members)
        elif p.is_dir():
            for py in p.rglob("*.py"):
                if not excluded(str(py)):
                    py_files.append(str(py))
            for whl in p.rglob("*.whl"):
                for member, content in extract_from_wheel(str(whl)).items():
                    if not excluded(member):
                        archive_members.append((member, content))
            for zst in p.rglob("*.tar.zst"):
                for member, content in extract_from_tar_zst(str(zst)).items():
                    if not excluded(member):
                        archive_members.append((member, content))
    return py_files, archive_members


def _handle_single_file(
    p: Path,
    excluded: Callable[..., Any],
    py_files: list[str],
    archive_members: list[tuple[str, str]],
) -> None:
    if p.suffix == ".py":
        if not excluded(str(p)):
            py_files.append(str(p))
    elif p.suffix == ".whl":
        for member, content in extract_from_wheel(str(p)).items():
            if not excluded(member):
                archive_members.append((member, content))
    elif p.suffix == ".zst" or p.name.endswith(".tar.zst"):
        for member, content in extract_from_tar_zst(str(p)).items():
            if not excluded(member):
                archive_members.append((member, content))


def report_builtin(
    results: list[FileResult],
    *,
    color: bool,
    verbose: bool,
    dry_run: bool,
    autofix: bool,
) -> None:
    if not color:
        Colors.disable()
    total_unused = 0
    files_with_issues = 0
    fixed = 0
    skipped = 0
    for res in results:
        if res.error:
            print(f"{Colors.RED}✗ {res.path} — {res.error}{Colors.RESET}")
            continue
        if not res.unused_imports:
            if verbose:
                print(f"{Colors.GREEN}✓{Colors.RESET} {res.path}")
            continue
        files_with_issues += 1
        print(f"\n{Colors.BOLD}{res.path}{Colors.RESET}")
        for item in res.unused_imports:
            total_unused += 1
            print(f"  line {Colors.CYAN}{item.lineno:>5}{Colors.RESET}  {Colors.YELLOW}{item.statement}{Colors.RESET}")
            print(f"{'':20}[unused: {', '.join(item.unused_names)}]")
        if autofix:
            if dry_run:
                print(f"  [dry-run] would fix {res.path}")
            else:
                ok, err = autofix_file(res.path, res.unused_imports)
                if ok:
                    print(f"  {Colors.GREEN}fixed{Colors.RESET} {res.path}")
                    fixed += 1
                else:
                    print(f"  {Colors.RED}SKIP{Colors.RESET} autofix on {res.path} — {err}")
                    skipped += 1
    print()
    print(f"Found {total_unused} unused import(s) across {files_with_issues} file(s).")
    if autofix:
        print(f"Fixed {fixed} file(s).")
        if skipped:
            print(f"Skipped {skipped} file(s).")


def report_autoflake(
    results: list[FileResult],
    *,
    color: bool,
    verbose: bool,
    dry_run: bool,
    autofix: bool,
    show_diff: bool,
) -> None:
    if not color:
        Colors.disable()
    files_with_issues = 0
    fixed = 0
    skipped = 0
    for res in results:
        if res.error:
            print(f"{Colors.RED}✗ {res.path} — {res.error}{Colors.RESET}")
            continue
        if not res.has_unused:
            if verbose:
                print(f"{Colors.GREEN}✓{Colors.RESET} {res.path}")
            continue
        files_with_issues += 1
        print(f"\n{Colors.BOLD}{res.path}{Colors.RESET}")
        if show_diff and res.diff:
            print(res.diff, end="" if res.diff.endswith("\n") else "\n")
        else:
            print(f"  {Colors.YELLOW}unused import(s) detected{Colors.RESET}")
        if autofix:
            if "::" in res.path:
                print(f"  {Colors.RED}SKIP{Colors.RESET} cannot autofix archive member")
                skipped += 1
                continue
            if dry_run:
                print(f"  [dry-run] would fix {res.path}")
                continue
            try:
                Path(res.path).write_text(res.fixed_source, encoding="utf-8")
                print(f"  {Colors.GREEN}fixed{Colors.RESET} {res.path}")
                fixed += 1
            except Exception as exc:
                print(f"  {Colors.RED}SKIP{Colors.RESET} write error: {exc}")
                skipped += 1
    print()
    print(f"Found {files_with_issues} file(s) with unused import(s).")
    if autofix:
        print(f"Fixed {fixed} file(s).")
        if skipped:
            print(f"Skipped {skipped} file(s).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Detect and optionally remove unused imports from Python files and archives.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python unused_imports.py
  python unused_imports.py src/
  python unused_imports.py src/ --autofix
  python unused_imports.py src/ --dry-run
  python unused_imports.py src/ --exclude "test_.*"
  python unused_imports.py src/ --autoflake --diff
  python unused_imports.py src/ --autoflake --autofix
""",
    )
    p.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="File or directory paths to analyze (default: current directory)",
    )
    p.add_argument("-a", "--autofix", action="store_true", help="Remove unused imports in-place")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing (implies --autofix)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Show files with zero issues")
    p.add_argument(
        "--autoflake",
        action="store_true",
        help="Use external autoflake instead of built-in AST analyzer",
    )
    p.add_argument("-d", "--diff", action="store_true", help="With --autoflake, show unified diff")
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Regex pattern to exclude files (repeatable)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.dry_run:
        args.autofix = True
    py_files, archive_members = collect_targets(args.paths, args.exclude)
    if not py_files and not archive_members:
        print("No Python files found to analyze.", file=sys.stderr)
        return 1
    if args.verbose:
        mode = "autoflake" if args.autoflake else "built-in AST analyzer"
        print(f"Scanning {len(args.paths)} path(s) with {MAX_WORKERS} worker(s) using {mode} …")
        print(f"  {len(py_files)} .py file(s), {len(archive_members)} archive member(s) queued.\n")
    results: list[FileResult] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        if args.autoflake:
            if shutil.which("autoflake") is None:
                print(
                    "Error: `autoflake` is not installed. Run `pip install autoflake`.",
                    file=sys.stderr,
                )
                return 1
            if py_files:
                results.extend(pool.map(run_autoflake_on_file, py_files))
            if archive_members:
                results.extend(pool.map(run_autoflake_on_source, archive_members))
            report_autoflake(
                results,
                color=not args.no_color,
                verbose=args.verbose,
                dry_run=args.dry_run,
                autofix=args.autofix,
                show_diff=args.diff,
            )
        else:
            if py_files:
                results.extend(pool.map(read_py_file, py_files))
            if archive_members:
                results.extend(pool.map(analyze_archive_member, archive_members))
            report_builtin(
                results,
                color=not args.no_color,
                verbose=args.verbose,
                dry_run=args.dry_run,
                autofix=args.autofix,
            )
    has_issues = any(r.has_unused for r in results)
    return 1 if has_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
