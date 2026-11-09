#!/data/data/com.termux/files/usr/bin/env python
"""Detect and optionally remove the unused imports of Python files.
Collects the imported names with ast, compares them with the names actually used, also reads the Python members of wheels and ``.tar.zst`` archives, and can rewrite the files in place with ``--autofix`` (``--dry-run`` previews, ``--exclude`` filters, ``--verbose`` adds the per-name detail).
Usage: afkco [-a] [--dry-run] [paths ...] Positional arguments: paths ...
File or directory paths to analyze (default: current directory) Options: -a, --autofix Remove unused imports in-place --dry-run Preview changes without writing (enables --autofix) -v, --verbose Detailed output with per-name breakdown --exclude Regex pattern to exclude files (repeatable) --no-color Disable ANSI color codes Examples: $ afkco -a src/ Command-line script: run ``python afkco.py`` or the ``afkco`` command.
The module is importable as well."""

from argparse import ArgumentParser, RawDescriptionHelpFormatter
import ast
from dataclasses import dataclass, field
from multiprocessing import Pool
from pathlib import Path
import re
import sys
import tarfile
import zipfile

from dh import MAX_WORKERS


try:
    import zstandard as zstd

    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False


@dataclass
class UnusedImport:
    lineno: int
    col_offset: int
    statement: str
    unused_names: list[str]
    module_path: str = ""


@dataclass
class FileReport:
    path: str
    unused_imports: list[UnusedImport] = field(default_factory=list)
    error: str | None = None
    file_size: int = 0


class Colors:
    BOLD = "\x1b[1m"
    CYAN = "\x1b[36m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    RESET = "\x1b[0m"

    @classmethod
    def disable(cls):
        for attr in dir(cls):
            if not attr.startswith("_") and attr != "disable":
                setattr(cls, attr, "")


class ImportVisitor(ast.NodeVisitor):
    def __init__(self):
        self.imports = {}
        self.type_checking_imports = set()
        self.future_imports = set()
        self.all_export = set()
        self.star_imports = set()
        self._type_checking_depth = 0

    def visit_If(self, node):
        type_checking = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        type_checking = type_checking or (
            isinstance(node.test, ast.Attribute)
            and isinstance(node.test.value, ast.Name)
            and node.test.value.id == "typing"
            and node.test.attr == "TYPE_CHECKING"
        )
        if not type_checking:
            self.generic_visit(node)
            return
        self.visit(node.test)
        self._type_checking_depth += 1
        try:
            for child in node.body:
                self.visit(child)
        finally:
            self._type_checking_depth -= 1
        for child in node.orelse:
            self.visit(child)

    def visit_ImportFrom(self, node):
        if node.module == "__future__":
            for alias in node.names:
                self.future_imports.add(alias.asname or alias.name)
            self.generic_visit(node)
            return
        if node.names[0].name == "*":
            module_name = node.module or ""
            self.star_imports.add(module_name)
            self.generic_visit(node)
            return
        statement = self._build_import_statement(node)
        for alias in node.names:
            name = alias.asname or alias.name
            if self._type_checking_depth:
                self.type_checking_imports.add(name)
            else:
                self.imports[name] = (node.lineno, node.col_offset, statement)
        self.generic_visit(node)

    def visit_Import(self, node):
        statement = self._build_import_statement(node)
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            if self._type_checking_depth:
                self.type_checking_imports.add(name)
            else:
                self.imports[name] = (node.lineno, node.col_offset, statement)
        self.generic_visit(node)

    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__" and isinstance(node.value, ast.List):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        self.all_export.add(elt.value)
        self.generic_visit(node)

    @staticmethod
    def _build_import_statement(node):
        try:
            return ast.unparse(node)
        except Exception:
            return "<import statement>"


class NameVisitor(ast.NodeVisitor):
    def __init__(self):
        self.used_names = set()
        self.in_annotation = False

    def visit_Name(self, node):
        self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name):
            self.used_names.add(node.value.id)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            identifiers = re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", node.value)
            self.used_names.update(identifiers)
        self.generic_visit(node)


def analyze_imports(source, path=""):
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ([], f"Syntax error: {e}")
    except Exception as e:
        return ([], f"Parse error: {e}")
    import_visitor = ImportVisitor()
    import_visitor.visit(tree)
    name_visitor = NameVisitor()
    name_visitor.visit(tree)
    used_names = name_visitor.used_names
    unused = []
    seen_lines = set()
    for imported_name, (
        lineno,
        col_offset,
        statement,
    ) in import_visitor.imports.items():
        if imported_name in import_visitor.future_imports:
            continue
        if imported_name in import_visitor.all_export:
            continue
        if imported_name in import_visitor.star_imports:
            continue
        if imported_name not in used_names:
            if lineno not in seen_lines:
                unused.append(
                    UnusedImport(
                        lineno=lineno,
                        col_offset=col_offset,
                        statement=statement,
                        unused_names=[imported_name],
                        module_path=path,
                    )
                )
                seen_lines.add(lineno)
            else:
                for u in unused:
                    if u.lineno == lineno:
                        u.unused_names.append(imported_name)
                        break
    return (unused, None)


