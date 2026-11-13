#!/data/data/com.termux/files/usr/bin/env python
"""Consolidate the duplicated Python objects of a tree into ``utils.py``.
Hashes the top-level definitions (ast or tree-sitter, archives included), writes the repeated ones into ``utils.py`` and rewrites the sources with ``-m`` (import inserted, snippets removed) or only copies them with ``-c``.
Usage: deduplicate_python [-m|-c] [paths ...] Options: -m, --move Move duplicate objects to utils.py and add imports -c, --copy Copy duplicate objects to utils.py without changing source files Examples: $ deduplicate_python -c src/ Command-line script: run ``python deduplicate_python.py`` or the ``deduplicate_python`` command.
The module is importable as well."""

import argparse
import ast
import bz2
from collections import defaultdict
import gzip
import hashlib
import lzma
import multiprocessing as mp
from pathlib import Path
import sys
import tarfile
import tempfile
import zipfile
from typing import Any, Iterable

from dh import MAX_WORKERS
from loguru import logger

try:
    from tree_sitter import Parser
    import tree_sitter_python

    TREE_SITTER_AVAILABLE = True
except Exception:
    TREE_SITTER_AVAILABLE = False
try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import brotli
except ImportError:
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


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed reading {path}: {e}")
        return None


def safe_write_text(path: Path, content: str) -> bool:
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"Failed writing {path}: {e}")
        return False


def normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def extract_with_tree_sitter(code: str):
    objects = []
    try:
        parser = Parser()
        parser.language = Language(tree_sitter_python.language())
        tree = parser.parse(code.encode("utf-8"))
        root = tree.root_node
        for node in root.children:
            if node.type in ("function_definition", "class_definition"):
                name_node = node.child_by_field_name("name")
                if not name_node:
                    continue
                name = code[name_node.start_byte : name_node.end_byte]
                snippet = code[node.start_byte : node.end_byte]
                kind = "function" if node.type == "function_definition" else "class"
                objects.append({
                    "name": name,
                    "kind": kind,
                    "snippet": snippet,
                    "start_byte": node.start_byte,
                    "end_byte": node.end_byte,
                })
            elif node.type == "expression_statement":
                text = code[node.start_byte : node.end_byte]
                try:
                    parsed = ast.parse(text)
                    if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Assign):
                        assign = parsed.body[0]
                        if all(isinstance(t, ast.Name) for t in assign.targets):
                            name = assign.targets[0].id
                            objects.append({
                                "name": name,
                                "kind": "constant",
                                "snippet": text,
                                "start_byte": node.start_byte,
                                "end_byte": node.end_byte,
                            })
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Tree-sitter failed; falling back to ast: {e}")
        return extract_with_ast(code)
    return objects


def extract_with_ast(code: str):
    objects = []
    try:
        tree = ast.parse(code)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                snippet = ast.get_source_segment(code, node)
                if snippet is None:
                    continue
                objects.append({
                    "name": node.name,
                    "kind": "function",
                    "snippet": snippet,
                    "start_byte": None,
                    "end_byte": None,
                    "lineno": node.lineno,
                    "end_lineno": getattr(node, "end_lineno", None),
                })
            elif isinstance(node, ast.ClassDef):
                snippet = ast.get_source_segment(code, node)
                if snippet is None:
                    continue
                objects.append({
                    "name": node.name,
                    "kind": "class",
                    "snippet": snippet,
                    "start_byte": None,
                    "end_byte": None,
                    "lineno": node.lineno,
                    "end_lineno": getattr(node, "end_lineno", None),
                })
            elif isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
                snippet = ast.get_source_segment(code, node)
                if snippet is None:
                    continue
                objects.append({
                    "name": node.targets[0].id,
                    "kind": "constant",
                    "snippet": snippet,
                    "start_byte": None,
                    "end_byte": None,
                    "lineno": node.lineno,
                    "end_lineno": getattr(node, "end_lineno", None),
                })
    except Exception as e:
        logger.error(f"AST parsing failed: {e}")
    return objects


def extract_objects(code: str):
    if TREE_SITTER_AVAILABLE:
        return extract_with_tree_sitter(code)
    return extract_with_ast(code)


def is_supported_archive(path: Path) -> bool:
    s = str(path).lower()
    return any(s.endswith(ext) for ext in SUPPORTED_ARCHIVES)


