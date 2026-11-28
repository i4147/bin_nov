#!/data/data/com.termux/files/usr/bin/env python
"""Find repeated top-level Python objects and consolidate them into a utils module.

Parses modules with libcst (with tree-sitter as a fallback extractor) including archives, hashes top-level functions, classes and constants, and reports exact duplicates; --move/--copy relocate or replicate the duplicates into a shared utils.py.
"""

from __future__ import annotations
import argparse
import bz2
from collections import defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
import lzma
import multiprocessing as mp
from pathlib import Path
import sys
import tarfile
import tempfile
from typing import TYPE_CHECKING, Literal, TypedDict
import zipfile

from dh import SKIP_DIRS
import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider


try:
    import brotli as _brotli
except ImportError:
    _brotli = None

try:
    import zstandard as _zstd
except ImportError:
    _zstd = None

try:
    from tree_sitter import (
        Language,
        Parser as _TSParser,
    )
    import tree_sitter_python as _ts_python
except ImportError:
    Language = None  # type: ignore[assignment]
    _TSParser = None
    _ts_python = None

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from types import ModuleType

try:
    from dh import MAX_WORKERS, atomic_write
except ImportError:
    MAX_WORKERS: int = 4

    def atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
        """Fallback atomic-style writer when `dh.atomic_write` is unavailable."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)


try:
    from loguru import logger as _logger
except ImportError:

    class _LoguruCompat:
        """Minimal loguru-style shim used when loguru is unavailable."""

        def __init__(self) -> None:
            import sys as _sys

            self._stream = _sys.stderr

        def _emit(self, level: str, message: str, *args: object) -> None:
            text = message.format(*args) if args else message
            print(f"[{level}] {text}", file=self._stream)

        def debug(self, message: str, *args: object) -> None:
            self._emit("DEBUG", message, *args)

        def info(self, message: str, *args: object) -> None:
            self._emit("INFO", message, *args)

        def warning(self, message: str, *args: object) -> None:
            self._emit("WARNING", message, *args)

        def error(self, message: str, *args: object) -> None:
            self._emit("ERROR", message, *args)

        def critical(self, message: str, *args: object) -> None:
            self._emit("CRITICAL", message, *args)

        def exception(self, message: str, *args: object) -> None:
            self._emit("ERROR", message, *args)

    _logger = _LoguruCompat()


ObjectKind = Literal["function", "class", "constant"]

SUPPORTED_ARCHIVES: tuple[str, ...] = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".br",
)


TEST_DIR_NAMES: frozenset[str] = frozenset({"test", "tests", "testing"})


class RawExtractedObject(TypedDict):
    """Intermediate extracted top-level object before hashing."""

    name: str
    kind: ObjectKind
    snippet: str
    start_line: int | None
    end_line: int | None


@dataclass(slots=True)
class IndexedObject:
    """Normalized top-level object record with content hash and location metadata."""

    file: str
    name: str
    kind: ObjectKind
    snippet: str
    digest: str
    start_line: int | None
    end_line: int | None


@dataclass(slots=True)
class ScanOptions:
    """Runtime configuration for scanning and deduplicating Python files."""

    root: Path
    move: bool
    copy: bool
    dry_run: bool
    min_occurrences: int
    include_tests: bool
    as_json: bool


def sha256_hex(content: str) -> str:
    """Return the lowercase SHA-256 hex digest of UTF-8 text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def safe_read_text(path: Path) -> str | None:
    """Read UTF-8 text from `path`, returning `None` on failure."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.error("Failed reading {}: {}", path, exc)
        return None


def safe_write_text(path: Path, content: str) -> bool:
    """Write UTF-8 text to `path` atomically, returning `True` on success."""
    try:
        atomic_write(path, content, encoding="utf-8")
        return True
    except OSError as exc:
        _logger.error("Failed writing {}: {}", path, exc)
        return False


def normalize_newlines(text: str) -> str:
    """Normalize CRLF and lone CR line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _assigned_name_if_single_target(assign: cst.Assign) -> str | None:
    """Return assigned identifier name if assignment has a single plain `Name` target."""
    if len(assign.targets) != 1:
        return None
    target = assign.targets[0].target
    if isinstance(target, cst.Name):
        return target.value
    return None


