#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments, docstrings and annotations of Python files.
``-c`` removes the comments, ``-d`` the docstrings, ``-a``/``-x`` everything, ``-t`` the annotations; the backend is ast, LibCST or tree-sitter (``-b``), the writes are atomic, and ``--dry-run``, ``--diff``, ``--backup`` and every pool method are available.
Usage: drmc [-c] [-d] [-t] [-b BACKEND] [--diff] [paths ...] Positional arguments: paths ...
Files or directories (default: cwd) Options: -c, --comments Remove all comments -d, --docstring Remove all docstrings except module docstring.
-a, --all Remove all comments and docstrings, even preserved ones.
-t, --type Remove type annotations.
-x Equivalent to -a -t; nothing written, output copied to clipboard.
-b, --backend Backend (default: libcst if installed, else ast).
--dry-run Show changes without writing.
--diff Show unified diff of changes.
--backup Create .bak backup before writing.
--remove-module-docstring Also strip module-level docstrings.
...
plus 2 more; run ``drmc --help`` for the full list.
Examples: $ drmc -c -d --diff src/ Command-line script: run ``python drmc.py`` or the ``drmc`` command.
The module is importable as well."""

import argparse
import ast
from dataclasses import dataclass
import difflib
import io
from itertools import chain
from multiprocessing import Pool
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from typing import Any, Callable, Iterable, Iterator, Sequence
import zipfile

from atomicio import atomic_write
from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


SKIP_DIR_GLOBS: tuple[str, ...] = ("*.dist-info",)
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "latin-1")
GREEN: str = "\033[32m"
GRAY: str = "\033[90m"
RESET: str = "\033[0m"
MP_METHODS: tuple[str, ...] = ("starmap", "map", "imap_unordered", "apply", "apply_async", "joblib", "none")


@dataclass(slots=True)
class FileResult:
    path: Path
    bytes_removed: int = 0
    comments_removed: int = 0
    docstrings_removed: int = 0
    error: str | None = None
    changed: bool = False
    final_code: str | None = None
    original_code: str | None = None


@dataclass(frozen=True, slots=True)
class Options:
    strip_comments: bool = True
    strip_docstrings: bool = False
    strip_all: bool = False
    strip_types: bool = False
    backend: str = "ast"
    dry_run: bool = False
    remove_module_docstring: bool = False
    clipboard: bool = False
    diff: bool = False
    backup: bool = False


@dataclass(frozen=True, slots=True)
class Edit:
    start: int
    end: int
    replacement: str
    kind: str


@dataclass(frozen=True, slots=True)
class Stripped:
    code: str
    comments_removed: int
    docstrings_removed: int


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    msg = "unknown"
    raise UnicodeDecodeError(msg, raw, 0, 1, "no encoding worked")


def is_python_candidate(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    if path.suffix:
        return False
    try:
        if path.stat().st_size > 2_000_000:
            return False
        with path.open("rb") as fh:
            head = fh.readline(256)
        if head.startswith(b"#!") and b"python" in head:
            return True
        if head.strip():
            return False
        ast.parse(read_text(path))
        return True
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return False


def iter_targets(inputs: Iterable[Path]) -> Iterator[Path]:
    for root in inputs:
        if root.is_file():
            if root.suffix == ".whl" or is_python_candidate(root):
                yield root
            continue
        if not root.is_dir():
            logger.error("Path does not exist: {}", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIRS and not any(Path(d).match(g) for g in SKIP_DIR_GLOBS)
            ]
            base = Path(dirpath)
            for name in filenames:
                p = base / name
                if p.is_symlink() or not p.exists():
                    continue
                if p.suffix == ".whl" or is_python_candidate(p):
                    yield p


def relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _pos(offsets: list[int], lineno: int, col: int) -> int:
    return offsets[lineno - 1] + col


def _is_preserved_comment(text: str, lineno: int) -> bool:
    body = text.lstrip("#").strip()
    if lineno == 1 and text.startswith("#!"):
        return True
    if text.startswith(("# type:", "#type:")):
        return True
    if text.startswith(("# fmt:", "# fmt :", "#fmt:")):
        return True
    return body.startswith(("type:", "fmt:"))


def _is_string_stmt(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _docstring_edits(source: str, tree: ast.Module, opts: Options) -> list[Edit]:
    offsets = _line_offsets(source)
    edits: list[Edit] = []
    remove_all = opts.strip_all or opts.strip_docstrings

    def handle_body(body: list[ast.stmt], is_module: bool) -> None:
        if not body or not _is_string_stmt(body[0]):
            return
        if is_module and not (opts.strip_all or opts.remove_module_docstring):
            return
        if not remove_all and not is_module:
            return
        if not remove_all and is_module and not opts.remove_module_docstring:
            return
        stmt = body[0]
        start = _pos(offsets, stmt.lineno, stmt.col_offset)
        end = _pos(offsets, stmt.end_lineno or stmt.lineno, stmt.end_col_offset or 0)
        only_stmt = len(body) == 1 and not is_module
        replacement = "pass" if only_stmt else ""
        edits.append(Edit(start, end, replacement, "docstring"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Module):
            handle_body(node.body, is_module=True)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            handle_body(node.body, is_module=False)
    return edits


def _comment_edits(source: str, opts: Options) -> list[Edit]:
    edits: list[Edit] = []
    lines = source.splitlines(keepends=True)
    offsets = _line_offsets(source)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError) as exc:
        msg = f"tokenize failed: {exc}"
        raise SyntaxError(msg) from exc
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        lineno, col = tok.start
        text = tok.string
        if _is_preserved_comment(text, lineno) and not opts.strip_all:
            continue
        if not opts.strip_comments and not opts.strip_all:
            continue
        line = lines[lineno - 1]
        end = _pos(offsets, lineno, col + len(text))
        before = line[:col]
        if before.strip() == "":
            edits.append(
                Edit(
                    _pos(offsets, lineno, 0),
                    _pos(offsets, lineno, len(line.rstrip("\r\n"))),
                    "",
                    "comment",
                )
            )
        else:
            trimmed_start = _pos(offsets, lineno, len(before.rstrip()))
            edits.append(Edit(trimmed_start, end, "", "comment"))
    return edits


def _type_annotation_edits(source: str, tree: ast.Module) -> list[Edit]:
    offsets = _line_offsets(source)
    edits: list[Edit] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                r_start = _pos(offsets, node.returns.lineno, node.returns.col_offset)
                arrow = source.rfind("->", 0, r_start)
                if arrow != -1:
                    end = _pos(
                        offsets,
                        node.returns.end_lineno or node.returns.lineno,
                        node.returns.end_col_offset or 0,
                    )
                    edits.append(Edit(arrow, end, "", "annotation"))
            all_args = (
                node.args.posonlyargs
                + node.args.args
                + node.args.kwonlyargs
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])
            )
            for arg in all_args:
                if arg.annotation is not None:
                    colon = source.rfind(":", 0, _pos(offsets, arg.lineno, arg.col_offset) + len(arg.arg) + 1)
                    end = _pos(
                        offsets,
                        arg.annotation.end_lineno or arg.annotation.lineno,
                        arg.annotation.end_col_offset or 0,
                    )
                    edits.append(Edit(colon, end, "", "annotation"))
        elif isinstance(node, ast.AnnAssign) and node.simple:
            t_end = _pos(
                offsets,
                node.target.end_lineno or node.target.lineno,
                node.target.end_col_offset or 0,
            )
            a_end = _pos(
                offsets,
                node.annotation.end_lineno or node.annotation.lineno,
                node.annotation.end_col_offset or 0,
            )
            if node.value is not None:
                eq = source.find("=", a_end)
                edits.append(Edit(t_end, eq, " ", "annotation"))
            else:
                edits.append(Edit(t_end, a_end, "", "annotation"))
    return edits


def _apply_edits(source: str, edits: list[Edit]) -> str:
    seen: set[tuple[int, int]] = set()
    unique: list[Edit] = []
    for e in sorted(edits, key=lambda x: (x.start, x.end)):
        key = (e.start, e.end)
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    out = source
    last_start = len(source) + 1
    for e in sorted(unique, key=lambda x: x.start, reverse=True):
        if e.end > last_start:
            continue
        out = out[: e.start] + e.replacement + out[e.end :]
        last_start = e.start
    return out


def _count(edits: list[Edit]) -> tuple[int, int]:
    c = sum(1 for e in edits if e.kind == "comment")
    d = sum(1 for e in edits if e.kind == "docstring")
    return c, d


def backend_ast(source: str, opts: Options) -> Stripped:
    tree = ast.parse(source)
    edits: list[Edit] = []
    if opts.strip_comments or opts.strip_all:
        edits += _comment_edits(source, opts)
    if opts.strip_docstrings or opts.strip_all or opts.remove_module_docstring:
        edits += _docstring_edits(source, tree, opts)
    if opts.strip_types:
        edits += _type_annotation_edits(source, tree)
    code = _apply_edits(source, edits)
    c, d = _count(edits)
    return Stripped(code, c, d)


def backend_libcst(source: str, opts: Options) -> Stripped:
    import libcst as cst

    class _Stripper(cst.CSTTransformer):
        def __init__(self) -> None:
            super().__init__()
            self.comments = 0
            self.docstrings = 0

        def _maybe_strip_docstring(self, body: cst.BaseSuite, is_module: bool) -> cst.BaseSuite:
            if not isinstance(body, cst.IndentedBlock) or not body.body:
                return body
            first = body.body[0]
            if not (
                isinstance(first, cst.SimpleStatementLine)
                and first.body
                and isinstance(first.body[0], cst.Expr)
                and isinstance(first.body[0].value, (cst.SimpleString, cst.ConcatenatedString))
            ):
                return body
            if is_module and not (opts.strip_all or opts.remove_module_docstring):
                return body
            if not is_module and not (opts.strip_all or opts.strip_docstrings):
                return body
            self.docstrings += 1
            rest = list(body.body[1:])
            if not rest:
                rest = [cst.SimpleStatementLine(body=[cst.Pass()])]
            return body.with_changes(body=rest)

        def leave_Module(self, original: cst.Module, updated: cst.Module) -> cst.Module:
            new_body = self._maybe_strip_docstring(cst.IndentedBlock(body=list(updated.body)), is_module=True)
            return updated.with_changes(body=list(new_body.body))

        def leave_FunctionDef(self, original: cst.FunctionDef, updated: cst.FunctionDef) -> cst.FunctionDef:
            return updated.with_changes(body=self._maybe_strip_docstring(updated.body, is_module=False))

        def leave_ClassDef(self, original: cst.ClassDef, updated: cst.ClassDef) -> cst.ClassDef:
            return updated.with_changes(body=self._maybe_strip_docstring(updated.body, is_module=False))

    module = cst.parse_module(source)
    stripper = _Stripper()
    module = module.visit(stripper)
    code = module.code
    if opts.strip_comments or opts.strip_all:
        edits = _comment_edits(code, opts)
        code = _apply_edits(code, edits)
        stripper.comments = sum(1 for e in edits if e.kind == "comment")
    return Stripped(code, stripper.comments, stripper.docstrings)


def backend_tree_sitter(source: str, opts: Options) -> Stripped:
    from tree_sitter import Language, Parser
    import tree_sitter_python as tspy

    lang = Language(tspy.language())
    try:
        parser = Parser(lang)
    except TypeError:
        parser = Parser()
        parser.set_language(lang)
    data = source.encode("utf-8")
    tree = parser.parse(data)
    edits: list[Edit] = []
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        stack.extend(reversed(n.children))
        if n.type == "comment" and (opts.strip_comments or opts.strip_all):
            text = data[n.start_byte : n.end_byte].decode("utf-8", "replace")
            if _is_preserved_comment(text, n.start_point[0] + 1) and not opts.strip_all:
                continue
            line_start = data.rfind(b"\n", 0, n.start_byte) + 1
            before = data[line_start : n.start_byte]
            s = line_start if before.strip() == b"" else line_start + len(before.rstrip())
            edits.append(Edit(s, n.end_byte, "", "comment"))
        elif n.type == "expression_statement" and n.children and n.children[0].type == "string":
            parent = n.parent
            is_module = parent is not None and parent.type == "module"
            is_body = (
                parent is not None
                and parent.type == "block"
                and parent.parent is not None
                and parent.parent.type in ("function_definition", "class_definition")
            )
            if is_module and not (opts.strip_all or opts.remove_module_docstring):
                continue
            if is_body and not (opts.strip_all or opts.strip_docstrings):
                continue
            if not (is_module or is_body):
                continue
            only = is_body and parent is not None and parent.named_child_count == 1
            edits.append(Edit(n.start_byte, n.end_byte, "pass" if only else "", "docstring"))
    out_bytes = bytearray(data)
    for e in sorted(edits, key=lambda x: x.start, reverse=True):
        out_bytes[e.start : e.end] = e.replacement.encode("utf-8")
    code = out_bytes.decode("utf-8")
    c, d = _count(edits)
    return Stripped(code, c, d)


BACKENDS: dict[str, Callable[[str, Options], Stripped]] = {
    "ast": backend_ast,
    "libcst": backend_libcst,
    "tree-sitter": backend_tree_sitter,
}


def resolve_backend(requested: str | None) -> str:
    if requested:
        if requested not in BACKENDS:
            msg = f"Unknown backend: {requested}"
            raise SystemExit(msg)
        return requested
    try:
        import libcst

        return "libcst"
    except ImportError:
        return "ast"


def check_backend_available(name: str) -> None:
    if name == "libcst":
        try:
            import libcst
        except ImportError as exc:
            msg = "libcst not installed: pip install libcst"
            raise SystemExit(msg) from exc
    elif name == "tree-sitter":
        try:
            import tree_sitter
            import tree_sitter_python
        except ImportError as exc:
            msg = "tree-sitter not installed: pip install tree-sitter tree-sitter-python"
            raise SystemExit(msg) from exc


def process_source(source: str, opts: Options) -> Stripped:
    result = BACKENDS[opts.backend](source, opts)
    ast.parse(result.code)
    return result


def _print_diff(path: Path, before: str, after: str) -> None:
    diff_text = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (stripped)",
        )
    )
    if not diff_text:
        return
    if not diff_text.endswith("\n"):
        diff_text += "\n"
    sys.stdout.write(diff_text)
    sys.stdout.flush()


def _make_backup(path: Path) -> None:
    try:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except OSError as exc:
        logger.warning("Backup failed for {}: {}", path, exc)


def process_python_file(path: Path, opts: Options) -> FileResult:
    result = FileResult(path=path)
    try:
        original = read_text(path)
    except PermissionError as exc:
        result.error = f"PermissionError: {exc}"
        logger.error("PermissionError reading {}: {}", path, exc)
        return result
    except UnicodeDecodeError as exc:
        result.error = f"UnicodeDecodeError: {exc}"
        logger.error("Encoding error in {}: {}", path, exc)
        return result
    except OSError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error("I/O error reading {}: {}", path, exc)
        return result
    result.original_code = original
    try:
        stripped = process_source(original, opts)
    except SyntaxError as exc:
        result.error = f"SyntaxError at line {exc.lineno}: {exc.msg}"
        logger.error("SyntaxError in {} line {}: {}", path, exc.lineno, exc.msg)
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.opt(exception=exc).error("Failed processing {}", path)
        return result
    result.comments_removed = stripped.comments_removed
    result.docstrings_removed = stripped.docstrings_removed
    new_bytes = len(stripped.code.encode("utf-8"))
    old_bytes = len(original.encode("utf-8"))
    result.bytes_removed = max(0, old_bytes - new_bytes)
    result.changed = stripped.code != original
    if opts.diff and result.changed:
        _print_diff(path, original, stripped.code)
    if opts.clipboard:
        result.final_code = stripped.code
        return result
    if result.changed and not opts.dry_run:
        try:
            if opts.backup:
                _make_backup(path)
            atomic_write(path, stripped.code)
        except PermissionError as exc:
            result.error = f"PermissionError writing: {exc}"
            result.changed = False
            logger.error("PermissionError writing {}: {}", path, exc)
        except OSError as exc:
            result.error = f"{type(exc).__name__} writing: {exc}"
            result.changed = False
            logger.opt(exception=exc).error("Write failed for {}", path)
    return result


def process_wheel(path: Path, opts: Options) -> FileResult:
    result = FileResult(path=path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="strip_whl_", dir=path.parent))
    out_tmp = tmp_dir / path.name
    try:
        with zipfile.ZipFile(path) as zin:
            bad = zin.testzip()
            if bad is not None:
                result.error = f"Corrupted wheel member: {bad}"
                return result
            total_removed = c_total = d_total = 0
            changed_any = False
            with zipfile.ZipFile(out_tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    data = zin.read(info.filename)
                    if info.filename.endswith(".py"):
                        try:
                            text = data.decode("utf-8")
                        except UnicodeDecodeError:
                            text = data.decode("latin-1")
                        try:
                            s = process_source(text, opts)
                            if s.code != text:
                                total_removed += len(text.encode()) - len(s.code.encode())
                                c_total += s.comments_removed
                                d_total += s.docstrings_removed
                                changed_any = True
                                if not opts.dry_run:
                                    data = s.code.encode("utf-8")
                        except SyntaxError as exc:
                            logger.error(
                                "SyntaxError in {}!{} line {}: {}",
                                path,
                                info.filename,
                                exc.lineno,
                                exc.msg,
                            )
                    zout.writestr(info, data)
            result.bytes_removed = max(0, total_removed)
            result.comments_removed = c_total
            result.docstrings_removed = d_total
            result.changed = total_removed != 0
            if changed_any and not opts.dry_run:
                if opts.backup:
                    _make_backup(path)
                shutil.move(str(out_tmp), path)
            return result
    except zipfile.BadZipFile as exc:
        result.error = f"Invalid wheel archive: {exc}"
        logger.error("Invalid wheel {}: {}", path, exc)
        return result
    except OSError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.opt(exception=exc).error("Wheel I/O failure {}", path)
        return result
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def report(result: FileResult) -> None:
    rel = relpath(result.path)
    if result.error:
        print(f"{rel}    ERROR: {result.error}")
    elif result.changed:
        print(f"{GREEN}{rel}    {result.bytes_removed}{RESET}")
    else:
        print(f"{GRAY}{rel}    (unchanged){RESET}")


def summarize(results: list[FileResult]) -> None:
    total = len(results)
    changed = sum(1 for r in results if r.changed)
    removed = sum(r.bytes_removed for r in results)
    comments = sum(r.comments_removed for r in results)
    docs = sum(r.docstrings_removed for r in results)
    errors = sum(1 for r in results if r.error)
    print(
        f"\nSummary: {total} files | {changed} changed | {removed} bytes removed | "
        f"{comments} comments | {docs} docstrings | {errors} errors"
    )


def copy_to_clipboard(code: str) -> None:
    try:
        subprocess.run(["termux-clipboard-set"], input=code, text=True, check=True)
    except FileNotFoundError:
        logger.error("termux-clipboard-set not found; install Termux:API")
    except subprocess.CalledProcessError as exc:
        logger.error("termux-clipboard-set failed: {}", exc)


def _worker_dispatch(args: tuple[Path, Options]) -> FileResult:
    path, opts = args
    if path.suffix == ".whl":
        return process_wheel(path, opts)
    return process_python_file(path, opts)


def _iter_joblib(args_iter: Iterable[tuple[Path, Options]]) -> Iterator[FileResult]:
    try:
        from joblib import Parallel, delayed
    except ImportError as exc:
        msg = "joblib not installed: pip install joblib"
        raise SystemExit(msg) from exc
    for r in Parallel(n_jobs=MAX_WORKERS)(delayed(_worker_dispatch)(a) for a in args_iter):
        yield r


def run_parallel(
    args_iter: Iterable[tuple[Path, Options]],
    method: str,
) -> Iterator[FileResult]:
    if method == "none":
        for a in args_iter:
            yield _worker_dispatch(a)
        return
    if method == "joblib":
        yield from _iter_joblib(args_iter)
        return
    if method == "starmap":
        with Pool(MAX_WORKERS) as pool:
            for r in pool.starmap(_worker_dispatch, args_iter):
                yield r
        return
    if method == "map":
        with Pool(MAX_WORKERS) as pool:
            for r in pool.map(_worker_dispatch, args_iter):
                yield r
        return
    if method == "imap_unordered":
        with Pool(MAX_WORKERS) as pool:
            for r in pool.imap_unordered(_worker_dispatch, args_iter):
                yield r
        return
    if method == "apply":
        with Pool(MAX_WORKERS) as pool:
            for a in args_iter:
                yield pool.apply(_worker_dispatch, a)
        return
    if method == "apply_async":
        with Pool(MAX_WORKERS) as pool:
            asyncs = [pool.apply_async(_worker_dispatch, a) for a in args_iter]
            for a in asyncs:
                yield a.get()
        return
    msg = f"Unknown mp method: {method}"
    raise SystemExit(msg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Strip comments and docstrings from Python files.")
    p.add_argument("paths", nargs="*", type=Path, help="Files or directories (default: cwd)")
    p.add_argument("-c", "--comments", action="store_true", help="Remove all comments")
    p.add_argument(
        "-d",
        "--docstring",
        action="store_true",
        help="Remove all docstrings except module docstring.",
    )
    p.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Remove all comments and docstrings, even preserved ones.",
    )
    p.add_argument("-t", "--type", action="store_true", help="Remove type annotations.")
    p.add_argument(
        "-x",
        action="store_true",
        help="Equivalent to -a -t; nothing written, output copied to clipboard.",
    )
    p.add_argument(
        "-b",
        "--backend",
        choices=sorted(BACKENDS),
        default=None,
        help="Backend (default: libcst if installed, else ast).",
    )
    p.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    p.add_argument("--diff", action="store_true", help="Show unified diff of changes.")
    p.add_argument("--backup", action="store_true", help="Create .bak backup before writing.")
    p.add_argument(
        "--remove-module-docstring",
        action="store_true",
        help="Also strip module-level docstrings.",
    )
    p.add_argument(
        "--mp-method",
        choices=list(MP_METHODS),
        default="imap_unordered",
        help="Multiprocessing method (default: imap_unordered).",
    )
    p.add_argument(
        "--no-mp",
        action="store_true",
        help="Disable multiprocessing (equivalent to --mp-method=none).",
    )
    return p


def build_options(ns: argparse.Namespace) -> Options:
    strip_all = ns.all or ns.x
    strip_types = ns.type or ns.x
    default_mode = not (ns.comments or ns.docstring or strip_all)
    backend = resolve_backend(ns.backend)
    if strip_types and backend != "ast":
        if ns.backend is None:
            backend = "ast"
        else:
            msg = f"Backend '{backend}' does not support type stripping; use --backend ast"
            raise SystemExit(msg)
    return Options(
        strip_comments=ns.comments or default_mode or strip_all,
        strip_docstrings=ns.docstring,
        strip_all=strip_all,
        strip_types=strip_types,
        backend=backend,
        dry_run=ns.dry_run or ns.x,
        remove_module_docstring=ns.remove_module_docstring,
        clipboard=ns.x,
        diff=ns.diff,
        backup=ns.backup,
    )


def main(argv: list[str] | None = None) -> int:
    t_start = time.perf_counter()
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level}</level> {message}")
    ns = build_parser().parse_args(argv)
    opts = build_options(ns)
    check_backend_available(opts.backend)
    mp_method = "none" if ns.no_mp else ns.mp_method
    if opts.diff:
        mp_method = "none"
    inputs = ns.paths or [Path.cwd()]
    target_iter = iter_targets(inputs)
    first = next(target_iter, None)
    results: list[FileResult] = []
    if first is None:
        summarize(results)
        print(f"Total runtime: {time.perf_counter() - t_start:.3f}s")
        return 0
    second = next(target_iter, None)
    if second is None:
        r = _worker_dispatch((first, opts))
        report(r)
        results.append(r)
    else:
        args_iter: Iterable[tuple[Path, Options]] = ((t, opts) for t in chain([first, second], target_iter))
        for r in run_parallel(args_iter, mp_method):
            report(r)
            results.append(r)
    summarize(results)
    if opts.clipboard:
        finals = [r.final_code for r in results if r.final_code is not None]
        if len(finals) == 1:
            copy_to_clipboard(finals[0])
        elif finals:
            copy_to_clipboard("\n".join(finals))
        else:
            logger.warning("No output produced for clipboard")
    print(f"Total runtime: {time.perf_counter() - t_start:.3f}s")
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
