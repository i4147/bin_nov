#!/data/data/com.termux/files/usr/bin/env python
"""
Add an argparse fallback to typer-based CLI scripts when typer is missing.

Scans the given directory (default: cwd) recursively for `.py` files that use typer, and refactors them *in place* so they keep working even when typer is not installed.

Transformation performed on each matching file:

1. Wrap import typer in try/except. When typer is missing, typer is replaced with a stub module whose attributes are no-op callables that return their first positional argument (or default= kwarg). Any from typer import ... statements get similar stubs.

2. Replace app = typer.Typer(...) with a conditional that uses a _NoopApp shim when typer is unavailable. This way the @app.command() decorators still apply (as identity functions) and the command bodies remain defined.

3. Insert a generated _run_argparse() function that mirrors every typer command as an argparse subparser (positional args, --opt for defaults, --flag for bools, custom option names via typer.Option("--x", ...)).

4. Rewrite if __name__ == "__main__": to dispatch through typer if available, else through _run_argparse().

Files that don't fit the expected shape (no `__main__` guard, `*args/**kwargs` command signatures, etc.) are skipped with a message. Already-refactored files are detected via a sentinel comment and left alone.

Usage ----- python refactor_typer_fallback.py [PATH] [--dry-run] [-v]

"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

from dh import SKIP_DIRS, atomic_write

MARKER: str = "# __typer_argparse_fallback__"


def iter_py_files(root: Path) -> Iterator[Path]:
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


# ---------------------------------------------------------------- AST helpers


def find_typer_imports(tree: ast.Module):
    """All top-level typer import statements."""
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(a.name == "typer" and a.asname is None for a in node.names):
                nodes.append(node)
        elif isinstance(node, ast.ImportFrom) and node.module == "typer":
            nodes.append(node)
    return nodes


def find_typer_app(tree: ast.Module):
    """Locate ``name = typer.Typer(...)``; returns (name, node) or (None, None)."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Call):
                f = node.value.func
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr == "Typer"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "typer"
                ):
                    return tgt.id, node
    return None, None


