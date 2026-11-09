#!/data/data/com.termux/files/usr/bin/env python
"""Split the constants, classes, functions and imports of the given sources into separate files.
Parses Python with ast, libcst or tree-sitter (``--backend``), classifies every top-level definition and writes the groups into ``output/`` as ``const.py``, ``classes.py``, ``funcs.py`` and ``imports.py``; the ``--const``/``--class``/``--function``/``--import`` flags restrict the kinds that are extracted.
Usage: xentity [paths ...] [-b auto|ast|libcst|tree-sitter] Positional arguments: paths ...
Options: -b, --backend --const --class --function --import Examples: $ xentity src/ --function --class Command-line script: run ``python xentity.py`` or the ``xentity`` command.
The module is importable as well."""

from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import multiprocessing as mp
import os
from pathlib import Path
import textwrap
import time
from typing import Iterator

from dh import SKIP_DIRS
from rich.console import Console
from rich.text import Text


OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)

ALL_KINDS = {"class", "function", "const", "import"}
ORDER = ("const", "class", "function", "import")

_FILENAME_MAP = {
    frozenset({"const"}): "const.py",
    frozenset({"class"}): "classes.py",
    frozenset({"function"}): "funcs.py",
    frozenset({"import"}): "imports.py",
}

_PARSER: object | None = None


def _is_const_name(name: str) -> bool:
    return bool(name) and name.isupper() and name.replace("_", "").isalnum()


def _make_ts_parser() -> object:
    from tree_sitter import Language, Parser
    import tree_sitter_python as tsp

    parser = Parser()
    parser.language = Language(tsp.language())
    return parser


def _init_worker(backend: str) -> None:
    global _PARSER
    _PARSER = _make_ts_parser() if backend == "ts" else None


def _try_types_ast() -> tuple:
    if hasattr(ast, "TryStar"):
        return (ast.Try, ast.TryStar)
    return (ast.Try,)


def _filter_trys_and_imports(
    import_nodes: list[tuple[object, tuple[object, ...]]],
    try_nodes: list[tuple[object, tuple[object, ...]]],
) -> list[object]:
    try_ids_with_imports: set[int] = set()
    for _imp, anc in import_nodes:
        for a in anc:
            try_ids_with_imports.add(id(a))

    candidate_trys = [(t, anc) for t, anc in try_nodes if id(t) in try_ids_with_imports]
    candidate_ids = {id(t) for t, _ in candidate_trys}
    outermost_trys = [t for t, anc in candidate_trys if not any(id(a) in candidate_ids for a in anc)]

    try_ids = {id(t) for t in outermost_trys}
    individual_imports = [imp for imp, anc in import_nodes if not any(id(a) in try_ids for a in anc)]
    return outermost_trys + individual_imports


def _ts_line_start(src: bytes, pos: int) -> int:
    while pos > 0 and src[pos - 1] not in (10, 13):
        pos -= 1
    return pos


def _ts_classify(node: object, src: bytes) -> str | None:
    t = node.type
    if t == "class_definition":
        return "class"
    if t == "function_definition":
        return "function"
    if t == "decorated_definition":
        for child in node.children:
            if child.type == "class_definition":
                return "class"
            if child.type == "function_definition":
                return "function"
    if t == "expression_statement":
        for child in node.children:
            if child.type == "assignment":
                left = child.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    name = src[left.start_byte : left.end_byte].decode()
                    if _is_const_name(name):
                        return "const"
    return None


def _find_ts_imports(root: object) -> list[object]:
    import_nodes: list[tuple[object, tuple[object, ...]]] = []
    try_nodes: list[tuple[object, tuple[object, ...]]] = []

    def walk(node, ancestors):
        if node.type in ("import_statement", "import_from_statement"):
            import_nodes.append((node, tuple(ancestors)))
            return
        if node.type == "try_statement":
            try_nodes.append((node, tuple(ancestors)))
        ancestors.append(node)
        for child in node.children:
            walk(child, ancestors)
        ancestors.pop()

    walk(root, [])
    result = _filter_trys_and_imports(import_nodes, try_nodes)
    result.sort(key=lambda n: n.start_byte)
    return result


def _extract_ts(src: bytes, parser: object, want: set[str]) -> list[tuple[str, str]]:
    root = parser.parse(src).root_node
    out: list[tuple[str, str]] = []
    for node in root.children:
        kind = _ts_classify(node, src)
        if kind in want:
            out.append((kind, src[node.start_byte : node.end_byte].decode()))
    if "import" in want:
        for node in _find_ts_imports(root):
            start = _ts_line_start(src, node.start_byte)
            text = src[start : node.end_byte].decode()
            out.append(("import", textwrap.dedent(text)))
    return out


