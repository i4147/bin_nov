#!/data/data/com.termux/files/usr/bin/env python
"""Rewrite the deprecated ``ast.Str``/``ast.Num``/``ast.Bytes``/``ast.NameConstant``/``ast.Ellipsis`` usages to ``ast.Constant``.
Finds the deprecated node constructors and the ``visit_Str``/``visit_Num``-style visitor methods, rewrites them with the right value type, and supports ``--apply`` (dry run by default), ``--diff``, ``--backup``, ``--stdin``, ``--json``, include/exclude globs and a verbose per-fix listing.
Usage: fixast [root] [-a] [--diff] [--backup] [--json] Positional arguments: root File or directory to scan (default: current directory) Options: -a, --apply Write fixes in place (default: dry run) -i, --include File glob(s) to scan; repeating replaces the default *.py -e, --exclude Extra file or directory glob to skip --diff Print unified diffs of proposed/applied fixes --backup Write <file>.bak before modifying --stdin Transform stdin to stdout instead of scanning files --json Emit machine-readable summary on stdout -v, --verbose List every individual fix with line:col -q, --quiet Suppress per-file output (summary only) --follow-symlinks Follow symlinked directories and files ...
plus 1 more; run ``fixast --help`` for the full list.
Examples: $ fixast --diff src/ $ fixast -a src/ Command-line script: run ``python fixast.py`` or the ``fixast`` command.
The module is importable as well."""

import argparse
import ast
import bisect
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
import difflib
import fnmatch
import io
import json
import os
from pathlib import Path
import re
import sys
import tokenize
from typing import Any, Final, Iterable