def process_py_file(path):
    path_obj = Path(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        file_size = len(source.encode("utf-8"))
    except PermissionError:
        return FileReport(str(path_obj), error="Permission denied")
    except Exception as e:
        return FileReport(str(path_obj), error=f"Read error: {e}")
    unused, error = analyze_imports(source, str(path_obj))
    return FileReport(path=str(path_obj), unused_imports=unused, error=error, file_size=file_size)


def extract_py_files_from_wheel(wheel_path):
    result = {}
    try:
        with zipfile.ZipFile(wheel_path, "r") as whl:
            for member in whl.namelist():
                if member.endswith(".py"):
                    try:
                        content = whl.read(member).decode("utf-8", errors="replace")
                        virtual_path = f"{Path(wheel_path).name}::{member}"
                        result[virtual_path] = content
                    except Exception:
                        pass
    except Exception:
        pass
    return result


def extract_py_files_from_tar_zst(archive_path):
    result = {}
    try:
        if HAS_ZSTD:
            with open(archive_path, "rb") as f:
                dctx = zstd.ZstdDecompressor()
                with (
                    dctx.stream_reader(f) as reader,
                    tarfile.open(fileobj=reader, mode="r|") as tar,
                ):
                    for member in tar:
                        if member.isfile() and member.name.endswith(".py"):
                            try:
                                f_obj = tar.extractfile(member)
                                if f_obj:
                                    content = f_obj.read().decode("utf-8", errors="replace")
                                    virtual_path = f"{Path(archive_path).name}::{member.name}"
                                    result[virtual_path] = content
                            except Exception:
                                pass
        else:
            try:
                with tarfile.open(archive_path, "r:*") as tar:
                    for member in tar:
                        if member.isfile() and member.name.endswith(".py"):
                            try:
                                f_obj = tar.extractfile(member)
                                if f_obj:
                                    content = f_obj.read().decode("utf-8", errors="replace")
                                    virtual_path = f"{Path(archive_path).name}::{member.name}"
                                    result[virtual_path] = content
                            except Exception:
                                pass
            except Exception:
                pass
    except Exception:
        pass
    return result


def process_archive_member(virtual_path, source):
    unused, error = analyze_imports(source, virtual_path)
    return FileReport(
        path=virtual_path,
        unused_imports=unused,
        error=error,
        file_size=len(source.encode("utf-8")),
    )


def _process_py_file_worker(path):
    return process_py_file(path)


def _process_archive_worker(args):
    virtual_path, source = args
    return process_archive_member(virtual_path, source)


def discover_files(paths, exclude_patterns):
    py_files = []
    archive_members = []
    exclude_regexes = [re.compile(p) for p in exclude_patterns]

    def should_exclude(path):
        return any(regex.search(path) for regex in exclude_regexes)

    for path_str in paths:
        path = Path(path_str)
        if not path.exists():
            print(f"⚠ Path not found: {path_str}", file=sys.stderr)
            continue
        if path.is_file():
            if path.suffix == ".py":
                if not should_exclude(str(path)):
                    py_files.append(str(path))
            elif path.suffix == ".whl":
                members = extract_py_files_from_wheel(str(path))
                for vpath, source in members.items():
                    if not should_exclude(vpath):
                        archive_members.append((vpath, source))
            elif path.suffix == ".zst" or path.name.endswith(".tar.zst"):
                members = extract_py_files_from_tar_zst(str(path))
                for vpath, source in members.items():
                    if not should_exclude(vpath):
                        archive_members.append((vpath, source))
        elif path.is_dir():
            for py_file in path.rglob("*.py"):
                if not should_exclude(str(py_file)):
                    py_files.append(str(py_file))
            for whl_file in path.rglob("*.whl"):
                members = extract_py_files_from_wheel(str(whl_file))
                for vpath, source in members.items():
                    if not should_exclude(vpath):
                        archive_members.append((vpath, source))
            for tar_file in path.rglob("*.tar.zst"):
                members = extract_py_files_from_tar_zst(str(tar_file))
                for vpath, source in members.items():
                    if not should_exclude(vpath):
                        archive_members.append((vpath, source))
    return (py_files, archive_members)


def remove_unused_imports(source, unused):
    lines = source.split("\n")
    unused_by_line = {}
    for u in unused:
        if u.lineno not in unused_by_line:
            unused_by_line[u.lineno] = set()
        unused_by_line[u.lineno].update(u.unused_names)
    for lineno in sorted(unused_by_line.keys(), reverse=True):
        idx = lineno - 1
        if idx < 0 or idx >= len(lines):
            return (source, False)
        line = lines[idx]
        unused_names = unused_by_line[lineno]
        stripped = line.lstrip()
        indentation = line[: len(line) - len(stripped)]
        if "#" in line:
            return (source, False)
        try:
            tree = ast.parse(stripped)
        except SyntaxError:
            return (source, False)
        if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.Import, ast.ImportFrom)):
            return (source, False)
        new_line = _reconstruct_import_line(tree.body[0], unused_names, line)
        if new_line is None:
            del lines[idx]
        else:
            lines[idx] = indentation + new_line
    result = "\n".join(lines)
    try:
        ast.parse(result)
    except SyntaxError:
        return (source, False)
    return (result, True)


