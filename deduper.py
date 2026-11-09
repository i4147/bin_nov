#!/data/data/com.termux/files/usr/bin/env python
"""Move or copy the repeated top-level Python objects into ``utils.py``.
Extracts the definitions with ast or tree-sitter (also from the zip, tar, gzip, xz and zstd archives), hashes them, writes the shared ones into ``utils.py`` and, with ``-m``, adds the import to each source after the module docstring; ``-c`` only copies.
Usage: deduper [-m|-c] [--log-level LEVEL] Options: -m, --move Move duplicate objects to utils.py and add imports -c, --copy Copy duplicate objects to utils.py without modifying source files --log-level DEBUG, INFO, WARNING, ERROR Examples: $ deduper -m Command-line script: run ``python deduper.py`` or the ``deduper`` command.
The module is importable as well."""

from __future__ import annotations

import argparse
import ast
from ast import Assign, AsyncFunctionDef, ClassDef, FunctionDef
import bz2
from collections import defaultdict
import gzip
import hashlib
import lzma
import multiprocessing as mp
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import zipfile
from typing import Any, Iterable

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


try:
    from tree_sitter import Language, Parser
    import tree_sitter_python

    TREE_SITTER_AVAILABLE = True
except Exception:
    TREE_SITTER_AVAILABLE = False
try:
    import zstandard as zstd
except Exception:
    zstd = None
try:
    import brotli
except Exception:
    brotli = None
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


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def safe_read_text(path: Path) -> str | None:
    try:
        return normalize_newlines(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed reading {path}: {e}")
        return None


def safe_write_text(path: Path, content: str) -> bool:
    try:
        path.write_text(content, encoding="utf-8", newline="\n")
        return True
    except Exception as e:
        logger.error(f"Failed writing {path}: {e}")
        return False


def is_supported_archive(path: Path) -> bool:
    s = str(path).lower()
    return any(s.endswith(ext) for ext in SUPPORTED_ARCHIVES)


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS


def extract_archive(path: Path) -> str:
    tmpdir = tempfile.mkdtemp(prefix="dedup_py_")
    low = str(path).lower()
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                zf.extractall(tmpdir, filter="data")
        elif low.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(path) as tf:
                tf.extractall(tmpdir, filter="data")
        elif low.endswith(".gz") and not low.endswith(".tar.gz"):
            out = Path(tmpdir) / path.stem
            with gzip.open(path, "rb") as fin, open(out, "wb") as fout:
                fout.write(fin.read())
        elif low.endswith(".bz2") and not low.endswith(".tar.bz2"):
            out = Path(tmpdir) / path.stem
            with bz2.open(path, "rb") as fin, open(out, "wb") as fout:
                fout.write(fin.read())
        elif low.endswith(".xz") and not low.endswith(".tar.xz"):
            out = Path(tmpdir) / path.stem
            with lzma.open(path, "rb") as fin, open(out, "wb") as fout:
                fout.write(fin.read())
        elif low.endswith(".zst"):
            if zstd is None:
                logger.error(f"zstandard not installed; cannot extract {path}")
            else:
                out = Path(tmpdir) / path.stem
                with open(path, "rb") as fin:
                    dctx = zstd.ZstdDecompressor()
                    data = dctx.decompress(fin.read())
                with open(out, "wb") as fout:
                    fout.write(data)
        elif low.endswith(".br"):
            if brotli is None:
                logger.error(f"brotli not installed; cannot extract {path}")
            else:
                out = Path(tmpdir) / path.stem
                with open(path, "rb") as fin:
                    data = brotli.decompress(fin.read())
                with open(out, "wb") as fout:
                    fout.write(data)
    except Exception as e:
        logger.error(f"Failed extracting {path}: {e}")
    return tmpdir


def collect_python_files(base: Path) -> list[Any] | None:
    files = []

    def walk(p: Path) -> None:
        if should_skip_dir(p.name):
            return
        try:
            for item in p.iterdir():
                if item.is_dir():
                    walk(item)
                elif item.is_file():
                    if item.suffix == ".py":
                        files.append(item)
                    elif is_supported_archive(item):
                        tmp = extract_archive(item)
                        files.extend(Path(tmp).rglob("*.py"))
        except PermissionError:
            logger.warning(f"Permission denied: {p}")

    walk(base)
    return files


def get_module_docstring_line_span(tree: ast.Module) -> tuple[int, int | None] | None:
    if not tree.body:
        return None
    first = tree.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first.lineno, first.end_lineno
    return None


def source_segment(code: str, node: Assign | AsyncFunctionDef | ClassDef | FunctionDef) -> str | None:
    seg = ast.get_source_segment(code, node)
    if seg is not None:
        return seg
    if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
        lines = code.splitlines(keepends=True)
        return "".join(lines[node.lineno - 1 : node.end_lineno])
    return None


def is_simple_constant_assign(node: ast.Assign) -> bool:
    return len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)


