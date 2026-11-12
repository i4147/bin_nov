#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments and docstrings from Python files (LibCST, AST and tree-sitter engines)."""

import argparse
import ast as ast_module
import multiprocessing as mp
from pathlib import Path
import sys
from typing import Any, Sequence

from dh import MAX_WORKERS
from loguru import logger

try:
    import libcst as cst
    from libcst.metadata import MetadataWrapper, ParentNodeProvider

    HAS_LIBCST = True
except ImportError:
    HAS_LIBCST = False
try:
    from tree_sitter import Language, Parser

    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False
try:
    from loguru import logger

    HAS_LOGURU = True
except ImportError:
    HAS_LOGURU = False
SUPPORTED_BACKENDS: tuple[str, str, str] = ("libcst", "tree-sitter", "ast")
PRESERVED_COMMENTS: tuple[str, str] = ("# type", "# fmt")
SHEBANG_PREFIX: str = "#!"


def setup_logger(verbose: bool = False) -> None:
    if HAS_LOGURU:
        logger.remove()
        logger.add(
            sys.stderr,
            level="DEBUG" if verbose else "INFO",
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        )
    else:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Strip comments and docstrings from Python files.",
        epilog="If no paths are provided, processes all .py files recursively in current dir.",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=SUPPORTED_BACKENDS,
        default="libcst",
        help="Backend to use for parsing (default: libcst)",
    )
    parser.add_argument(
        "-c",
        "--comments",
        action="store_true",
        help="Remove all comments except shebangs, #type, and # fmt",
    )
    parser.add_argument(
        "-d",
        "--docstring",
        action="store_true",
        help="Remove all docstrings except module docstring",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Remove all comments and docstrings (even preserved ones)",
    )
    parser.add_argument(
        "-t",
        "--type",
        action="store_true",
        help="Remove type annotations",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to process (default: current directory)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def collect_python_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix == ".py" or _looks_like_python(path):
                files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.py"))
        else:
            logger.warning(f"Path does not exist: {path}")
    return sorted(set(files))


def _looks_like_python(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            return first_line.startswith("#!") or "python" in first_line.lower()
    except (UnicodeDecodeError, OSError):
        return False


def strip_comments_libcst(source: str, options: argparse.Namespace) -> str:
    if not HAS_LIBCST:
        msg = "libcst is required for this backend. Install with: pip install libcst"
        raise ImportError(msg)

    class CommentRemover(cst.CSTTransformer):
        METADATA_DEPENDENCIES = (ParentNodeProvider,)

        def __init__(self, options: argparse.Namespace) -> None:
            self.options = options

        def leave_Comment(
            self, original_node: cst.Comment, updated_node: cst.Comment
        ) -> cst.Comment | cst.RemovalSentinel:
            comment_text = original_node.value
            if comment_text.startswith(SHEBANG_PREFIX):
                return updated_node
            if not self.options.all and any(comment_text.startswith(p) for p in PRESERVED_COMMENTS):
                return updated_node
            if self.options.all or self.options.comments:
                return cst.RemoveFromParent()
            if self.options.docstring or self.options.type:
                return updated_node
            return cst.RemoveFromParent()

        def _remove_docstring_from_body(
            self, body: Sequence[cst.BaseStatement], is_module: bool
        ) -> Sequence[cst.BaseStatement]:
            if not body:
                return body
            first = body[0]
            if isinstance(first, cst.SimpleStatementLine):
                if len(first.body) == 1 and isinstance(first.body[0], cst.Expr):
                    expr = first.body[0]
                    if isinstance(expr.value, cst.SimpleString):
                        if is_module and not self.options.all:
                            return body
                        if self.options.all or self.options.docstring:
                            new_body = list(body[1:])
                            if not new_body:
                                new_body = [cst.SimpleStatementLine(body=[cst.Pass()])]
                            return new_body
            return body

        def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
            new_body = self._remove_docstring_from_body(updated_node.body, True)
            return updated_node.with_changes(body=new_body)

        def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
            new_body = self._remove_docstring_from_body(updated_node.body.body, False)
            new_body_stmt = updated_node.body.with_changes(body=new_body)
            return updated_node.with_changes(body=new_body_stmt)

        def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
            new_body = self._remove_docstring_from_body(updated_node.body.body, False)
            new_body_stmt = updated_node.body.with_changes(body=new_body)
            if self.options.type:
                updated_node = updated_node.with_changes(returns=None)
            return updated_node.with_changes(body=new_body_stmt)

        def leave_Param(self, original_node: cst.Param, updated_node: cst.Param) -> cst.Param:
            if self.options.type:
                return updated_node.with_changes(annotation=None)
            return updated_node

        def leave_AnnAssign(
            self, original_node: cst.AnnAssign, updated_node: cst.AnnAssign
        ) -> cst.BaseSmallStatement | cst.RemovalSentinel:
            if self.options.type:
                if updated_node.value is not None:
                    return cst.Assign(
                        targets=[cst.AssignTarget(updated_node.target)],
                        value=updated_node.value,
                    )
                else:
                    return cst.RemoveFromParent()
            return updated_node

    wrapper = MetadataWrapper(cst.parse_module(source))
    transformer = CommentRemover(options)
    modified_tree = wrapper.visit(transformer)
    return modified_tree.code


class AstTransformer(ast_module.NodeTransformer):
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options

    def _remove_docstring(self, node: ast_module.AST) -> None:
        if not (self.options.docstring or self.options.all):
            return
        if not hasattr(node, "body") or not node.body:
            return
        first = node.body[0]
        if (
            isinstance(first, ast_module.Expr)
            and isinstance(first.value, ast_module.Constant)
            and isinstance(first.value.value, str)
        ):
            if isinstance(node, ast_module.Module) and not self.options.all:
                return
            node.body = node.body[1:]
            if not node.body:
                node.body = [ast_module.Pass()]

    def visit_Module(self, node: ast_module.Module) -> ast_module.Module:
        self._remove_docstring(node)
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast_module.FunctionDef) -> ast_module.FunctionDef:
        self._remove_docstring(node)
        if self.options.type:
            node.returns = None
            for arg in node.args.args:
                arg.annotation = None
            for arg in node.args.kwonlyargs:
                arg.annotation = None
            if node.args.vararg:
                node.args.vararg.annotation = None
            if node.args.kwarg:
                node.args.kwarg.annotation = None
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast_module.AsyncFunctionDef) -> ast_module.AsyncFunctionDef:
        self._remove_docstring(node)
        if self.options.type:
            node.returns = None
            for arg in node.args.args:
                arg.annotation = None
            for arg in node.args.kwonlyargs:
                arg.annotation = None
            if node.args.vararg:
                node.args.vararg.annotation = None
            if node.args.kwarg:
                node.args.kwarg.annotation = None
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast_module.ClassDef) -> ast_module.ClassDef:
        self._remove_docstring(node)
        self.generic_visit(node)
        return node

    def visit_AnnAssign(self, node: ast_module.AnnAssign) -> ast_module.AST:
        if self.options.type:
            if node.value is not None:
                return ast_module.Assign(targets=[node.target], value=node.value)
            else:
                return ast_module.Pass()
        return node


