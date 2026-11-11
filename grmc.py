#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments, docstrings and type annotations of Python files.
``-c`` removes the comments (keeping the shebang, ``# type:`` and ``# fmt:``), ``-d`` the docstrings except the module one, ``-a`` everything and ``-t`` the annotations; the backend is LibCST, ast or tree-sitter (``-b``) and the output is validated before it is written.
Usage: grmc [-c] [-d] [-a] [-t] [-b BACKEND] [paths ...] Positional arguments: paths ...
Files or directories to process (default: current directory).
Options: -c, --comments Remove all comments except shebang, # type:, and # fmt:.
-d, --docstring Remove all docstrings except the module-level docstring.
-a, --all Remove all comments and docstrings, including shebang, module docstring, # type:, and # fmt:.
-t, --type Remove type annotations.
-b, --backend Select parsing backend (default: libcst).
Examples: $ grmc -c -t src/ Command-line script: run ``python grmc.py`` or the ``grmc`` command.
The module is importable as well."""

import argparse
import ast
import dataclasses
from dataclasses import dataclass
import multiprocessing
import os
from pathlib import Path
import sys
from typing import Any

from atomicio import atomic_write_file
from loguru import logger

from dh import MAX_WORKERS, SKIP_DIRS

try:
    import libcst as cst

    HAS_LIBCST = True
except ImportError:
    cst = None
    HAS_LIBCST = False

try:
    from tree_sitter import Language, Parser
    import tree_sitter_python as tspython

    HAS_TREESITTER = True
except ImportError:
    Parser = None
    Language = None
    tspython = None
    HAS_TREESITTER = False


logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<=8}</level> | <cyan>{message}</cyan>",
    level="INFO",
)


@dataclass(slots=True, frozen=True)
class StripOptions:
    backend: str = "libcst"
    remove_comments: bool = False
    inline_comments_only: bool = True
    remove_docstrings: bool = False
    preserve_module_docstring: bool = True
    remove_types: bool = False
    remove_all: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> StripOptions:
        remove_all = getattr(args, "all", False)
        remove_comments = getattr(args, "comments", False) or remove_all
        remove_docstrings = getattr(args, "docstring", False) or remove_all
        preserve_module_docstring = not (getattr(args, "docstring", False) or remove_all)
        inline_comments_only = not remove_comments and not remove_all

        return cls(
            backend=getattr(args, "backend", "libcst"),
            remove_comments=remove_comments,
            inline_comments_only=inline_comments_only,
            remove_docstrings=remove_docstrings,
            preserve_module_docstring=preserve_module_docstring,
            remove_types=getattr(args, "type", False),
            remove_all=remove_all,
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StripOptions:
        return cls(**data)


class ASTStripper(ast.NodeTransformer):
    def __init__(self, options: StripOptions) -> None:
        super().__init__()
        self.options = options

    @staticmethod
    def _is_docstring_node(node: ast.AST) -> bool:
        return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)

    def visit_Module(self, node: ast.Module) -> ast.Module:
        new_body: list[ast.stmt] = []
        for idx, stmt in enumerate(node.body):
            if idx == 0 and self._is_docstring_node(stmt):
                if self.options.preserve_module_docstring and not self.options.remove_all:
                    new_body.append(self.visit(stmt))
                else:
                    continue
            elif self._is_docstring_node(stmt) and (self.options.remove_docstrings or self.options.remove_all):
                continue
            else:
                visited = self.visit(stmt)
                if visited is not None:
                    new_body.append(visited)

        node.body = new_body
        return node

    def _clean_body(self, body: list[ast.stmt]) -> list[ast.stmt]:
        new_body: list[ast.stmt] = []
        for idx, stmt in enumerate(body):
            if (
                idx == 0
                and (self.options.remove_docstrings or self.options.remove_all)
                and self._is_docstring_node(stmt)
            ):
                continue

            if (self.options.remove_types or self.options.remove_all) and isinstance(stmt, ast.AnnAssign):
                if stmt.value is not None:
                    visited_target = self.visit(stmt.target)
                    visited_value = self.visit(stmt.value)
                    new_body.append(ast.Assign(targets=[visited_target], value=visited_value))
                else:
                    continue
            else:
                visited = self.visit(stmt)
                if visited is not None:
                    new_body.append(visited)

        if not new_body:
            new_body = [ast.Pass()]
        return new_body

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if self.options.remove_types or self.options.remove_all:
            node.returns = None
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                arg.annotation = None
            if node.args.vararg:
                node.args.vararg.annotation = None
            if node.args.kwarg:
                node.args.kwarg.annotation = None

        node.body = self._clean_body(node.body)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if self.options.remove_types or self.options.remove_all:
            node.returns = None
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                arg.annotation = None
            if node.args.vararg:
                node.args.vararg.annotation = None
            if node.args.kwarg:
                node.args.kwarg.annotation = None

        node.body = self._clean_body(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.body = self._clean_body(node.body)
        return node


def transform_ast(source: str, options: StripOptions) -> str:
    lines = source.splitlines()
    shebang: str | None = None
    if lines and lines[0].startswith("#!") and not options.remove_all:
        shebang = lines[0]

    tree = ast.parse(source)
    stripper = ASTStripper(options)
    transformed_tree = stripper.visit(tree)
    ast.fix_missing_locations(transformed_tree)

    result = ast.unparse(transformed_tree)
    if shebang:
        result = shebang + "\n" + result
    return result


if HAS_LIBCST:

    class LibCSTStripper(cst.CSTTransformer):
        def __init__(self, options: StripOptions) -> None:
            super().__init__()
            self.options = options

        @staticmethod
        def _is_special_comment(comment_text: str) -> bool:
            clean = comment_text.strip()
            return clean.startswith(("# type:", "# fmt:", "# type :"))

        @staticmethod
        def _is_docstring_stmt(node: cst.CSTNode) -> bool:
            if isinstance(node, cst.SimpleStatementLine) and len(node.body) == 1:
                expr = node.body[0]
                if isinstance(expr, cst.Expr):
                    val = expr.value
                    return isinstance(val, (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString))
            return False

        def leave_TrailingWhitespace(
            self, original_node: cst.TrailingWhitespace, updated_node: cst.TrailingWhitespace
        ) -> cst.TrailingWhitespace:
            if updated_node.comment is not None:
                c_text = updated_node.comment.value
                if self.options.remove_all:
                    return updated_node.with_changes(comment=None)
                if self._is_special_comment(c_text):
                    return updated_node
                return updated_node.with_changes(comment=None)
            return updated_node

        def leave_EmptyLine(self, original_node: cst.EmptyLine, updated_node: cst.EmptyLine) -> cst.EmptyLine:
            if updated_node.comment is not None:
                c_text = updated_node.comment.value
                if self.options.remove_all:
                    return updated_node.with_changes(comment=None)
                if c_text.startswith("#!"):
                    return updated_node
                if self._is_special_comment(c_text):
                    return updated_node
                if self.options.remove_comments:
                    return updated_node.with_changes(comment=None)
            return updated_node

        def leave_Param(self, original_node: cst.Param, updated_node: cst.Param) -> cst.Param:
            if self.options.remove_types or self.options.remove_all:
                return updated_node.with_changes(annotation=None)
            return updated_node

        def _clean_statements(self, stmts: list[cst.CSTNode], is_module: bool = False) -> list[cst.CSTNode]:
            new_stmts: list[cst.CSTNode] = []
            for idx, stmt in enumerate(stmts):
                if idx == 0 and is_module:
                    if self._is_docstring_stmt(stmt):
                        if self.options.preserve_module_docstring and not self.options.remove_all:
                            new_stmts.append(stmt)
                            continue
                        else:
                            continue

                elif idx == 0 and not is_module:
                    if self._is_docstring_stmt(stmt) and (self.options.remove_docstrings or self.options.remove_all):
                        continue

                if (
                    idx > 0
                    and self._is_docstring_stmt(stmt)
                    and (self.options.remove_docstrings or self.options.remove_all)
                ):
                    continue

                if (
                    (self.options.remove_types or self.options.remove_all)
                    and isinstance(stmt, cst.SimpleStatementLine)
                    and len(stmt.body) == 1
                ):
                    small = stmt.body[0]
                    if isinstance(small, cst.AnnAssign):
                        if small.value is not None:
                            assign = cst.Assign(
                                targets=[cst.AssignTarget(target=small.target)],
                                value=small.value,
                            )
                            new_stmts.append(stmt.with_changes(body=[assign]))
                            continue
                        else:
                            continue

                new_stmts.append(stmt)

            if not new_stmts and not is_module:
                new_stmts = [cst.SimpleStatementLine(body=[cst.Pass()])]

            return new_stmts

        def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
            new_body = self._clean_statements(list(updated_node.body), is_module=True)
            return updated_node.with_changes(body=new_body)

        def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
            changes: dict[str, Any] = {}
            if self.options.remove_types or self.options.remove_all:
                changes["returns"] = None

            if isinstance(updated_node.body, cst.IndentedBlock):
                new_body = self._clean_statements(list(updated_node.body.body), is_module=False)
                changes["body"] = updated_node.body.with_changes(body=new_body)

            return updated_node.with_changes(**changes)

        def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
            if isinstance(updated_node.body, cst.IndentedBlock):
                new_body = self._clean_statements(list(updated_node.body.body), is_module=False)
                return updated_node.with_changes(body=updated_node.body.with_changes(body=new_body))
            return updated_node


def transform_libcst(source: str, options: StripOptions) -> str:
    if not HAS_LIBCST:
        msg = "libcst library is not installed."
        raise RuntimeError(msg)
    cst_tree = cst.parse_module(source)
    stripper = LibCSTStripper(options)
    transformed_tree = cst_tree.visit(stripper)
    return transformed_tree.code


def transform_tree_sitter(source: str, options: StripOptions) -> str:
    if not HAS_TREESITTER:
        msg = "tree-sitter or tree-sitter-python library is not installed."
        raise RuntimeError(msg)

    parser = Parser(Language(tspython.language()))
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)

    edits: list[tuple[int, int, bytes]] = []

    def is_special_comment(text: str) -> bool:
        s = text.strip()
        return s.startswith(("# type:", "# fmt:", "# type :"))

    def walk(node: Any, parent: Any = None, is_first: bool = False) -> None:
        if node.type == "comment":
            c_text = node.text.decode("utf-8", errors="replace")
            if options.remove_all:
                edits.append((node.start_byte, node.end_byte, b""))
            elif c_text.startswith("#!"):
                pass
            elif is_special_comment(c_text):
                pass
            elif options.remove_comments:
                edits.append((node.start_byte, node.end_byte, b""))
            else:
                line_start = source_bytes.rfind(b"\n", 0, node.start_byte)
                line_start = 0 if line_start == -1 else line_start + 1
                before_comment = source_bytes[line_start : node.start_byte].strip()
                if before_comment:
                    edits.append((node.start_byte, node.end_byte, b""))

        elif node.type == "expression_statement":
            if len(node.children) == 1 and node.children[0].type in ("string", "concatenated_string"):
                is_module_doc = parent and parent.type == "module" and is_first
                if is_module_doc:
                    if options.remove_all or (options.remove_docstrings and not options.preserve_module_docstring):
                        edits.append((node.start_byte, node.end_byte, b""))
                elif options.remove_docstrings or options.remove_all:
                    if parent and parent.type == "block" and len(parent.children) == 1:
                        edits.append((node.start_byte, node.end_byte, b"pass"))
                    else:
                        edits.append((node.start_byte, node.end_byte, b""))

        elif options.remove_types or options.remove_all:
            if node.type == "typed_parameter":
                name_node = node.child_by_field_name("name")
                if name_node:
                    edits.append((node.start_byte, node.end_byte, name_node.text))
            elif node.type == "type" and parent and parent.type == "function_definition":
                arrow = next((child for child in parent.children if child.type == "->"), None)
                start = arrow.start_byte if arrow else node.start_byte
                edits.append((start, node.end_byte, b""))

        for idx, child in enumerate(node.children):
            walk(child, parent=node, is_first=(idx == 0))

    walk(tree.root_node)

    res = bytearray(source_bytes)
    edits.sort(key=lambda x: x[0], reverse=True)
    last_start = len(res) + 1
    for start, end, repl in edits:
        if end <= last_start:
            res[start:end] = repl
            last_start = start

    return res.decode("utf-8", errors="replace")


def validate_transformed_code(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def read_file_safely(path: Path) -> str | None:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, PermissionError):
            continue
    return None


def is_python_file(path: Path) -> bool:
    if not path.is_file():
        return False

    if path.name.startswith(".") or path.suffix in (".pyc", ".pyo", ".so", ".dll", ".exe"):
        return False

    if path.suffix in (".py", ".pyw", ".pyi"):
        return True

    if path.suffix == "":
        try:
            with path.open("rb") as f:
                header = f.readline(256)
                if header.startswith(b"#!") and b"python" in header.lower():
                    return True
                f.seek(0)
                sample = f.read(4096).decode("utf-8", errors="ignore")
                ast.parse(sample)
                return True
        except Exception:
            return False

    return False


def discover_files(targets: list[Path]) -> list[Path]:
    found: list[Path] = []

    if not targets:
        targets = [Path()]

    for target in targets:
        if target.is_file():
            if is_python_file(target):
                found.append(target)
        elif target.is_dir():
            for root, dirs, files in os.walk(target):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
                for file_name in files:
                    file_path = Path(root) / file_name
                    if is_python_file(file_path):
                        found.append(file_path)

    return sorted(set(found))


def process_file_worker(args_tuple: tuple[str, dict[str, Any]]) -> tuple[str, str, str]:
    file_path_str, options_dict = args_tuple
    path = Path(file_path_str)
    options = StripOptions.from_dict(options_dict)

    source = read_file_safely(path)
    if source is None:
        return (file_path_str, "ERROR", "Failed to read file or incompatible encoding")

    backend = options.backend.lower()

    try:
        if backend == "libcst":
            if HAS_LIBCST:
                transformed = transform_libcst(source, options)
            else:
                transformed = transform_ast(source, options)
        elif backend == "tree-sitter":
            if HAS_TREESITTER:
                transformed = transform_tree_sitter(source, options)
            else:
                transformed = transform_ast(source, options)
        else:
            transformed = transform_ast(source, options)
    except Exception as exc:
        return (file_path_str, "ERROR", f"Transformation error: {exc}")

    if not validate_transformed_code(transformed):
        return (file_path_str, "ERROR", "Validation failed: transformed source is invalid Python syntax")

    if transformed == source:
        return (file_path_str, "SKIPPED", "No changes required")

    try:
        atomic_write_file(path, transformed)
    except Exception as exc:
        return (file_path_str, "ERROR", f"Failed to write transformed file: {exc}")

    return (file_path_str, "SUCCESS", "Successfully stripped")


def run_pipeline(paths: list[Path], options: StripOptions) -> None:
    actual_backend = options.backend
    if options.backend == "libcst" and not HAS_LIBCST:
        logger.warning("LibCST requested but not installed. Falling back to 'ast' backend.")
        actual_backend = "ast"
    elif options.backend == "tree-sitter" and not HAS_TREESITTER:
        logger.warning("Tree-sitter requested but not installed. Falling back to 'ast' backend.")
        actual_backend = "ast"

    files = discover_files(paths)
    if not files:
        logger.warning("No Python files found for processing.")
        return

    logger.info(
        f"Discovered {len(files)} Python file(s). Processing with 8 workers using backend '{actual_backend}'..."
    )

    options_dict = options.to_dict()
    tasks = [(str(p), options_dict) for p in files]

    async_results = []

    with multiprocessing.Pool(processes=MAX_WORKERS) as pool:
        for task in tasks:
            res = pool.apply_async(process_file_worker, (task,))
            async_results.append(res)

        pool.close()
        pool.join()

    success_count = 0
    skipped_count = 0
    error_count = 0

    for async_res in async_results:
        file_path, status, message = async_res.get()
        if status == "SUCCESS":
            success_count += 1
            logger.info(f"SUCCESS: {file_path}")
        elif status == "SKIPPED":
            skipped_count += 1
        else:
            error_count += 1
            logger.error(f"FAILURE: {file_path} - {message}")

    logger.info(f"Summary: {success_count} processed, {skipped_count} skipped, {error_count} failed.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strip comments, docstrings, and type annotations from Python source files."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process (default: current directory).",
    )
    parser.add_argument(
        "-c",
        "--comments",
        action="store_true",
        help="Remove all comments except shebang, # type:, and # fmt:.",
    )
    parser.add_argument(
        "-d",
        "--docstring",
        action="store_true",
        help="Remove all docstrings except the module-level docstring.",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Remove all comments and docstrings, including shebang, module docstring, # type:, and # fmt:.",
    )
    parser.add_argument(
        "-t",
        "--type",
        action="store_true",
        help="Remove type annotations.",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=["libcst", "tree-sitter", "ast"],
        default="libcst",
        help="Select parsing backend (default: libcst).",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    options = StripOptions.from_args(args)
    run_pipeline(args.paths, options)


if __name__ == "__main__":
    main()
