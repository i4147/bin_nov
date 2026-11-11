#!/data/data/com.termux/files/usr/bin/env python
"""Add or remove the type annotations of Python files.
In annotate mode it applies the annotations of a ``.pyi`` stub (typeshed included) with LibCST, keeping or overriding the existing hints, and in ``--remove`` mode it strips the annotations and the ``# type:`` comments through tree-sitter.
``--diff``, ``--backup``, ``--dry-run`` and parallel processing are available.
Usage: annotate [annotate]|--remove [options] [paths ...] Positional arguments: paths ...
Files, directories, or globs to process (default: current directory).
Options: -r, --remove, --strip Remove all type annotations (and `# type:` comments) instead of adding them.
-s, --stub-file Custom .pyi stub (annotate mode only, single input file only).
--no-overwrite (annotate) Do not overwrite existing type annotations.
--future-annotations (annotate) Emit `from __future__ import annotations`.
-b, --backup Write a '<file>.py.bak' backup before modifying.
-d, --diff Show a unified diff for each modified file.
-n, --dry-run Report what would change without writing anything.
-q, --quiet Suppress normal output (errors are still printed).
--validate-compile Also run the built-in compile() on the modified output.
--validate-mypy After writing, run mypy on the updated file (slow, annotate mode).
Examples: $ annotate module.py --diff $ annotate --remove module.py Command-line script: run ``python annotate.py`` or the ``annotate`` command.
The module is importable as well."""

import argparse
import ast
import contextlib
from dataclasses import dataclass, field
import difflib
import multiprocessing as mp
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import TYPE_CHECKING, Optional

from dh import MAX_WORKERS
import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import ApplyTypeAnnotationsVisitor


if TYPE_CHECKING:
    from tree_sitter import Language, Node, Parser, Tree


class TypeshedSanitizer(cst.CSTTransformer):
    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom) -> cst.ImportFrom:
        if original_node.module and cst.helpers.get_full_name_for_node(original_node.module) == "_typeshed":
            return cst.ImportFrom(
                module=cst.Name("typing"),
                names=[cst.ImportAlias(name=cst.Name("Any"))],
            )
        return updated_node

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> cst.Import:
        names: list[cst.ImportAlias] = []
        for alias in original_node.names:
            if cst.helpers.get_full_name_for_node(alias.name) == "_typeshed":
                names.append(alias.with_changes(name=cst.Name("typing")))
            else:
                names.append(alias)
        return updated_node.with_changes(names=names)

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.Attribute:
        if (
            isinstance(original_node.value, cst.Name)
            and original_node.value.value == "_typeshed"
            and original_node.attr.value == "Incomplete"
        ):
            return cst.Attribute(value=cst.Name("typing"), attr=cst.Name("Any"))
        return updated_node

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
        if original_node.value == "Incomplete":
            return cst.Name("Any")
        return updated_node


def sanitize_stub_cst(stub_cst: cst.Module) -> cst.Module:
    return stub_cst.visit(TypeshedSanitizer())


def apply_type_annotations(
    source_code: str,
    stub_code: str,
    overwrite_existing: bool = True,
    use_future_annotations: bool = False,
) -> str:
    source_cst: cst.Module
    try:
        source_cst = cst.parse_module(source_code)
    except Exception as e:
        msg = f"Failed to parse source file with LibCST: {e}"
        raise ValueError(msg) from e
    stub_cst: cst.Module
    try:
        stub_cst = cst.parse_module(stub_code)
    except Exception as e:
        msg = f"Failed to parse stub file with LibCST: {e}"
        raise ValueError(msg) from e
    stub_cst = sanitize_stub_cst(stub_cst)
    context: CodemodContext = CodemodContext()
    ApplyTypeAnnotationsVisitor.store_stub_in_context(
        context=context,
        stub=stub_cst,
        overwrite_existing_annotations=overwrite_existing,
        use_future_annotations=use_future_annotations,
    )
    transformer: ApplyTypeAnnotationsVisitor = ApplyTypeAnnotationsVisitor(context)
    annotated_cst: cst.Module = transformer.transform_module(source_cst)
    return annotated_cst.code


