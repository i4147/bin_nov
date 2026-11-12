#!/data/data/com.termux/files/usr/bin/env python
"""Strip type annotations from Python files in place with LibCST.
Parameter, return and variable annotations are removed while the code itself stays byte-identical elsewhere; files are processed in a worker pool and written atomically.
Non-Python files are skipped.
Usage: clean_type_annotations [paths ...] Examples: $ clean_type_annotations src/ Runs as a script (``python clean_type_annotations.py``) in the directory it should act on; the module can also be imported.
"""

from collections.abc import Iterable, Sequence
import contextlib
import io
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tokenize
from typing import Any, Iterable, TYPE_CHECKING

from dh import MAX_WORKERS, SKIP_DIR
import libcst as cst


class TypeAnnotationRemover(cst.CSTTransformer):
    def leave_Param(self, original_node: cst.Param, updated_node: cst.Param) -> cst.CSTNode:
        if updated_node.annotation is None:
            return updated_node
        return updated_node.with_changes(annotation=None)

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.CSTNode:
        changes = {}
        if updated_node.returns is not None:
            changes["returns"] = None
        if getattr(updated_node, "type_parameters", None) is not None:
            changes["type_parameters"] = None
        if not changes:
            return updated_node
        return updated_node.with_changes(**changes)

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.CSTNode:
        if getattr(updated_node, "type_parameters", None) is not None:
            return updated_node.with_changes(type_parameters=None)
        return updated_node

    def leave_AnnAssign(self, original_node: cst.AnnAssign, updated_node: cst.AnnAssign) -> cst.CSTNode:
        if updated_node.value is None:
            return cst.RemoveFromParent()
        return cst.Assign(
            targets=[cst.AssignTarget(target=updated_node.target)],
            value=updated_node.value,
            semicolon=updated_node.semicolon,
        )

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.CSTNode:
        if not updated_node.body:
            return cst.RemoveFromParent()
        return updated_node

    def leave_SimpleStatementSuite(
        self, original_node: cst.SimpleStatementSuite, updated_node: cst.SimpleStatementSuite
    ) -> cst.CSTNode:
        if not updated_node.body:
            return updated_node.with_changes(body=[cst.Pass()])
        return updated_node

    def leave_IndentedBlock(self, original_node: cst.IndentedBlock, updated_node: cst.IndentedBlock) -> cst.CSTNode:
        if not updated_node.body:
            return updated_node.with_changes(body=[cst.SimpleStatementLine(body=[cst.Pass()])])
        return updated_node


def _read_source(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding), encoding


def _atomic_write(path: Path, text: str, encoding: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        with contextlib.suppress(OSError):
            os.chmod(tmp, path.stat().st_mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def process_file(path_str: str) -> tuple[str, str | None, bool]:
    path = Path(path_str)
    try:
        source, encoding = _read_source(path)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return path_str, f"read failed: {exc}", False
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return path_str, f"parse failed: {exc}", False
    except Exception as exc:
        return path_str, f"parse failed: {exc!r}", False
    try:
        new_code = module.visit(TypeAnnotationRemover()).code
    except Exception as exc:
        return path_str, f"transform failed: {exc!r}", False
    if new_code == source:
        return path_str, None, False
    try:
        compile(new_code, str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        return path_str, f"validation failed: {exc}", False
    try:
        _atomic_write(path, new_code, encoding)
    except OSError as exc:
        return path_str, f"write failed: {exc}", False
    return path_str, None, True


def _iter_python_files(root: Path) -> Iterable[Path]:
    for candidate in root.rglob("*.py"):
        if not candidate.is_file():
            continue
        if any(part in SKIP_DIR for part in candidate.parts):
            continue
        yield candidate


def collect_files(inputs: Sequence[str]) -> list[Path]:
    roots: list[Path] = [Path(p) for p in inputs] if inputs else [Path.cwd()]
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates: Iterable[Path] = _iter_python_files(root)
        elif root.is_file():
            if root.suffix != ".py":
                print(f"warning: not a .py file: {root}", file=sys.stderr)
                continue
            candidates = (root,)
        else:
            print(f"warning: path not found: {root}", file=sys.stderr)
            continue
        for path in candidates:
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    files.sort(key=str)
    return files


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    files = collect_files(args)

    if not files:
        print("No Python files to process.", file=sys.stderr)
        return 0
    print(
        f"Processing {len(files)} file(s) with {MAX_WORKERS} worker(s)...",
        file=sys.stderr,
    )
    tasks = [(str(p),) for p in files]
    chunksize = max(1, len(tasks) // (MAX_WORKERS * 4))
    with mp.Pool(processes=MAX_WORKERS) as pool:
        results = pool.starmap(process_file, tasks, chunksize=chunksize)
    changed = 0
    unchanged = 0
    errors = 0
    for path_str, error, was_changed in results:
        if error is not None:
            errors += 1
            print(f"ERROR {path_str}: {error}", file=sys.stderr)
        elif was_changed:
            changed += 1
        else:
            unchanged += 1
    print(
        f"Done: {changed} modified, {unchanged} unchanged, {errors} error(s).",
        file=sys.stderr,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