def _cst_classify(node: object, cst: object) -> str | None:
    if isinstance(node, cst.ClassDef):
        return "class"
    if isinstance(node, cst.FunctionDef):
        return "function"
    if isinstance(node, cst.Assign):
        for target in node.targets:
            inner = target.target
            if isinstance(inner, cst.Name) and _is_const_name(inner.value):
                return "const"
    if isinstance(node, cst.AnnAssign) and isinstance(node.target, cst.Name) and _is_const_name(node.target.value):
        return "const"
    return None


def _make_cst_visitor() -> object:
    import libcst as cst

    class _Visitor(cst.CSTVisitor):
        def __init__(self) -> None:
            self.stack: list[object] = []
            self.imports: list[tuple[object, tuple[object, ...]]] = []
            self.trys: list[tuple[object, tuple[object, ...]]] = []

        def on_visit(self, node: object) -> bool:
            if isinstance(node, (cst.Import, cst.ImportFrom)):
                self.imports.append((node, tuple(self.stack)))
            if isinstance(node, cst.Try):
                self.trys.append((node, tuple(self.stack)))
            self.stack.append(node)
            return True

        def on_leave(self, original_node: object) -> None:
            self.stack.pop()

    return _Visitor()


def _extract_libcst(src: str, want: set[str]) -> list[tuple[str, str]]:
    import libcst as cst
    from libcst.metadata import MetadataWrapper, PositionProvider

    module = cst.parse_module(src)
    out: list[tuple[str, str]] = []
    for stmt in module.body:
        kind = _cst_classify(stmt, cst)
        if kind in want:
            out.append((kind, module.code_for_node(stmt).rstrip()))
    if "import" in want:
        wrapper = MetadataWrapper(module)
        positions = wrapper.resolve(PositionProvider)
        visitor = _make_cst_visitor()
        wrapper.module.visit(visitor)
        result = _filter_trys_and_imports(visitor.imports, visitor.trys)
        lines = src.splitlines()
        for node in result:
            pos = positions[node]
            s = pos.start.line
            e = pos.end.line if pos.end.column > 0 else pos.end.line - 1
            text = "\n".join(lines[s - 1 : e])
            out.append(("import", textwrap.dedent(text)))
    return out


def _ast_classify(node: ast.AST) -> str | None:
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "function"
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and _is_const_name(target.id):
                return "const"
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and _is_const_name(node.target.id):
        return "const"
    return None


def _find_ast_imports(tree: ast.AST) -> list[ast.AST]:
    import_nodes: list[tuple[ast.AST, tuple[ast.AST, ...]]] = []
    try_nodes: list[tuple[ast.AST, tuple[ast.AST, ...]]] = []
    try_types = _try_types_ast()

    def walk(node: ast.AST, ancestors: list[ast.AST]) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_nodes.append((node, tuple(ancestors)))
            return
        if isinstance(node, try_types):
            try_nodes.append((node, tuple(ancestors)))
        ancestors.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child, ancestors)
        ancestors.pop()

    walk(tree, [])
    result = _filter_trys_and_imports(import_nodes, try_nodes)
    result.sort(key=lambda n: n.lineno)
    return result


def _extract_ast(src: str, want: set[str]) -> list[tuple[str, str]]:
    tree = ast.parse(src)
    lines = src.splitlines()
    out: list[tuple[str, str]] = []
    for node in tree.body:
        kind = _ast_classify(node)
        if kind is None or kind not in want:
            continue
        decorators = getattr(node, "decorator_list", None)
        start = decorators[0].lineno if decorators else node.lineno
        end = node.end_lineno or node.lineno
        out.append((kind, "\n".join(lines[start - 1 : end])))
    if "import" in want:
        for node in _find_ast_imports(tree):
            start = node.lineno
            end = node.end_lineno or node.lineno
            text = "\n".join(lines[start - 1 : end])
            out.append(("import", textwrap.dedent(text)))
    return out


def _extract(
    backend: str,
    src_bytes: bytes,
    src_str: str,
    parser: object | None,
    want: set[str],
) -> list[tuple[str, str]]:
    if backend == "ts":
        return _extract_ts(src_bytes, parser, want)
    if backend == "libcst":
        return _extract_libcst(src_str, want)
    if backend == "ast":
        return _extract_ast(src_str, want)
    msg = f"Unknown backend: {backend}"
    raise ValueError(msg)