_PY_LANGUAGE: Optional["Language"] = None
_TS_PARSER_FACTORY: Optional[type["Parser"]] = None


def _ensure_tree_sitter() -> None:
    global _PY_LANGUAGE, _TS_PARSER_FACTORY
    if _PY_LANGUAGE is not None:
        return
    try:
        from tree_sitter import Language, Parser
    except Exception as exc:
        msg = "tree-sitter not installed"
        raise RuntimeError(msg) from exc
    try:
        import tree_sitter_python

        _PY_LANGUAGE = Language(tree_sitter_python.language())
    except Exception as exc:
        msg = "Failed to load Python grammar from tree_sitter_python. Install with: pip install tree_sitter_python"
        raise RuntimeError(msg) from exc
    _TS_PARSER_FACTORY = Parser


TYPE_COMMENT_RE: "re.Pattern[str]" = re.compile(r"\s*#\s*type\s*:\s*([^\n]*)$", flags=re.IGNORECASE)


def _prev_nonspace(buf: bytes, i: int) -> int:
    j: int = i - 1
    while j >= 0 and buf[j] in b" \t\r":
        j -= 1
    return j


def _next_nonspace(buf: bytes, i: int) -> int:
    n: int = len(buf)
    j: int = i
    while j < n and buf[j] in b" \t\r":
        j += 1
    return min(n, j)


def _collect_annotation_nodes(root: "Node") -> list["Node"]:
    stack: list["Node"] = [root]
    ann_nodes: list["Node"] = []
    while stack:
        node: "Node" = stack.pop()
        if node.type == "annotation":
            ann_nodes.append(node)
        for c in node.children:
            stack.append(c)
    return ann_nodes


def _remove_ranges_from_bytes(src: bytes, ranges: list[tuple[int, int]]) -> bytes:
    if not ranges:
        return src
    ranges_sorted: list[tuple[int, int]] = sorted(ranges, key=lambda r: r[0])
    merged: list[tuple[int, int]] = []
    cur_s: int
    cur_e: int
    cur_s, cur_e = ranges_sorted[0]
    for s, e in ranges_sorted[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    out: bytearray = bytearray(src)
    for s, e in reversed(merged):
        del out[s:e]
    return bytes(out)


def strip_annotations_from_bytes(src_bytes: bytes, path: Path) -> tuple[bytes, list[str], Optional[str]]:
    _ensure_tree_sitter()
    assert _TS_PARSER_FACTORY is not None
    assert _PY_LANGUAGE is not None
    parser: "Parser" = _TS_PARSER_FACTORY(_PY_LANGUAGE)
    try:
        tree: "Tree" = parser.parse(src_bytes)
    except Exception as e:
        return src_bytes, [], f"parse error: {e}"
    root: "Node" = tree.root_node
    ann_nodes: list["Node"] = _collect_annotation_nodes(root)
    remove_ranges: list[tuple[int, int]] = []
    warnings: list[str] = []
    for node in ann_nodes:
        s: int = node.start_byte
        e: int = node.end_byte
        prev_i: int = _prev_nonspace(src_bytes, s)
        removed_prefix_start: int
        if prev_i >= 1 and src_bytes[prev_i - 1 : prev_i + 1] == b"->":
            removed_prefix_start = prev_i - 1
        elif prev_i >= 0 and src_bytes[prev_i] == ord(":"):
            removed_prefix_start = prev_i
        else:
            removed_prefix_start = s
        next_i: int = _next_nonspace(src_bytes, e)
        next_char: bytes = src_bytes[next_i : next_i + 1] if next_i < len(src_bytes) else b""
        safe_next: bool = next_char in (b"=", b",", b")", b":")
        safe: bool
        if src_bytes[removed_prefix_start : removed_prefix_start + 2] == b"->":
            safe = True
        else:
            safe = safe_next
        if not safe:
            line_start: int = src_bytes.rfind(b"\n", 0, s) + 1
            line_end: int = src_bytes.find(b"\n", e)
            if line_end == -1:
                line_end = len(src_bytes)
            snippet: str = src_bytes[line_start:line_end].decode(errors="replace").strip()
            warnings.append(f'skipped standalone annotation at {path}:{node.start_point[0] + 1}: "{snippet}"')
            continue
        remove_ranges.append((removed_prefix_start, e))
    type_comment_ranges: list[tuple[int, int]] = []
    lines: list[bytes] = src_bytes.splitlines(keepends=True)
    offset: int = 0
    for ln in lines:
        try:
            text: str = ln.decode()
        except Exception:
            offset += len(ln)
            continue
        m: Optional["re.Match[str]"] = TYPE_COMMENT_RE.search(text)
        if m:
            byte_start: int = offset + len(text[: m.start(0)].encode())
            byte_end: int = offset + len(text[: m.end(0)].encode())
            type_comment_ranges.append((byte_start, byte_end))
        offset += len(ln)
    all_remove: list[tuple[int, int]] = remove_ranges + type_comment_ranges
    if not all_remove:
        return src_bytes, warnings, None
    new_bytes: bytes = _remove_ranges_from_bytes(src_bytes, all_remove)
    return new_bytes, warnings, None


def validate_python_code(code: str, filename: str) -> None:
    try:
        ast.parse(code, filename=filename)
    except SyntaxError as e:
        msg = f"Invalid Python syntax at line {e.lineno}, col {e.offset}: {e.msg}"
        raise SyntaxError(msg) from e


def compute_diff(original: str, modified: str, filename: str) -> str:
    diff: str = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"{filename} (original)",
            tofile=f"{filename} (modified)",
        )
    )
    return diff