def extract_with_libcst(code: str) -> list[RawExtractedObject]:
    """Extract top-level functions, classes, and simple constants using `libcst`."""
    try:
        module = cst.parse_module(code)
    except cst.ParserSyntaxError as exc:
        _logger.error("LibCST parsing failed: {}", exc)
        return []

    wrapper = MetadataWrapper(module)
    positions = wrapper.resolve(PositionProvider)
    objects: list[RawExtractedObject] = []

    for stmt in wrapper.module.body:
        pos = positions.get(stmt)
        start_line = pos.start.line if pos is not None else None
        end_line = pos.end.line if pos is not None else None

        if isinstance(stmt, cst.FunctionDef):
            objects.append({
                "name": stmt.name.value,
                "kind": "function",
                "snippet": wrapper.module.code_for_node(stmt),
                "start_line": start_line,
                "end_line": end_line,
            })
        elif isinstance(stmt, cst.ClassDef):
            objects.append({
                "name": stmt.name.value,
                "kind": "class",
                "snippet": wrapper.module.code_for_node(stmt),
                "start_line": start_line,
                "end_line": end_line,
            })
        elif isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
            inner = stmt.body[0]
            if isinstance(inner, cst.Assign):
                const_name = _assigned_name_if_single_target(inner)
                if const_name is not None:
                    objects.append({
                        "name": const_name,
                        "kind": "constant",
                        "snippet": wrapper.module.code_for_node(stmt).rstrip("\r\n"),
                        "start_line": start_line,
                        "end_line": end_line,
                    })

    return objects


def extract_with_tree_sitter(code: str) -> list[RawExtractedObject] | None:
    """Extract top-level objects with Tree-sitter when installed, or `None` if unavailable."""
    if _TSParser is None or Language is None or _ts_python is None:
        return None

    try:
        parser = _TSParser()
        parser.language = Language(_ts_python.language())
        encoded = code.encode("utf-8")
        tree = parser.parse(encoded)
        root = tree.root_node
        objects: list[RawExtractedObject] = []

        for node in root.children:
            start_line = node.start_point.row + 1
            end_line = node.end_point.row + 1
            if node.type in ("function_definition", "class_definition"):
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                name = encoded[name_node.start_byte : name_node.end_byte].decode("utf-8")
                snippet = encoded[node.start_byte : node.end_byte].decode("utf-8")
                kind: ObjectKind = "function" if node.type == "function_definition" else "class"
                objects.append({
                    "name": name,
                    "kind": kind,
                    "snippet": snippet,
                    "start_line": start_line,
                    "end_line": end_line,
                })
            elif node.type == "expression_statement":
                snippet = encoded[node.start_byte : node.end_byte].decode("utf-8")
                try:
                    stmt = cst.parse_statement(snippet)
                except cst.ParserSyntaxError:
                    continue
                if isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
                    inner = stmt.body[0]
                    if isinstance(inner, cst.Assign):
                        const_name = _assigned_name_if_single_target(inner)
                        if const_name is not None:
                            objects.append({
                                "name": const_name,
                                "kind": "constant",
                                "snippet": snippet,
                                "start_line": start_line,
                                "end_line": end_line,
                            })
    except Exception as exc:
        _logger.warning("Tree-sitter extraction failed ({}); falling back to libcst.", exc)
        return None
    else:
        return objects


def extract_objects(code: str) -> list[RawExtractedObject]:
    """Extract top-level Python objects using Tree-sitter if available, else `libcst`."""
    ts_objects = extract_with_tree_sitter(code)
    if ts_objects is not None:
        return ts_objects
    return extract_with_libcst(code)


def is_supported_archive(path: Path) -> bool:
    """Return `True` if `path` matches a supported archive or compressed file suffix."""
    lower = str(path).lower()
    return any(lower.endswith(ext) for ext in SUPPORTED_ARCHIVES)


def _decompress_single_stream(
    archive_path: Path,
    dest_dir: Path,
    module: ModuleType | None,
    lib_name: str,
) -> None:
    """Decompress a single-file `.zst` or `.br` stream into `dest_dir`."""
    if module is None:
        _logger.error("{} is not installed; cannot extract {}", lib_name, archive_path)
        return
    out_path = dest_dir / archive_path.stem
    raw_bytes = archive_path.read_bytes()
    decompressed: bytes = module.decompress(raw_bytes)
    out_path.write_bytes(decompressed)


