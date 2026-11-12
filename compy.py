#!/data/data/com.termux/files/usr/bin/env python
"""Compress Python sources for an LLM prompt, with a header note.
Same pipeline as :doc:`compy` (comments, docstrings, annotations and stdlib imports removed, short names, joined statements) and a ``COMPRESSION_NOTE`` header that explains the transformation to the model, written to ``compressed.txt``.
Usage: compy2 [paths ...] Positional arguments: inputs ...
Python files and/or directories.
Defaults to the current directory.
Examples: $ compy2 src/ Command-line script: run ``python compy2.py`` or the ``compy2`` command.
The module is importable as well."""

import argparse
import ast
import builtins
from io import StringIO
import keyword
import multiprocessing as mp
from pathlib import Path
import re
import sys
import sysconfig
import token
import tokenize
from typing import Any, Iterable

from dh import MAX_WORKERS, runcmd

OUTPUT_FILE: Path = Path("compressed.txt")
COMPRESSION_NOTE: str = (
    "# NOTE: This code is compressed. Standard library imports have been removed. "
    "You may rename variables and functions freely."
)
PROTECTED_NAMES: set[str] = {
    "__name__",
    "__main__",
    "__file__",
    "__package__",
    "__path__",
    "__spec__",
    "__loader__",
    "__cached__",
    "__builtins__",
    "__doc__",
    "__all__",
    "__version__",
    "__author__",
    "__class__",
    "__init__",
    "__new__",
    "__repr__",
    "__str__",
    "__len__",
    "__iter__",
    "__next__",
    "__getitem__",
    "__setitem__",
    "__contains__",
    "__enter__",
    "__exit__",
    "__call__",
    "__getattr__",
    "__setattr__",
    "__delattr__",
    "__dict__",
    "__slots__",
    "__annotations__",
    "self",
    "cls",
}
BUILTIN_NAMES = set(dir(builtins))
KEYWORDS = set(keyword.kwlist)
SHORT_NAMES: list[list[Any]] = [
    *list("abcdefghijklmnopqrstuvwxyz"),
    *list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
]


def strip_comments_and_shebang(source: str) -> str:
    lines = source.splitlines()
    if lines and lines[0].startswith("#!"):
        lines[0] = ""
    source = "\n".join(lines)
    result: list[tokenize.TokenInfo] = []
    try:
        tokens = tokenize.generate_tokens(StringIO(source).readline)
        for item in tokens:
            if item.type == token.COMMENT:
                continue
            result.append(item)
        return tokenize.untokenize(result)
    except tokenize.TokenError:
        return source


def get_stdlib_modules() -> set[str]:
    modules = set(getattr(sys, "stdlib_module_names", set()))
    if not modules:
        modules.update(sys.builtin_module_names)
        stdlib_dir = Path(sysconfig.get_paths().get("stdlib", ""))
        if stdlib_dir.exists():
            for item in stdlib_dir.iterdir():
                if item.name.startswith("_"):
                    continue
                if item.is_file() and item.suffix == ".py":
                    modules.add(item.stem)
                elif item.is_dir() and (item / "__init__.py").exists():
                    modules.add(item.name)
    return modules


STDLIB_MODULES = get_stdlib_modules()


def is_stdlib_import(module_name: str | None) -> bool:
    if not module_name:
        return False
    root = module_name.split(".", 1)[0]
    return root in STDLIB_MODULES