def extract_with_ast(code: str):
    objects = []
    try:
        tree = ast.parse(code)
    except Exception as e:
        logger.error(f"AST parse failed: {e}")
        return objects
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            snippet = source_segment(code, node)
            if not snippet:
                continue
            objects.append({
                "name": node.name,
                "kind": "function",
                "snippet": snippet,
                "lineno": node.lineno,
                "end_lineno": node.end_lineno,
            })
        elif isinstance(node, ast.ClassDef):
            snippet = source_segment(code, node)
            if not snippet:
                continue
            objects.append({
                "name": node.name,
                "kind": "class",
                "snippet": snippet,
                "lineno": node.lineno,
                "end_lineno": node.end_lineno,
            })
        elif isinstance(node, ast.Assign) and is_simple_constant_assign(node):
            name = node.targets[0].id
            if name in {"__all__", "__version__", "__doc__"}:
                continue
            snippet = source_segment(code, node)
            if not snippet:
                continue
            objects.append({
                "name": name,
                "kind": "constant",
                "snippet": snippet,
                "lineno": node.lineno,
                "end_lineno": node.end_lineno,
            })
    return objects


def extract_with_tree_sitter(code: str):
    if not TREE_SITTER_AVAILABLE:
        return extract_with_ast(code)
    try:
        parser = Parser()
        lang = tree_sitter_python.language()
        try:
            parser.language = Language(lang)
        except AttributeError:
            parser.language = lang
        parser.parse(code.encode("utf-8"))
    except Exception as e:
        logger.warning(f"Tree-sitter failed; falling back to AST: {e}")
        return extract_with_ast(code)
    return extract_with_ast(code)


def extract_objects(code: str):
    if TREE_SITTER_AVAILABLE:
        return extract_with_tree_sitter(code)
    return extract_with_ast(code)


def process_file(path_str: str):
    path = Path(path_str)
    code = safe_read_text(path)
    if code is None:
        return []
    objs = extract_objects(code)
    out = []
    for obj in objs:
        stripped = obj["snippet"].strip()
        if not stripped:
            continue
        out.append({
            "file": str(path),
            "name": obj["name"],
            "kind": obj["kind"],
            "snippet": obj["snippet"],
            "hash": sha256(stripped),
            "lineno": obj["lineno"],
            "end_lineno": obj["end_lineno"],
        })
    return out


def get_utils_path(base: Path) -> Path:
    p = base / "utils.py"
    if not p.exists():
        return p
    i = 1
    while True:
        cand = base / f"utils_{i}.py"
        if not cand.exists():
            return cand
        i += 1


def write_utils_file(path: Path, objects: Iterable[Any]) -> bool:
    names_seen = set()
    parts = []
    for obj in objects:
        key = obj["hash"], obj["name"]
        if key in names_seen:
            continue
        names_seen.add(key)
        parts.append(obj["snippet"].rstrip())
    content = "\n\n".join(parts).rstrip() + "\n"
    try:
        ast.parse(content)
    except SyntaxError as e:
        logger.error(f"Generated {path.name} has syntax error: {e}")
        return False
    return safe_write_text(path, content)


_ENCODING_RE = re.compile(r"^#.*coding[:=]\s*([-\w.]+)")


def find_import_insertion_index(code: str) -> int:
    lines = code.splitlines(keepends=True)
    idx = 0
    if idx < len(lines) and lines[idx].startswith("#!"):
        idx += 1
    if idx < len(lines) and _ENCODING_RE.match(lines[idx]):
        idx += 1
    elif idx + 1 < len(lines) and _ENCODING_RE.match(lines[idx + 1]) and idx == 0:
        idx += 2
    try:
        tree = ast.parse(code)
        docspan = get_module_docstring_line_span(tree)
        if docspan:
            _, end_lineno = docspan
            idx = max(idx, end_lineno)
    except Exception:
        pass
    return idx