def _reconstruct_import_line(node, unused_names, original_line):
    if isinstance(node, ast.Import):
        names_to_keep = []
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            if name not in unused_names:
                names_to_keep.append(alias)
        if not names_to_keep:
            return None
        parts = []
        for alias in names_to_keep:
            if alias.asname:
                parts.append(f"{alias.name} as {alias.asname}")
            else:
                parts.append(alias.name)
        return f"import {', '.join(parts)}"
    elif isinstance(node, ast.ImportFrom):
        names_to_keep = []
        for alias in node.names:
            name = alias.asname or alias.name
            if name not in unused_names:
                names_to_keep.append(alias)
        if not names_to_keep:
            return None
        module = node.module or ""
        level = "." * node.level if node.level else ""
        parts = []
        for alias in names_to_keep:
            if alias.asname:
                parts.append(f"{alias.name} as {alias.asname}")
            else:
                parts.append(alias.name)
        return f"from {level}{module} import {', '.join(parts)}"
    return original_line


def autofix_file(path, unused, dry_run=False):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception as e:
        return (False, f"Read error: {e}")
    modified, success = remove_unused_imports(source, unused)
    if not success:
        return (False, "Result failed to parse")
    if dry_run:
        return (True, None)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(modified)
    except Exception as e:
        return (False, f"Write error: {e}")
    return (True, None)


def print_report(
    reports,
    use_color=True,
    verbose=False,
    dry_run=False,
    autofix=False,
):
    if not use_color:
        Colors.disable()
    total_unused = 0
    files_with_issues = 0
    fixed_count = 0
    skipped_count = 0
    for report in reports:
        if report.error:
            print(f"{Colors.RED}✗ {report.path} — {report.error}{Colors.RESET}")
            continue
        if not report.unused_imports:
            print(f"{Colors.GREEN}✓{Colors.RESET} {report.path}")
            continue
        files_with_issues += 1
        for unused in report.unused_imports:
            total_unused += 1
            print(
                f"{Colors.BOLD}{report.path}{Colors.RESET}  -->  line {Colors.CYAN}{unused.lineno:>5}{Colors.RESET}  "
                f"{Colors.YELLOW}{unused.statement}{Colors.RESET}"
            )
            if verbose and len(unused.unused_names) > 1:
                print(f"{'':30}[unused: {', '.join(unused.unused_names)}]")
            else:
                print(f"{'':30}[unused: {unused.unused_names[0]}]")
            if autofix and (not dry_run):
                success, error = autofix_file(report.path, [unused])
                if success:
                    print(f"  {Colors.GREEN}fixed{Colors.RESET} {report.path}")
                    fixed_count += 1
                else:
                    print(f"  {Colors.RED}SKIP{Colors.RESET} autofix on {report.path} — {error}")
                    skipped_count += 1
            elif autofix and dry_run:
                print(f"  [dry-run] would fix {report.path}")
    print()
    print(f"Found {total_unused} unused import(s) across {files_with_issues} file(s).")
    if autofix:
        print(f"Fixed {fixed_count} file(s).")
        if skipped_count:
            print(f"Skipped {skipped_count} file(s).")


def main():
    parser = ArgumentParser(
        description="Detect and optionally remove unused imports from Python files.",
        formatter_class=RawDescriptionHelpFormatter,
        epilog='\nExamples:\n\n  python unused_imports.py\n\n\n  python unused_imports.py src/main.py\n\n\n  python unused_imports.py src/ --autofix\n\n\n  python unused_imports.py src/ --dry-run\n\n\n  python unused_imports.py src/ --exclude "test_.*" --exclude ".*_test\\.py"\n        ',
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="File or directory paths to analyze (default: current directory)",
    )
    parser.add_argument("-a", "--autofix", action="store_true", help="Remove unused imports in-place")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing (enables --autofix)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Detailed output with per-name breakdown",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Regex pattern to exclude files (repeatable)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color codes")
    args = parser.parse_args()
    if args.dry_run:
        args.autofix = True
    py_files, archive_members = discover_files(args.paths, args.exclude)
    if not py_files and (not archive_members):
        print("No Python files found to analyze.", file=sys.stderr)
        return 1
    if args.verbose:
        print(f"Scanning {len(args.paths)} path(s) with {MAX_WORKERS} worker(s) …\n")
        print(f"  {len(py_files)} .py file(s), {len(archive_members)} archive member(s) queued.\n")
    reports = []
    with Pool(processes=MAX_WORKERS) as pool:
        if py_files:
            reports.extend(pool.map(_process_py_file_worker, py_files))
        if archive_members:
            reports.extend(pool.map(_process_archive_worker, archive_members))
    print_report(
        reports,
        use_color=not args.no_color,
        verbose=args.verbose,
        dry_run=args.dry_run,
        autofix=args.autofix,
    )
    has_unused = any(r.unused_imports for r in reports)
    return 1 if has_unused else 0


if __name__ == "__main__":
    raise SystemExit(main())