def strip_comments_ast(source: str, options: argparse.Namespace) -> str:
    try:
        tree = ast_module.parse(source)
    except SyntaxError as e:
        logger.error(f"Syntax error in source: {e}")
        return source
    transformer = AstTransformer(options)
    tree = transformer.visit(tree)
    ast_module.fix_missing_locations(tree)
    result = ast_module.unparse(tree)
    if source.startswith("#!"):
        shebang = source.splitlines()[0] + "\n"
        result = shebang + result
    return result


def strip_comments_tree_sitter(source: str, options: argparse.Namespace) -> str:
    if not HAS_TREE_SITTER:
        msg = "tree-sitter is required for this backend. Install with: pip install tree-sitter"
        raise ImportError(msg)
    logger.warning("tree-sitter backend is experimental; falling back to ast")
    return strip_comments_ast(source, options)


def validate_python_code(code: str) -> bool:
    try:
        ast_module.parse(code)
        return True
    except SyntaxError as e:
        logger.error(f"Invalid Python code after transformation: {e}")
        return False


def process_file(file_path: Path, options: argparse.Namespace) -> tuple[Path, bool, str]:
    try:
        logger.debug(f"Processing: {file_path}")
        source = file_path.read_text(encoding="utf-8")
        if options.backend == "libcst":
            result = strip_comments_libcst(source, options)
        elif options.backend == "tree-sitter":
            result = strip_comments_tree_sitter(source, options)
        elif options.backend == "ast":
            result = strip_comments_ast(source, options)
        else:
            msg = f"Unsupported backend: {options.backend}"
            raise ValueError(msg)
        if not validate_python_code(result):
            logger.error(f"Validation failed for: {file_path}")
            return (file_path, False, "Validation failed")
        if result != source:
            file_path.write_text(result, encoding="utf-8")
            logger.info(f"Processed: {file_path}")
            return (file_path, True, "Success")
        else:
            logger.debug(f"No changes needed: {file_path}")
            return (file_path, True, "No changes")
    except Exception as e:
        logger.error(f"Error processing {file_path}: {e}")
        return (file_path, False, str(e))


def main() -> None:
    args = parse_args()
    setup_logger(args.verbose)
    if args.paths:
        paths = [Path(p) for p in args.paths]
    else:
        paths = [Path.cwd()]
    files = collect_python_files(paths)
    if not files:
        logger.warning("No Python files found to process.")
        return
    logger.info(f"Found {len(files)} Python files to process")
    results: list[tuple[Path, bool, str]] = []
    if len(files) == 1:
        results.append(process_file(files[0], args))
    else:
        with mp.Pool(processes=min(MAX_WORKERS, len(files))) as pool:
            async_results = [pool.apply_async(process_file, (file_path, args)) for file_path in files]
            for async_result in async_results:
                try:
                    results.append(async_result.get(timeout=60))
                except Exception as e:
                    logger.error(f"Worker failed: {e}")
    success_count = sum(1 for _, success, _ in results if success)
    failed_count = len(results) - success_count
    logger.info(f"Processing complete: {success_count} succeeded, {failed_count} failed")
    if failed_count > 0:
        logger.warning("Failed files:")
        for file_path, success, error in results:
            if not success:
                logger.warning(f"  {file_path}: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
