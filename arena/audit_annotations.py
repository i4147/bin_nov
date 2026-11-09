#!/usr/bin/env python3
"""Safety net for an annotation run.

Three checks, each of which must come back clean before the changes are kept:

1. **syntax** - every file still parses and ``compile()``s;
2. **undefined names** - every name used inside an annotation is imported or
   defined in that very file (a new hint must never introduce a ``NameError``
   under ``from __future__ import annotations``-less tooling, e.g. ``get_type_hints``);
3. **semantic drift** - the AST of the file with all annotations stripped must
   be identical to the AST of the same file at a reference git revision.  This
   catches accidental changes to expressions, defaults, decorators, ...

    python arena/audit_annotations.py                 # audit the working tree
    python arena/audit_annotations.py --rev HEAD      # compare against HEAD
    python arena/audit_annotations.py --rev HEAD~1 --paths '*.py'
"""
from __future__ import annotations

import argparse
import ast
import builtins
import fnmatch
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def normalize(tree: ast.AST) -> str:
    """AST dump with every annotation stripped, ``AnnAssign`` folded to ``Assign``."""
    import copy

    tree = copy.deepcopy(tree)
    for n in ast.walk(tree):
        if hasattr(n, "annotation"):
            n.annotation = None  # type: ignore[attr-defined]
        if hasattr(n, "returns"):
            n.returns = None  # type: ignore[attr-defined]
    for parent in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(parent, attr, None)
            if isinstance(body, list) and body:
                out = []
                for stmt in body:
                    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                        out.append(ast.Assign(targets=[stmt.target], value=stmt.value))
                    elif isinstance(stmt, ast.AnnAssign):
                        continue
                    else:
                        out.append(stmt)
                setattr(parent, attr, out)
    # imports are allowed to change (that is how the hints become importable)
    tree.body = [s for s in tree.body if not isinstance(s, (ast.Import, ast.ImportFrom))]
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def defined_names(tree: ast.AST) -> set[str]:
    names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        names.add(x.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def module_level_statements(tree: ast.Module) -> list[ast.stmt]:
    """Statements of the module and of every class body (not of functions)."""
    out: list[ast.stmt] = []

    def visit(body: list[ast.stmt]) -> None:
        for stmt in body:
            out.append(stmt)
            if isinstance(stmt, ast.ClassDef):
                visit(stmt.body)

    visit(tree.body)
    return out


def annotated_names(tree: ast.AST) -> set[str]:
    """Names used by annotations that are *evaluated* at import time.

    Local variable annotations (``x: int = 0`` inside a function) are never
    evaluated, so they cannot raise ``NameError`` and are skipped here.
    """
    used: set[str] = set()
    nodes: list[ast.AST] = list(module_level_statements(tree))
    nodes += [n for n in ast.walk(tree) if isinstance(n, (ast.arg, ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in nodes:
        ann = None
        if isinstance(node, ast.arg):
            ann = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ann = node.returns
        elif isinstance(node, ast.AnnAssign):
            ann = node.annotation
        if ann is None:
            continue
        # `x: "Foo"` is a forward reference: parse it as an expression.
        # Strings *inside* a subscript (Literal["a"], Annotated[..., "doc"])
        # are values, not names, and must not be treated as such.
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                continue
        for sub in ast.walk(ann):
            root = sub
            if isinstance(sub, ast.Attribute):
                while isinstance(root, ast.Attribute):
                    root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
    return used


def git_show(rev: str, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:  # a file outside the repository has no revision
        return None
    try:
        out = subprocess.run(
            ["git", "show", f"{rev}:{rel}"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return out.stdout if out.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rev", default="", help="git revision to compare against (default: none)")
    ap.add_argument("--paths", default="*.py", help="glob of the files to audit")
    ap.add_argument("--root", default="", help="directory to audit (default: the repo root)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    problems = {"syntax": [], "undefined": [], "drift": []}
    root = Path(args.root).resolve() if args.root else REPO_ROOT
    files = sorted(p for p in root.glob("*.py") if fnmatch.fnmatch(p.name, args.paths))
    for path in files:
        text = path.read_text()
        try:
            tree = ast.parse(text)
            compile(text, str(path), "exec")
        except SyntaxError as e:
            problems["syntax"].append(f"{path.name}: {e}")
            continue
        missing = annotated_names(tree) - defined_names(tree)
        if missing:
            problems["undefined"].append(f"{path.name}: {', '.join(sorted(missing))}")
        if args.rev:
            old = git_show(args.rev, path)
            if old is None:
                continue
            try:
                old_tree = ast.parse(old)
            except SyntaxError:
                continue
            if normalize(old_tree) != normalize(tree):
                problems["drift"].append(path.name)

    for kind, items in problems.items():
        if items and not args.quiet:
            print(f"\n{kind} ({len(items)}):")
            for item in items[:20]:
                print(f"  {item}")
            if len(items) > 20:
                print(f"  ... and {len(items) - 20} more")
    total = sum(len(v) for v in problems.values())
    print(f"\naudited {len(files)} files: syntax={len(problems['syntax'])} "
          f"undefined={len(problems['undefined'])} drift={len(problems['drift'])}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
