#!/data/data/com.termux/files/usr/bin/env python
import argparse
import difflib
import os
import re
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

import libcst as cst


# ---------------------------------------------------------------------------
# Heuristic inference (default engine)
# ---------------------------------------------------------------------------


def infer_from_expr(expr):
    """Infer a simple builtin type name from a literal-ish expression."""
    if isinstance(expr, cst.Integer):
        return "int"
    if isinstance(expr, cst.Float):
        return "float"
    if isinstance(expr, cst.Imaginary):
        return "complex"
    if isinstance(expr, cst.UnaryOperation) and isinstance(expr.operator, (cst.Minus, cst.Plus)):
        return infer_from_expr(expr.expression)
    if isinstance(expr, cst.SimpleString):
        prefix = re.match(r"[A-Za-z]*", expr.value).group(0).lower()
        return "bytes" if "b" in prefix else "str"
    if isinstance(expr, cst.FormattedString):
        return "str"
    if isinstance(expr, cst.ConcatenatedString):
        return infer_from_expr(expr.left)
    if isinstance(expr, cst.Name):
        if expr.value in ("True", "False"):
            return "bool"
        if expr.value == "None":
            return "None"
    if isinstance(expr, cst.List):
        return "list"
    if isinstance(expr, cst.Dict):
        return "dict"
    if isinstance(expr, cst.Set):
        return "set"
    if isinstance(expr, cst.Tuple):
        return "tuple"
    return None


class _ReturnCollector(cst.CSTVisitor):
    """Collects return values in a function body, skipping nested scopes."""

    def __init__(self) -> None:
        self.returns = []
        self.has_yield = False

    def visit_FunctionDef(self, node) -> bool:
        return False

    def visit_ClassDef(self, node) -> bool:
        return False

    def visit_Lambda(self, node) -> bool:
        return False

    def visit_Yield(self, node) -> None:
        self.has_yield = True

    def visit_Return(self, node) -> None:
        self.returns.append(node.value)


def _last_is_exit(body) -> bool:
    """True if the block's last statement is a return or raise."""
    if isinstance(body, cst.SimpleStatementSuite):
        last = body.body[-1] if body.body else None
        return isinstance(last, (cst.Return, cst.Raise))
    last = body.body[-1] if body.body else None
    return bool(
        isinstance(last, cst.SimpleStatementLine) and last.body and isinstance(last.body[-1], (cst.Return, cst.Raise))
    )


def infer_return(func) -> str | None:
    """Return an annotation string for the function, or None if unsure."""
    if func.returns is not None:
        return None
    collector = _ReturnCollector()
    func.body.visit(collector)
    if collector.has_yield:
        return None  # generators: too ambiguous for a heuristic

    values = collector.returns
    exits = _last_is_exit(func.body)

    if not values:
        # A function ending in raise with no returns is NoReturn; skip it.
        return None if exits else "None"

    has_value = any(v is not None for v in values)
    if not exits:
        # Possible implicit fall-through to None: too ambiguous to annotate.
        return None if has_value else "None"

    types = set()
    for value in values:
        if value is None:
            types.add("None")
            continue
        t = infer_from_expr(value)
        if t is None:
            return None
        types.add(t)

    non_none = types - {"None"}
    if not non_none:
        return "None"
    if len(non_none) == 1:
        t = non_none.pop()
        return f"{t} | None" if "None" in types else t
    return None  # mixed types, e.g. int and str


class AnnotateTransformer(cst.CSTTransformer):
    def __init__(self) -> None:
        super().__init__()
        self.changes = 0

    def _annotate_params(self, params):
        out = []
        for p in params:
            if p.annotation is None and p.default is not None:
                t = infer_from_expr(p.default)
                if t and t != "None":
                    p = p.with_changes(
                        annotation=cst.Annotation(cst.Name(t)),
                        equal=cst.AssignEqual(
                            whitespace_before=cst.SimpleWhitespace(" "),
                            whitespace_after=cst.SimpleWhitespace(" "),
                        ),
                    )
                    self.changes += 1
            out.append(p)
        return out

    def leave_FunctionDef(self, original_node, updated_node):
        params = updated_node.params
        new_node = updated_node.with_changes(
            params=params.with_changes(
                posonly_params=self._annotate_params(params.posonly_params),
                params=self._annotate_params(params.params),
                kwonly_params=self._annotate_params(params.kwonly_params),
            )
        )

        if new_node.returns is None:
            rt = infer_return(original_node)
            if rt:
                new_node = new_node.with_changes(returns=cst.Annotation(cst.parse_expression(rt)))
                self.changes += 1
        return new_node