def extract_archive(path: Path) -> Path:
    """Extract an archive or compressed file to a temporary directory and return its path."""
    temp_dir = Path(tempfile.mkdtemp(prefix="dedup_py_"))
    lower = str(path).lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                zf.extractall(temp_dir)
        elif lower.endswith((
            ".tar",
            ".tar.gz",
            ".tgz",
            ".tar.bz2",
            ".tbz2",
            ".tar.xz",
            ".txz",
        )):
            with tarfile.open(path) as tf:
                tf.extractall(temp_dir, filter="data")
        elif lower.endswith(".gz") and not lower.endswith(".tar.gz"):
            out_path = temp_dir / path.stem
            with gzip.open(path, "rb") as f_in:
                out_path.write_bytes(f_in.read())
        elif lower.endswith(".bz2") and not lower.endswith(".tar.bz2"):
            out_path = temp_dir / path.stem
            with bz2.open(path, "rb") as f_in:
                out_path.write_bytes(f_in.read())
        elif lower.endswith(".xz") and not lower.endswith(".tar.xz"):
            out_path = temp_dir / path.stem
            with lzma.open(path, "rb") as f_in:
                out_path.write_bytes(f_in.read())
        elif lower.endswith(".zst"):
            _decompress_single_stream(path, temp_dir, _zstd, "zstandard")
        elif lower.endswith(".br"):
            _decompress_single_stream(path, temp_dir, _brotli, "brotli")
    except Exception as exc:
        _logger.error("Failed extracting archive {}: {}", path, exc)
    return temp_dir


def _is_test_path(path: Path) -> bool:
    """Return `True` if any path component or filename looks like a test target."""
    if any(part.lower() in TEST_DIR_NAMES for part in path.parts):
        return True
    name = path.name.lower()
    return name.startswith("test_") or name.endswith("_test.py")


def should_skip_dir(path: Path, *, include_tests: bool = False) -> bool:
    """Return `True` if directory `path` should be skipped during traversal."""
    if path.name in SKIP_DIRS:
        return True
    return bool(not include_tests and path.name.lower() in TEST_DIR_NAMES)


def collect_python_files(base: Path, *, include_tests: bool = False) -> list[Path]:
    """Collect `.py` files under `base`, including `.py` files inside supported archives."""
    files: list[Path] = []

    def walk(current: Path) -> None:
        if should_skip_dir(current, include_tests=include_tests):
            return
        try:
            entries = list(current.iterdir())
        except PermissionError:
            _logger.warning("Permission denied: {}", current)
            return

        for item in entries:
            if item.is_dir():
                walk(item)
            elif item.is_file():
                if not include_tests and _is_test_path(item):
                    continue
                if item.suffix == ".py":
                    files.append(item)
                elif is_supported_archive(item):
                    extracted_dir = extract_archive(item)
                    for py_file in extracted_dir.rglob("*.py"):
                        if include_tests or not _is_test_path(py_file):
                            files.append(py_file)

    walk(base)
    return files


def process_file(path_str: str) -> list[IndexedObject]:
    """Read and extract normalized top-level objects from a single Python file."""
    path = Path(path_str)
    raw = safe_read_text(path)
    if not raw:
        return []
    code = normalize_newlines(raw)
    objs = extract_objects(code)
    results: list[IndexedObject] = []

    for obj in objs:
        snippet = obj["snippet"].strip()
        if not snippet:
            continue
        results.append(
            IndexedObject(
                file=str(path),
                name=obj["name"],
                kind=obj["kind"],
                snippet=obj["snippet"],
                digest=sha256_hex(snippet),
                start_line=obj["start_line"],
                end_line=obj["end_line"],
            )
        )
    return results


def get_utils_path(base: Path) -> Path:
    """Return `base/utils.py` or the first unused `base/utils_{i}.py` path."""
    default_path = base / "utils.py"
    if not default_path.exists():
        return default_path
    idx = 1
    while True:
        candidate = base / f"utils_{idx}.py"
        if not candidate.exists():
            return candidate
        idx += 1


def build_import_statement(utils_module_name: str, names: Sequence[str]) -> cst.ImportFrom:
    """Build a `libcst.ImportFrom` node importing `names` from `utils_module_name`."""
    unique_sorted = sorted(set(names))
    aliases = [cst.ImportAlias(name=cst.Name(value=n)) for n in unique_sorted]
    return cst.ImportFrom(
        module=cst.Name(value=utils_module_name),
        names=aliases,
    )