def extract_archive(path: Path) -> str:
    temp_dir = tempfile.mkdtemp(prefix="dedup_py_")
    lower = str(path).lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                zf.extractall(temp_dir)
        elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(path) as tf:
                tf.extractall(temp_dir)
        elif lower.endswith(".gz") and (not lower.endswith(".tar.gz")):
            out_path = Path(temp_dir) / path.stem
            with gzip.open(path, "rb") as f_in, open(out_path, "wb") as f_out:
                f_out.write(f_in.read())
        elif lower.endswith(".bz2") and (not lower.endswith(".tar.bz2")):
            out_path = Path(temp_dir) / path.stem
            with bz2.open(path, "rb") as f_in, open(out_path, "wb") as f_out:
                f_out.write(f_in.read())
        elif lower.endswith(".xz") and (not lower.endswith(".tar.xz")):
            out_path = Path(temp_dir) / path.stem
            with lzma.open(path, "rb") as f_in, open(out_path, "wb") as f_out:
                f_out.write(f_in.read())
        elif lower.endswith(".zst"):
            if zstd is None:
                logger.error(f"zstandard not installed; cannot extract {path}")
                return temp_dir
            out_path = Path(temp_dir) / path.stem
            with open(path, "rb") as f_in:
                dctx = zstd.ZstdDecompressor()
                data = dctx.decompress(f_in.read())
            with open(out_path, "wb") as f_out:
                f_out.write(data)
        elif lower.endswith(".br"):
            if brotli is None:
                logger.error(f"brotli not installed; cannot extract {path}")
                return temp_dir
            out_path = Path(temp_dir) / path.stem
            with open(path, "rb") as f_in:
                data = brotli.decompress(f_in.read())
            with open(out_path, "wb") as f_out:
                f_out.write(data)
    except Exception as e:
        logger.error(f"Failed extracting archive {path}: {e}")
    return temp_dir


def should_skip_dir(path: Path) -> bool:
    skip_names = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "env",
        ".eggs",
        "site-packages",
    }
    return path.name in skip_names


def collect_python_files(base: Path) -> list[Any] | None:
    files = []

    def walk(p: Path) -> None:
        if should_skip_dir(p):
            return
        try:
            for item in p.iterdir():
                if item.is_dir():
                    walk(item)
                elif item.is_file():
                    if item.suffix == ".py":
                        files.append(item)
                    elif is_supported_archive(item):
                        extracted_dir = extract_archive(item)
                        for py_file in Path(extracted_dir).rglob("*.py"):
                            files.append(py_file)
        except PermissionError:
            logger.warning(f"Permission denied: {p}")

    walk(base)
    return files


def process_file(path_str: str):
    path = Path(path_str)
    code = safe_read_text(path)
    if not code:
        return []
    code = normalize_newlines(code)
    objs = extract_objects(code)
    result = []
    for obj in objs:
        snippet = obj["snippet"].strip()
        if not snippet:
            continue
        result.append({
            "file": str(path),
            "name": obj["name"],
            "kind": obj["kind"],
            "snippet": obj["snippet"],
            "hash": sha256(snippet),
            "start_byte": obj.get("start_byte"),
            "end_byte": obj.get("end_byte"),
            "lineno": obj.get("lineno"),
            "end_lineno": obj.get("end_lineno"),
        })
    return result


def get_utils_path(base: Path) -> Path:
    default_path = base / "utils.py"
    if not default_path.exists():
        return default_path
    i = 1
    while True:
        candidate = base / f"utils_{i}.py"
        if not candidate.exists():
            return candidate
        i += 1


def build_import_line(utils_module_name: str, names: list[Any]) -> str:
    names = sorted(set(names))
    return f"from {utils_module_name} import ({', '.join(names)})\n"


def write_utils_file(path: Path, objects: Iterable[Any]) -> bool:
    content = "\n\n".join(obj["snippet"].rstrip() for obj in objects).rstrip() + "\n"
    try:
        ast.parse(content)
    except SyntaxError as e:
        logger.error(f"Refusing to write {path}: generated code has syntax error: {e}")
        return False
    return safe_write_text(path, content)