__version__: str = "1.1.0"
DEPRECATED_FAMILY: Final[dict[str, str]] = {
    "Str": "str",
    "Num": "num",
    "Bytes": "bytes",
    "NameConstant": "nameconst",
    "Ellipsis": "ellipsis",
}
FAMILY_NAMES: Final[dict[str, str]] = {v: k for k, v in DEPRECATED_FAMILY.items()}
VALUE_TYPES: Final[dict[str, str]] = {
    "str": "str",
    "num": "int, float, complex",
    "bytes": "bytes",
    "nameconst": "bool, type(None)",
    "ellipsis": "type(...)",
}
FAMILY_KWARGS: Final[dict[str, frozenset[str]]] = {
    "str": frozenset({"s"}),
    "num": frozenset({"n"}),
    "bytes": frozenset({"s"}),
    "nameconst": frozenset(),
    "ellipsis": frozenset(),
}
ATTR_TO_VALUE: Final[dict[str, str]] = {"s": "value", "n": "value"}
VISITOR_PREFIXES: Final[dict[str, str]] = {f"visit_{name}": "visit_Constant" for name in DEPRECATED_FAMILY}
DEFAULT_EXCLUDES: Final[frozenset[str]] = frozenset({
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".eggs",
    "node_modules",
    "build",
    "dist",
    ".pixi",
    ".idea",
    ".vscode",
})
DEFAULT_INCLUDES: Final[list[str]] = ["*.py"]
DRY_RUN_HINT: Final[str] = "dry run: no files were modified - re-run with -a/--apply to write changes"
_DEF_NAME_RE: Final[re.Pattern[str]] = re.compile(r"(?:async[ \t]+)?def[ \t]+([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True, slots=True)
class Edit:
    start: int
    end: int
    replacement: str
    line: int
    col: int
    message: str


@dataclass(slots=True)
class FileResult:
    path: Path
    fixes: list[Edit] = field(default_factory=list)
    flags: list[Edit] = field(default_factory=list)
    error: str | None = None
    diff: str | None = None
    written: bool = False


def _flatten_and(expr: ast.expr) -> Iterator[ast.expr]:
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        for value in expr.values:
            yield from _flatten_and(value)
    else:
        yield expr


def resolve_overlaps(edits: list[Edit]) -> list[Edit]:
    kept: list[Edit] = []
    for edit in sorted(edits, key=lambda e: (e.start, -e.end)):
        if kept and edit.start < kept[-1].end:
            continue
        kept.append(edit)
    return kept


def apply_edits(source: str, edits: Sequence[Edit]) -> str:
    result = source
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result


class DeprecatedFinder:
    def __init__(self, source: str) -> None:
        self.source = source
        self.lines: list[str] = source.splitlines(keepends=True)
        starts: list[int] = []
        offset = 0
        for line in self.lines:
            starts.append(offset)
            offset += len(line)
        self.line_starts: list[int] = starts
        self.edits: list[Edit] = []
        self.flags: list[Edit] = []
        self.ast_aliases: set[str] = {"ast"}
        self.local_deprecated: dict[str, str] = {}
        self.visitor_renames: dict[str, str] = {}
        self.dep_guard_segments: set[str] = set()

    def scan(self, tree: ast.Module) -> None:
        self._prescan(tree)
        self._visit_body(tree.body, frozenset())

    def _prescan(self, tree: ast.AST) -> None:
        bound: set[str] = set()
        func_names: set[str] = set()
        dep_imports: dict[str, str] = {}
        visitor_nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound.add(alias.asname or alias.name.split(".")[0])
                    if alias.name == "ast":
                        self.ast_aliases.add(alias.asname or "ast")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "ast" and node.level == 0:
                    for alias in node.names:
                        if alias.name in DEPRECATED_FAMILY:
                            dep_imports[alias.asname or alias.name] = DEPRECATED_FAMILY[alias.name]
                        elif alias.name == "*":
                            self._flag(node, "'from ast import *' cannot be analyzed - manual review needed")
                        else:
                            bound.add(alias.asname or alias.name)
                else:
                    for alias in node.names:
                        bound.add(alias.asname or alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(node.name)
                func_names.add(node.name)
                if node.name in VISITOR_PREFIXES:
                    visitor_nodes.append(node)
            elif isinstance(node, ast.ClassDef):
                bound.add(node.name)
            elif isinstance(
                node,
                (
                    ast.Assign,
                    ast.AnnAssign,
                    ast.AugAssign,
                    ast.NamedExpr,
                    ast.For,
                    ast.AsyncFor,
                    ast.With,
                    ast.AsyncWith,
                    ast.Delete,
                ),
            ):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                        bound.add(sub.id)
        self.local_deprecated = {k: v for k, v in dep_imports.items() if k not in bound}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Call, ast.Compare)):
                seg = self._guard_name(node)
                if seg:
                    self.dep_guard_segments.add(seg)
        if len({n.name for n in visitor_nodes}) == 1 and "visit_Constant" not in func_names:
            self.visitor_renames = dict(VISITOR_PREFIXES)
        elif visitor_nodes:
            for node in visitor_nodes:
                self._flag(
                    node,
                    "deprecated visitor method not auto-renamed (visit_Constant already defined or multiple deprecated visitors) - merge into visit_Constant manually",
                )

    def _abs(self, lineno: int, col: int) -> int:
        base = self.line_starts[lineno - 1]
        line = self.lines[lineno - 1]
        try:
            char_col = len(line.encode("utf-8")[:col].decode("utf-8"))
        except UnicodeDecodeError:
            char_col = len(line)
        return base + min(char_col, len(line))

    def _line_col(self, pos: int) -> tuple[int, int]:
        idx = bisect.bisect_right(self.line_starts, pos) - 1
        return idx + 1, pos - self.line_starts[idx] + 1

    def _seg(self, node: ast.AST) -> str | None:
        return ast.get_source_segment(self.source, node)

    def _add_edit(self, node: ast.AST, replacement: str, message: str) -> None:
        start = self._abs(node.lineno, node.col_offset)
        end = self._abs(node.end_lineno, node.end_col_offset)
        line, col = self._line_col(start)
        self.edits.append(Edit(start, end, replacement, line, col, message))

    def _add_attr_rename(self, value_node: ast.expr, attr_node: ast.Attribute, replacement: str, message: str) -> None:
        start = self._abs(value_node.end_lineno, value_node.end_col_offset)
        end = self._abs(attr_node.end_lineno, attr_node.end_col_offset)
        line, col = self._line_col(start)
        self.edits.append(Edit(start, end, replacement, line, col, message))

    def _add_token_rename(self, node: ast.AST, token_len: int, replacement: str, message: str) -> None:
        start = self._abs(node.lineno, node.col_offset)
        end = self._abs(node.lineno, node.col_offset + token_len)
        line, col = self._line_col(start)
        self.edits.append(Edit(start, end, replacement, line, col, message))

    def _flag(self, node: ast.AST, message: str) -> None:
        start = self._abs(node.lineno, node.col_offset)
        line, col = self._line_col(start)
        self.flags.append(Edit(start, start, "", line, col, message))

    def _family_of(self, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Attribute):
            if (
                expr.attr in DEPRECATED_FAMILY
                and isinstance(expr.value, ast.Name)
                and expr.value.id in self.ast_aliases
            ):
                return DEPRECATED_FAMILY[expr.attr]
            return None
        if isinstance(expr, ast.Name):
            return self.local_deprecated.get(expr.id)
        return None

    def _families_of(self, expr: ast.expr) -> tuple[str, ...] | None:
        if isinstance(expr, ast.Tuple):
            fams = tuple(self._family_of(e) for e in expr.elts)
            if fams and all(fams):
                return fams
            return None
        fam = self._family_of(expr)
        return (fam,) if fam else None

    def _constant_ref(self, expr: ast.expr) -> str:
        if isinstance(expr, ast.Attribute):
            return f"{expr.value.id}.Constant"
        return "Constant"

    def _guard_name(self, test: ast.expr) -> str | None:
        if (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "isinstance"
            and len(test.args) == 2
            and not test.keywords
        ) and self._families_of(test.args[1]):
            return self._seg(test.args[0])
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            return self._compare_guard_name(test)
        return None

    def _compare_guard_name(self, node: ast.Compare) -> str | None:
        if not isinstance(node.ops[0], (ast.Is, ast.Eq)):
            return None
        for typ_call, dep_ref in ((node.left, node.comparators[0]), (node.comparators[0], node.left)):
            if (
                isinstance(typ_call, ast.Call)
                and isinstance(typ_call.func, ast.Name)
                and typ_call.func.id == "type"
                and len(typ_call.args) == 1
                and self._family_of(dep_ref) is not None
            ):
                return self._seg(typ_call.args[0])
        return None

    def _guard_names(self, test: ast.expr) -> frozenset[str]:
        out: set[str] = set()
        for sub in _flatten_and(test):
            name = self._guard_name(sub)
            if name:
                out.add(name)
        return frozenset(out)

    def _neg_guard_names(self, test: ast.expr) -> frozenset[str]:
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            name = self._guard_name(test.operand)
            if name:
                return frozenset({name})
        return frozenset()

    def _target_names(self, node: ast.AST) -> set[str]:
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def _rebound(self, stmt: ast.stmt) -> set[str] | None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return {stmt.name}
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            return None
        out = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.ExceptHandler) and sub.name:
                out.add(sub.name)
        return out

    def _visit_body(self, stmts: list[ast.stmt], narrowed: frozenset[str]) -> None:
        for stmt in stmts:
            self._visit(stmt, narrowed)
            cleared = self._rebound(stmt)
            if cleared is None:
                narrowed = frozenset()
            else:
                narrowed = frozenset(
                    seg for seg in narrowed if not any(re.search(rf"\b{re.escape(name)}\b", seg) for name in cleared)
                )
            if isinstance(stmt, ast.Assert):
                seg = self._guard_name(stmt.test)
                if seg:
                    narrowed = narrowed | {seg}

    def _visit(self, node: ast.AST, narrowed: frozenset[str], annotation: bool = False) -> None:
        if isinstance(node, ast.Call):
            self._on_call(node, narrowed, annotation)
            return
        if isinstance(node, ast.Attribute):
            self._on_attribute(node, narrowed, annotation)
            return
        if isinstance(node, ast.Name):
            self._on_name(node, narrowed, annotation)
            return
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and self._try_type_compare(node):
            return
        if isinstance(node, ast.If):
            self._visit(node.test, narrowed)
            pos = self._guard_names(node.test)
            neg = self._neg_guard_names(node.test)
            self._visit_body(node.body, narrowed | pos)
            self._visit_body(node.orelse, narrowed | neg)
            return
        if isinstance(node, ast.While):
            self._visit(node.test, narrowed)
            pos = self._guard_names(node.test)
            neg = self._neg_guard_names(node.test)
            self._visit_body(node.body, narrowed | pos)
            self._visit_body(node.orelse, narrowed | neg)
            return
        if isinstance(node, ast.Assert):
            self._visit(node.test, narrowed)
            if node.msg is not None:
                self._visit(node.msg, narrowed)
            return
        if isinstance(node, ast.IfExp):
            self._visit(node.test, narrowed)
            self._visit(node.body, narrowed | self._guard_names(node.test))
            self._visit(node.orelse, narrowed | self._neg_guard_names(node.test))
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._on_function(node, narrowed)
            return
        if isinstance(node, ast.Lambda):
            self._visit(node.args, narrowed)
            self._visit(node.body, frozenset())
            return
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                self._visit(dec, narrowed)
            for tp in node.type_params:
                self._visit(tp, narrowed, True)
            for base in node.bases:
                self._visit(base, narrowed)
            for kw in node.keywords:
                self._visit(kw.value, narrowed)
            self._visit_body(node.body, frozenset())
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            self._on_comprehension(node, narrowed)
            return
        if isinstance(node, ast.AnnAssign):
            self._visit(node.annotation, narrowed, True)
            if node.value is not None:
                self._visit(node.value, narrowed)
            return
        if isinstance(node, ast.arg):
            if node.annotation is not None:
                self._visit(node.annotation, narrowed, True)
            return
        if isinstance(node, ast.TypeAlias):
            for tp in node.type_params:
                self._visit(tp, narrowed, True)
            self._visit(node.value, narrowed, True)
            return
        if isinstance(node, ast.ImportFrom):
            self._on_import_from(node)
            return
        if isinstance(node, ast.Import):
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child, narrowed, annotation and isinstance(child, ast.expr))

    def _on_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, narrowed: frozenset[str]) -> None:
        if node.name in self.visitor_renames:
            self._rename_def(node, self.visitor_renames[node.name])
        for dec in node.decorator_list:
            self._visit(dec, narrowed)
        for tp in node.type_params:
            self._visit(tp, narrowed, True)
        self._visit(node.args, narrowed)
        if node.returns is not None:
            self._visit(node.returns, narrowed, True)
        self._visit_body(node.body, frozenset())

    def _rename_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef, new_name: str) -> None:
        start = self._abs(node.lineno, node.col_offset)
        match = _DEF_NAME_RE.match(self.source, start)
        if match is None:
            self._flag(node, f"visitor method '{node.name}' could not be renamed safely")
            return
        line, col = self._line_col(match.start(1))
        self.edits.append(
            Edit(match.start(1), match.end(1), new_name, line, col, f"visitor dispatch '{node.name}' -> '{new_name}'")
        )

    def _on_comprehension(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp, narrowed: frozenset[str]
    ) -> None:
        cur = narrowed
        for comp in node.generators:
            self._visit(comp.iter, cur)
            cur = cur - self._target_names(comp.target)
            for iff in comp.ifs:
                self._visit(iff, cur)
                cur = cur | self._guard_names(iff)
        if isinstance(node, ast.DictComp):
            self._visit(node.key, cur)
            self._visit(node.value, cur)
        else:
            self._visit(node.elt, cur)

    def _on_call(self, node: ast.Call, narrowed: frozenset[str], annotation: bool) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
            and not node.keywords
            and self._try_isinstance(node)
        ):
            return
        fam = self._family_of(node.func)
        if fam is not None:
            self._fix_constructor(node, fam)
            for arg in node.args:
                self._visit(arg, narrowed, annotation)
            for kw in node.keywords:
                self._visit(kw.value, narrowed, annotation)
            return
        if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) == 2 and not node.keywords:
            if not annotation and narrowed and self._try_getattr(node, narrowed):
                return
            if (
                isinstance(node.args[0], ast.Name)
                and node.args[0].id in self.ast_aliases
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in DEPRECATED_FAMILY
            ):
                self._flag(node, f"dynamic getattr(ast, '{node.args[1].value}') - manual fix needed")
                return
            if (
                isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in ATTR_TO_VALUE
            ):
                seg = self._seg(node.args[0])
                if seg is not None and seg in self.dep_guard_segments:
                    self._flag(
                        node,
                        f"getattr({seg}, '{node.args[1].value}') cannot be proven narrowed to ast.Constant - manual review recommended",
                    )
                    return
        self._visit(node.func, narrowed, annotation)
        for arg in node.args:
            self._visit(arg, narrowed, annotation)
        for kw in node.keywords:
            self._visit(kw.value, narrowed, annotation)

    def _try_isinstance(self, call: ast.Call) -> bool:
        target, second = call.args
        seg_t = self._seg(target)
        if isinstance(second, ast.Tuple):
            fams = [self._family_of(e) for e in second.elts]
            if not any(fams):
                return False
            if any(
                f is None and (isinstance(e, ast.Starred) or self._seg(e) is None) for f, e in zip(fams, second.elts)
            ):
                self._flag(
                    call,
                    "isinstance(...) with deprecated constant class could not be rewritten safely - manual fix needed",
                )
                return True
            dep_elems = [e for f, e in zip(fams, second.elts) if f]
            base = "Constant" if any(isinstance(e, ast.Name) for e in dep_elems) else "ast.Constant"
            disjuncts: list[str] = []
            i = 0
            while i < len(second.elts):
                if fams[i]:
                    types: list[str] = []
                    while i < len(second.elts) and fams[i]:
                        types.append(VALUE_TYPES[fams[i]])
                        i += 1
                    vt = ", ".join(dict.fromkeys(types))
                    vt = f"({vt})" if "," in vt else vt
                    disjuncts.append(f"(isinstance({seg_t}, {base}) and isinstance({seg_t}.value, {vt}))")
                else:
                    disjuncts.append(f"isinstance({seg_t}, {self._seg(second.elts[i])})")
                    i += 1
            joined = " or ".join(disjuncts)
            repl = joined if len(disjuncts) == 1 else f"({joined})"
            self._add_edit(call, repl, "isinstance(..., deprecated constant class tuple) -> ast.Constant value check")
            return True
        fam = self._family_of(second)
        if fam is None:
            return False
        if seg_t is None:
            self._flag(call, "isinstance(...) target segment unavailable - manual fix needed")
            return True
        base = self._constant_ref(second)
        vt = VALUE_TYPES[fam]
        vt = f"({vt})" if "," in vt else vt
        repl = f"(isinstance({seg_t}, {base}) and isinstance({seg_t}.value, {vt}))"
        self._add_edit(call, repl, f"isinstance(..., ast.{FAMILY_NAMES[fam]}) -> ast.Constant value check")
        return True

    def _try_type_compare(self, node: ast.Compare) -> bool:
        positive = isinstance(node.ops[0], (ast.Is, ast.Eq))
        negative = isinstance(node.ops[0], (ast.IsNot, ast.NotEq))
        if not positive and not negative:
            return False
        for typ_call, dep_ref in ((node.left, node.comparators[0]), (node.comparators[0], node.left)):
            if (
                isinstance(typ_call, ast.Call)
                and isinstance(typ_call.func, ast.Name)
                and typ_call.func.id == "type"
                and len(typ_call.args) == 1
                and not typ_call.keywords
            ):
                fam = self._family_of(dep_ref)
                if fam is None:
                    continue
                seg = self._seg(typ_call.args[0])
                if seg is None:
                    self._flag(
                        node,
                        "type(...) comparison with deprecated constant class could not be rewritten safely - manual fix needed",
                    )
                    return True
                base = self._constant_ref(dep_ref)
                vt = VALUE_TYPES[fam]
                vt = f"({vt})" if "," in vt else vt
                core = f"isinstance({seg}, {base}) and isinstance({seg}.value, {vt})"
                repl = f"({core})" if positive else f"(not ({core}))"
                word = "is" if positive else "is not"
                self._add_edit(node, repl, f"type(...) {word} ast.{FAMILY_NAMES[fam]} -> ast.Constant value check")
                return True
        return False

    def _fix_constructor(self, call: ast.Call, fam: str) -> None:
        old = f"ast.{FAMILY_NAMES[fam]}" if isinstance(call.func, ast.Attribute) else FAMILY_NAMES[fam]
        if fam == "ellipsis":
            if not call.args and not call.keywords:
                self._add_edit(call, "ast.Constant(...)", "ast.Ellipsis() -> ast.Constant(...)")
            else:
                self._flag(call, "ast.Ellipsis(...) with arguments needs manual rewrite to ast.Constant(...)")
            return
        for kw in call.keywords:
            if kw.arg in FAMILY_KWARGS[fam]:
                self._add_token_rename(kw, len(kw.arg.encode("utf-8")), "value", f"keyword '{kw.arg}=' -> 'value='")
        repl = self._constant_ref(call.func)
        self._add_edit(call.func, repl, f"{old}(...) -> {repl}(...)")

    def _try_getattr(self, call: ast.Call, narrowed: frozenset[str]) -> bool:
        target, name = call.args
        if isinstance(name, ast.Constant) and isinstance(name.value, str) and name.value in ATTR_TO_VALUE:
            seg = self._seg(target)
            if seg is not None and seg in narrowed:
                self._add_edit(call, f"{seg}.value", f"getattr({seg}, '{name.value}') -> {seg}.value")
                return True
        return False

    def _on_attribute(self, node: ast.Attribute, narrowed: frozenset[str], annotation: bool) -> None:
        fam = self._family_of(node)
        if fam is not None:
            if annotation:
                repl = self._constant_ref(node)
                self._add_edit(node, repl, f"annotation ast.{FAMILY_NAMES[fam]} -> {repl}")
            else:
                self._flag(
                    node,
                    f"ambiguous reference to ast.{FAMILY_NAMES[fam]} - manual fix needed (use ast.Constant + value type check)",
                )
            return
        if node.attr in self.visitor_renames:
            new = self.visitor_renames[node.attr]
            self._add_attr_rename(node.value, node, f".{new}", f"visitor reference '{node.attr}' -> '{new}'")
            return
        if node.attr in ATTR_TO_VALUE:
            seg = self._seg(node.value)
            if seg is not None:
                if seg in narrowed:
                    self._add_attr_rename(
                        node.value, node, ".value", f".{node.attr} -> .value (narrowed to ast.Constant)"
                    )
                    return
                if seg in self.dep_guard_segments:
                    self._flag(
                        node,
                        f".{node.attr} on '{seg}' cannot be proven narrowed to ast.Constant - manual review recommended",
                    )
                    return
        self._visit(node.value, narrowed, annotation)

    def _on_name(self, node: ast.Name, narrowed: frozenset[str], annotation: bool) -> None:
        if node.id in self.visitor_renames:
            self._add_token_rename(
                node,
                len(node.id.encode("utf-8")),
                self.visitor_renames[node.id],
                f"visitor reference '{node.id}' -> '{self.visitor_renames[node.id]}'",
            )
            return
        fam = self.local_deprecated.get(node.id)
        if fam is None:
            return
        if annotation:
            self._add_edit(node, "Constant", f"annotation {node.id} -> Constant")
        else:
            self._flag(node, f"ambiguous reference to '{node.id}' (ast.{FAMILY_NAMES[fam]}) - manual fix needed")

    def _on_import_from(self, node: ast.ImportFrom) -> None:
        if node.module != "ast" or node.level != 0:
            return
        if not any(alias.name in DEPRECATED_FAMILY for alias in node.names):
            return
        entries: list[str] = []
        seen: set[str] = set()
        for alias in node.names:
            if alias.name in DEPRECATED_FAMILY:
                text = f"Constant as {alias.asname}" if alias.asname else "Constant"
            elif alias.asname:
                text = f"{alias.name} as {alias.asname}"
            else:
                text = alias.name
            if text not in seen:
                seen.add(text)
                entries.append(text)
        if any(a.name in DEPRECATED_FAMILY and a.asname for a in node.names) and "Constant" not in seen:
            entries.append("Constant")
        first, last = node.names[0], node.names[-1]
        start = self._abs(first.lineno, first.col_offset)
        end = self._abs(last.end_lineno, last.end_col_offset)
        line, col = self._line_col(start)
        self.edits.append(
            Edit(start, end, ", ".join(entries), line, col, "import: deprecated ast constant classes -> ast.Constant")
        )


