#!/data/data/com.termux/files/usr/bin/env python
"""Python source cleaner: docstrings and comments.
Subcommands: ``strip`` removes the docstrings and/or the comments with the ast, LibCST or text engine (``--engine``), ``check`` reports what a file still contains and ``clean-module-doc`` drops the generated ``Module for X.`` docstring; ``--tidy`` also collapses the blank lines.
Usage: pyclean {strip,check,clean-module-doc} [options] [paths ...] Positional arguments: paths ...
Files/directories (default: cwd) directory Subcommands: strip Remove docstrings and/or comments.
check Find comments and docstrings (check_rmc.py).
clean-module-doc Remove the auto-generated 'Module for X.' docstring (rm_moduledoc.py).
Options: --engine Ast-rewrite = AST span splice, preserves comments/format (brmc.py) ast-unparse = AST unparse, loses comments/format (brmc2.py) text-fallback = line-based delete + regex fallback (remc.py) libcst = libcst-based, also...
--preserve-module-docstring Keep the module-level docstring (default: True) --remove-comments Also remove comments (libcst engine only) --preserve-shebang Keep '#!/usr/bin/env ...' shebang (libcst engine, default: True) --preserve-fmt-type Keep '#fmt:' / '#type:' comments (libcst engine, default: True) --tidy Collapse blank-line runs and trim trailing whitespace (text-fallback engine) -a, --auto-remove Automatically remove the found lines --top --pattern Override the regex; must match the entire line after stripping (default: '^\\s*\"\"\"\\s*Module for <name>...') Examples: $ pyclean check src/ $ pyclean strip --remove-comments src/ Command-line script: run ``python pyclean.py`` or the ``pyclean`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
import io
from pathlib import Path
import re
import sys
from typing import Any, Optional, Sequence

from dh import MAX_WORKERS
from loguru import logger


try:
    import libcst as cst

    HAS_LIBCST = True
except ImportError:
    cst = None
    HAS_LIBCST = False
LOG = logger
DEFAULT_TOP_LINES: int = 5
GREEN: str = "\x1b[92m"
WHITE: str = "\x1b[97m"
YELLOW: str = "\x1b[93m"
RESET: str = "\x1b[0m"


def _is_docstring_node(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _is_valid_python(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except (SyntaxError, ValueError):
        return False


def _read_source(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except Exception:
            return None
    except Exception:
        return None


def _collect_py_files(paths: Sequence[Path], recursive: bool = True) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        try:
            p = p.resolve()
        except OSError:
            continue
        if p.is_file() and p.suffix == ".py":
            if p not in seen:
                seen.add(p)
                out.append(p)
        elif p.is_dir():
            it = p.rglob("*.py") if recursive else p.glob("*.py")
            for f in it:
                if f.is_file() and f not in seen:
                    seen.add(f)
                    out.append(f)
    return sorted(out)


def _default_paths(raw: Sequence[str]) -> list[Path]:
    if not raw:
        return [Path.cwd()]
    return [Path(r).expanduser() for r in raw]


def strip_ast_rewrite(source: str, *, preserve_module_docstring: bool = True) -> tuple[str, int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0
    preserve_first = preserve_module_docstring and bool(tree.body) and _is_docstring_node(tree.body[0])
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        v = getattr(node, "value", None)
        if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
            continue
        if preserve_first and tree.body and node is tree.body[0]:
            continue
        if (
            hasattr(node, "lineno")
            and hasattr(node, "end_lineno")
            and hasattr(node, "col_offset")
            and hasattr(node, "end_col_offset")
        ):
            spans.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
    if not spans:
        return source, 0
    lines = source.splitlines(keepends=True)
    spans.sort(key=lambda r: (r[0], r[1]), reverse=True)
    for start_line, start_col, end_line, end_col in spans:
        si, ei = start_line - 1, end_line - 1
        if si < 0 or ei >= len(lines):
            continue
        if si == ei:
            ln = lines[si]
            lines[si] = ln[:start_col] + ln[end_col:]
        else:
            lines[si] = lines[si][:start_col]
            lines[ei] = lines[ei][end_col:]
            for k in range(si + 1, ei):
                lines[k] = ""
    return "".join(lines), len(spans)


class _DocstringStripper(ast.NodeTransformer):
    def __init__(self, preserve_module_docstring: bool = True) -> None:
        super().__init__()
        self.preserve_module_docstring = preserve_module_docstring

    def _strip_body(self, body: list[ast.stmt]) -> list[ast.stmt]:
        if body and _is_docstring_node(body[0]):
            body = body[1:]
        new = [self.visit(n) for n in body]
        if not new:
            new = [ast.Pass()]
        return new

    def visit_Module(self, node: ast.Module) -> ast.Module:
        if self.preserve_module_docstring and node.body and _is_docstring_node(node.body[0]):
            head = node.body[0]
            rest = [self.visit(n) for n in node.body[1:]]
            node.body = [head] + rest
        else:
            node.body = self._strip_body(node.body)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.body = self._strip_body(node.body)
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node.body = self._strip_body(node.body)
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        return node


def _count_docstrings(tree: ast.AST, preserve_module: bool) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.body or not _is_docstring_node(node.body[0]):
                continue
            if preserve_module and isinstance(node, ast.Module):
                continue
            n += 1
    return n


def strip_ast_unparse(source: str, *, preserve_module_docstring: bool = True) -> tuple[Optional[str], int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None, 0
    count = _count_docstrings(tree, preserve_module_docstring)
    tr = _DocstringStripper(preserve_module_docstring)
    new_tree = tr.visit(tree)
    ast.fix_missing_locations(new_tree)
    try:
        return ast.unparse(new_tree), count
    except Exception:
        return None, 0


def _regex_fallback(source: str) -> tuple[str, int]:
    lines = source.split("\n")
    out: list[str] = []
    count = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        quote = None
        if '"""' in line:
            quote = '"""'
        elif "'''" in line:
            quote = "'''"
        if quote is not None:
            if line.count(quote) >= 2:
                start = line.find(quote)
                end = line.find(quote, start + 3)
                prefix = line[:start].rstrip()
                if prefix.endswith(":") or not prefix:
                    out.append(line[:start] + line[end + 3 :])
                    count += 1
                    i += 1
                    continue
            prefix = line[: line.find(quote)].rstrip()
            if prefix.endswith(":") or not prefix or "=" not in prefix:
                count += 1
                if prefix:
                    out.append(prefix)
                j = i + 1
                while j < len(lines):
                    if quote in lines[j]:
                        tail = lines[j][lines[j].find(quote) + 3 :].strip()
                        if tail:
                            out.append(tail)
                        i = j + 1
                        break
                    j += 1
                else:
                    i = j
            else:
                out.append(line)
                i += 1
        else:
            out.append(line)
            i += 1
    return "\n".join(out), count


