#!/data/data/com.termux/files/usr/bin/env python
"""Unified cleaner for Python comments, docstrings and unused code (AST, regex, just-the-code engines)."""


import argparse
import ast
import io
import multiprocessing as mp
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tokenize
import traceback
from typing import Callable, Iterable, NamedTuple, Sequence

from dh import MAX_WORKERS


try:
    import libcst as cst
    from libcst import matchers as _m

    _HAS_LIBCST = True
except ImportError:
    cst = None
    _m = None
    _HAS_LIBCST = False
try:
    import astor

    _HAS_ASTOR = True
except ImportError:
    astor = None
    _HAS_ASTOR = False
try:
    from loguru import logger as _loguru

    def log_error(msg: str) -> None:
        _loguru.error(msg)

    def log_warning(msg: str) -> None:
        _loguru.warning(msg)

except ImportError:

    def log_error(msg: str) -> None:
        print(f"ERROR: {msg}", file=sys.stderr)

    def log_warning(msg: str) -> None:
        print(f"WARNING: {msg}", file=sys.stderr)


DEFAULT_EXCLUDES: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    ".tox",
    "build",
    "dist",
})
FILE_COMMENT_KEYWORDS: tuple[str, ...] = ("coding", "encoding", "type:", "fmt:")


def _looks_like_python_script(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return False
    return first.startswith("#!") and "python" in first.lower()


def gather_python_files(
    paths: Sequence[str | Path],
    excludes: Iterable[str] = DEFAULT_EXCLUDES,
    include_shebang_scripts: bool = False,
) -> list[Path]:
    excl = {e.lower() for e in excludes}
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw).resolve()
        if p.is_file():
            if p.suffix == ".py" or (include_shebang_scripts and _looks_like_python_script(p)):
                if not any(part.lower() in excl for part in p.parts):
                    seen.add(p)
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if f.is_symlink():
                    continue
                if any(part.lower() in excl for part in f.parts):
                    continue
                if include_shebang_scripts or f.suffix == ".py":
                    seen.add(f)
        else:
            log_warning(f"Skipping non-existent path: {p}")
    return sorted(seen)


def safe_read_text(path: Path) -> tuple[str | None, str | None]:
    try:
        with tokenize.open(path) as fh:
            return fh.read(), None
    except Exception as exc:
        return None, f"read-error: {exc}"


def write_text_preserving_newlines(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.write_text(text, encoding=encoding, newline="\n")


def run_parallel(
    func: Callable[[Path], object],
    files: Sequence[Path],
    workers: int,
) -> list[tuple[Path, object]]:
    if not files:
        return []
    workers = max(1, min(workers, len(files)))
    results: list[tuple[Path, object]] = []
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=workers) as pool:
        async_results = [(f, pool.apply_async(func, (f,))) for f in files]
        for f, ar in async_results:
            try:
                results.append((f, ar.get()))
            except Exception as exc:
                log_error(f"Worker error on {f}: {exc}")
                results.append((f, ("worker-exception", str(exc))))
    return results


def split_leading_comments(src: str) -> tuple[str, str]:
    lines = src.splitlines(keepends=True)
    header: list[str] = []
    i = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and line.startswith("#!"):
            header.append(line)
            continue
        if not stripped:
            if header:
                header.append(line)
            continue
        if stripped.startswith("#"):
            low = stripped.lower()
            if any(k in low for k in FILE_COMMENT_KEYWORDS):
                header.append(line)
                continue
            break
        break
    return "".join(header), "".join(lines[i:]) if i < len(lines) else ""


