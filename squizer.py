#!/data/data/com.termux/files/usr/bin/env python
"""Compress Python sources by renaming names, dropping docstrings and self-extracting the result.
Strips the docstrings and annotations, removes the unused stdlib imports, renames the local names to short ones, joins the simple statements and optionally emits a single self-decoding ``b85``/``zlib`` payload, keeping the builtins and the protected names untouched.
Usage: squizer [paths ...] [--payload] Examples: $ squizer script.py Runs as a script (``python squizer.py``) in the directory it should act on; the module can also be imported."""

import ast
import base64
import builtins
import operator
from pathlib import Path
import string
import sys
from typing import Any, Callable, Iterable, Iterator, Sequence
import zlib

from dh import cprint


PROTECTED_NAMES: frozenset[str] = frozenset(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__main__",
    "self",
    "cls",
}


def get_stdlib_modules() -> set[str]:
    if hasattr(sys, "stdlib_module_names"):
        return set(sys.stdlib_module_names)
    import sysconfig

    stdlib_path = Path(sysconfig.get_paths()["stdlib"])
    mods: set[str] = set(sys.builtin_module_names)
    for entry in stdlib_path.iterdir():
        name = entry.name
        if name.endswith(".py"):
            mods.add(name[:-3])
        elif name.endswith(".so"):
            mods.add(name.split(".")[0])
        elif entry.is_dir():
            mods.add(name)
    return mods


STDLIB_MODULES: set[str] = get_stdlib_modules()
STDLIB_KEEP: frozenset[str] = frozenset()


class StripDocstringsAndTypes(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.returns = None
        _remove_docstring(node)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.returns = None
        _remove_docstring(node)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        _remove_docstring(node)
        self.generic_visit(node)
        return node

    def visit_Module(self, node: ast.Module) -> ast.AST:
        _remove_docstring(node)
        self.generic_visit(node)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if node.value is None:
            return None
        new = ast.Assign(targets=[node.target], value=node.value)
        return self.generic_visit(new)


def _remove_docstring(node: ast.AST) -> None:
    body = getattr(node, "body", None)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)


class StdlibImportStripper(ast.NodeTransformer):
    def __init__(self) -> None:
        self.removed: list[str] = []

    def visit_Import(self, node: ast.Import) -> ast.AST | None:
        kept: list[ast.alias] = []
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in STDLIB_MODULES and top not in STDLIB_KEEP:
                text = f"{alias.name} as {alias.asname}" if alias.asname else alias.name
                self.removed.append(text)
            else:
                kept.append(alias)
        if not kept:
            return None
        node.names = kept
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
        if node.module is None:
            return node
        top = node.module.split(".")[0]
        if top in STDLIB_MODULES or top in STDLIB_KEEP:
            for alias in node.names:
                if alias.asname:
                    self.removed.append(f"from {node.module} import {alias.name} as {alias.asname}")
                else:
                    self.removed.append(f"from {node.module} import {alias.name}")
            return None
        return node


class FutureImportGuard(ast.NodeTransformer):
    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
        if node.module == "__future__":
            return None
        return self.generic_visit(node)


def generate_short_names() -> Iterator[str]:
    letters = string.ascii_lowercase
    idx = 0
    while True:
        yield letters[idx] if idx < 26 else f"{letters[idx % 26]}{idx // 26}"
        idx += 1