def _run_cmd(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


@dataclass(slots=True)
class Options:
    dry_run: bool = False
    backup: bool = False
    show_diff: bool = False
    quiet: bool = False
    validate_compile: bool = False
    validate_mypy: bool = False
    overwrite_existing: bool = True
    use_future_annotations: bool = False
    stub_file: Optional[str] = None
    remove: bool = False


@dataclass(slots=True)
class Result:
    path: Path
    changed: bool = False
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    diff: str = ""
    backup_path: Optional[Path] = None
    mode: str = "annotate"
    _new_text: Optional[str] = None


def _annotate_file(path: Path, options: Options) -> Result:
    result: Result = Result(path=path, mode="annotate")
    stub_path: Path
    if options.stub_file:
        stub_path = Path(options.stub_file)
    else:
        stub_path = path.with_suffix(".pyi")
    if not stub_path.is_file():
        result.error = f"missing stub file: {stub_path}"
        return result
    original: str
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as e:
        result.error = f"read error: {e}"
        return result
    stub_code: str
    try:
        stub_code = stub_path.read_text(encoding="utf-8")
    except Exception as e:
        result.error = f"stub read error: {e}"
        return result
    try:
        validate_python_code(original, str(path))
    except SyntaxError as e:
        result.error = f"source has invalid syntax: {e}"
        return result
    modified: str
    try:
        modified = apply_type_annotations(
            source_code=original,
            stub_code=stub_code,
            overwrite_existing=options.overwrite_existing,
            use_future_annotations=options.use_future_annotations,
        )
    except Exception as e:
        result.error = f"annotation failed: {e}"
        return result
    try:
        validate_python_code(modified, str(path))
    except SyntaxError as e:
        result.error = f"result has invalid syntax: {e}"
        return result
    if options.validate_compile:
        try:
            compile(modified, str(path), "exec")
        except SyntaxError as e:
            result.error = f"compile() validation failed: {e}"
            return result
    result.changed = modified != original
    result.diff = compute_diff(original, modified, str(path))
    return result


def _strip_file(path: Path, options: Options) -> Result:
    result: Result = Result(path=path, mode="remove")
    src_bytes: bytes
    try:
        src_bytes = path.read_bytes()
    except Exception as e:
        result.error = f"read error: {e}"
        return result
    new_bytes: bytes
    warnings: list[str]
    err: Optional[str]
    new_bytes, warnings, err = strip_annotations_from_bytes(src_bytes, path)
    result.warnings.extend(warnings)
    if err:
        result.error = err
        return result
    if new_bytes == src_bytes:
        return result
    new_text: str
    try:
        new_text = new_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        result.error = f"decoded result is not utf-8: {e}"
        return result
    if options.validate_compile or options.validate_mypy:
        try:
            validate_python_code(new_text, str(path))
        except SyntaxError as e:
            result.error = f"result has invalid syntax: {e}"
            return result
    if options.validate_compile:
        try:
            compile(new_text, str(path), "exec")
        except SyntaxError as e:
            result.error = f"compile() validation failed: {e}"
            return result
    result.changed = True
    if options.show_diff:
        original_text: str
        try:
            original_text = src_bytes.decode("utf-8")
        except UnicodeDecodeError:
            original_text = src_bytes.decode("utf-8", errors="replace")
        result.diff = compute_diff(original_text, new_text, str(path))
    result._new_text = new_text
    return result


def process_file(path_str: str, options: Options) -> Result:
    path: Path = Path(path_str)
    if not path.exists() or not path.is_file():
        return Result(
            path=path,
            error="file not found",
            mode=("remove" if options.remove else "annotate"),
        )
    if path.suffix != ".py":
        return Result(
            path=path,
            error="not a .py file",
            mode=("remove" if options.remove else "annotate"),
        )
    result: Result = _strip_file(path, options) if options.remove else _annotate_file(path, options)
    if result.error or not result.changed or options.dry_run:
        return result
    if options.backup:
        try:
            backup_path: Path = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup_path)
            result.backup_path = backup_path
        except Exception as e:
            result.warnings.append(f"failed to create backup: {e}")
    tmp_path: Path = path.with_suffix(path.suffix + ".tmp")
    try:
        if result._new_text is not None:
            tmp_path.write_text(result._new_text, encoding="utf-8")
        else:
            original: str = path.read_text(encoding="utf-8")
            stub_path: Path = Path(options.stub_file) if options.stub_file else path.with_suffix(".pyi")
            stub_code: str = stub_path.read_text(encoding="utf-8")
            new_text: str = apply_type_annotations(
                source_code=original,
                stub_code=stub_code,
                overwrite_existing=options.overwrite_existing,
                use_future_annotations=options.use_future_annotations,
            )
            tmp_path.write_text(new_text, encoding="utf-8")
        try:
            st: os.stat_result = path.stat()
            os.chmod(tmp_path, stat.S_IMODE(st.st_mode))
        except Exception:
            pass
        tmp_path.replace(path)
    except Exception as e:
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        result.error = f"write error: {e}"
        return result
    if options.validate_mypy:
        res: "subprocess.CompletedProcess[str]" = _run_cmd([
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            str(path),
        ])
        if res.returncode != 0:
            result.warnings.append(f"mypy reported issues:\n{res.stdout}{res.stderr}")
    return result