def process_file(path: Path, args: argparse.Namespace) -> FileResult:
    result = FileResult(path=path)
    try:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        source = raw.decode(encoding)
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError, RecursionError) as exc:
        result.error = f"cannot process: {exc.__class__.__name__}: {exc}"
        return result
    finder = DeprecatedFinder(source)
    finder.scan(tree)
    result.fixes = resolve_overlaps(finder.edits)
    result.flags = finder.flags
    if not result.fixes:
        return result
    fixed = apply_edits(source, result.fixes)
    try:
        ast.parse(fixed, filename=str(path))
    except (SyntaxError, RecursionError) as exc:
        result.error = f"post-fix parse check failed, file left untouched ({exc.__class__.__name__}: {exc})"
        result.fixes = []
        return result
    if args.diff:
        result.diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                fixed.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path) + " (fixed)",
            )
        )
    if args.apply:
        try:
            if args.backup:
                path.with_name(path.name + ".bak").write_bytes(raw)
            path.write_bytes(fixed.encode(encoding))
        except OSError as exc:
            result.error = f"write failed: {exc}"
            return result
        result.written = True
    return result


def _matches(name: str, rel: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) for pat in patterns)


def discover_files(root: Path, include: Sequence[str], exclude: Sequence[str], follow: bool) -> list[Path]:
    if root.is_file():
        return [root]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow):
        here = Path(dirpath)
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = (here / name).relative_to(root).as_posix()
            if name in DEFAULT_EXCLUDES or _matches(name, rel, exclude):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = here / name
            if path.is_symlink() and not follow:
                continue
            rel = path.relative_to(root).as_posix()
            if _matches(name, rel, exclude):
                continue
            if _matches(name, rel, include):
                files.append(path)
    return files


