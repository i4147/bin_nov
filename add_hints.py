#!/data/data/com.termux/files/usr/bin/env python
"""Add simple type hints to Python files in place with LibCST.
Infers the parameter and return types from the literal defaults and the returns, optionally also from the parameter names (``name`` -> ``str``, ``count`` -> ``int`` ...), validates every result by parsing it and offers ``--dry-run``, ``--diff``, ``--backup`` and ``--no-returns``.
Usage: add_hints [options] FILE.py ...
Positional arguments: files ...
.py file(s) to annotate Options: --dry-run Don't write, just report --diff Print a unified diff --backup Write FILE.py.bak before overwriting --no-returns Only annotate parameters --name-heuristics Also guess types from parameter names (name→str, count→int, ...) Examples: $ add_hints --diff util.py Command-line script: run ``python add_hints.py`` or the ``add_hints`` command.
The module is importable as well."""

import argparse
import ast
import difflib
from pathlib import Path
import shutil
import sys
from typing import Any, Optional

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor


NAME_HINTS: dict[str, str] = {
    "name": "str",
    "path": "str",
    "text": "str",
    "msg": "str",
    "message": "str",
    "url": "str",
    "filename": "str",
    "prefix": "str",
    "suffix": "str",
    "key": "str",
    "count": "int",
    "n": "int",
    "i": "int",
    "index": "int",
    "idx": "int",
    "size": "int",
    "limit": "int",
    "offset": "int",
    "port": "int",
    "verbose": "bool",
    "debug": "bool",
    "force": "bool",
    "enabled": "bool",
    "flag": "bool",
    "dry_run": "bool",
    "ratio": "float",
    "threshold": "float",
    "items": "list",
    "values": "list",
    "options": "dict",
    "kwargs": "dict",
}


def infer_literal_type(node: cst.BaseExpression) -> Optional[str]:
    if isinstance(node, cst.Integer):
        return "int"
    if isinstance(node, cst.Float):
        return "float"
    if isinstance(node, cst.Imaginary):
        return "complex"
    if isinstance(node, cst.SimpleString):
        prefix = node.prefix.lower()
        return "bytes" if "b" in prefix else "str"
    if isinstance(node, (cst.ConcatenatedString, cst.FormattedString)):
        return "str"
    if isinstance(node, cst.Name):
        if node.value in ("True", "False"):
            return "bool"
        if node.value == "None":
            return "None"
        return None
    if isinstance(node, cst.List):
        return "list"
    if isinstance(node, cst.Tuple):
        return "tuple"
    if isinstance(node, cst.Dict):
        return "dict"
    if isinstance(node, cst.Set):
        return "set"
    if isinstance(node, (cst.ListComp,)):
        return "list"
    if isinstance(node, cst.DictComp):
        return "dict"
    if isinstance(node, cst.SetComp):
        return "set"
    if isinstance(node, cst.Comparison):
        return "bool"
    if isinstance(node, cst.UnaryOperation):
        if isinstance(node.operator, cst.Not):
            return "bool"
        if isinstance(node.operand, (cst.Integer, cst.Float)):
            return infer_literal_type(node.operand)
    if (
        isinstance(node, cst.Call)
        and isinstance(node.func, cst.Name)
        and node.func.value
        in {
            "int",
            "float",
            "str",
            "bool",
            "bytes",
            "list",
            "dict",
            "set",
            "tuple",
            "len",
            "repr",
        }
    ):
        return {"len": "int", "repr": "str"}.get(node.func.value, node.func.value)
    return None


class _ReturnCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.types: set[str] = set()
        self.has_yield = False
        self._depth = 0

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._depth += 1

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._depth -= 1

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._depth += 1

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._depth -= 1

    def visit_Yield(self, node: cst.Yield) -> None:
        if self._depth == 0:
            self.has_yield = True

    def visit_Return(self, node: cst.Return) -> None:
        if self._depth:
            return
        if node.value is None:
            self.types.add("None")
        else:
            self.types.add(infer_literal_type(node.value) or "?")


def infer_return_type(func: cst.FunctionDef) -> Optional[str]:
    collector = _ReturnCollector()
    func.body.visit(collector)
    if collector.has_yield:
        return None
    if func.name.value in {"__init__", "__init_subclass__", "__set_name__"}:
        return "None"
    if not collector.types:
        return "None"
    if "?" in collector.types:
        return None
    if collector.types == {"None"}:
        return "None"
    non_none = collector.types - {"None"}
    if len(non_none) != 1:
        return None
    (only,) = non_none
    if "None" in collector.types:
        return f"Optional[{only}]"
    return only


