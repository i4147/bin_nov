#!/data/data/com.termux/files/usr/bin/env python
"""Find repeated top-level Python objects and consolidate them into a utils module.

Parses modules with libcst (including .zip/.tar archives), hashes top-level functions, classes and constants, and reports exact duplicates; --move/--copy relocate or replicate the duplicates into a shared utils.py.
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
from typing import TYPE_CHECKING, Literal
import zipfile

from dh import MAX_WORKERS, SKIP_DIRS, atomic_write
import libcst as cst
from libcst import matchers as m
from loguru import logger


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


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


@dataclass(frozen=True)
class ExtractedObject:
    """Represent a top-level Python object extracted from a source file."""

    file: str
    name: str
    kind: ObjectKind
    snippet: str
    hash: str
    lineno: int
    end_lineno: int


def sha256(text: str) -> str:
    """Return the hex-encoded SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_newlines(text: str) -> str:
    """Normalize CRLF and CR line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def safe_read_text(path: Path) -> str | None:
    """Read UTF-8 text from a file with normalized newlines, logging errors."""
    try:
        return normalize_newlines(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error(f"Failed reading {path}: {exc}")
        return None


def safe_write_text(path: Path, content: str) -> bool:
    """Atomically write UTF-8 text with LF newlines to disk, logging errors."""
    try:
        atomic_write(path, content, encoding="utf-8", newline="\n")
        return True
    except OSError as exc:
        logger.error(f"Failed writing {path}: {exc}")
        return False


def is_supported_archive(path: Path) -> bool:
    """Return True if the file path ends with a supported archive extension."""
    lowered = str(path).lower()
    return any(lowered.endswith(ext) for ext in SUPPORTED_ARCHIVES)


def should_skip_dir(name: str) -> bool:
    """Return True if a directory name is listed in SKIP_DIRS."""
    return name in SKIP_DIRS


def _decompress_single_stream(path: Path, tmpdir: Path) -> None:
    """Decompress a single-file archive (.gz, .bz2, .xz, .zst, .br) into tmpdir."""
    low = str(path).lower()
    out = tmpdir / path.stem

    if low.endswith(".gz") and not low.endswith(".tar.gz"):
        with gzip.open(path, "rb") as fin, out.open("wb") as fout:
            fout.write(fin.read())
    elif low.endswith(".bz2") and not low.endswith(".tar.bz2"):
        with bz2.open(path, "rb") as fin, out.open("wb") as fout:
            fout.write(fin.read())
    elif low.endswith(".xz") and not low.endswith(".tar.xz"):
        with lzma.open(path, "rb") as fin, out.open("wb") as fout:
            fout.write(fin.read())
    elif low.endswith(".zst"):
        try:
            import zstandard as zstd  # type: ignore[import-not-found]
        except ImportError:
            logger.error(f"zstandard not installed; cannot extract {path}")
            return
        with path.open("rb") as fin:
            data: bytes = zstd.ZstdDecompressor().decompress(fin.read())
        out.write_bytes(data)
    elif low.endswith(".br"):
        try:
            import brotli  # type: ignore[import-not-found]
        except ImportError:
            logger.error(f"brotli not installed; cannot extract {path}")
            return
        data_br: bytes = brotli.decompress(path.read_bytes())
        out.write_bytes(data_br)


def extract_archive(path: Path) -> str:
    """Extract an archive into a temporary directory and return its path."""
    tmpdir_str = tempfile.mkdtemp(prefix="dedup_py_")
    tmpdir = Path(tmpdir_str)
    low = str(path).lower()
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                zf.extractall(tmpdir, filter="data")
        elif low.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(path) as tf:
                tf.extractall(tmpdir, filter="data")
        else:
            _decompress_single_stream(path, tmpdir)
    except Exception as exc:
        logger.error(f"Failed extracting {path}: {exc}")
    return tmpdir_str


def collect_python_files(base: Path) -> list[Path]:
    """Collect Python files under base, including inside supported archives."""
    files: list[Path] = []

    def walk(current: Path) -> None:
        if should_skip_dir(current.name):
            return
        try:
            for item in current.iterdir():
                if item.is_dir():
                    walk(item)
                elif item.is_file():
                    if item.suffix == ".py":
                        files.append(item)
                    elif is_supported_archive(item):
                        tmp = extract_archive(item)
                        files.extend(sorted(Path(tmp).rglob("*.py")))
        except PermissionError:
            logger.warning(f"Permission denied: {current}")

    walk(base)
    return files


def _line_span_for_node(wrapper: cst.MetadataWrapper, node: cst.CSTNode) -> tuple[int, int]:
    """Return 1-based (start_line, end_line) for a CST node."""
    pos = wrapper.resolve(cst.metadata.PositionProvider)[node]
    return pos.start.line, pos.end.line


def _extract_constant_name(assign: cst.Assign) -> str | None:
    """Return the identifier name for a simple single-target assignment, or None."""
    if len(assign.targets) != 1:
        return None
    target = assign.targets[0].target
    if isinstance(target, cst.Name):
        return target.value
    return None


def extract_objects_with_cst(code: str, file_path: str = "") -> list[ExtractedObject]:
    """Extract top-level functions, classes, and simple constants using LibCST."""
    try:
        module = cst.parse_module(code)
    except cst.ParserSyntaxError as exc:
        logger.error(f"LibCST parse failed for {file_path or '<memory>'}: {exc}")
        return []

    wrapper = cst.MetadataWrapper(module)
    objects: list[ExtractedObject] = []

    for stmt in wrapper.module.body:
        if isinstance(stmt, cst.FunctionDef):
            snippet = wrapper.module.code_for_node(stmt)
            start_line, end_line = _line_span_for_node(wrapper, stmt)
            objects.append(
                ExtractedObject(
                    file=file_path,
                    name=stmt.name.value,
                    kind="function",
                    snippet=snippet,
                    hash=sha256(snippet.strip()),
                    lineno=start_line,
                    end_lineno=end_line,
                )
            )
        elif isinstance(stmt, cst.ClassDef):
            snippet = wrapper.module.code_for_node(stmt)
            start_line, end_line = _line_span_for_node(wrapper, stmt)
            objects.append(
                ExtractedObject(
                    file=file_path,
                    name=stmt.name.value,
                    kind="class",
                    snippet=snippet,
                    hash=sha256(snippet.strip()),
                    lineno=start_line,
                    end_lineno=end_line,
                )
            )
        elif isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
            inner = stmt.body[0]
            if isinstance(inner, cst.Assign):
                const_name = _extract_constant_name(inner)
                if const_name is None or const_name in {
                    "__all__",
                    "__version__",
                    "__doc__",
                }:
                    continue
                snippet = wrapper.module.code_for_node(stmt)
                start_line, end_line = _line_span_for_node(wrapper, stmt)
                objects.append(
                    ExtractedObject(
                        file=file_path,
                        name=const_name,
                        kind="constant",
                        snippet=snippet,
                        hash=sha256(snippet.strip()),
                        lineno=start_line,
                        end_lineno=end_line,
                    )
                )

    return objects


def process_file(path_str: str) -> list[ExtractedObject]:
    """Read a Python file and return extracted top-level objects."""
    path = Path(path_str)
    code = safe_read_text(path)
    if code is None:
        return []
    return [obj for obj in extract_objects_with_cst(code, str(path)) if obj.snippet.strip()]


def get_utils_path(base: Path, custom_output: Path | None = None) -> Path:
    """Resolve the target path for deduplicated objects."""
    if custom_output is not None:
        return custom_output
    candidate = base / "utils.py"
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        numbered = base / f"utils_{idx}.py"
        if not numbered.exists():
            return numbered
        idx += 1


def write_utils_file(path: Path, objects: Iterable[ExtractedObject], dry_run: bool = False) -> bool:
    """Write deduplicated objects to path after validating CST syntax."""
    seen: set[tuple[str, str]] = set()
    parts: list[str] = []
    for obj in objects:
        key = (obj.hash, obj.name)
        if key in seen:
            continue
        seen.add(key)
        parts.append(obj.snippet.rstrip())

    if not parts:
        return False

    content = "\n\n".join(parts).rstrip() + "\n"
    try:
        cst.parse_module(content)
    except cst.ParserSyntaxError as exc:
        logger.error(f"Generated {path.name} has syntax error: {exc}")
        return False

    if dry_run:
        logger.info(f"[dry-run] Would write {len(parts)} objects to {path}")
        return True
    return safe_write_text(path, content)


class _RemoveTargetObjectsTransformer(cst.CSTTransformer):
    """Remove top-level statements whose 1-based line ranges match target ranges."""

    def __init__(self, target_ranges: set[tuple[int, int]], wrapper: cst.MetadataWrapper) -> None:
        super().__init__()
        self._target_ranges = target_ranges
        self._wrapper = wrapper

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        kept_body: list[cst.BaseStatement | cst.SmallStatement] = []
        for stmt in updated_node.body:
            if isinstance(stmt, cst.BaseStatement):
                pos = self._wrapper.resolve(cst.metadata.PositionProvider)[stmt]
                if (pos.start.line, pos.end.line) in self._target_ranges:
                    continue
            kept_body.append(stmt)
        return updated_node.with_changes(body=kept_body)


def _build_from_import(module_name: str, names: Sequence[str]) -> cst.SimpleStatementLine:
    """Construct a `from <module_name> import (<names>)` statement node."""
    sorted_names = sorted(set(names))
    if len(sorted_names) == 1:
        imports: Sequence[cst.ImportAlias] = [cst.ImportAlias(name=cst.Name(sorted_names[0]))]
        import_node = cst.ImportFrom(
            module=cst.Name(module_name),
            imports=imports,
        )
        return cst.SimpleStatementLine(body=[import_node])

    aliases = [
        cst.ImportAlias(
            name=cst.Name(n),
            comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")),
        )
        for n in sorted_names[:-1]
    ]
    aliases.append(cst.ImportAlias(name=cst.Name(sorted_names[-1])))
    import_node = cst.ImportFrom(
        module=cst.Name(module_name),
        imports=aliases,
        lpar=cst.LeftParen(),
        rpar=cst.RightParen(),
    )
    return cst.SimpleStatementLine(body=[import_node])


def _find_import_insertion_index(
    body: Sequence[cst.BaseStatement | cst.SmallStatement],
) -> int:
    """Find the statement index immediately after a module docstring if present."""
    if not body:
        return 0
    first = body[0]
    if m.matches(
        first,
        m.SimpleStatementLine(body=[m.Expr(value=m.SimpleString())]),
    ):
        return 1
    return 0


def transform_source_for_move(
    code: str,
    ranges_and_names: Iterable[tuple[int, int, str]],
    utils_module_name: str,
) -> str | None:
    """Remove duplicate top-level definitions and insert an import from utils_module_name."""
    pairs = list(ranges_and_names)
    if not pairs:
        return None

    target_ranges = {(start, end) for start, end, _ in pairs}
    imported_names = sorted({name for _, _, name in pairs})

    try:
        module = cst.parse_module(code)
    except cst.ParserSyntaxError as exc:
        logger.error(f"Failed parsing source before move: {exc}")
        return None

    wrapper = cst.MetadataWrapper(module)
    removed_module = wrapper.visit(_RemoveTargetObjectsTransformer(target_ranges, wrapper))

    import_stmt = _build_from_import(utils_module_name, imported_names)
    new_body = list(removed_module.body)
    insert_idx = _find_import_insertion_index(new_body)
    new_body.insert(insert_idx, import_stmt)

    final_module = removed_module.with_changes(body=new_body)
    rendered = final_module.code
    try:
        cst.parse_module(rendered)
    except cst.ParserSyntaxError as exc:
        logger.error(f"Transformed code failed CST validation: {exc}")
        return None
    return rendered


def file_is_under_base(path: Path, base: Path) -> bool:
    """Return True if resolving path stays within base directory."""
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except OSError:
        return False
    except ValueError:
        return False


def update_file_for_move(
    path: Path,
    objects_to_remove: Iterable[ExtractedObject],
    utils_module_name: str,
    dry_run: bool = False,
) -> bool:
    """Update a source file to remove moved objects and import them from utils_module_name."""
    code = safe_read_text(path)
    if code is None:
        return False

    ranges_and_names = [
        (obj.lineno, obj.end_lineno, obj.name)
        for obj in objects_to_remove
        if obj.lineno > 0 and obj.end_lineno >= obj.lineno
    ]
    if not ranges_and_names:
        logger.warning(f"No valid line ranges to remove in {path}")
        return False

    updated_code = transform_source_for_move(code, ranges_and_names, utils_module_name)
    if updated_code is None:
        return False

    if dry_run:
        logger.info(f"[dry-run] Would update {path}")
        return True
    return safe_write_text(path, updated_code)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for duplicate detection and consolidation."""
    parser = argparse.ArgumentParser(
        description="Find repeated top-level Python objects and optionally move/copy them to utils.py"
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
        help="Copy duplicate objects to utils.py without modifying source files",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview changes without writing files to disk",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Target path for consolidated utils module (default: auto utils.py)",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=max(1, MAX_WORKERS),
        help="Number of worker processes for file analysis",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="DEBUG, INFO, WARNING, ERROR",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run duplicate object discovery and optional consolidation."""
    args = _parse_args(argv)
    if args.move and args.copy:
        logger.error("Choose only one of --move or --copy.")
        return 2

    logger.remove()
    logger.add(sys.stderr, level=str(args.log_level).upper())

    base = Path.cwd()
    files = collect_python_files(base)
    if not files:
        if args.json:
            print(json.dumps({"files": 0, "duplicate_groups": []}))
        else:
            print("No Python files found.")
        return 0

    workers = max(1, int(args.workers))
    with mp.Pool(processes=workers) as pool:
        nested = pool.map(process_file, [str(p) for p in files])

    all_objects = [obj for sub in nested for obj in sub]
    if not all_objects:
        if args.json:
            print(json.dumps({"files": len(files), "duplicate_groups": []}))
        else:
            print("No top-level objects found.")
        return 0

    by_hash: dict[str, list[ExtractedObject]] = defaultdict(list)
    for obj in all_objects:
        by_hash[obj.hash].append(obj)

    duplicate_groups = {h: group for h, group in by_hash.items() if len(group) > 1}
    if not duplicate_groups:
        if args.json:
            print(json.dumps({"files": len(files), "duplicate_groups": []}))
        else:
            print("No duplicates found.")
        return 0

    if args.json and not args.move and not args.copy:
        payload = {
            "files": len(files),
            "duplicate_groups": [
                {
                    "hash": h,
                    "items": [
                        {
                            "file": item.file,
                            "name": item.name,
                            "kind": item.kind,
                            "lineno": item.lineno,
                            "end_lineno": item.end_lineno,
                        }
                        for item in group
                    ],
                }
                for h, group in duplicate_groups.items()
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not args.json:
        print(f"Found {len(duplicate_groups)} duplicate content groups.")

    if not args.move and not args.copy:
        for h, group in duplicate_groups.items():
            print(f"Duplicate {h[:12]}:")
            for item in group:
                print(f"  {item.file} :: {item.name} ({item.kind}) lines {item.lineno}-{item.end_lineno}")
        return 0

    custom_out: Path | None = args.output
    utils_path = get_utils_path(base, custom_out)
    utils_module_name = utils_path.stem
    utils_objects = [group[0] for group in duplicate_groups.values()]

    if not write_utils_file(utils_path, utils_objects, dry_run=bool(args.dry_run)):
        logger.error("Aborting because utils file could not be written.")
        return 1

    if not args.json:
        print(f"Wrote deduplicated objects to {utils_path}")

    if args.copy:
        if args.json:
            print(json.dumps({"utils_path": str(utils_path), "mode": "copy", "updated": []}))
        else:
            print("Copy mode complete; source files unchanged.")
        return 0

    by_file: dict[str, list[ExtractedObject]] = defaultdict(list)
    for group in duplicate_groups.values():
        for obj in group:
            candidate = Path(obj.file)
            if candidate.exists() and candidate.suffix == ".py" and file_is_under_base(candidate, base):
                if candidate.resolve() == utils_path.resolve():
                    continue
                by_file[str(candidate)].append(obj)

    updated_files: list[str] = []
    for file_str, objs in by_file.items():
        if update_file_for_move(Path(file_str), objs, utils_module_name, dry_run=bool(args.dry_run)):
            updated_files.append(file_str)
            if not args.json:
                print(f"Updated {file_str}")
        else:
            logger.error(f"Failed to update {file_str}")

    if args.json:
        print(
            json.dumps(
                {
                    "utils_path": str(utils_path),
                    "mode": "move",
                    "dry_run": bool(args.dry_run),
                    "updated": updated_files,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