def run_stdin(args: argparse.Namespace) -> int:
    source = sys.stdin.read()
    try:
        tree = ast.parse(source, filename="<stdin>")
    except SyntaxError as exc:
        print(f"<stdin>: parse error: {exc}", file=sys.stderr)
        return 2
    finder = DeprecatedFinder(source)
    finder.scan(tree)
    fixes = resolve_overlaps(finder.edits)
    fixed = apply_edits(source, fixes) if fixes else source
    for flag in finder.flags:
        print(f"<stdin>:{flag.line}:{flag.col}: [ambiguous] {flag.message}", file=sys.stderr)
    if args.diff and fixes:
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                fixed.splitlines(keepends=True),
                fromfile="<stdin>",
                tofile="<stdin> (fixed)",
            )
        )
        sys.stderr.write(diff)
    sys.stdout.write(fixed)
    print(f"<stdin>: fixes={len(fixes)} ambiguous={len(finder.flags)} mode=stream", file=sys.stderr)
    return 1 if finder.flags else 0


def print_report(results: Sequence[FileResult], args: argparse.Namespace) -> int:
    total_fixes = sum(len(r.fixes) for r in results)
    total_flags = sum(len(r.flags) for r in results)
    total_errors = sum(1 for r in results if r.error)
    if args.json:
        payload = {
            "mode": "apply" if args.apply else "dry-run",
            "root": str(args.root),
            "scanned": len(results),
            "files_with_fixes": sum(1 for r in results if r.fixes),
            "files_written": sum(1 for r in results if r.written),
            "fixes": total_fixes,
            "ambiguous": [
                {"file": str(r.path), "line": f.line, "col": f.col, "message": f.message}
                for r in results
                for f in r.flags
            ],
            "errors": [{"file": str(r.path), "message": r.error} for r in results if r.error],
        }
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            for f in r.flags:
                print(f"{r.path}:{f.line}:{f.col}: [ambiguous] {f.message}", file=sys.stderr)
        for r in results:
            if r.error:
                print(f"{r.path}: ERROR {r.error}", file=sys.stderr)
        if not args.quiet:
            for r in results:
                if args.verbose:
                    for f in r.fixes:
                        print(f"{r.path}:{f.line}:{f.col}: {f.message}")
                if r.fixes:
                    status = "applied" if r.written else "pending"
                    print(f"{r.path}: {len(r.fixes)} fix(es) [{status}]")
                if r.diff:
                    print(r.diff, end="")
        mode = "apply" if args.apply else "dry-run"
        print(f"scanned={len(results)} fixes={total_fixes} ambiguous={total_flags} errors={total_errors} mode={mode}")
        if not args.apply and (total_fixes or total_flags):
            print(DRY_RUN_HINT, file=sys.stderr)
        if total_flags:
            print(f"{total_flags} ambiguous reference(s) need manual review", file=sys.stderr)
    if total_errors:
        return 2
    pending = any(r.fixes and not r.written for r in results) or total_flags > 0
    return 1 if pending else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fix-ast-str",
        description=(
            "Find and fix deprecated ast.Str / ast.Num / ast.Bytes / ast.NameConstant / ast.Ellipsis "
            "usages (removed in CPython 3.14) by rewriting them to ast.Constant with value type checks. "
            "Runs as a dry run by default; files are only modified with -a/--apply."
        ),
        epilog=(
            "examples:\n"
            "  fix_ast_str.py                        dry run over the current directory tree\n"
            "  fix_ast_str.py -a --backup            apply fixes in place, keep .bak copies\n"
            "  fix_ast_str.py src --diff -v          show unified diffs and every individual fix\n"
            "  fix_ast_str.py --stdin < tool.py      transform stdin to stdout\n"
            "\n"
            "exit codes: 0 = clean/success, 1 = pending fixes or ambiguous refs, 2 = errors\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root", nargs="?", default=".", help="file or directory to scan (default: current directory)")
    parser.add_argument("-a", "--apply", action="store_true", help="write fixes in place (default: dry run)")
    parser.add_argument(
        "-i",
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="file glob(s) to scan; repeating replaces the default *.py",
    )
    parser.add_argument(
        "-e", "--exclude", action="append", default=[], metavar="GLOB", help="extra file or directory glob to skip"
    )
    parser.add_argument("--diff", action="store_true", help="print unified diffs of proposed/applied fixes")
    parser.add_argument("--backup", action="store_true", help="write <file>.bak before modifying")
    parser.add_argument("--stdin", action="store_true", help="transform stdin to stdout instead of scanning files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary on stdout")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every individual fix with line:col")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress per-file output (summary only)")
    parser.add_argument("--follow-symlinks", action="store_true", help="follow symlinked directories and files")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__} (target: CPython 3.14)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.stdin:
        return run_stdin(args)
    root = Path(args.root)
    if not root.exists():
        print(f"error: root does not exist: {root}", file=sys.stderr)
        return 2
    includes = args.include or list(DEFAULT_INCLUDES)
    files = discover_files(root, includes, args.exclude, args.follow_symlinks)
    results = [process_file(path, args) for path in files]
    return print_report(results, args)


if __name__ == "__main__":
    sys.exit(main())