def gather_py_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    provided: list[str] = list(paths) if paths else ["."]
    for p in provided:
        path: Path = Path(p)
        if path.is_file():
            if path.suffix == ".py":
                out.append(path.resolve())
        elif path.is_dir():
            for f in path.rglob("*.py"):
                if f.is_file():
                    out.append(f.resolve())
        else:
            for f in Path().glob(p):
                if f.is_file() and f.suffix == ".py":
                    out.append(f.resolve())
    return sorted(set(out))


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="batch_annotate",
        description=(
            "Add OR remove type annotations on Python (.py) files.\n\n"
            "  Default mode (annotate): uses a sibling .pyi stub (LibCST).\n"
            "                           A stub is REQUIRED for every target file.\n"
            "  --remove     mode:       strips all annotations + `# type:` comments\n"
            "                           (tree-sitter). No stub needed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files, directories, or globs to process (default: current directory).",
    )
    mode: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-r",
        "--remove",
        "--strip",
        dest="remove",
        action="store_true",
        help="Remove all type annotations (and `# type:` comments) instead of adding them.",
    )
    parser.add_argument(
        "-s",
        "--stub-file",
        default=None,
        help="Custom .pyi stub (annotate mode only, single input file only).",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="(annotate) Do not overwrite existing type annotations.",
    )
    parser.add_argument(
        "--future-annotations",
        action="store_true",
        help="(annotate) Emit `from __future__ import annotations`.",
    )
    parser.add_argument(
        "-b",
        "--backup",
        action="store_true",
        help="Write a '<file>.py.bak' backup before modifying.",
    )
    parser.add_argument(
        "-d",
        "--diff",
        action="store_true",
        help="Show a unified diff for each modified file.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress normal output (errors are still printed).",
    )
    parser.add_argument(
        "--validate-compile",
        action="store_true",
        help="Also run the built-in compile() on the modified output.",
    )
    parser.add_argument(
        "--validate-mypy",
        action="store_true",
        help="After writing, run mypy on the updated file (slow, annotate mode).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser: argparse.ArgumentParser = build_parser()
    args: argparse.Namespace = parser.parse_args(argv)
    files: list[Path] = gather_py_files(args.paths)
    if not files:
        print("No .py files found.", file=sys.stderr)
        return 1
    if args.stub_file and len(files) > 1:
        print(
            "Error: --stub-file is only valid with a single input file.",
            file=sys.stderr,
        )
        return 2
    if args.stub_file and args.remove:
        print("Error: --stub-file is not valid with --remove.", file=sys.stderr)
        return 2
    if args.future_annotations and args.remove:
        print("Error: --future-annotations is not valid with --remove.", file=sys.stderr)
        return 2
    if args.no_overwrite and args.remove:
        print("Error: --no-overwrite is not valid with --remove.", file=sys.stderr)
        return 2
    options: Options = Options(
        dry_run=args.dry_run,
        backup=args.backup,
        show_diff=args.diff,
        quiet=args.quiet,
        validate_compile=args.validate_compile,
        validate_mypy=args.validate_mypy,
        overwrite_existing=not args.no_overwrite,
        use_future_annotations=args.future_annotations,
        stub_file=args.stub_file,
        remove=args.remove,
    )
    if options.remove:
        try:
            _ensure_tree_sitter()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 3
    use_pool: bool = len(files) > 1 and (MAX_WORKERS is None or MAX_WORKERS > 1)
    results: list[Result]
    if use_pool:
        jobs: int = MAX_WORKERS or min(8, len(files))
        with mp.Pool(processes=jobs) as pool:
            results = pool.starmap(process_file, [(str(f), options) for f in files])
    else:
        results = [process_file(str(f), options) for f in files]
    changed: list[Result] = [r for r in results if r.changed and not r.error]
    unchanged: list[Result] = [r for r in results if not r.changed and not r.error]
    failed: list[Result] = [r for r in results if r.error]
    warnings: list[str] = [w for r in results for w in r.warnings]
    verb: str = "strip" if options.remove else "annotate"
    if not args.quiet:
        for r in changed:
            if args.dry_run:
                print(f"[dry-run] would {verb}: {r.path}")
            else:
                print(f"{'stripped' if options.remove else 'updated'}: {r.path}")
                if r.backup_path is not None:
                    print(f"  backup: {r.backup_path}")
        for r in unchanged:
            print(f"no-change: {r.path}")
    for r in failed:
        print(f"error: {r.path} -> {r.error}", file=sys.stderr)
    if args.diff:
        for r in changed:
            if r.diff:
                print("\n" + "=" * 60)
                print(f"DIFF: {r.path}")
                print("=" * 60)
                print(r.diff, end="")
                print("=" * 60 + "\n")
    if warnings and not args.quiet:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")
    if not args.quiet:
        print(
            f"\nSummary: mode={verb} processed={len(results)} "
            f"changed={len(changed)} "
            f"no-change={len(unchanged)} "
            f"errors={len(failed)} "
            f"warnings={len(warnings)}"
        )
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