if _HAS_LIBCST:

    def _is_plain_string(stmt: "cst.BaseStatement", binary_check: bool) -> bool:
        if not isinstance(stmt, cst.SimpleStatementLine):
            return False
        if len(stmt.body) != 1 or not isinstance(stmt.body[0], cst.Expr):
            return False
        val = stmt.body[0].value
        if not isinstance(val, cst.SimpleString):
            return False
        return not (binary_check and "b" in val.prefix.lower())

    class _LibCSTStripper(cst.CSTTransformer):
        def __init__(
            self,
            strip_comments: bool = True,
            strip_docstrings: bool = True,
            preserve_shebang: bool = True,
            preserve_file_comments: bool = True,
            preserve_module_docstring: bool = False,
            binary_check: bool = True,
        ) -> None:
            super().__init__()
            self.strip_comments = strip_comments
            self.strip_docstrings = strip_docstrings
            self.preserve_shebang = preserve_shebang
            self.preserve_file_comments = preserve_file_comments
            self.preserve_module_docstring = preserve_module_docstring
            self.binary_check = binary_check
            self.comments_removed = 0
            self.docstrings_removed = 0

        def leave_Comment(self, original: "cst.Comment", updated: "cst.Comment") -> "cst.Comment | cst.RemovalSentinel":
            if not self.strip_comments:
                return updated
            val = original.value
            if self.preserve_shebang and val.startswith("#!"):
                return updated
            if self.preserve_file_comments:
                low = val.lower()
                if any(k in low for k in FILE_COMMENT_KEYWORDS):
                    return updated
            self.comments_removed += 1
            return cst.RemoveFromParent()

        def _strip_body(self, body: Sequence["cst.BaseStatement"]) -> tuple["cst.BaseStatement", ...]:
            body = tuple(body)
            if not body:
                return body
            if _is_plain_string(body[0], self.binary_check):
                self.docstrings_removed += 1
                return body[1:]
            return body

        def leave_Module(self, original: "cst.Module", updated: "cst.Module") -> "cst.Module":
            if not self.strip_docstrings or self.preserve_module_docstring:
                return updated
            new_body = self._strip_body(updated.body)
            if not new_body:
                new_body = (cst.SimpleStatementLine(body=[cst.Pass()]),)
            return updated.with_changes(body=new_body)

        def leave_ClassDef(self, original: "cst.ClassDef", updated: "cst.ClassDef") -> "cst.ClassDef":
            if not self.strip_docstrings:
                return updated
            if isinstance(updated.body, cst.IndentedBlock):
                new_body = self._strip_body(updated.body.body)
                if not new_body:
                    new_body = (cst.SimpleStatementLine(body=[cst.Pass()]),)
                return updated.with_changes(body=updated.body.with_changes(body=new_body))
            if isinstance(updated.body, cst.SimpleStatementSuite):
                body = tuple(updated.body.body)
                if body and _is_plain_string(_wrap_suite(body[0]), self.binary_check):
                    self.docstrings_removed += 1
                    body = body[1:]
                if not body:
                    body = (cst.Pass(),)
                return updated.with_changes(body=updated.body.with_changes(body=body))
            return updated

        def leave_FunctionDef(self, original: "cst.FunctionDef", updated: "cst.FunctionDef") -> "cst.FunctionDef":
            if not self.strip_docstrings:
                return updated
            if isinstance(updated.body, cst.IndentedBlock):
                new_body = self._strip_body(updated.body.body)
                if not new_body:
                    new_body = (cst.SimpleStatementLine(body=[cst.Pass()]),)
                return updated.with_changes(body=updated.body.with_changes(body=new_body))
            if isinstance(updated.body, cst.SimpleStatementSuite):
                body = tuple(updated.body.body)
                if body and _is_plain_string(_wrap_suite(body[0]), self.binary_check):
                    self.docstrings_removed += 1
                    body = body[1:]
                if not body:
                    body = (cst.Pass(),)
                return updated.with_changes(body=updated.body.with_changes(body=body))
            return updated

    def _wrap_suite(stmt: "cst.BaseSmallStatement") -> "cst.SimpleStatementLine":
        return cst.SimpleStatementLine(body=[stmt])

    class _LibCSTResult(NamedTuple):
        path: Path
        comments_removed: int
        docstrings_removed: int
        error: str | None
        changed: bool

    def _libcst_process_file(
        args: tuple[
            Path,
            bool,
            bool,
            bool,
            bool,
            bool,
            bool,
            bool,
        ],
    ) -> _LibCSTResult:
        (
            path,
            strip_comments,
            strip_docstrings,
            preserve_shebang,
            preserve_file_comments,
            preserve_module_docstring,
            binary_check,
            dry_run,
        ) = args
        src, err = safe_read_text(path)
        if err:
            return _LibCSTResult(path, 0, 0, err, False)
        assert src is not None
        if not src.strip():
            return _LibCSTResult(path, 0, 0, None, False)
        try:
            module = cst.parse_module(src)
        except Exception as exc:
            return _LibCSTResult(path, 0, 0, f"CST parse error: {exc}", False)
        stripper = _LibCSTStripper(
            strip_comments=strip_comments,
            strip_docstrings=strip_docstrings,
            preserve_shebang=preserve_shebang,
            preserve_file_comments=preserve_file_comments,
            preserve_module_docstring=preserve_module_docstring,
            binary_check=binary_check,
        )
        new_module = module.visit(stripper)
        out = new_module.code
        if preserve_shebang and src.startswith("#!") and not out.startswith("#!"):
            first_line = src.splitlines(keepends=True)[0]
            out = first_line + out
        if out == src:
            return _LibCSTResult(path, 0, 0, None, False)
        try:
            ast.parse(out)
        except SyntaxError as exc:
            return _LibCSTResult(path, 0, 0, f"Result failed AST validation: {exc}", False)
        if not dry_run:
            try:
                write_text_preserving_newlines(path, out)
            except Exception as exc:
                return _LibCSTResult(path, 0, 0, f"write-error: {exc}", False)
        return _LibCSTResult(path, stripper.comments_removed, stripper.docstrings_removed, None, True)

    def cmd_libcst(args: argparse.Namespace) -> int:
        if not _HAS_LIBCST:
            print(
                "Error: 'libcst' is required for this subcommand. pip install libcst",
                file=sys.stderr,
            )
            return 2
        files = gather_python_files(args.paths, args.exclude, include_shebang_scripts=True)
        if not files:
            print("No Python files found.")
            return 0
        print(f"Processing {len(files)} Python file(s) with libcst ...")
        jobs = [
            (
                f,
                not args.no_comments,
                not args.no_docstrings,
                not args.no_shebang,
                not args.no_file_comments,
                args.preserve_module_docstring,
                not args.no_binary_guard,
                args.dry_run,
            )
            for f in files
        ]
        total_c = total_d = total_changed = total_err = 0
        try:
            ctx = mp.get_context("spawn")
            with ctx.Pool(processes=max(1, min(MAX_WORKERS, len(jobs)))) as pool:
                for res in pool.imap_unordered(_libcst_process_file, jobs):
                    _libcst_report(res)
                    total_c += res.comments_removed
                    total_d += res.docstrings_removed
                    total_changed += int(res.changed)
                    total_err += int(bool(res.error))
        except Exception as exc:
            log_error(f"Pool failed ({exc}); falling back to serial.")
            for job in jobs:
                res = _libcst_process_file(job)
                _libcst_report(res)
                total_c += res.comments_removed
                total_d += res.docstrings_removed
                total_changed += int(res.changed)
                total_err += int(bool(res.error))
        _libcst_summary(len(files), total_changed, total_c, total_d, total_err, args.dry_run)
        return 2 if total_err else 0

    def _libcst_report(res: _LibCSTResult) -> None:
        if res.error:
            print(f"[ERROR] {res.path}: {res.error}")
        elif res.changed:
            tag = "UPDATED"
            print(f"[{tag}] {res.path} -> {res.comments_removed} comment(s), {res.docstrings_removed} docstring(s)")

    def _libcst_summary(n: int, changed: int, c: int, d: int, errs: int, dry: bool) -> None:
        print("-" * 60)
        print(f"Files scanned       : {n}")
        print(f"Files {'would change' if dry else 'updated'}   : {changed}")
        print(f"Comments removed    : {c}")
        print(f"Docstrings removed  : {d}")
        if errs:
            print(f"Errors              : {errs}")