def annotate_heuristic(source: str):
    module = cst.parse_module(source)
    return module.visit(AnnotateTransformer()).code


# ---------------------------------------------------------------------------
# pyannotate engine (runtime type collection)
# ---------------------------------------------------------------------------


def annotate_with_pyannotate(path: Path, original: str):
    """Run the file, record observed types, and let pyannotate insert them.

    pyannotate writes into the file, so the original text is restored in a
    finally block. The caller decides whether to keep the annotated version.
    """
    try:
        from pyannotate_runtime import collect_types
    except ImportError:
        sys.exit("pyannotate is not installed. Run: pip install pyannotate")

    with tempfile.TemporaryDirectory() as tmp:
        type_info = Path(tmp) / "type_info.json"

        # Execute the file so pyannotate can observe real argument/return types.
        # Use the file's stem as module name: pyannotate ignores "__main__".
        sys.path.insert(0, str(path.parent))
        collect_types.init_types_collection()
        with collect_types.collect():
            runpy.run_path(str(path), run_name=path.stem)
        collect_types.dump_stats(str(type_info))

        cmd = [
            sys.executable,
            "-m",
            "pyannotate_tools.annotations",
            "--type-info",
            str(type_info),
            "--py3",  # emit annotation syntax instead of type comments
            "-w",  # write the file (we restore it below)
            "-q",  # suppress pyannotate's own diff output
            str(path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                sys.exit(f"pyannotate failed:\n{result.stderr or result.stdout}")
            annotated = path.read_text(encoding="utf-8")
        finally:
            path.write_text(original, encoding="utf-8")

    return annotated


# ---------------------------------------------------------------------------
# Colored diff output
# ---------------------------------------------------------------------------

BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
CYAN = "\033[36m"
RESET = "\033[0m"


def print_colored_diff(old: str, new, filename: str) -> None:
    use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    for raw in diff:
        line = raw.rstrip("\n")
        if not use_color:
            print(line)
            continue
        if line.startswith(("---", "+++")):
            print(f"{BOLD}{line}{RESET}")
        elif line.startswith("@@"):
            print(f"{CYAN}{line}{RESET}")
        elif line.startswith("+"):
            print(f"{GREEN}{line}{RESET}")
        elif line.startswith("-"):
            print(f"{RED}{line}{RESET}")
        else:
            print(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Add type annotations to a Python file in place.")
    parser.add_argument("file", help="Python file to annotate")
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Show a colored diff and do not modify the file (dry run).",
    )
    parser.add_argument(
        "--use-pyannotate",
        action="store_true",
        help="Use pyannotate's runtime type collection instead of heuristics. WARNING: this executes the file.",
    )
    args = parser.parse_args()

    path = Path(args.file).resolve()
    if not path.is_file() or path.suffix != ".py":
        sys.exit(f"Not a Python file: {path}")

    original = path.read_text(encoding="utf-8")

    if args.use_pyannotate:
        new_source = annotate_with_pyannotate(path, original)
    else:
        new_source = annotate_heuristic(original)

    # Make sure the result is still valid Python before touching anything.
    compile(new_source, str(path), "exec")

    if new_source == original:
        print("No annotations were added.")
        return

    if args.diff:
        print_colored_diff(original, new_source, path.name)
        print(f"\nDry run: {path} was not modified (drop --diff to write).")
    else:
        path.write_text(new_source, encoding="utf-8")
        print(f"Updated {path}")


if __name__ == "__main__":
    main()