def find_commands(tree: ast.Module, app_name: str):
    """Return list of (command_name, func_node) for ``@<app>.command()``."""
    cmds = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            ref = call.func if call else dec
            if not (
                isinstance(ref, ast.Attribute)
                and ref.attr in ("command", "callback")
                and isinstance(ref.value, ast.Name)
                and ref.value.id == app_name
            ):
                continue
            name = node.name
            if call:
                if (
                    call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    name = call.args[0].value
                else:
                    for kw in call.keywords:
                        if (
                            kw.arg == "name"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                        ):
                            name = kw.value.value
            cmds.append((name, node))
    return cmds


def find_main_guard(tree: ast.Module):
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            t = node.test
            if (
                isinstance(t.left, ast.Name)
                and t.left.id == "__name__"
                and len(t.comparators) == 1
                and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value == "__main__"
            ):
                return node
    return None


# ---------------------------------------------------------- signature parsing

_TYPER_CALLABLE_NAMES: tuple[str, str, str, str] = (
    "Option",
    "Argument",
    "option",
    "argument",
)


def _is_typer_callable_ref(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr in _TYPER_CALLABLE_NAMES:
        return True
    return isinstance(node, ast.Name) and node.id in _TYPER_CALLABLE_NAMES


def _extract_option_meta(call_node: ast.Call) -> tuple[list[Any],]:
    """Return (has_default, default_repr, option_names) from a typer.Option/Arg."""
    has_def, def_repr = False, None
    if call_node.args:
        a0 = call_node.args[0]
        if (
            isinstance(a0, ast.Constant)
            and a0.value is not Ellipsis
            and not (isinstance(a0.value, str) and a0.value.startswith("-"))
        ):
            has_def, def_repr = True, repr(a0.value)
        elif isinstance(a0, ast.Constant) and a0.value is Ellipsis:
            has_def, def_repr = False, None  # Ellipsis == required
    for kw in call_node.keywords:
        if kw.arg == "default" and isinstance(kw.value, ast.Constant):
            has_def, def_repr = True, repr(kw.value.value)
    option_names = [
        a.value
        for a in call_node.args
        if isinstance(a, ast.Constant)
        and isinstance(a.value, str)
        and a.value.startswith("-")
    ]
    return has_def, def_repr, option_names


def parse_annotation(ann: ast.AST | None):
    """Return dict: type_name, has_default, default_repr, option_names."""
    info = {
        "type_name": "str",
        "has_default": False,
        "default_repr": None,
        "option_names": [],
    }
    if ann is None:
        return info

    inner = ann
    metadata: list[ast.AST] = []
    if (
        isinstance(ann, ast.Subscript)
        and isinstance(ann.value, ast.Name)
        and ann.value.id == "Annotated"
    ):
        sl = ann.slice
        if isinstance(sl, ast.Tuple) and sl.elts:
            inner = sl.elts[0]
            metadata = list(sl.elts[1:])

    if isinstance(inner, ast.Name) and inner.id in ("str", "int", "float", "bool"):
        info["type_name"] = inner.id

    for m in metadata:
        if isinstance(m, ast.Call) and _is_typer_callable_ref(m.func):
            hd, dr, names = _extract_option_meta(m)
            if hd:
                info["has_default"] = True
                info["default_repr"] = dr
            info["option_names"].extend(names)
    return info


def _default_from_node(node: ast.AST):
    """Return (has_default, default_repr) from a default-value AST node."""
    if node is None:
        return False, None
    if isinstance(node, ast.Constant):
        return True, repr(node.value)
    if isinstance(node, ast.Call) and _is_typer_callable_ref(node.func):
        hd, dr, _ = _extract_option_meta(node)
        return (True, dr if hd else "None")
    try:
        return True, ast.unparse(node)
    except Exception:
        return True, "None"


def iter_params(fn: ast.FunctionDef) -> Iterator[tuple[Any, ...]]:
    """Yield (name, info_dict) for each parameter of ``fn``."""
    a = fn.args
    pos = list(a.posonlyargs) + list(a.args)
    defaults = list(a.defaults)
    first_def = len(pos) - len(defaults)

    for i, arg in enumerate(pos):
        if arg.arg in ("self", "cls"):
            continue
        info = parse_annotation(arg.annotation)
        dn = defaults[i - first_def] if i >= first_def else None
        hd, dr = _default_from_node(dn)
        if hd and not info["has_default"]:
            info["has_default"] = True
            info["default_repr"] = dr
        if isinstance(dn, ast.Call) and _is_typer_callable_ref(dn.func):
            info["option_names"].extend(_extract_option_meta(dn)[2])
        yield arg.arg, info

    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        info = parse_annotation(arg.annotation)
        hd, dr = _default_from_node(d)
        if hd and not info["has_default"]:
            info["has_default"] = True
            info["default_repr"] = dr
        if isinstance(d, ast.Call) and _is_typer_callable_ref(d.func):
            info["option_names"].extend(_extract_option_meta(d)[2])
        yield arg.arg, info


# -------------------------------------------------------------- code emission


def render_argparse_function(fn_name: str, cmds: Iterable[Any]) -> list[str]:
    L = [f"def {fn_name}():"]
    L.append("    import argparse")
    L.append("    parser = argparse.ArgumentParser()")
    L.append('    sub = parser.add_subparsers(dest="command", required=True)')

    for cmd_name, fn in cmds:
        doc = ast.get_docstring(fn) or ""
        help_str = repr(doc.splitlines()[0]) if doc else "None"
        L.append("")
        L.append(f"    p = sub.add_parser({cmd_name!r}, help={help_str})")
        call_parts = []
        for pname, info in iter_params(fn):
            tname = info["type_name"]
            if tname == "bool":
                dflt = info["default_repr"] if info["has_default"] else "False"
                L.append(
                    f'    p.add_argument("--{pname.replace("_", "-")}", action="store_true", default={dflt})'
                )
            elif not info["has_default"]:
                L.append(f"    p.add_argument({pname!r}, type={tname})")
            else:
                opts = [n for n in info["option_names"] if n.startswith("--")]
                if not opts:
                    opts = ["--" + pname.replace("_", "-")]
                L.append(
                    f"    p.add_argument({opts[0]!r}, dest={pname!r}, type={tname}, default={info['default_repr']})"
                )
            call_parts.append(f"{pname}=a.{pname}")

        call_str = ", ".join(call_parts)
        L.append(f"    p.set_defaults(func=lambda a: {fn.name}({call_str}))")

    L.append("")
    L.append("    args = parser.parse_args()")
    L.append("    args.func(args)")
    return L


def build_import_shim(
    node: ast.stmt, indent: str, flag_name: str, is_first: bool
) -> list[str]:
    marker = f"  {MARKER}" if is_first else ""
    if isinstance(node, ast.Import):
        return [
            f"{indent}try:\n",
            f"{indent}    import typer\n",
            f"{indent}    {flag_name} = True\n",
            f"{indent}except ImportError:\n",
            f"{indent}    {flag_name} = False{marker}\n",
            "\n",
            f"{indent}    class _TyperStubModule:\n",
            f"{indent}        def __getattr__(self, name):\n",
            f"{indent}            def _stub(*args, **kwargs):\n",
            f"{indent}                return args[0] if args else kwargs.get('default')\n",
            f"{indent}            return _stub\n",
            f"{indent}    typer = _TyperStubModule()\n",
        ]
    # ast.ImportFrom typer
    names_src = ", ".join(
        f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names
    )
    out = [
        f"{indent}try:\n",
        f"{indent}    from typer import {names_src}\n",
        f"{indent}except ImportError:\n",
    ]
    for a in node.names:
        target = a.asname or a.name
        out += [
            f"{indent}    def {target}(*args, **kwargs):\n",
            f"{indent}        return args[0] if args else kwargs.get('default')\n",
        ]
    return out


def _top_level_end(lines: list[str], start: int) -> int:
    """Index one past the last line belonging to the block beginning at ``start``."""
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if not s.strip():
            continue
        if not (s.startswith((" ", "\t"))):
            return i
    return len(lines)


def refactor(source: str, path: Path) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"  ! syntax error in {path}: {e}", file=sys.stderr)
        return None
    if MARKER in source:
        return None

    typer_imports = find_typer_imports(tree)
    if not typer_imports:
        return None
    app_name, app_node = find_typer_app(tree)
    if app_node is None:
        print(f"  - {path}: no `x = typer.Typer(...)` found; skipping")
        return None
    cmds = find_commands(tree, app_name)
    if not cmds:
        print(f"  - {path}: no @{app_name}.command() functions; skipping")
        return None
    for cmd_name, fn in cmds:
        if fn.args.vararg or fn.args.kwarg:
            print(f"  - {path}: command {cmd_name!r} uses *args/**kwargs; skipping")
            return None
    main_if = find_main_guard(tree)
    if main_if is None:
        print(f'  - {path}: no `if __name__ == "__main__":` block; skipping')
        return None

    # pick conflict-free helper names
    taken = {
        n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
    }

    def unique(base: str) -> str:
        name, i = base, 1
        while name in taken:
            name = f"{base}_{i}"
            i += 1
        taken.add(name)
        return name

    flag_name = unique("_TYPER_AVAILABLE")
    noop_name = unique("_NoopApp")
    run_name = unique("_run_argparse")

    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, int, list[str]]] = []

    # edit: typer imports
    for idx, node in enumerate(typer_imports):
        s, e = node.lineno - 1, node.end_lineno
        indent = lines[s][: len(lines[s]) - len(lines[s].lstrip())]
        edits.append((s, e, build_import_shim(node, indent, flag_name, idx == 0)))

    # edit: `app = typer.Typer(...)`
    s, e = app_node.lineno - 1, app_node.end_lineno
    indent = lines[s][: len(lines[s]) - len(lines[s].lstrip())]
    try:
        value_src = ast.unparse(app_node.value)
    except Exception:
        value_src = "typer.Typer()"
    edits.append(
        (
            s,
            e,
            [
                f"{indent}class {noop_name}:\n",
                f'{indent}    """Stand-in for typer.Typer when typer isn\'t installed."""\n',
                f"{indent}    def command(self, *args, **kwargs):\n",
                f"{indent}        if args and callable(args[0]) and not isinstance(args[0], str):\n",
                f"{indent}            return args[0]\n",
                f"{indent}        def _decorator(fn):\n",
                f"{indent}            return fn\n",
                f"{indent}        return _decorator\n",
                f"{indent}    callback = command\n",
                "\n",
                f"{indent}{app_name} = {value_src} if {flag_name} else {noop_name}()\n",
            ],
        )
    )

    # edit: main-guard body
    m_head = main_if.lineno - 1
    m_body = main_if.lineno
    m_end = _top_level_end(lines, m_head)
    body_indent = "    "
    for j in range(m_body, m_end):
        if lines[j].strip():
            body_indent = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
            break
    edits.append(
        (
            m_body,
            m_end,
            [
                f"{body_indent}if {flag_name}:\n",
                f"{body_indent}    {app_name}()\n",
                f"{body_indent}else:\n",
                f"{body_indent}    {run_name}()\n",
            ],
        )
    )

    # edit: insert generated _run_argparse just above the main guard
    argparse_lines = [l + "\n" for l in render_argparse_function(run_name, cmds)] + [
        "\n"
    ]
    edits.append((m_head, m_head, argparse_lines))

    # apply bottom-up so earlier line numbers stay valid
    edits.sort(key=lambda t: (t[0], t[1]), reverse=True)
    for s, e, new in edits:
        lines = lines[:s] + new + lines[e:]

    return "".join(lines)


# ------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Add argparse fallback to typer-based scripts when typer isn't installed."
    )
    ap.add_argument(
        "root", nargs="?", default=".", help="directory to scan (default: .)"
    )
    ap.add_argument("--dry-run", action="store_true", help="don't modify files")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    self_path = Path(__file__).resolve()
    targets = sorted(p for p in iter_py_files(root) if p.resolve() != self_path)
    print(f"Scanning {len(targets)} python file(s) under {root}...")

    changed = 0
    for path in targets:
        try:
            src = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            print(f"  ! cannot read {path}: {e}", file=sys.stderr)
            continue

        try:
            new = refactor(src, path)
        except Exception as e:
            print(f"  ! error on {path}: {e}", file=sys.stderr)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        if new is None or new == src:
            continue

        if args.dry_run:
            print(f"  ~ would refactor {path}")
            if args.verbose:
                print(new)
        else:
            try:
                atomic_write(path, new)
                print(f"  ✓ refactored {path}")
            except OSError as e:
                print(f"  ! cannot write {path}: {e}", file=sys.stderr)
                continue
        changed += 1

    verb = "would be" if args.dry_run else "were"
    print(f"Done. {changed} file(s) {verb} updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