class AnnotateTransformer(cst.CSTTransformer):
    def __init__(
        self,
        context: CodemodContext,
        *,
        add_returns: bool = True,
        name_heuristics: bool = False,
    ) -> None:
        super().__init__()
        self.context = context
        self.add_returns = add_returns
        self.name_heuristics = name_heuristics
        self.stats = {"params": 0, "returns": 0}
        self._scope: list[str] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._scope.append("class")

    def leave_ClassDef(self, original: cst.ClassDef, updated: cst.ClassDef) -> cst.ClassDef:
        self._scope.pop()
        return updated

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.append("func")

    def leave_FunctionDef(self, original: cst.FunctionDef, updated: cst.FunctionDef) -> cst.FunctionDef:
        self._scope.pop()
        is_method = bool(self._scope) and self._scope[-1] == "class"
        is_static = any(
            isinstance(d.decorator, cst.Name) and d.decorator.value == "staticmethod" for d in updated.decorators
        )
        params = updated.params
        new_posonly = self._annotate_params(params.posonly_params, is_method and not is_static, first_group=True)
        new_params = self._annotate_params(
            params.params,
            is_method and not is_static and not params.posonly_params,
            first_group=True,
        )
        new_kwonly = self._annotate_params(params.kwonly_params, False)
        updated = updated.with_changes(
            params=params.with_changes(posonly_params=new_posonly, params=new_params, kwonly_params=new_kwonly)
        )
        if self.add_returns and updated.returns is None:
            ret = infer_return_type(updated)
            if ret is not None:
                if ret.startswith("Optional["):
                    AddImportsVisitor.add_needed_import(self.context, "typing", "Optional")
                updated = updated.with_changes(returns=cst.Annotation(annotation=cst.parse_expression(ret)))
                self.stats["returns"] += 1
        return updated

    def _annotate_params(self, params: dict[str, Any], skip_self: bool, first_group: bool = False):
        out = []
        for i, p in enumerate(params):
            if p.annotation is None and not (skip_self and i == 0 and p.name.value in ("self", "cls")):
                t = self._infer_param(p)
                if t is not None:
                    if t.startswith("Optional["):
                        AddImportsVisitor.add_needed_import(self.context, "typing", "Optional")
                    p = p.with_changes(
                        annotation=cst.Annotation(annotation=cst.parse_expression(t)),
                        equal=(cst.AssignEqual() if p.default is not None else cst.MaybeSentinel.DEFAULT),
                    )
                    self.stats["params"] += 1
            out.append(p)
        return out

    def _infer_param(self, p: cst.Param) -> Optional[str]:
        if p.default is not None:
            t = infer_literal_type(p.default)
            if t == "None":
                base = NAME_HINTS.get(p.name.value) if self.name_heuristics else None
                return f"Optional[{base}]" if base else None
            return t
        if self.name_heuristics:
            return NAME_HINTS.get(p.name.value)
        return None


class _StripAnnotations(ast.NodeTransformer):
    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Optional[ast.AST]:
        return None if node.module == "typing" else node


def _normalized_dump(tree: ast.AST) -> str:
    tree = _StripAnnotations().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False)


def validate(original: str, new: str, filename: str) -> None:
    try:
        new_tree = ast.parse(new, filename=filename)
    except SyntaxError as exc:
        msg = f"generated code does not parse: {exc}"
        raise ValueError(msg) from exc
    old_tree = ast.parse(original, filename=filename)
    if _normalized_dump(old_tree) != _normalized_dump(new_tree):
        msg = "generated code differs from the original beyond annotations"
        raise ValueError(msg)


def process_file(
    path: Path,
    *,
    dry_run: bool,
    show_diff: bool,
    backup: bool,
    add_returns: bool,
    name_heuristics: bool,
) -> int:
    source = path.read_text(encoding="utf-8")
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        print(f"[skip] {path}: cannot parse with libcst: {exc}", file=sys.stderr)
        return 1
    context = CodemodContext(filename=str(path))
    transformer = AnnotateTransformer(context, add_returns=add_returns, name_heuristics=name_heuristics)
    new_module = module.visit(transformer)
    new_module = AddImportsVisitor(context).transform_module(new_module)
    new_source = new_module.code
    if new_source == source:
        print(f"[ok]   {path}: nothing to do")
        return 0
    try:
        validate(source, new_source, str(path))
    except ValueError as exc:
        print(f"[FAIL] {path}: {exc} — file NOT modified", file=sys.stderr)
        return 1
    if show_diff:
        sys.stdout.writelines(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                new_source.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    s = transformer.stats
    if dry_run:
        print(f"[dry]  {path}: would add {s['params']} param + {s['returns']} return hints")
        return 0
    if backup:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(new_source, encoding="utf-8")
    print(f"[done] {path}: added {s['params']} param + {s['returns']} return hints")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Add simple type hints to .py files in place (libcst).")
    ap.add_argument("files", nargs="+", type=Path, help=".py file(s) to annotate")
    ap.add_argument("--dry-run", action="store_true", help="don't write, just report")
    ap.add_argument("--diff", action="store_true", help="print a unified diff")
    ap.add_argument("--backup", action="store_true", help="write FILE.py.bak before overwriting")
    ap.add_argument("--no-returns", action="store_true", help="only annotate parameters")
    ap.add_argument(
        "--name-heuristics",
        action="store_true",
        help="also guess types from parameter names (name→str, count→int, ...)",
    )
    args = ap.parse_args(argv)
    rc = 0
    for f in args.files:
        if not f.is_file() or f.suffix != ".py":
            print(f"[skip] {f}: not a .py file", file=sys.stderr)
            rc = 1
            continue
        rc |= process_file(
            f,
            dry_run=args.dry_run,
            show_diff=args.diff,
            backup=args.backup,
            add_returns=not args.no_returns,
            name_heuristics=args.name_heuristics,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