def add_import_line(code: str, module_name: str, names: list[Any]) -> str:
    names = sorted(set(names))
    if not names:
        return code
    import_line = f"from {module_name} import ({', '.join(names)})\n"
    if import_line in code:
        return code
    lines = code.splitlines(keepends=True)
    idx = find_import_insertion_index(code)
    lines.insert(idx, import_line)
    return "".join(lines)


def merge_overlapping_ranges(ranges: Iterable[Any]) -> list[tuple[Any]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged = [list(ranges[0])]
    for s, e in ranges[1:]:
        last = merged[-1]
        if s <= last[1]:
            last[1] = max(last[1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def remove_line_ranges(code: str, ranges: Iterable[Any]) -> str:
    lines = code.splitlines(keepends=True)
    zero_based = [(s - 1, e - 1) for s, e in ranges]
    merged = merge_overlapping_ranges(zero_based)
    out = []
    cursor = 0
    for s, e in merged:
        out.extend(lines[cursor:s])
        cursor = e + 1
    out.extend(lines[cursor:])
    text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def file_is_under_base(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def update_file_for_move(path: Path, objects_to_remove: Iterable[Any], utils_module_name: str) -> bool:
    code = safe_read_text(path)
    if code is None:
        return False
    ranges = []
    names = []
    for obj in objects_to_remove:
        if obj.get("lineno") and obj.get("end_lineno"):
            ranges.append((obj["lineno"], obj["end_lineno"]))
            names.append(obj["name"])
    if not ranges:
        logger.warning(f"No valid ranges to remove in {path}")
        return False
    new_code = remove_line_ranges(code, ranges)
    try:
        ast.parse(new_code)
    except SyntaxError as e:
        logger.error(f"After removal, {path} is invalid: {e}")
        return False
    new_code = add_import_line(new_code, utils_module_name, names)
    try:
        ast.parse(new_code)
    except SyntaxError as e:
        logger.error(f"After adding import, {path} is invalid: {e}")
        return False
    return safe_write_text(path, new_code)


def main() -> None:
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
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    args = parser.parse_args()
    if args.move and args.copy:
        logger.error("Choose only one of --move or --copy.")
        sys.exit(2)
    logger.remove()
    logger.add(sys.stderr, level=args.log_level.upper())
    base = Path.cwd()
    files = collect_python_files(base)
    if not files:
        print("No Python files found.")
        return
    with mp.Pool(processes=max(1, MAX_WORKERS)) as pool:
        nested = pool.map(process_file, [str(p) for p in files])
    all_objects = [obj for sub in nested for obj in sub]
    if not all_objects:
        print("No top-level objects found.")
        return
    by_hash = defaultdict(list)
    for obj in all_objects:
        by_hash[obj["hash"]].append(obj)
    duplicate_groups = {h: group for h, group in by_hash.items() if len(group) > 1}
    if not duplicate_groups:
        print("No duplicates found.")
        return
    print(f"Found {len(duplicate_groups)} duplicate content groups.")
    if not args.move and not args.copy:
        for h, group in duplicate_groups.items():
            print(f"Duplicate {h[:12]}:")
            for g in group:
                print(f"  {g['file']} :: {g['name']} ({g['kind']}) lines {g['lineno']}-{g['end_lineno']}")
        return
    utils_path = get_utils_path(base)
    utils_module_name = utils_path.stem
    utils_objects = [group[0] for group in duplicate_groups.values()]
    if not write_utils_file(utils_path, utils_objects):
        logger.error("Aborting because utils file could not be written.")
        sys.exit(1)
    print(f"Wrote deduplicated objects to {utils_path}")
    if args.copy:
        print("Copy mode complete; source files unchanged.")
        return
    by_file = defaultdict(list)
    for group in duplicate_groups.values():
        for obj in group:
            p = Path(obj["file"])
            if p.exists() and p.suffix == ".py" and file_is_under_base(p, base):
                if p.resolve() == utils_path.resolve():
                    continue
                by_file[str(p)].append(obj)
    for file_str, objs in by_file.items():
        ok = update_file_for_move(Path(file_str), objs, utils_module_name)
        if ok:
            print(f"Updated {file_str}")
        else:
            logger.error(f"Failed to update {file_str}")


if __name__ == "__main__":
    raise SystemExit(main())