def strip_text_fallback(source: str, *, preserve_module_docstring: bool = True) -> tuple[str, int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _regex_fallback(source)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not node.body or not _is_docstring_node(node.body[0]):
            continue
        if preserve_module_docstring and isinstance(node, ast.Module):
            continue
        first = node.body[0]
        if getattr(first, "lineno", None) and getattr(first, "end_lineno", None):
            spans.append((first.lineno, first.end_lineno))
    lines = source.split("\n")
    for start, end in sorted(spans, reverse=True):
        del lines[start - 1 : end]
    return "\n".join(lines), len(spans)


def _tidy(source: str) -> str:
    source = re.sub(r"\n\n+", "\n", source)
    return "\n".join(line.rstrip() for line in source.split("\n"))


def _libcst_is_docstring_line(node: ast.AST) -> bool:
    if not isinstance(node, cst.SimpleStatementLine):
        return False
    if len(node.body) != 1:
        return False
    stmt = node.body[0]
    if not isinstance(stmt, cst.Expr):
        return False
    return isinstance(stmt.value, (cst.SimpleString, cst.ConcatenatedString))


class _LibCSTStripper(cst.CSTTransformer):
    def __init__(
        self,
        *,
        preserve_module_docstring: bool,
        remove_comments: bool,
        preserve_fmt_type: bool,
    ) -> None:
        super().__init__()
        self.preserve_module_docstring = preserve_module_docstring
        self.remove_comments = remove_comments
        self.preserve_fmt_type = preserve_fmt_type
        self.comments_removed = 0
        self.docstrings_removed = 0

    def _keep_comment(self, text: str) -> bool:
        if not self.preserve_fmt_type:
            return False
        return text.lstrip().startswith(("#!", "# fmt", "# type"))

    def leave_EmptyLine(self, orig: Any, updated: Any) -> cst.CSTNode:
        if self.remove_comments and updated.comment is not None:
            if not self._keep_comment(updated.comment.value):
                self.comments_removed += 1
                return updated.with_changes(comment=None)
        return updated

    def leave_TrailingWhitespace(self, orig: Any, updated: Any) -> cst.CSTNode:
        if self.remove_comments and updated.comment is not None:
            if not self._keep_comment(updated.comment.value):
                self.comments_removed += 1
                return updated.with_changes(comment=None)
        return updated

    @staticmethod
    def _fix_empty(body: ast.Module):
        if not body:
            return (cst.SimpleStatementLine(body=[cst.Pass()]),)
        return body

    def leave_Module(self, orig: Any, updated: Any) -> cst.CSTNode:
        if not self.preserve_module_docstring and updated.body and _libcst_is_docstring_line(updated.body[0]):
            self.docstrings_removed += 1
            return updated.with_changes(body=updated.body[1:])
        return updated

    def _strip_suite(self, indented_block: "cst.IndentedBlock"):
        if indented_block.body and _libcst_is_docstring_line(indented_block.body[0]):
            self.docstrings_removed += 1
            new_body = self._fix_empty(indented_block.body[1:])
            return indented_block.with_changes(body=new_body)
        return indented_block

    def leave_FunctionDef(self, orig: Any, updated: Any) -> cst.CSTNode:
        if isinstance(updated.body, cst.IndentedBlock):
            new_block = self._strip_suite(updated.body)
            if new_block is not updated.body:
                return updated.with_changes(body=new_block)
        return updated

    leave_AsyncFunctionDef = leave_FunctionDef

    def leave_ClassDef(self, orig: Any, updated: Any) -> cst.CSTNode:
        if isinstance(updated.body, cst.IndentedBlock):
            new_block = self._strip_suite(updated.body)
            if new_block is not updated.body:
                return updated.with_changes(body=new_block)
        return updated


def strip_libcst(
    source: str,
    *,
    preserve_module_docstring: bool = True,
    remove_comments: bool = True,
    preserve_shebang: bool = True,
    preserve_fmt_type: bool = True,
) -> tuple[Optional[str], int, int]:
    if not HAS_LIBCST:
        return None, 0, 0
    shebang = ""
    body = source
    if preserve_shebang and source.startswith("#!"):
        buf = io.StringIO(source)
        shebang = buf.readline()
        body = buf.read()
    try:
        module = cst.parse_module(body)
    except Exception:
        return None, 0, 0
    stripper = _LibCSTStripper(
        preserve_module_docstring=preserve_module_docstring,
        remove_comments=remove_comments,
        preserve_fmt_type=preserve_fmt_type,
    )
    try:
        new_module = module.visit(stripper)
    except Exception:
        return None, 0, 0
    out = new_module.code
    if shebang:
        out = shebang + out.lstrip("\n")
    return out, stripper.comments_removed, stripper.docstrings_removed


def _process_strip(path_str: str, opts: dict) -> Optional[tuple[str, int, int, bool, Optional[str]]]:
    path = Path(path_str)
    src = _read_source(path)
    if src is None:
        return None
    engine = opts["engine"]
    preserve_mod = opts["preserve_module_docstring"]
    if engine == "ast-rewrite":
        new_src, docstrings = strip_ast_rewrite(src, preserve_module_docstring=preserve_mod)
        comments = 0
    elif engine == "ast-unparse":
        new_src, docstrings = strip_ast_unparse(src, preserve_module_docstring=preserve_mod)
        comments = 0
    elif engine == "text-fallback":
        new_src, docstrings = strip_text_fallback(src, preserve_module_docstring=preserve_mod)
        comments = 0
        if new_src is not None and opts.get("tidy"):
            new_src = _tidy(new_src)
    elif engine == "libcst":
        new_src, comments, docstrings = strip_libcst(
            src,
            preserve_module_docstring=preserve_mod,
            remove_comments=opts["remove_comments"],
            preserve_shebang=opts["preserve_shebang"],
            preserve_fmt_type=opts["preserve_fmt_type"],
        )
    else:
        return None
    if new_src is None:
        return (path_str, 0, 0, False, "engine produced no output")
    if new_src == src:
        return (path_str, 0, 0, False, None)
    if not _is_valid_python(new_src):
        return (path_str, 0, 0, False, "modified source failed AST validation")
    try:
        path.write_text(new_src, encoding="utf-8")
    except OSError as e:
        return (path_str, 0, 0, False, str(e))
    return (path_str, comments, docstrings, True, None)


def cmd_strip(args: argparse.Namespace) -> int:
    paths = _default_paths(args.paths)
    files = _collect_py_files(paths)
    if not files:
        LOG.warning("No Python files found to process.")
        return 0
    if args.engine == "libcst" and not HAS_LIBCST:
        LOG.error("The 'libcst' engine requires the libcst package (pip install libcst).")
        return 2
    opts = {
        "engine": args.engine,
        "preserve_module_docstring": args.preserve_module_docstring,
        "remove_comments": args.remove_comments,
        "preserve_shebang": args.preserve_shebang,
        "preserve_fmt_type": args.preserve_fmt_type,
        "tidy": args.tidy,
    }
    total_changed = 0
    total_comments = 0
    total_docstrings = 0
    failures = 0

    def iter_results():
        jobs = [str(p) for p in files]
        if MAX_WORKERS > 1 and len(jobs) > 1:
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
                yield from pool.map(_process_strip, jobs, [opts] * len(jobs), chunksize=8)
        else:
            for j in jobs:
                yield _process_strip(j, opts)

    for res in iter_results():
        if res is None:
            continue
        path_str, comments, docstrings, changed, err = res
        if err:
            LOG.error("Failed: {} - {}", path_str, err)
            failures += 1
            continue
        if changed:
            total_changed += 1
            total_comments += comments
            total_docstrings += docstrings
            if args.engine == "libcst":
                print(f"{path_str}: comments={comments} docstrings={docstrings}")
            else:
                print(f"{path_str}: docstrings={docstrings}")
    print("=" * 40)
    print("Processing complete:")
    print(f"  Files changed:      {total_changed}/{len(files)}")
    print(f"  Comments removed:   {total_comments}")
    print(f"  Docstrings removed: {total_docstrings}")
    print(f"  Failures:           {failures}")
    print("=" * 40)
    return 0 if failures == 0 else 1


def _scan_file_for_findings(path_str: str) -> tuple[str, list[tuple[int, str, bool]]]:
    path = Path(path_str)
    findings: list[tuple[int, str, bool]] = []
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return path_str, findings
    doc_lines: set[int] = set()
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.body and _is_docstring_node(node.body[0]):
                    ds = node.body[0]
                    if hasattr(ds, "lineno"):
                        last = getattr(ds, "end_lineno", ds.lineno) or ds.lineno
                        for ln in range(ds.lineno, last + 1):
                            doc_lines.add(ln - 1)
    except SyntaxError:
        pass
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if i in doc_lines:
            findings.append((i, line, True))
            continue
        if "#" not in line:
            continue
        stripped = line.strip()
        if i == 0 and stripped.startswith("#!"):
            continue
        if re.match(r"#\s*(type|fmt):", stripped):
            continue
        col = line.find("#")
        before = line[:col]
        if before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
            findings.append((i, line, False))
    return path_str, findings


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    if not root.is_dir():
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        return 1
    files = _collect_py_files([root])
    if not files:
        print("No Python files found.")
        return 0
    print(f"Scanning {len(files)} Python files with {MAX_WORKERS} workers...\n")

    def iter_results():
        jobs = [str(p) for p in files]
        if MAX_WORKERS > 1 and len(jobs) > 1:
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
                yield from pool.map(_scan_file_for_findings, jobs, chunksize=8)
        else:
            for j in jobs:
                yield _scan_file_for_findings(j)

    findings_by_file: dict[str, list[tuple[int, str, bool]]] = {}
    total = 0
    for path_str, items in iter_results():
        if items:
            findings_by_file[path_str] = items
            total += len(items)
    if not findings_by_file:
        print("No comments or docstrings found.")
        return 0
    print(f"Found {total} comments/docstrings:\n")
    print("=" * 40)
    for path_str in sorted(findings_by_file):
        items = findings_by_file[path_str]
        lines = Path(path_str).read_text(encoding="utf-8", errors="ignore").splitlines()
        for ln0, text, is_doc in sorted(items, key=lambda x: x[0], reverse=args.auto_remove):
            label = "docstring" if is_doc else "comment  "
            print(f"{GREEN}{path_str}:{ln0 + 1}{RESET} {YELLOW}[{label}]{RESET} {text.rstrip()}")
        if args.auto_remove:
            keep = [ln for i, ln in enumerate(lines) if i not in {x[0] for x in items}]
            Path(path_str).write_text("\n".join(keep), encoding="utf-8")
            print(f"  {YELLOW}[REMOVED {len(items)} lines]{RESET}")
    print("\n" + "=" * 40)
    if args.auto_remove:
        print(f"{YELLOW}Removed {total} comments/docstrings.{RESET}")
    else:
        print(f"Total findings: {total}")
        print("Use -a/--auto-remove to remove them.")
    return 0


def _default_module_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf'^\s*"""\s*Module for\s+{re.escape(name)}\s*\.?\s*"""\s*$')