class NameCollector(ast.NodeVisitor):
    def __init__(self, name_map: dict[str, str], name_gen: Iterator[str]) -> None:
        self.name_map = name_map
        self.name_gen = name_gen

    def _register(self, name: str) -> None:
        if (
            name
            and len(name) > 3
            and name not in PROTECTED_NAMES
            and not name.startswith("__")
            and name not in self.name_map
        ):
            self.name_map[name] = next(self.name_gen)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._register(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._register(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._register(node.name)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self._register(node.arg)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._register(node.id)


class NameRenamer(ast.NodeTransformer):
    def __init__(self, name_map: dict[str, str]) -> None:
        self.name_map = name_map

    def _map(self, name: str) -> str:
        return self.name_map.get(name, name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.name = self._map(node.name)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.name = self._map(node.name)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.name = self._map(node.name)
        self.generic_visit(node)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.arg = self._map(node.arg)
        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self._map(node.id)
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.keyword:
        if node.arg is not None:
            node.arg = self._map(node.arg)
        return node


_BIN_OPS: dict[type[ast.operator], Callable[[object, object], object]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.BitAnd: operator.and_,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[object], object]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Invert: operator.invert,
    ast.Not: operator.not_,
}
_AUG_OPS: frozenset[type[ast.operator]] = frozenset([
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.LShift,
    ast.RShift,
    ast.BitOr,
    ast.BitXor,
    ast.BitAnd,
    ast.MatMult,
])
_TERMINATORS: tuple[type[ast.stmt], ...] = (
    ast.Return,
    ast.Raise,
    ast.Break,
    ast.Continue,
)


def _is_const(node: ast.AST | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


class PeepholeOptimizer(ast.NodeTransformer):
    def generic_visit(self, node: ast.AST) -> ast.AST:
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list) and stmts and all(isinstance(s, ast.stmt) for s in stmts):
                setattr(node, field, self._optimize_stmts(stmts))
        return node

    def _optimize_stmts(self, stmts: list[ast.stmt]) -> list[ast.stmt]:
        for _ in range(3):
            new = self._opt_pass(stmts)
            if len(new) == len(stmts) and all(a is b for a, b in zip(new, stmts)):
                break
            stmts = new
        return stmts

    def _opt_pass(self, stmts: list[ast.stmt]) -> list[ast.stmt]:
        live: list[ast.stmt] = []
        for s in stmts:
            if live and isinstance(live[-1], _TERMINATORS):
                break
            live.append(s)
        flattened: list[ast.stmt] = []
        for s in live:
            if isinstance(s, ast.If) and s.orelse and s.body and isinstance(s.body[-1], _TERMINATORS):
                flattened.append(ast.If(test=s.test, body=s.body, orelse=[]))
                flattened.extend(s.orelse)
            else:
                flattened.append(s)
        out: list[ast.stmt] = []
        i = 0
        while i < len(flattened):
            s = flattened[i]
            if (
                i + 1 < len(flattened)
                and isinstance(s, ast.If)
                and not s.orelse
                and len(s.body) == 1
                and isinstance(s.body[0], ast.Return)
                and isinstance(flattened[i + 1], ast.Return)
            ):
                then_v = s.body[0].value
                else_v = flattened[i + 1].value
                if _is_const(then_v, True) and _is_const(else_v, False):
                    out.append(
                        ast.Return(
                            value=ast.Call(
                                func=ast.Name(id="bool", ctx=ast.Load()),
                                args=[s.test],
                                keywords=[],
                            )
                        )
                    )
                    i += 2
                    continue
                if _is_const(then_v, False) and _is_const(else_v, True):
                    out.append(ast.Return(value=ast.UnaryOp(op=ast.Not(), operand=s.test)))
                    i += 2
                    continue
            out.append(s)
            i += 1
        return out

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        self.generic_visit(node)
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.BinOp)
            and isinstance(node.value.left, ast.Name)
            and node.targets[0].id == node.value.left.id
            and type(node.value.op) in _AUG_OPS
        ):
            return ast.copy_location(
                ast.AugAssign(target=node.targets[0], op=node.value.op, value=node.value.right),
                node,
            )
        return node

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        if not node.orelse and len(node.body) == 1 and isinstance(node.body[0], ast.If) and not node.body[0].orelse:
            inner = node.body[0]
            return ast.copy_location(
                ast.If(
                    test=ast.BoolOp(op=ast.And(), values=[node.test, inner.test]),
                    body=inner.body,
                    orelse=[],
                ),
                node,
            )
        if isinstance(node.test, ast.Constant):
            if node.test.value is True:
                node.test = ast.copy_location(ast.Constant(value=1), node.test)
            elif node.test.value is False:
                node.test = ast.copy_location(ast.Constant(value=0), node.test)
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            node.test = ast.copy_location(ast.Constant(value=1), node.test)
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if not (isinstance(node.left, ast.Constant) and isinstance(node.right, ast.Constant)):
            return node
        op_fn = _BIN_OPS.get(type(node.op))
        if op_fn is None:
            return node
        if isinstance(node.op, (ast.Pow, ast.LShift)) and isinstance(node.right.value, int) and node.right.value > 64:
            return node
        try:
            result = op_fn(node.left.value, node.right.value)
        except Exception:
            return node
        new = ast.Constant(value=result)
        try:
            if len(ast.unparse(new)) < len(ast.unparse(node)):
                return ast.copy_location(new, node)
        except Exception:
            pass
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if not isinstance(node.operand, ast.Constant):
            return node
        op_fn = _UNARY_OPS.get(type(node.op))
        if op_fn is None:
            return node
        try:
            result = op_fn(node.operand.value)
        except Exception:
            return node
        new = ast.Constant(value=result)
        try:
            if len(ast.unparse(new)) < len(ast.unparse(node)):
                return ast.copy_location(new, node)
        except Exception:
            pass
        return node


def _is_simple_line(line: str) -> bool:
    s = line.strip()
    return not (not s or s.startswith("#") or s.endswith(":"))


def _leading_ws(line: str) -> str:
    stripped = line.lstrip()
    return line[: len(line) - len(stripped)]