def _is_python_file(path: Path) -> bool:
    if path.is_symlink():
        return False
    if path.suffix == ".py":
        return True
    if path.suffix:
        return False
    try:
        with open(path, "rb") as f:
            first = f.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def _walk_dir(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        base = Path(dirpath)
        for fn in filenames:
            p = base / fn
            if p.is_symlink():
                continue
            if _is_python_file(p):
                yield p


def walk_py_files(inputs: list[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    out_resolved = OUT_DIR.resolve()
    for inp in inputs:
        if inp.is_symlink():
            continue
        if inp.is_file():
            if _is_python_file(inp):
                rp = inp.resolve()
                if rp not in seen and out_resolved not in rp.parents:
                    seen.add(rp)
                    yield inp
        elif inp.is_dir():
            for p in _walk_dir(inp):
                rp = p.resolve()
                if rp in seen or out_resolved in rp.parents:
                    continue
                seen.add(rp)
                yield p


def _select_kinds(args: argparse.Namespace) -> set[str]:
    want: set[str] = set()
    if args.const:
        want.add("const")
    if args.classes:
        want.add("class")
    if args.function:
        want.add("function")
    if args.imports:
        want.add("import")
    return want or set(ALL_KINDS)


def _output_filename(want: set[str]) -> str:
    return _FILENAME_MAP.get(frozenset(want), "extracted.py")


def _worker(
    task: tuple[str, str, list[str]],
) -> tuple[str, dict[str, int], list[str]]:
    path_str, backend, want_list = task
    want = set(want_list)
    path = Path(path_str)
    try:
        src_bytes = path.read_bytes()
        src_str = src_bytes.decode()
    except (OSError, UnicodeDecodeError):
        return path_str, dict.fromkeys(want, 0), []

    try:
        items = _extract(backend, src_bytes, src_str, _PARSER, want)
    except Exception:
        return path_str, dict.fromkeys(want, 0), []

    counts: dict[str, int] = dict.fromkeys(want, 0)
    texts: list[str] = []
    for kind, text in items:
        counts[kind] = counts.get(kind, 0) + 1
        texts.append(text)
    return path_str, counts, texts


def _truncate(s: str, w: int) -> str:
    if len(s) <= w:
        return s
    if w <= 1:
        return s[:w]
    return s[: w - 1] + "…"


def _display(
    i: int,
    n: int,
    path_str: str,
    counts: dict[str, int],
    want: set[str],
    console: Console,
) -> None:
    ordered = [k for k in ORDER if k in want]
    pairs: list[tuple[int, str]] = []
    for k in ordered:
        v = counts.get(k, 0)
        pairs.append((v, "green" if v > 0 else "dim"))

    counts_len = sum(len(str(v)) for v, _ in pairs) + max(0, len(pairs) - 1)
    idx = f"[{i}/{n}]"
    fname = Path(path_str).name
    width = console.width or 80
    fname_w = max(width - len(idx) - counts_len - 3, 8)
    fname_disp = _truncate(fname, fname_w).ljust(fname_w)

    text = Text()
    text.append(idx + " ", style="cyan")
    text.append(fname_disp)
    text.append(" ")
    for j, (v, color) in enumerate(pairs):
        if j > 0:
            text.append("|")
        text.append(str(v), style=color)
    console.print(text, highlight=False, soft_wrap=True)


def _rel_folder(py: Path) -> Path:
    try:
        return py.resolve().parent.relative_to(Path.cwd().resolve())
    except ValueError:
        return Path("_external") / py.resolve().parent.name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("-b", "--backend", choices=("ts", "libcst", "ast"), default="ts")
    ap.add_argument("--const", action="store_true")
    ap.add_argument("--class", dest="classes", action="store_true")
    ap.add_argument("--function", action="store_true")
    ap.add_argument("--import", dest="imports", action="store_true")
    args = ap.parse_args()

    want = _select_kinds(args)
    inputs = args.paths or [Path()]
    console = Console()

    start = time.perf_counter()
    files = list(walk_py_files(inputs))
    n = len(files)

    if n == 0:
        console.print("[yellow]No Python files found.[/yellow]")
        return

    tasks = [(str(f), args.backend, sorted(want)) for f in files]
    results: list[tuple[str, dict[str, int], list[str]]] = []

    if n > 1:
        with mp.Pool(4, initializer=_init_worker, initargs=(args.backend,)) as pool:
            for i, res in enumerate(pool.imap_unordered(_worker, tasks), 1):
                results.append(res)
                _display(i, n, res[0], res[1], want, console)
    else:
        _init_worker(args.backend)
        for i, task in enumerate(tasks, 1):
            res = _worker(task)
            results.append(res)
            _display(i, n, res[0], res[1], want, console)

    out_name = _output_filename(want)
    folder_items: defaultdict[Path, list[str]] = defaultdict(list)
    for path_str, _counts, texts in results:
        if texts:
            folder_items[_rel_folder(Path(path_str))].append("\n\n".join(texts))

    for folder, contents in folder_items.items():
        if not contents:
            continue
        out_file = OUT_DIR / folder / out_name
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("\n\n".join(contents))

    elapsed = time.perf_counter() - start
    console.print(
        f"[bold]Done![/bold] Processed {n} file(s) [backend={args.backend}, kinds={sorted(want)}] in {elapsed:.2f}s"
    )


if __name__ == "__main__":
    main()