def insert_import_after_shebang(code: str, import_line: str) -> str:
    lines = code.splitlines(keepends=True)
    if not lines:
        return import_line
    insert_at = 0
    if lines[0].startswith("#!"):
        insert_at = 1
    joined = "".join(lines)
    if import_line in joined:
        return joined
    lines.insert(insert_at, import_line)
    return "".join(lines)


def remove_snippets_from_code(code: str, objects: Iterable[Any]) -> str:
    if not objects:
        return code
    if all(o.get("start_byte") is not None and o.get("end_byte") is not None for o in objects):
        encoded = code.encode("utf-8")
        spans = sorted(
            [(o["start_byte"], o["end_byte"]) for o in objects],
            key=lambda x: x[0],
            reverse=True,
        )
        for start, end in spans:
            encoded = encoded[:start] + encoded[end:]
        return encoded.decode("utf-8")
    lines = code.splitlines(keepends=True)
    ranges = []
    for o in objects:
        if o.get("lineno") is not None and o.get("end_lineno") is not None:
            ranges.append((o["lineno"] - 1, o["end_lineno"]))
    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]
    return "".join(lines)


def update_file_for_move(path: Path, objects_to_remove: Iterable[Any], utils_module_name: str) -> bool:
    code = safe_read_text(path)
    if code is None:
        return False
    code = normalize_newlines(code)
    names = [obj["name"] for obj in objects_to_remove]
    new_code = remove_snippets_from_code(code, objects_to_remove)
    try:
        ast.parse(new_code)
    except SyntaxError as e:
        logger.error(f"Skipping {path}: code after removal is invalid: {e}")
        return False
    import_line = build_import_line(utils_module_name, names)
    new_code = insert_import_after_shebang(new_code, import_line)
    try:
        ast.parse(new_code)
    except SyntaxError as e:
        logger.error(f"Skipping {path}: code after adding import is invalid: {e}")
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
        help="Copy duplicate objects to utils.py without changing source files",
    )
    args = parser.parse_args()
    if args.move and args.copy:
        logger.error("Use either --move or --copy, not both.")
        sys.exit(1)
    base = Path.cwd()
    files = collect_python_files(base)
    if not files:
        print("No Python files found.")
        return
    file_args = [str(p) for p in files]
    with mp.Pool(processes=max(1, MAX_WORKERS)) as pool:
        nested_results = pool.map(process_file, file_args)
    all_objects = [obj for sub in nested_results for obj in sub]
    if not all_objects:
        print("No extractable objects found.")
        return
    by_hash = defaultdict(list)
    for obj in all_objects:
        by_hash[obj["hash"]].append(obj)
    duplicate_groups = {h: group for h, group in by_hash.items() if len(group) > 1}
    if not duplicate_groups:
        print("No duplicates found.")
        return
    utils_objects = []
    seen_hashes = set()
    for h, group in duplicate_groups.items():
        if h in seen_hashes:
            continue
        utils_objects.append(group[0])
        seen_hashes.add(h)
    print(f"Found {len(utils_objects)} unique duplicated objects.")
    if not (args.move or args.copy):
        for h, group in duplicate_groups.items():
            print(f"Duplicate hash {h[:12]} found in:")
            for item in group:
                print(f"  {item['file']} :: {item['name']} ({item['kind']})")
        return
    utils_path = get_utils_path(base)
    utils_module_name = utils_path.stem
    if not write_utils_file(utils_path, utils_objects):
        logger.error("Failed to create utils file; aborting source changes.")
        sys.exit(1)
    print(f"Wrote deduplicated objects to {utils_path}")
    if args.copy:
        print("Copy mode: source files were not modified and no imports were added.")
        return
    by_file_to_remove = defaultdict(list)
    for h, group in duplicate_groups.items():
        for obj in group:
            obj_path = Path(obj["file"])
            try:
                obj_path.relative_to(base)
                is_under_base = True
            except ValueError:
                is_under_base = False
            if is_under_base and obj_path.suffix == ".py" and obj_path.exists():
                by_file_to_remove[obj["file"]].append(obj)
    for file_str, objects in by_file_to_remove.items():
        path = Path(file_str)
        if path.resolve() == utils_path.resolve():
            continue
        ok = update_file_for_move(path, objects, utils_module_name)
        if ok:
            print(f"Updated {path}")
        else:
            logger.error(f"Failed to update {path}")


if __name__ == "__main__":
    raise SystemExit(main())