def join_simple_lines(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not _is_simple_line(line):
            out.append(line)
            i += 1
            continue
        indent = _leading_ws(line)
        group = [line.strip()]
        j = i + 1
        while j < len(lines) and _is_simple_line(lines[j]) and _leading_ws(lines[j]) == indent:
            group.append(lines[j].strip())
            j += 1
        if len(group) > 1:
            out.append(indent + "; ".join(group))
        else:
            out.append(line)
        i = j
    return "\n".join(out)


_STUB_HEADER: str = "import base64 as _b,zlib as _z\nexec(_z.decompress(_b.b85decode(_p)))\n"


def wrap_payload(source: str, *, level: int = 9) -> str:
    raw = source.encode("utf-8")
    packed = zlib.compress(raw, level)
    b85 = base64.b85encode(packed).decode("ascii")
    chunk = 100
    lines = [b85[k : k + chunk] for k in range(0, len(b85), chunk)]
    payload_literal = "_p=(" + "\n".join(f'"{ln}"' for ln in lines) + ")"
    header = (
        f"# Compressed Python payload  "
        f"({len(raw)} -> {len(packed)} bytes zlib, "
        f"{len(b85)} base85 chars)\n"
        f"# Run with: python {Path(sys.argv[0]).name}\n"
    )
    return f"{header}{payload_literal}\n\n{_STUB_HEADER}"


def _render_file(filepath: str, tree: ast.AST) -> str:
    tree = FutureImportGuard().visit(tree)
    ast.fix_missing_locations(tree)
    minified = ast.unparse(tree)
    minified = join_simple_lines(minified)
    body_lines = [ln for ln in minified.splitlines() if ln.strip()]
    return f"# --- File: {filepath} ---\n" + "\n".join(body_lines)


def compress_files(file_paths: Sequence[str]) -> None:
    name_map: dict[str, str] = {}
    name_gen = generate_short_names()
    parsed_trees: list[tuple[str, ast.AST]] = []
    removed_imports: list[str] = []
    for filepath in file_paths:
        path = Path(filepath)
        if not path.exists():
            print(f"Warning: File '{filepath}' not found. Skipping.", file=sys.stderr)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=filepath)
        tree = StripDocstringsAndTypes().visit(tree)
        ast.fix_missing_locations(tree)
        NameCollector(name_map, name_gen).visit(tree)
        stripper = StdlibImportStripper()
        tree = stripper.visit(tree)
        ast.fix_missing_locations(tree)
        removed_imports.extend(stripper.removed)
        parsed_trees.append((filepath, tree))
    if not parsed_trees:
        cprint("Nothing to compress.")
        return
    blocks: list[str] = []
    for filepath, tree in parsed_trees:
        tree = NameRenamer(name_map).visit(tree)
        ast.fix_missing_locations(tree)
        tree = PeepholeOptimizer().visit(tree)
        ast.fix_missing_locations(tree)
        blocks.append(_render_file(filepath, tree))
    compressed = "\n\n".join(blocks)
    if removed_imports:
        uniq = sorted(set(removed_imports))
        compressed += (
            "\n\n# --- NOTE: stdlib imports were stripped during compression ---\n"
            "# Re-add them (or ensure they are globally available) before running:\n"
            + "\n".join(f"#   {imp}" for imp in uniq)
        )
    if len(parsed_trees) == 1:
        src = Path(parsed_trees[0][0])
        stub_path = src.with_name(f"{src.stem}_compressed.py")
        txt_path = src.with_name(f"{src.stem}_compressed.txt")
        stub_path.write_text(wrap_payload(compressed), encoding="utf-8")
        txt_path.write_text(compressed, encoding="utf-8")
        target = stub_path
    else:
        txt_path = Path("compressed.txt")
        stub_path = Path("compressed_stub.py")
        txt_path.write_text(compressed, encoding="utf-8")
        stub_path.write_text(wrap_payload(compressed), encoding="utf-8")
        target = stub_path
    raw_sz = len(compressed.encode("utf-8"))
    stub_sz = target.stat().st_size
    ratio = raw_sz / stub_sz if stub_sz else 0.0
    cprint(
        f"Compressed {len(parsed_trees)} file(s) -> {target}  "
        f"[{raw_sz} -> {stub_sz} bytes, {ratio:.2f}x, "
        f"removed {len(set(removed_imports))} stdlib import(s)]"
    )
    cprint(f"  readable: {txt_path}")
    cprint(f"  runnable: {stub_path}")


def get_python_files() -> list[str]:
    return [str(p) for p in Path().rglob("*.py") if p.is_file()]


def main(argv: list[str]) -> int:
    if not argv:
        files = get_python_files()
        if not files:
            print("No Python files found in current directory.")
            return 0
        print(f"Processing {len(files)} Python file(s) from current directory...")
        compress_files(files)
        return 0
    compress_files(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