class _DocstringStripper(ast.NodeTransformer):
    def __init__(self) -> None:
        super().__init__()
        self.docstrings_removed = 0

    def _strip_first(self, node: ast.AST) -> ast.AST:
        body = getattr(node, "body", None)
        if not body:
            return node
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(getattr(first, "value", None), ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
            self.docstrings_removed += 1
            if not body:
                body.append(ast.Pass())
        return node

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self.generic_visit(node)
        return self._strip_first(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        return self._strip_first(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        return self._strip_first(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self.generic_visit(node)
        return self._strip_first(node)


def _extract_kept_comments(src: str, keep_noqa: bool) -> tuple[dict[int, list[str]], int]:
    kept: dict[int, list[str]] = {}
    hash_only = 0
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in toks:
            if tok.type != tokenize.COMMENT:
                continue
            low = tok.string.lower()
            is_kept = "type:" in low or "fmt:" in low or (keep_noqa and "noqa" in low)
            if is_kept:
                kept.setdefault(tok.start[0], []).append(tok.string)
            else:
                hash_only += 1
    except tokenize.TokenError:
        pass
    return kept, hash_only


def _reapply_kept_comments(new_src: str, kept: dict[int, list[str]]) -> str:
    if not kept:
        return new_src
    lines = new_src.splitlines()
    inserted: set[tuple[int, str]] = set()
    for lineno in sorted(kept):
        idx = lineno - 1
        if 0 <= idx < len(lines):
            for comment in kept[lineno]:
                if comment not in lines[idx]:
                    lines[idx] = (lines[idx] + "  " + comment) if lines[idx].strip() else comment
                inserted.add((lineno, comment))
    for lineno in sorted(kept):
        for comment in kept[lineno]:
            if (lineno, comment) not in inserted:
                lines.append(comment)
    out = "\n".join(lines)
    if new_src.endswith("\n") and not out.endswith("\n"):
        out += "\n"
    return out


class _ASTResult(NamedTuple):
    path: Path
    changed: bool
    error: str | None
    docstrings_removed: int
    comments_removed: int


def _ast_process_file(args: tuple[Path, str, bool, bool, bool, bool]) -> _ASTResult:
    path, unparser, preserve_shebang, preserve_file_comments, keep_noqa, dry_run = args
    src, err = safe_read_text(path)
    if err:
        return _ASTResult(path, False, err, 0, 0)
    assert src is not None
    if not src.strip():
        return _ASTResult(path, False, None, 0, 0)
    if preserve_shebang or preserve_file_comments:
        header, _body = split_leading_comments(src)
    else:
        header = ""
    kept_comments, comment_count = _extract_kept_comments(src, keep_noqa)
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return _ASTResult(path, False, f"syntax-error-original: {exc}", 0, 0)
    stripper = _DocstringStripper()
    tree = stripper.visit(tree)
    ast.fix_missing_locations(tree)
    try:
        if unparser == "astor":
            if not _HAS_ASTOR:
                return _ASTResult(path, False, "astor not installed", 0, 0)
            new_code = astor.to_source(tree)
        else:
            new_code = ast.unparse(tree)
    except Exception as exc:
        return _ASTResult(path, False, f"unparse-failed: {exc}", 0, 0)
    if header:
        if not header.endswith("\n"):
            header += "\n"
        new_code = header + new_code
    new_code = _reapply_kept_comments(new_code, kept_comments)
    if not new_code.endswith("\n"):
        new_code += "\n"
    try:
        ast.parse(new_code)
    except SyntaxError as exc:
        return _ASTResult(path, False, f"syntax-error-transformed: {exc}", 0, 0)
    if new_code == src:
        return _ASTResult(path, False, None, 0, 0)
    if not dry_run:
        try:
            write_text_preserving_newlines(path, new_code)
        except Exception as exc:
            return _ASTResult(path, False, f"write-error: {exc}", 0, 0)
    return _ASTResult(path, True, None, stripper.docstrings_removed, comment_count)


def cmd_ast(args: argparse.Namespace) -> int:
    if args.unparser == "astor" and not _HAS_ASTOR:
        print("Error: astor not installed. pip install astor", file=sys.stderr)
        return 2
    files = gather_python_files(args.paths, args.exclude, include_shebang_scripts=True)
    if not files:
        print("No Python files found.")
        return 0
    print(f"Processing {len(files)} Python file(s) with ast ({args.unparser}) ...")
    jobs = [
        (
            f,
            args.unparser,
            not args.no_shebang,
            not args.no_file_comments,
            args.keep_noqa,
            args.dry_run,
        )
        for f in files
    ]
    ctx = mp.get_context("spawn")
    total_d = total_c = changed = errs = 0
    with ctx.Pool(processes=max(1, min(MAX_WORKERS, len(jobs)))) as pool:
        for res in pool.imap_unordered(_ast_process_file, jobs):
            if res.error:
                print(f"[ERROR] {res.path}: {res.error}")
                errs += 1
            elif res.changed:
                print(f"[UPDATED] {res.path} ({res.docstrings_removed} docstring(s))")
                total_d += res.docstrings_removed
                total_c += res.comments_removed
                changed += 1
    print("-" * 60)
    print(f"Files scanned      : {len(files)}")
    print(f"Files {'would change' if args.dry_run else 'updated'}  : {changed}")
    print(f"Docstrings removed : {total_d}")
    print(f"Comments removed   : {total_c}")
    if errs:
        print(f"Errors             : {errs}")
    return 2 if errs else 0


_LANG_EXTS: dict[str, str] = {
    "c": "c",
    "cpp": "cpp",
    "h": "h",
    "hpp": "hpp",
    "py": "py",
    "sh": "sh",
}


def _regex_strip_text(src: str, ext: str, keep_strings: bool) -> str:
    if ext in {"c", "cpp", "h", "hpp"}:
        src = re.sub(r"//.*", "", src)
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
        if not keep_strings:
            src = re.sub(r'"[^"]*"', "", src)
            src = re.sub(r"'[^']*'", "", src)
    elif ext == "py":
        src = re.sub(r"#.*", "", src)
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"'''[\s\S]*?'''", "", src)
        if not keep_strings:
            src = re.sub(r'"[^"]*"', "", src)
            src = re.sub(r"'[^']*'", "", src)
    elif ext == "sh":
        src = re.sub(r"#.*", "", src)
        if not keep_strings:
            src = re.sub(r'"[^"]*"', "", src)
            src = re.sub(r"'[^']*'", "", src)
    return src


class _RegexResult(NamedTuple):
    path: Path
    changed: bool
    error: str | None


def _regex_process_file(args: tuple[Path, str, bool, bool, bool, bool]) -> _RegexResult:
    path, lang_override, keep_strings, hash_only, dry_run, validate_python = args
    src, err = safe_read_text(path)
    if err:
        return _RegexResult(path, False, err)
    assert src is not None
    ext = lang_override or path.suffix.lstrip(".").lower()
    if hash_only:
        new = re.sub(r"#.*", "", src)
        new = re.sub(r"\n\n*", "\n", new)
    else:
        new = _regex_strip_text(src, ext, keep_strings)
    if new == src:
        return _RegexResult(path, False, None)
    if validate_python and ext == "py":
        try:
            ast.parse(new)
        except SyntaxError as exc:
            return _RegexResult(path, False, f"invalid result: {exc}")
    if not dry_run:
        try:
            write_text_preserving_newlines(path, new)
        except Exception as exc:
            return _RegexResult(path, False, f"write-error: {exc}")
    return _RegexResult(path, True, None)


def cmd_regex(args: argparse.Namespace) -> int:
    if args.lang == "all":
        files = gather_python_files(args.paths, args.exclude, include_shebang_scripts=True)
        for p in args.paths:
            root = Path(p).resolve()
            if root.is_dir():
                for suffix in ("*.c", "*.cpp", "*.h", "*.hpp", "*.sh"):
                    files.extend(f for f in root.rglob(suffix) if f.is_file())
        files = sorted(set(files))
    else:
        files = gather_python_files(args.paths, args.exclude, include_shebang_scripts=True)
        if args.lang != "py":
            for p in args.paths:
                root = Path(p).resolve()
                if root.is_dir():
                    files.extend(f for f in root.rglob(f"*.{args.lang}") if f.is_file())
            files = sorted(set(files))
    if not files:
        print("No matching files found.")
        return 0
    lang_override = args.lang if args.lang not in {"all", "auto"} else ""
    print(f"Processing {len(files)} file(s) with regex ...")
    jobs = [(f, lang_override, args.keep_strings, args.hash_only, args.dry_run, True) for f in files]
    ctx = mp.get_context("spawn")
    changed = errs = 0
    with ctx.Pool(processes=max(1, min(MAX_WORKERS, len(jobs)))) as pool:
        for res in pool.imap_unordered(_regex_process_file, jobs):
            if res.error:
                print(f"[ERROR] {res.path}: {res.error}")
                errs += 1
            elif res.changed:
                print(f"[UPDATED] {res.path}")
                changed += 1
    print("-" * 60)
    print(f"Files scanned : {len(files)}")
    print(f"Files changed : {changed}")
    if errs:
        print(f"Errors        : {errs}")
    return 2 if errs else 0


class _UnusedCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.func_defs: set[str] = set()
        self.class_defs: set[str] = set()
        self.var_defs: set[str] = set()
        self.var_uses: set[str] = set()
        self.func_calls: set[str] = set()
        self.class_uses: set[str] = set()
        self.imports: dict[str, ast.AST] = {}
        self.import_uses: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.func_defs.add(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_defs.add(node.name)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(getattr(node, "parent", None), ast.Module):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    self.var_defs.add(tgt.id)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.var_uses.add(node.id)
            self.func_calls.add(node.id)
            self.class_uses.add(node.id)
            self.import_uses.add(node.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports[alias.asname or alias.name] = node
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports[alias.asname or alias.name] = node
        self.generic_visit(node)


def _attach_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node


def _collect_unused(src: str) -> tuple[dict[str, object], list[str]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}, ["SyntaxError while parsing file"]
    _attach_parents(tree)
    c = _UnusedCollector()
    c.visit(tree)
    info: dict[str, object] = {
        "functions": sorted(c.func_defs - c.func_calls),
        "classes": sorted(c.class_defs - c.class_uses),
        "variables": sorted(c.var_defs - c.var_uses),
        "imports": {k: v for k, v in c.imports.items() if k not in c.import_uses},
    }
    return info, []


def _rewrite_without_unused(src: str, info: dict[str, object]) -> str:
    tree = ast.parse(src)
    _attach_parents(tree)
    unused_funcs = set(info["functions"])
    unused_classes = set(info["classes"])
    unused_vars = set(info["variables"])
    unused_imports: dict = info["imports"]
    kept: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in unused_funcs:
            continue
        if isinstance(stmt, ast.ClassDef) and stmt.name in unused_classes:
            continue
        if isinstance(stmt, ast.Assign) and isinstance(getattr(stmt, "parent", None), ast.Module):
            names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if names and all(n in unused_vars for n in names):
                continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            alias_names = [a.asname or a.name for a in stmt.names]
            if all(n in unused_imports for n in alias_names):
                continue
        kept.append(stmt)
    tree.body = kept
    return ast.unparse(tree)


class _UnusedResult(NamedTuple):
    path: Path
    info: dict
    errors: list[str]
    changed: bool


def _unused_process_file(args: tuple[Path, bool]) -> _UnusedResult:
    path, dry_run = args
    src, err = safe_read_text(path)
    if err:
        return _UnusedResult(path, {}, [err], False)
    assert src is not None
    info, errs = _collect_unused(src)
    if not info or not any(info.values()):
        return _UnusedResult(path, info, errs, False)
    try:
        new_code = _rewrite_without_unused(src, info)
    except Exception:
        errs.append("Error rewriting file:\n" + traceback.format_exc())
        return _UnusedResult(path, info, errs, False)
    if not dry_run:
        try:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
            write_text_preserving_newlines(path, new_code)
        except Exception as exc:
            return _UnusedResult(path, info, [f"write-error: {exc}"], False)
    return _UnusedResult(path, info, errs, True)


def cmd_unused(args: argparse.Namespace) -> int:
    files = gather_python_files(args.paths, args.exclude)
    if not files:
        print("No Python files found.")
        return 0
    print(f"Scanning {len(files)} Python file(s) for unused definitions ...")
    jobs = [(f, args.dry_run) for f in files]
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=max(1, min(MAX_WORKERS, len(jobs)))) as pool:
        results = list(pool.imap_unordered(_unused_process_file, jobs))
    print("\n=== RESULTS ===\n")
    for res in results:
        has_unused = any(res.info.values()) if res.info else False
        if has_unused:
            tag = "[DRY-RUN]" if args.dry_run else "[UPDATED]"
            print(f"{tag} {res.path}")
            for cat in ("functions", "classes", "variables", "imports"):
                vals = res.info.get(cat)
                if vals:
                    label = "imports" if cat == "imports" else f"Unused {cat}"
                    print(f"  {label}: {list(vals) if cat == 'imports' else vals}")
        for err in res.errors:
            print(f"[ERROR] {res.path}: {err}")
    return 0


def _detect_jtc_language(path: Path, override: str) -> str | None:
    if override == "python":
        return "python"
    if override == "rust":
        return None
    if override == "auto":
        return "python" if path.suffix == ".py" else None
    return None


def _run_just_the_code(path: Path, language: str | None) -> tuple[bool, str | None]:
    cmd = ["just-the-code", "-s"]
    if language:
        cmd.append(f"--language={language}")
    cmd.append(str(path))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "'just-the-code' executable not found on PATH"
    if res.returncode != 0:
        return False, res.stderr.strip() or f"exit code {res.returncode}"
    return True, res.stdout


class _JTCResult(NamedTuple):
    path: Path
    changed: bool
    error: str | None


def _jtc_process_file(args: tuple[Path, str, bool]) -> _JTCResult:
    path, language_override, dry_run = args
    lang = _detect_jtc_language(path, language_override)
    ok, out = _run_just_the_code(path, lang)
    if not ok:
        return _JTCResult(path, False, out)
    assert out is not None
    if path.suffix == ".py":
        try:
            ast.parse(out)
        except SyntaxError as exc:
            return _JTCResult(path, False, f"just-the-code produced invalid Python: {exc}")
    src, _err = safe_read_text(path)
    if src == out:
        return _JTCResult(path, False, None)
    if not dry_run:
        try:
            write_text_preserving_newlines(path, out)
        except Exception as exc:
            return _JTCResult(path, False, f"write-error: {exc}")
    return _JTCResult(path, True, None)


def cmd_jtc(args: argparse.Namespace) -> int:
    if args.language == "python":
        files = gather_python_files(args.paths, args.exclude, include_shebang_scripts=True)
    else:
        files = []
        for p in args.paths:
            root = Path(p).resolve()
            if root.is_file():
                files.append(root)
            elif root.is_dir():
                files.extend(f for f in root.rglob("*") if f.suffix in {".py", ".rs"} and f.is_file())
        files = sorted(set(files))
    if not files:
        print("No matching files found.")
        return 0
    print(f"Running just-the-code on {len(files)} file(s) ...")
    jobs = [(f, args.language, args.dry_run) for f in files]
    ctx = mp.get_context("spawn")
    changed = errs = 0
    with ctx.Pool(processes=max(1, min(MAX_WORKERS, len(jobs)))) as pool:
        for res in pool.imap_unordered(_jtc_process_file, jobs):
            if res.error:
                print(f"[ERROR] {res.path}: {res.error}")
                errs += 1
            elif res.changed:
                print(f"[UPDATED] {res.path}")
                changed += 1
    print("-" * 60)
    print(f"Files scanned : {len(files)}")
    print(f"Files changed : {changed}")
    if errs:
        print(f"Errors        : {errs}")
    return 2 if errs else 0


def _add_common_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="*", default=["."], help="Files or directories (default: cwd).")
    p.add_argument(
        "--exclude",
        nargs="*",
        default=sorted(DEFAULT_EXCLUDES),
        help="Directory names to skip (default: VCS/build/cache dirs).",
    )
    p.add_argument("--dry-run", action="store_true", help="Report changes without writing.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged.py",
        description="Unified Python comment/docstring/unused-code cleaner.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_libcst = sub.add_parser(
        "libcst",
        help="Strip comments/docstrings using libcst (aremci/cleanpy2/grmc/rrmc).",
    )
    _add_common_options(p_libcst)
    p_libcst.add_argument("--no-comments", action="store_true", help="Do not remove comments.")
    p_libcst.add_argument("--no-docstrings", action="store_true", help="Do not remove docstrings.")
    p_libcst.add_argument("--no-shebang", action="store_true", help="Also strip a leading #! line.")
    p_libcst.add_argument(
        "--no-file-comments",
        action="store_true",
        help="Also strip # fmt:, # type:, # coding:, # encoding: comments.",
    )
    p_libcst.add_argument(
        "--preserve-module-docstring",
        action="store_true",
        help="Keep the module-level docstring.",
    )
    p_libcst.add_argument(
        "--no-binary-guard",
        action="store_true",
        help="Do not special-case bytes-literals when stripping docstrings.",
    )
    p_libcst.set_defaults(func=cmd_libcst)
    p_ast = sub.add_parser(
        "ast",
        help="Strip comments/docstrings using ast + ast.unparse (cormc) or astor (rmco).",
    )
    _add_common_options(p_ast)
    p_ast.add_argument(
        "--unparser",
        choices=("ast", "astor"),
        default="ast",
        help="Source unparser backend (default: ast).",
    )
    p_ast.add_argument("--no-shebang", action="store_true", help="Do not preserve the shebang line.")
    p_ast.add_argument(
        "--no-file-comments",
        action="store_true",
        help="Do not preserve # fmt:/# type:/# coding:/# encoding: comments.",
    )
    p_ast.add_argument("--keep-noqa", action="store_true", help="Attempt to preserve # noqa comments.")
    p_ast.set_defaults(func=cmd_ast)
    p_reg = sub.add_parser(
        "regex",
        help="Regex-based stripping (pyjtc for multi-lang, rmmc for '#...' only).",
    )
    _add_common_options(p_reg)
    p_reg.add_argument(
        "--lang",
        choices=("py", "c", "cpp", "h", "hpp", "sh", "all", "auto"),
        default="py",
        help="Language (default: py).",
    )
    p_reg.add_argument(
        "--keep-strings",
        action="store_true",
        help="Do NOT remove string literals (pyjtc -s).",
    )
    p_reg.add_argument(
        "--hash-only",
        action="store_true",
        help="rmmc mode: only strip '#...' and collapse blank lines.",
    )
    p_reg.set_defaults(func=cmd_regex)
    p_un = sub.add_parser("unused", help="Remove unused functions/classes/variables/imports (clean_py).")
    _add_common_options(p_un)
    p_un.set_defaults(func=cmd_unused)
    p_jtc = sub.add_parser("jtc", help="Run 'just-the-code' on files (jtc / jtc2).")
    _add_common_options(p_jtc)
    p_jtc.add_argument(
        "--language",
        choices=("python", "rust", "auto"),
        default="auto",
        help="Language to pass to just-the-code (default: auto by extension).",
    )
    p_jtc.set_defaults(func=cmd_jtc)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "paths", None):
        args.paths = ["."]
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
