#!/data/data/com.termux/files/usr/bin/env python
"""Find and remove duplicate definitions with regexes, ASTs or tree-sitter.
The ``const`` subcommand drops repeated constant assignments in one file, ``ast`` hashes (normalised) top-level functions, classes and constants across the given paths and writes one representative per duplicate to ``utils.py``, ``ts`` does the same by tree-sitter source text, and ``refactor`` extracts the surviving definitions into a package.
``--exclude``/``--exclude-self`` filter the inputs.
Usage: deduptools {const,ast,ts,refactor} [options] [paths ...] Positional arguments: file Python file to dedup in place.
paths ...
Files or directories.
Empty = walk cwd.
Subcommands: const Regex-based duplicate-constant removal in a single file.
ast AST-based dedup across one or more paths (default: cwd).
ts Tree-sitter content dedup (diduper/tsdeduper).
refactor Extract top-level defs/classes/consts into a package.
Options: --dup-file Name of the archive file written next to FILE (default: dup_const.py).
--suffix Suffix for per-file archive (default: _dups.py).
--output File to write representative duplicates to (default: utils.py).
--exclude Basename to skip (repeatable).
--exclude-self Skip the running script's own file (diduper default).
--input-dir Directory to scan (default: .).
--output-dir Output package directory (default: output).
Examples: $ deduptools ast src/ --output utils.py $ deduptools const script.py Command-line script: run ``python deduptools.py`` or the ``deduptools`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
import copy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterator, Sequence

from dh import MAX_WORKERS


def iter_py_files(root: Path, *, exclude_names: set[str] | None = None) -> Iterator[Path]:
    excl = exclude_names or set()
    for p in root.rglob("*.py"):
        if p.name in excl:
            continue
        yield p


def read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slice_lines(lines: Sequence[str], start: int, end: int) -> str:
    return "".join(lines[start - 1 : end])


@dataclass
class Declaration:
    kind: str
    name: str
    lineno: int
    end_lineno: int
    source: str
    content_hash: str


class _NameStripper(ast.NodeTransformer):
    def _rename(self, node: ast.AST) -> ast.AST:
        node.name = "__NAME__"
        self.generic_visit(node)
        return node

    visit_FunctionDef = _rename
    visit_AsyncFunctionDef = _rename
    visit_ClassDef = _rename


def ast_hash(node: ast.AST) -> str:
    clone = copy.deepcopy(node)
    clone = _NameStripper().visit(clone)
    ast.fix_missing_locations(clone)
    dumped = ast.dump(clone, annotate_fields=True, include_attributes=False)
    return sha256_hex(dumped)


def is_simple_assign(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    return all(isinstance(t, ast.Name) for t in node.targets)


def assign_names(node: ast.Assign) -> list[str]:
    return [t.id for t in node.targets if isinstance(t, ast.Name)]


def collect_ast_declarations(tree: ast.Module, lines: Sequence[str]) -> list[Declaration]:
    out: list[Declaration] = []
    for node in tree.body:
        if is_simple_assign(node):
            src = slice_lines(lines, node.lineno, node.end_lineno)
            h = ast_hash(node)
            for name in assign_names(node):
                out.append(Declaration("assign", name, node.lineno, node.end_lineno, src, h))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(
                Declaration(
                    "function",
                    node.name,
                    node.lineno,
                    node.end_lineno,
                    slice_lines(lines, node.lineno, node.end_lineno),
                    ast_hash(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            out.append(
                Declaration(
                    "class",
                    node.name,
                    node.lineno,
                    node.end_lineno,
                    slice_lines(lines, node.lineno, node.end_lineno),
                    ast_hash(node),
                )
            )
    return out


def cmd_const(args: argparse.Namespace) -> int:
    target: Path = args.file
    if not target.exists():
        print(f"File not found: {target}")
        return 1
    pattern = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=")
    lines = target.read_text().splitlines(keepends=True)
    first_seen: set[str] = set()
    dup_lines: list[str] = []
    dup_idx: set[int] = set()
    for idx, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        name = m.group(1)
        if name in first_seen:
            dup_lines.append(line)
            dup_idx.add(idx)
        else:
            first_seen.add(name)
    if not dup_lines:
        print("No duplicates found.")
        return 0
    kept = [ln for i, ln in enumerate(lines) if i not in dup_idx]
    target.write_text("".join(kept))
    out_path = target.parent / args.dup_file
    with out_path.open("a") as f:
        f.write(f"\n# Duplicate declarations from {target.name}\n")
        for ln in dup_lines:
            f.write(ln)
    print("Kept the first declaration of each constant.")
    print(f"Moved {len(dup_lines)} duplicate declarations to {out_path}")
    print(f"Updated {target} in place.")
    return 0


def _ast_process_file(job: tuple[str, str]) -> tuple[str, int, str | None]:
    path_str, suffix = job
    path = Path(path_str)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return path_str, 0, f"Syntax error in {path}: {e}"
    decls = collect_ast_declarations(tree, lines)
    seen_keys: set[tuple[str, str]] = set()
    seen_hashes: set[tuple[str, str]] = set()
    dup_ranges: list[tuple[int, int]] = []
    dup_records: list[tuple[Declaration, str]] = []
    ranges_seen: set[tuple[int, int]] = set()
    for d in decls:
        key_name = (d.kind, d.name)
        key_hash = (d.kind, d.content_hash)
        rng = (d.lineno, d.end_lineno)
        reason: str | None = None
        if key_name in seen_keys:
            reason = f"duplicate {d.kind} name: {d.name}"
        elif key_hash in seen_hashes:
            reason = f"duplicate {d.kind} content hash: {d.name}"
        else:
            seen_keys.add(key_name)
            seen_hashes.add(key_hash)
        if reason and rng not in ranges_seen:
            dup_ranges.append(rng)
            dup_records.append((d, reason))
            ranges_seen.add(rng)
    if not dup_ranges:
        return path_str, 0, None
    to_strip: set[int] = set()
    for start, end in dup_ranges:
        to_strip.update(range(start, end + 1))
    kept = [ln for i, ln in enumerate(lines, start=1) if i not in to_strip]
    path.write_text("".join(kept), encoding="utf-8")
    out_path = path.parent / f"{path.stem}{suffix}"
    buf: list[str] = [f"\n# Duplicates moved from {path.name}\n"]
    for d, reason in dup_records:
        buf.append(f"\n# {reason} @ lines {d.lineno}-{d.end_lineno}\n")
        buf.append(d.source)
        if not d.source.endswith("\n"):
            buf.append("\n")
    with out_path.open("a", encoding="utf-8") as f:
        f.write("".join(buf))
    return path_str, len(dup_ranges), None


def cmd_ast(args: argparse.Namespace) -> int:
    targets: list[Path] = []
    if args.paths:
        for p in args.paths:
            if p.is_file():
                targets.append(p)
            elif p.is_dir():
                targets.extend(iter_py_files(p))
            else:
                print(f"Skipping (not found): {p}", file=sys.stderr)
    else:
        targets = list(iter_py_files(Path.cwd()))
    if not targets:
        print("No Python files to process.")
        return 0
    jobs = [(str(p), args.suffix) for p in targets]
    workers = MAX_WORKERS
    if workers <= 0:
        workers = min(len(jobs), os.cpu_count() or 1)
    moved_total = 0
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for path_str, count, err in ex.map(_ast_process_file, jobs):
                if err:
                    print(err, file=sys.stderr)
                    continue
                if count:
                    moved_total += count
                    print(f"Updated {path_str} (moved {count} block(s))")
    else:
        for job in jobs:
            path_str, count, err = _ast_process_file(job)
            if err:
                print(err, file=sys.stderr)
                continue
            if count:
                moved_total += count
                print(f"Updated {path_str} (moved {count} block(s))")
    if moved_total == 0:
        print("No duplicate top-level assignments/functions/classes found.")
    else:
        print(f"Total duplicate block(s) moved: {moved_total}")
    return 0


def _ts_make_parser():
    from tree_sitter import Language, Parser
    import tree_sitter_python as tsp

    parser = Parser()
    parser.language = Language(tsp.language())
    return parser


def _ts_node_text(src_bytes: bytes, node: Any) -> str:
    return src_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _ts_collect(path: Path, parser: Any) -> list[Declaration]:
    text = read_text_safe(path)
    if text is None:
        return []
    src_bytes = text.encode("utf-8", errors="replace")
    tree = parser.parse(src_bytes)
    root = tree.root_node
    out: list[Declaration] = []
    for node in root.children:
        if node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _ts_node_text(src_bytes, name_node)
            source = _ts_node_text(src_bytes, node)
            kind = "function" if node.type == "function_definition" else "class"
            out.append(
                Declaration(
                    kind,
                    name,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    source,
                    sha256_hex(source),
                )
            )
        elif node.type in {"expression_statement", "assignment"}:
            assign = node
            if node.type == "expression_statement" and node.children:
                assign = node.children[0]
            if assign.type != "assignment" or len(assign.children) < 3:
                continue
            lhs = assign.children[0]
            if lhs.type != "identifier":
                continue
            name = _ts_node_text(src_bytes, lhs)
            if not name.isupper():
                continue
            source = _ts_node_text(src_bytes, node)
            out.append(
                Declaration(
                    "const",
                    name,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                    source,
                    sha256_hex(source),
                )
            )
    return out


def _ts_write_output(representatives: dict[str, Declaration], out_path: Path) -> None:
    parts = [
        "# Auto-generated file",
        "# Contains duplicate top-level constants, functions, and classes.",
        "",
    ]
    emitted: set[str] = set()
    for h, d in representatives.items():
        if h in emitted:
            continue
        emitted.add(h)
        parts.append(f"# Duplicate {d.kind}: {d.name}")
        if getattr(d, "path", None):
            parts.append(f"# Source: {d.path}")
        parts.append(d.source)
        parts.append("")
    out_path.write_text("\n".join(parts), encoding="utf-8")


def cmd_ts(args: argparse.Namespace) -> int:
    try:
        parser = _ts_make_parser()
    except ImportError as e:
        print(
            f"`ts` subcommand requires tree_sitter + tree_sitter_python: {e}",
            file=sys.stderr,
        )
        return 2
    roots = args.paths or [Path.cwd()]
    output_path = (Path.cwd() / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output)
    exclude_names: set[str] = set(args.exclude or [])
    exclude_names.add(output_path.name)
    if args.exclude_self:
        exclude_names.add(Path(sys.argv[0]).name)
    seen: dict[str, Declaration] = {}
    dups: dict[str, Declaration] = {}
    for root in roots:
        for file in iter_py_files(root, exclude_names=exclude_names):
            for d in _ts_collect(file, parser):
                if d.content_hash in seen:
                    dups[d.content_hash] = seen[d.content_hash]
                else:
                    seen[d.content_hash] = d
    if not dups:
        print("No duplicates found.")
        return 0
    _ts_write_output(dups, output_path)
    print(f"Found {len(dups)} duplicate items.")
    print(f"Wrote them to: {output_path}")
    return 0


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text + "\n\n")


def cmd_refactor(args: argparse.Namespace) -> int:
    input_dir: Path = args.input_dir.resolve()
    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    func_file = out_dir / "func.py"
    cls_file = out_dir / "classes.py"
    const_file = out_dir / "const.py"
    init_file = out_dir / "__init__.py"
    for f in (func_file, cls_file, const_file, init_file):
        if f.exists():
            f.unlink()
    for path in iter_py_files(input_dir, exclude_names={out_dir.name}):
        try:
            rel = path.relative_to(input_dir)
        except ValueError:
            rel = path
        if out_dir.name in rel.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, UnicodeDecodeError, SyntaxError) as e:
            print(f"Skipping {path}: {e}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                seg = ast.get_source_segment(source, node) or ""
                _append(func_file, seg)
            elif isinstance(node, ast.ClassDef):
                seg = ast.get_source_segment(source, node) or ""
                _append(cls_file, seg)
            elif is_simple_assign(node):
                seg = ast.get_source_segment(source, node) or ""
                _append(const_file, seg)
    with init_file.open("w", encoding="utf-8") as f:
        f.write("from .func import *\n")
        f.write("from .classes import *\n")
        f.write("from .const import *\n")
    print(f"Wrote refactor output to {out_dir}/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedup_tool",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_const = sub.add_parser(
        "const",
        help="Regex-based duplicate-constant removal in a single file.",
    )
    p_const.add_argument("file", type=Path, help="Python file to dedup in place.")
    p_const.add_argument(
        "--dup-file",
        default="dup_const.py",
        help="Name of the archive file written next to FILE (default: dup_const.py).",
    )
    p_const.set_defaults(func=cmd_const)
    p_ast = sub.add_parser(
        "ast",
        help="AST-based dedup across one or more paths (default: cwd).",
    )
    p_ast.add_argument("paths", nargs="*", type=Path, help="Files or directories. Empty = walk cwd.")
    p_ast.add_argument(
        "--suffix",
        default="_dups.py",
        help="Suffix for per-file archive (default: _dups.py).",
    )
    p_ast.set_defaults(func=cmd_ast)
    p_ts = sub.add_parser(
        "ts",
        help="Tree-sitter content dedup (diduper/tsdeduper).",
    )
    p_ts.add_argument("paths", nargs="*", type=Path, help="Roots to scan (default: cwd).")
    p_ts.add_argument(
        "--output",
        default="utils.py",
        help="File to write representative duplicates to (default: utils.py).",
    )
    p_ts.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME",
        help="Basename to skip (repeatable).",
    )
    p_ts.add_argument(
        "--exclude-self",
        action="store_true",
        help="Skip the running script's own file (diduper default).",
    )
    p_ts.set_defaults(func=cmd_ts)
    p_ref = sub.add_parser(
        "refactor",
        help="Extract top-level defs/classes/consts into a package.",
    )
    p_ref.add_argument(
        "--input-dir",
        type=Path,
        default=Path(),
        help="Directory to scan (default: .).",
    )
    p_ref.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Output package directory (default: output).",
    )
    p_ref.set_defaults(func=cmd_refactor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