def write_utils_file(path: Path, objects: Iterable[IndexedObject]) -> bool:
    """Write deduplicated object snippets into `path`."""
    joined = "\n\n".join(obj.snippet.rstrip() for obj in objects).rstrip() + "\n"
    return safe_write_text(path, joined)


def _line_ranges_to_remove(objects: Iterable[IndexedObject]) -> set[tuple[int, int]]:
    """Collect 1-based inclusive `(start_line, end_line)` tuples for removable objects."""
    ranges: set[tuple[int, int]] = set()
    for obj in objects:
        if obj.start_line is not None and obj.end_line is not None:
            ranges.add((obj.start_line, obj.end_line))
    return ranges


def _remove_objects_via_cst(code: str, objects: Iterable[IndexedObject]) -> str:
    """Remove statements matching `objects` using `libcst` line metadata."""
    target_ranges = _line_ranges_to_remove(objects)
    if not target_ranges:
        return code

    module = cst.parse_module(code)
    wrapper = MetadataWrapper(module)
    positions = wrapper.resolve(PositionProvider)

    class _StatementRemover(cst.CSTTransformer):
        def leave_SimpleStatementLine(
            self,
            original_node: cst.SimpleStatementLine,
            updated_node: cst.SimpleStatementLine,
        ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
            pos = positions.get(original_node)
            if pos is not None and (pos.start.line, pos.end.line) in target_ranges:
                return cst.RemoveFromParent()
            return updated_node

        def leave_FunctionDef(
            self,
            original_node: cst.FunctionDef,
            updated_node: cst.FunctionDef,
        ) -> cst.FunctionDef | cst.RemovalSentinel:
            pos = positions.get(original_node)
            if pos is not None and (pos.start.line, pos.end.line) in target_ranges:
                return cst.RemoveFromParent()
            return updated_node

        def leave_ClassDef(
            self,
            original_node: cst.ClassDef,
            updated_node: cst.ClassDef,
        ) -> cst.ClassDef | cst.RemovalSentinel:
            pos = positions.get(original_node)
            if pos is not None and (pos.start.line, pos.end.line) in target_ranges:
                return cst.RemoveFromParent()
            return updated_node

    transformed = wrapper.visit(_StatementRemover())
    return transformed.code


def _insert_import_via_cst(code: str, utils_module_name: str, names: Sequence[str]) -> str:
    """Insert a `from <utils_module_name> import ...` statement after shebang/module header."""
    if not names:
        return code
    module = cst.parse_module(code)
    import_stmt = build_import_statement(utils_module_name, names)
    rendered_import = cst.Module(body=[import_stmt]).code.strip()
    if rendered_import in module.code:
        return module.code

    new_body: list[cst.BaseStatement] = list(module.body)
    insert_idx = 0
    if (new_body and isinstance(new_body[0], cst.EmptyLine) and new_body[0].comment is not None) and new_body[
        0
    ].comment.value.startswith("#!"):
        insert_idx = 1

    new_body.insert(insert_idx, import_stmt)
    updated = module.with_changes(body=new_body)
    return updated.code


def update_file_for_move(
    path: Path,
    objects_to_remove: Iterable[IndexedObject],
    utils_module_name: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Remove moved objects and add import from `utils_module_name` using `libcst`."""
    raw = safe_read_text(path)
    if raw is None:
        return False
    code = normalize_newlines(raw)
    removed_list = list(objects_to_remove)
    names = [obj.name for obj in removed_list]

    try:
        without_moved = _remove_objects_via_cst(code, removed_list)
        final_code = _insert_import_via_cst(without_moved, utils_module_name, names)
        cst.parse_module(final_code)
    except cst.ParserSyntaxError as exc:
        _logger.error("Skipping {}: resulting code is invalid CST: {}", path, exc)
        return False

    if dry_run:
        return True
    return safe_write_text(path, final_code)


def parse_args(argv: Sequence[str] | None = None) -> ScanOptions:
    """Parse CLI arguments into a strongly typed `ScanOptions` instance."""
    parser = argparse.ArgumentParser(
        description="Find repeated top-level Python objects and optionally move/copy them to utils.py"
    )
    parser.add_argument(
        "-r",
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Root directory to scan for Python files (default: current directory)",
    )
    parser.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="Move duplicate objects to utils.py and add imports",
    )
    parser.add_argument(
        "-c",
        "--copy",
        action="store_true",
        help="Copy duplicate objects to utils.py without changing source files",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview changes without writing files to disk",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=2,
        help="Minimum number of occurrences required to treat an object as duplicated (default: 2)",
    )
    parser.add_argument(
        "-t",
        "--include-tests",
        action="store_true",
        help="Include test directories and test files during scanning",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit duplicate report as JSON",
    )
    args = parser.parse_args(argv)

    if args.move and args.copy:
        _logger.error("Use either --move or --copy, not both.")
        sys.exit(1)
    if args.min_occurrences < 2:
        _logger.error("--min-occurrences must be at least 2.")
        sys.exit(1)

    return ScanOptions(
        root=args.root.resolve(),
        move=bool(args.move),
        copy=bool(args.copy),
        dry_run=bool(args.dry_run),
        min_occurrences=args.min_occurrences,
        include_tests=bool(args.include_tests),
        as_json=bool(args.json),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for scanning and deduplicating top-level Python objects."""
    opts = parse_args(argv)
    files = collect_python_files(opts.root, include_tests=opts.include_tests)
    if not files:
        if opts.as_json:
            print(json.dumps({"duplicates": [], "files_scanned": 0}))
        else:
            print("No Python files found.")
        return 0

    file_args = [str(p) for p in files]
    with mp.Pool(processes=max(1, MAX_WORKERS)) as pool:
        nested_results = pool.map(process_file, file_args)

    all_objects = [obj for sub in nested_results for obj in sub]
    if not all_objects:
        if opts.as_json:
            print(json.dumps({"duplicates": [], "files_scanned": len(files)}))
        else:
            print("No extractable objects found.")
        return 0

    by_hash: dict[str, list[IndexedObject]] = defaultdict(list)
    for obj in all_objects:
        by_hash[obj.digest].append(obj)

    duplicate_groups: dict[str, list[IndexedObject]] = {
        digest: group for digest, group in by_hash.items() if len(group) >= opts.min_occurrences
    }
    if not duplicate_groups:
        if opts.as_json:
            print(json.dumps({"duplicates": [], "files_scanned": len(files)}))
        else:
            print("No duplicates found.")
        return 0

    utils_objects = [group[0] for group in duplicate_groups.values()]

    if opts.as_json:
        payload = {
            "files_scanned": len(files),
            "unique_duplicates": len(utils_objects),
            "duplicates": [
                {
                    "hash": digest,
                    "occurrences": [
                        {
                            "file": item.file,
                            "name": item.name,
                            "kind": item.kind,
                            "start_line": item.start_line,
                            "end_line": item.end_line,
                        }
                        for item in group
                    ],
                }
                for digest, group in duplicate_groups.items()
            ],
        }
        print(json.dumps(payload, indent=2))
        if not (opts.move or opts.copy):
            return 0

    if not opts.as_json:
        print(f"Found {len(utils_objects)} unique duplicated objects.")

    if not (opts.move or opts.copy):
        for digest, group in duplicate_groups.items():
            print(f"Duplicate hash {digest[:12]} found in:")
            for item in group:
                print(f"  {item.file} :: {item.name} ({item.kind})")
        return 0

    utils_path = get_utils_path(opts.root)
    utils_module_name = utils_path.stem

    if opts.dry_run:
        print(f"[dry-run] Would write deduplicated objects to {utils_path}")
    elif not write_utils_file(utils_path, utils_objects):
        _logger.error("Failed to create utils file; aborting source changes.")
        return 1
    else:
        print(f"Wrote deduplicated objects to {utils_path}")

    if opts.copy:
        print("Copy mode: source files were not modified and no imports were added.")
        return 0

    by_file_to_remove: dict[str, list[IndexedObject]] = defaultdict(list)
    for group in duplicate_groups.values():
        for obj in group:
            obj_path = Path(obj.file)
            try:
                obj_path.relative_to(opts.root)
                is_under_root = True
            except ValueError:
                is_under_root = False
            if is_under_root and obj_path.suffix == ".py" and obj_path.exists():
                by_file_to_remove[obj.file].append(obj)

    for file_str, objects in by_file_to_remove.items():
        path = Path(file_str)
        if path.resolve() == utils_path.resolve():
            continue
        ok = update_file_for_move(
            path,
            objects,
            utils_module_name,
            dry_run=opts.dry_run,
        )
        if ok:
            prefix = "[dry-run] Would update" if opts.dry_run else "Updated"
            print(f"{prefix} {path}")
        else:
            _logger.error("Failed to update {}", path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