def _clean_module_doc_worker(
    job: tuple[str, int, Optional[str]],
) -> Optional[tuple[str, bool, Optional[int]]]:
    path_str, top_n, pattern_str = job
    path = Path(path_str)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    if pattern_str:
        rx = re.compile(pattern_str)
    else:
        rx = _default_module_pattern(path.name)
    limit = min(top_n, len(lines))
    removed_at: Optional[int] = None
    for i in range(limit):
        if rx.match(lines[i].strip()):
            lines.pop(i)
            removed_at = i
            break
    if removed_at is None:
        return None
    new_src = "".join(lines)
    if not _is_valid_python(new_src):
        return (path_str, False, None)
    try:
        path.write_text(new_src, encoding="utf-8")
    except OSError:
        return None
    return (path_str, True, removed_at + 1)


def cmd_clean_module_doc(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    files = _collect_py_files([root])
    self_name = Path(__file__).name
    files = [f for f in files if f.name != self_name]
    if not files:
        LOG.warning("No Python files found.")
        return 0
    print(f"Scanning top {args.top} lines of {len(files)} files with {MAX_WORKERS} workers...")
    jobs = [(str(f), args.top, args.pattern) for f in files]

    def iter_results():
        if MAX_WORKERS > 1 and len(jobs) > 1:
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
                yield from pool.map(_clean_module_doc_worker, jobs, chunksize=8)
        else:
            for j in jobs:
                yield _clean_module_doc_worker(j)

    cleaned = 0
    for res in iter_results():
        if res is None:
            continue
        path_str, ok, line_no = res
        if ok:
            cleaned += 1
            print(f"Cleaned docstring at line {line_no} of: {path_str}")
        else:
            LOG.error("Refusing to write {}: modified source failed AST validation", path_str)
    print(f"Fast cleanup complete! ({cleaned} file(s) modified)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyclean.py",
        description="Unified Python source cleaner (docstrings + comments).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("strip", help="Remove docstrings and/or comments.")
    p.add_argument("paths", nargs="*", help="Files/directories (default: cwd)")
    p.add_argument(
        "--engine",
        choices=("ast-rewrite", "ast-unparse", "text-fallback", "libcst"),
        default="ast-rewrite",
        help=(
            "ast-rewrite   = AST span splice, preserves comments/format (brmc.py)\n"
            "ast-unparse   = AST unparse, loses comments/format (brmc2.py)\n"
            "text-fallback = line-based delete + regex fallback (remc.py)\n"
            "libcst        = libcst-based, also removes comments (rmccst/rmcst)"
        ),
    )
    p.add_argument(
        "--preserve-module-docstring",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep the module-level docstring (default: True)",
    )
    p.add_argument(
        "--remove-comments",
        action="store_true",
        help="Also remove comments (libcst engine only)",
    )
    p.add_argument(
        "--preserve-shebang",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep '#!/usr/bin/env ...' shebang (libcst engine, default: True)",
    )
    p.add_argument(
        "--preserve-fmt-type",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep '#fmt:' / '#type:' comments (libcst engine, default: True)",
    )
    p.add_argument(
        "--tidy",
        action="store_true",
        help="Collapse blank-line runs and trim trailing whitespace (text-fallback engine)",
    )
    p.set_defaults(func=cmd_strip)
    p = sub.add_parser("check", help="Find comments and docstrings (check_rmc.py).")
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument(
        "-a",
        "--auto-remove",
        action="store_true",
        help="Automatically remove the found lines",
    )
    p.set_defaults(func=cmd_check)
    p = sub.add_parser(
        "clean-module-doc",
        help="Remove the auto-generated 'Module for X.' docstring (rm_moduledoc.py).",
    )
    p.add_argument("directory", nargs="?", default=".")
    p.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP_LINES,
        help=f"Only scan the top N lines (default: {DEFAULT_TOP_LINES})",
    )
    p.add_argument(
        "--pattern",
        default=None,
        help="Override the regex; must match the entire line "
        'after stripping (default: \'^\\s*"""\\s*Module for <name>...\')',
    )
    p.set_defaults(func=cmd_clean_module_doc)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        LOG.warning("Operation cancelled by user")
        return 130
    except Exception as e:
        LOG.opt(exception=True).error(
            "Unexpected error: {}",
            e,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