def is_docstring_expr(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def is_terminating_statement(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True
    if isinstance(node, ast.If):
        return bool(node.body) and bool(node.orelse) and block_terminates(node.body) and block_terminates(node.orelse)
    if isinstance(node, ast.Try):
        branches = [node.body, *[handler.body for handler in node.handlers]]
        if node.orelse:
            branches.append(node.orelse)
        return bool(branches) and all(block_terminates(branch) for branch in branches)
    return False


def block_terminates(statements: list[ast.stmt]) -> bool:
    if not statements:
        return False
    return any(is_terminating_statement(statement) for statement in statements)


def make_short_name(index: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    base = len(alphabet)
    if index < base:
        return alphabet[index]
    result = ""
    while index >= 0:
        result = alphabet[index % base] + result
        index = index // base - 1
    return result


class AnnotationStripper(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        self._strip_arguments(node.args)
        node.type_comment = None
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        self._strip_arguments(node.args)
        node.type_comment = None
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        self.generic_visit(node)
        if node.value is None:
            return None
        return ast.copy_location(
            ast.Assign(
                targets=[node.target],
                value=node.value,
                type_comment=None,
            ),
            node,
        )

    @staticmethod
    def _strip_arguments(arguments: ast.arguments) -> None:
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            argument.annotation = None
            argument.type_comment = None
        if arguments.vararg:
            arguments.vararg.annotation = None
            arguments.vararg.type_comment = None
        if arguments.kwarg:
            arguments.kwarg.annotation = None
            arguments.kwarg.type_comment = None


class ImportCleaner(ast.NodeTransformer):
    def visit_Module(self, node: ast.Module) -> ast.AST:
        self.generic_visit(node)
        node.body = self._clean_block(node.body)
        return node

    def visit_Import(self, node: ast.Import) -> ast.AST | None:
        kept = [alias for alias in node.names if not is_stdlib_import(alias.name)]
        if not kept:
            return None
        node.names = kept
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
        if node.module == "__future__":
            return None
        if node.level > 0:
            return node
        if is_stdlib_import(node.module):
            return None
        return node

    @staticmethod
    def _clean_block(statements: list[ast.stmt]) -> list[ast.stmt]:
        cleaned: list[ast.stmt] = []
        first_real_statement = True
        terminated = False
        for statement in statements:
            if terminated:
                continue
            if first_real_statement and is_docstring_expr(statement):
                first_real_statement = False
                continue
            first_real_statement = False
            cleaned.append(statement)
            if is_terminating_statement(statement):
                terminated = True
        return cleaned


class Simplifier(ast.NodeTransformer):
    def visit_Module(self, node: ast.Module) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        node.orelse = self._simplify_block(node.orelse)
        node.test = self._shorten_boolean_constant(node.test)
        bool_return = self._convert_boolean_return_pattern(node)
        if bool_return is not None:
            return ast.copy_location(bool_return, node)
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        node.orelse = self._simplify_block(node.orelse)
        node.test = self._shorten_boolean_constant(node.test)
        return node

    def visit_For(self, node: ast.For) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        node.orelse = self._simplify_block(node.orelse)
        return node

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        node.orelse = self._simplify_block(node.orelse)
        return node

    def visit_With(self, node: ast.With) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        return node

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        return node

    def visit_Try(self, node: ast.Try) -> ast.AST:
        self.generic_visit(node)
        node.body = self._simplify_block(node.body)
        node.orelse = self._simplify_block(node.orelse)
        node.finalbody = self._simplify_block(node.finalbody)
        for handler in node.handlers:
            handler.body = self._simplify_block(handler.body)
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        self.generic_visit(node)
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.BinOp)
            and isinstance(node.value.left, ast.Name)
            and node.targets[0].id == node.value.left.id
        ):
            return ast.copy_location(
                ast.AugAssign(
                    target=node.targets[0],
                    op=node.value.op,
                    value=node.value.right,
                ),
                node,
            )
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        return self._fold_expression_if_shorter(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        return self._fold_expression_if_shorter(node)

    def _simplify_block(self, statements: list[ast.stmt]) -> list[ast.stmt]:
        output: list[ast.stmt] = []
        terminated = False
        for statement in statements:
            if terminated:
                continue
            if isinstance(statement, ast.If) and statement.orelse and block_terminates(statement.body):
                else_body = statement.orelse
                statement.orelse = []
                output.append(statement)
                output.extend(else_body)
                if block_terminates(else_body):
                    terminated = True
                continue
            if (
                isinstance(statement, ast.If)
                and not statement.orelse
                and len(statement.body) == 1
                and isinstance(statement.body[0], ast.If)
                and not statement.body[0].orelse
            ):
                inner = statement.body[0]
                statement.test = ast.BoolOp(
                    op=ast.And(),
                    values=[statement.test, inner.test],
                )
                statement.body = inner.body
            output.append(statement)
            if is_terminating_statement(statement):
                terminated = True
        return output

    @staticmethod
    def _shorten_boolean_constant(expression: ast.expr) -> ast.expr:
        if isinstance(expression, ast.Constant) and expression.value is True:
            return ast.copy_location(ast.Constant(value=1), expression)
        if isinstance(expression, ast.Constant) and expression.value is False:
            return ast.copy_location(ast.Constant(value=0), expression)
        return expression

    @staticmethod
    def _convert_boolean_return_pattern(node: ast.If) -> ast.Return | None:
        if len(node.body) != 1 or len(node.orelse) != 1:
            return None
        true_branch = node.body[0]
        false_branch = node.orelse[0]
        if not isinstance(true_branch, ast.Return):
            return None
        if not isinstance(false_branch, ast.Return):
            return None
        if not isinstance(true_branch.value, ast.Constant):
            return None
        if not isinstance(false_branch.value, ast.Constant):
            return None
        left = true_branch.value.value
        right = false_branch.value.value
        if left is True and right is False:
            return ast.Return(
                value=ast.Call(
                    func=ast.Name(id="bool", ctx=ast.Load()),
                    args=[node.test],
                    keywords=[],
                )
            )
        if left is False and right is True:
            return ast.Return(
                value=ast.UnaryOp(
                    op=ast.Not(),
                    operand=node.test,
                )
            )
        return None

    @staticmethod
    def _fold_expression_if_shorter(node: ast.expr) -> ast.expr:
        if not isinstance(node, (ast.BinOp, ast.UnaryOp)):
            return node
        try:
            before = ast.unparse(node)
            compiled = compile(
                ast.Expression(body=node),
                filename="<constant-fold>",
                mode="eval",
            )
            value = eval(compiled, {"__builtins__": {}}, {})
            folded = ast.Constant(value=value)
            after = ast.unparse(folded)
        except (
            ArithmeticError,
            MemoryError,
            OverflowError,
            SyntaxError,
            TypeError,
            ValueError,
            NameError,
        ):
            return node
        if len(after) < len(before):
            return ast.copy_location(folded, node)
        return node


class NameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.imported_names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            self.imported_names.add(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            self.imported_names.add(name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_name(node.name)
        self._collect_arguments(node.args)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add_name(node.name)
        self._collect_arguments(node.args)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_name(node.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._add_name(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self._add_name(name)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self._add_name(name)

    def _collect_arguments(self, arguments: ast.arguments) -> None:
        all_arguments = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
        if arguments.vararg:
            all_arguments.append(arguments.vararg)
        if arguments.kwarg:
            all_arguments.append(arguments.kwarg)
        for argument in all_arguments:
            self._add_name(argument.arg)

    def _add_name(self, name: str) -> None:
        if self._can_rename(name):
            self.names.add(name)

    def _can_rename(self, name: str) -> bool:
        return (
            name not in PROTECTED_NAMES
            and name not in BUILTIN_NAMES
            and name not in KEYWORDS
            and not name.startswith("__")
            and name not in self.imported_names
        )


class NameRenamer(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.mapping:
            node.id = self.mapping[node.id]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if node.name in self.mapping:
            node.name = self.mapping[node.name]
        self._rename_arguments(node.args)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        if node.name in self.mapping:
            node.name = self.mapping[node.name]
        self._rename_arguments(node.args)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        if node.name in self.mapping:
            node.name = self.mapping[node.name]
        self.generic_visit(node)
        return node

    def visit_Global(self, node: ast.Global) -> ast.AST:
        node.names = [self.mapping.get(name, name) for name in node.names]
        return node

    def visit_Nonlocal(self, node: ast.Nonlocal) -> ast.AST:
        node.names = [self.mapping.get(name, name) for name in node.names]
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        self.generic_visit(node)
        if node.arg is not None and node.arg in self.mapping:
            node.arg = self.mapping[node.arg]
        return node

    def _rename_arguments(self, arguments: ast.arguments) -> None:
        all_arguments = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
        if arguments.vararg:
            all_arguments.append(arguments.vararg)
        if arguments.kwarg:
            all_arguments.append(arguments.kwarg)
        for argument in all_arguments:
            if argument.arg in self.mapping:
                argument.arg = self.mapping[argument.arg]


def build_name_mapping(tree: ast.AST) -> dict[str, str]:
    collector = NameCollector()
    collector.visit(tree)
    candidates = sorted(collector.names, key=lambda value: (-len(value), value))
    reserved = set(collector.names) | BUILTIN_NAMES | PROTECTED_NAMES | KEYWORDS
    mapping: dict[str, str] = {}
    index = 0
    for old_name in candidates:
        while True:
            new_name = make_short_name(index)
            index += 1
            if new_name not in reserved:
                break
        if len(new_name) < len(old_name):
            mapping[old_name] = new_name
            reserved.add(new_name)
    return mapping


def join_simple_lines(source: str) -> str:
    lines = [line.rstrip() for line in source.splitlines() if line.strip()]
    output: list[str] = []

    def indentation(line: str) -> str:
        return line[: len(line) - len(line.lstrip())]

    def is_simple_statement(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        return not stripped.endswith(":")

    index = 0
    while index < len(lines):
        current = lines[index]
        current_indent = indentation(current)
        if not is_simple_statement(current):
            output.append(current)
            index += 1
            continue
        group = [current.strip()]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if indentation(candidate) != current_indent:
                break
            if not is_simple_statement(candidate):
                break
            group.append(candidate.strip())
            index += 1
        output.append(f"{current_indent}{';'.join(group)}")
    return "\n".join(output)


def compact_spacing(source: str) -> str:
    source = re.sub(r",\s+", ",", source)
    source = re.sub(r":\s+", ":", source)
    operators = [
        r"\*\*",
        r"//",
        r"<<",
        r">>",
        r"==",
        r"!=",
        r"<=",
        r">=",
        r"\+=",
        r"-=",
        r"\*=",
        r"/=",
        r"//=",
        r"%=",
        r"&=",
        r"\|=",
        r"\^=",
        r">>=",
        r"<<=",
        r"=",
        r"\+",
        r"-",
        r"\*",
        r"/",
        r"%",
        r"<",
        r">",
        r"&",
        r"\|",
        r"\^",
    ]
    for operator in operators:
        source = re.sub(rf"\s*({operator})\s*", r"\1", source)
    return source


def compress_source(source: str, filename: str = "<input>") -> str:
    source = strip_comments_and_shebang(source)
    tree = ast.parse(source, filename=filename)
    tree = AnnotationStripper().visit(tree)
    ast.fix_missing_locations(tree)
    tree = ImportCleaner().visit(tree)
    ast.fix_missing_locations(tree)
    tree = Simplifier().visit(tree)
    ast.fix_missing_locations(tree)
    mapping = build_name_mapping(tree)
    tree = NameRenamer(mapping).visit(tree)
    ast.fix_missing_locations(tree)
    result = ast.unparse(tree)
    result = join_simple_lines(result)
    result = compact_spacing(result)
    return "\n".join(line for line in result.splitlines() if line.strip())


def discover_python_files(inputs: list[str]) -> list[Path]:
    roots = [Path(item) for item in inputs] if inputs else [Path.cwd()]
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            if root.suffix == ".py" and root.name != OUTPUT_FILE.name:
                found.add(root.resolve())
            continue
        if root.is_dir():
            for path in root.rglob("*.py"):
                if path.name == OUTPUT_FILE.name:
                    continue
                if path.is_file():
                    found.add(path.resolve())
    return sorted(found, key=str)


def process_file(path_string: str) -> tuple[str, str, str | None]:
    path = Path(path_string)
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            source = path.read_text(encoding="utf-8-sig")
        except Exception as error:
            return str(path), "", f"Unable to read {path}: {error}"
    except Exception as error:
        return str(path), "", f"Unable to read {path}: {error}"
    try:
        compressed = compress_source(source, str(path))
        return str(path), compressed, None
    except SyntaxError as error:
        return str(path), "", f"Syntax error in {path}: {error}"
    except Exception as error:
        return str(path), "", f"Failed to compress {path}: {error}"


def format_output(results: list[tuple[str, str, str | None]]) -> str:
    successful = [(name, content) for name, content, error in results if error is None and content.strip()]
    if not successful:
        return ""
    multiple_files = len(successful) > 1
    sections: list[str] = []
    for name, content in successful:
        lines: list[str] = []
        if multiple_files:
            lines.append(f"# File: {Path(name).name}")
        lines.extend(line for line in content.splitlines() if line.strip())
        sections.append("\n".join(lines))
    return "\n".join(sections)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compress Python files for LLM input.")
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Python files and/or directories. Defaults to the current directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    files = discover_python_files(args.inputs)
    if not files:
        print("No Python files found.", file=sys.stderr)
        OUTPUT_FILE.write_text("", encoding="utf-8")
        return 1
    paths = [str(path) for path in files]
    if len(paths) == 1:
        results = [process_file(paths[0])]
    else:
        with mp.Pool(processes=MAX_WORKERS) as pool:
            results = list(pool.imap_unordered(process_file, paths))
    results.sort(key=lambda item: item[0])
    for _, _, error in results:
        if error:
            print(error, file=sys.stderr)
    output = format_output(results)
    output = "\n".join(line for line in output.splitlines() if line.strip())
    if output:
        output += "\n"
        output += COMPRESSION_NOTE + "\n"
    single_py_file = len(args.inputs) == 1 and Path(args.inputs[0]).is_file() and Path(args.inputs[0]).suffix == ".py"
    if not single_py_file:
        OUTPUT_FILE.write_text(output, encoding="utf-8")
    cmd = ["termux-clipboard-set", output]
    runcmd(cmd, show_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
