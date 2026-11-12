#!/data/data/com.termux/files/usr/bin/env python
"""Detect and refactor the duplicate Python functions of a project.
Subcommands: ``single`` finds (and with ``-r`` interactively removes) the duplicates of one file, ``scan`` produces exact and fuzzy JSON reports of a tree, ``consolidate`` moves the duplicates into a shared module (``--refactor``, ``--threshold``) and ``prune`` removes the functions matching a reference file.
Usage: dedup_func {single,scan,consolidate,prune} [options] Positional arguments: file Python file to analyze.
path Root path (default: .).
reference Reference file (functions to keep).
inputs ...
Target files/directories (default: current directory).
Subcommands: single Find / remove duplicate functions in ONE file (dedupfunc.py).
scan Recursive exact/fuzzy duplicate scanner (dup_detector.py).
consolidate Consolidate exact duplicates into a shared module (find_dup_func_class_const.py).
prune Remove functions matching a reference file (remove_duplicate_functions.py).
Options: -r, --remove Interactively remove duplicates (keep one per group).
--backup Create a .backup copy before removing (implies removal flow).
-o, --output-dir Directory for JSON reports (default: .).
--refactor Move heavy duplicates into the shared module.
--shared-module Shared module path for --refactor (default: utils.py).
--threshold Refactor only duplicates appearing MORE than N times (default: 5).
--fuzzy Also compute fuzzy duplicate pairs (requires ssdeep + rapidfuzz).
--similarity Fuzzy similarity threshold in %% (default: 50).
-m, --move Actually move duplicates and rewrite imports.
-a, --apply Apply changes (default: dry-run).
Examples: $ dedup_func scan .
--fuzzy $ dedup_func consolidate src/ --refactor Command-line script: run ``python dedup_func.py`` or the ``dedup_func`` command.
The module is importable as well."""

import argparse
import ast
from collections import defaultdict
import concurrent.futures
import contextlib
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
from typing import Any, Iterable, Optional

from dh import MAX_WORKERS
from loguru import logger

with contextlib.suppress(ImportError):
    from loguru import logger


def iter_python_files(root: Path, exclude_paths: Iterable[Path] = ()) -> list[Path]:
    excludes = {Path(p).resolve() for p in exclude_paths if p is not None}
    results: list[Path] = []
    if root.is_file():
        if root.suffix == ".py" and root.resolve() not in excludes:
            results.append(root)
        return results
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            p = Path(dirpath) / fname
            if p.resolve() in excludes:
                continue
            results.append(p)
    return results


def parse_file(path: Path) -> tuple[Optional[str], Optional[ast.AST]]:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        return src, tree
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None, None


def normalize_body(body: str) -> str:
    lines: list[str] = []
    for line in body.split("\n"):
        line = re.sub(r'(?<!["\'])#.*$', "", line)
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def clean_source(lines: list[str]) -> str:
    nonempty = [ln for ln in lines if ln.strip()]
    if not nonempty:
        return ""
    indent = min(len(ln) - len(ln.lstrip()) for ln in nonempty)
    return "\n".join(ln[indent:] if ln.strip() else "" for ln in lines)


def function_line_range(node: ast.AST) -> tuple[int, int]:
    end = getattr(node, "end_lineno", None) or node.lineno
    decs = getattr(node, "decorator_list", None)
    start = decs[0].lineno if decs else node.lineno
    return start, end


class FunctionRecord:
    __slots__ = ("body", "lineno", "name", "node", "original_body")

    def __init__(self, name: str, body: str, lineno: int, node: ast.AST) -> None:
        self.name = name
        self.original_body = body
        self.body = normalize_body(body)
        self.lineno = lineno
        self.node = node


class FunctionCollector(ast.NodeVisitor):
    def __init__(self, source_lines: list[str]) -> None:
        self.source_lines = source_lines
        self.functions: list[FunctionRecord] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._collect(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._collect(node)

    def _collect(self, node: ast.AST) -> None:
        body_first = node.body[0].lineno - 1
        body_last = node.body[-1].end_lineno
        body = "\n".join(self.source_lines[body_first:body_last])
        self.functions.append(FunctionRecord(name=node.name, body=body, lineno=node.lineno, node=node))
        self.generic_visit(node)


def find_duplicates_in_file(path: Path) -> dict[str, list[FunctionRecord]]:
    src, tree = parse_file(path)
    if tree is None:
        print(f"Syntax error in file: {path}")
        return {}
    collector = FunctionCollector(src.splitlines())
    collector.visit(tree)
    groups: dict[str, list[FunctionRecord]] = defaultdict(list)
    for fn in collector.functions:
        groups[fn.body].append(fn)
    return {k: v for k, v in groups.items() if len(v) > 1}


def print_duplicate_groups(groups: dict[str, list[FunctionRecord]]) -> bool:
    if not groups:
        print("No duplicate functions found!")
        return False
    print("\n" + "=" * 40)
    print("DUPLICATE FUNCTIONS FOUND")
    print("-" * 40)
    for idx, (body, fns) in enumerate(groups.items(), 1):
        print(f"\nGroup {idx}:")
        print(f"  Body hash: {hash(body)}")
        print(f"  {len(fns)} functions with identical body:")
        for i, fn in enumerate(fns, 1):
            print(f"    {i}. '{fn.name}' (line {fn.lineno})")
        preview = body[:150] + "..." if len(body) > 150 else body
        print(f"\n  Body preview:\n{preview}")
        print("-" * 40)
    return True


def prompt_keep_choices(groups: dict[str, list[FunctionRecord]]) -> dict[str, int]:
    choices: dict[str, int] = {}
    for body, fns in groups.items():
        print(f"\nGroup with {len(fns)} duplicate functions:")
        for i, fn in enumerate(fns):
            print(f"  [{i}] Keep '{fn.name}' (line {fn.lineno})")
        while True:
            raw = input(f"Which function to keep? [0-{len(fns) - 1}]: ")
            try:
                idx = int(raw)
            except ValueError:
                print("Please enter a valid number")
                continue
            if 0 <= idx < len(fns):
                choices[body] = idx
                break
            print(f"Please enter a number between 0 and {len(fns) - 1}")
    return choices


def remove_duplicates_in_file(
    path: Path,
    groups: dict[str, list[FunctionRecord]],
    keep_choices: dict[str, int],
) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        print(f"Error reading file: {e}")
        return False
    to_delete: set[int] = set()
    for body, fns in groups.items():
        keep = keep_choices.get(body, 0)
        for i, fn in enumerate(fns):
            if i == keep:
                continue
            start, end = function_line_range(fn.node)
            to_delete.update(range(start - 1, end))
    kept = [ln for i, ln in enumerate(lines) if i not in to_delete]
    try:
        path.write_text("\n".join(kept), encoding="utf-8")
        return True
    except OSError as e:
        print(f"Error writing to file: {e}")
        return False


def cmd_single(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"Error: File '{path}' not found")
        return 1
    if path.suffix != ".py":
        print(f"Warning: File '{path}' does not have .py extension")
        if input("Continue anyway? (y/N): ").lower() != "y":
            return 0
    print(f"Analyzing {path}...")
    groups = find_duplicates_in_file(path)
    if not print_duplicate_groups(groups):
        return 0
    if not (args.remove or args.backup):
        print("\nUse -r/--remove to remove duplicates with user confirmation")
        return 0
    if args.backup:
        backup = path.with_suffix(path.suffix + ".backup")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"\nBackup created at: {backup}")
    print("\n" + "=" * 40)
    print("SELECT FUNCTIONS TO KEEP")
    print("-" * 40)
    choices = prompt_keep_choices(groups)
    print("\n" + "=" * 40)
    if input("Proceed with removing duplicate functions? (y/N): ").lower() == "y":
        if remove_duplicates_in_file(path, groups, choices):
            print("✓ Duplicate functions removed successfully!")
            return 0
        print("✗ Failed to remove duplicates")
        return 1
    print("Operation cancelled")
    return 0


def extract_objects_from_file(path: Path) -> list[dict[str, Any]]:
    _src, tree = parse_file(path)
    if tree is None:
        return []
    objects: list[dict[str, Any]] = []
    for node in tree.body:
        obj_type: Optional[str] = None
        obj_name: Optional[str] = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            obj_type, obj_name = "function", node.name
        elif isinstance(node, ast.ClassDef):
            obj_type, obj_name = "class", node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            obj_type, obj_name = "constant", node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            obj_type, obj_name = "constant", node.target.id
        if not (obj_type and obj_name):
            continue
        source = ast.unparse(node)
        content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        objects.append({
            "object_type": obj_type,
            "object_name": obj_name,
            "source_code": source,
            "reference_file": str(path),
            "content_hash": content_hash,
            "start_line": node.lineno,
            "end_line": node.end_lineno,
        })
    return objects


def _scan_worker(path_str: str) -> list[dict[str, Any]]:
    return extract_objects_from_file(Path(path_str))


def find_exact_duplicates(
    objects: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obj in objects:
        by_hash[obj["content_hash"]].append(obj)
    return {h: v for h, v in by_hash.items() if len(v) > 1}


def save_scan_reports(groups: dict[str, list[dict[str, Any]]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    flat: list[dict[str, Any]] = []
    for objs in groups.values():
        count = len(objs)
        for obj in objs:
            rec = {
                k: obj[k]
                for k in (
                    "object_type",
                    "object_name",
                    "source_code",
                    "reference_file",
                    "content_hash",
                    "start_line",
                    "end_line",
                )
            }
            rec["occurrence_count"] = count
            flat.append(rec)
    flat.sort(
        key=lambda o: (
            -o["occurrence_count"],
            o["object_type"],
            o["object_name"],
            o["reference_file"],
        )
    )
    per_type = {
        "function": "function_duplicates.json",
        "class": "class_duplicates.json",
        "constant": "constant_duplicates.json",
    }
    for obj_type, fname in per_type.items():
        subset = [o for o in flat if o["object_type"] == obj_type]
        (output_dir / fname).write_text(json.dumps(subset, indent=4), encoding="utf-8")
        print(f"[+] Saved {len(subset)} {obj_type} duplicate instances to {output_dir / fname}")
    (output_dir / "exact_duplicates.json").write_text(json.dumps(flat, indent=4), encoding="utf-8")
    print(f"[+] Saved {len(flat)} exact duplicate instances to {output_dir / 'exact_duplicates.json'}")


def refactor_to_shared_module(
    groups: dict[str, list[dict[str, Any]]],
    shared_module: Path,
    min_occurrences: int,
) -> None:
    content = shared_module.read_text(encoding="utf-8") if shared_module.exists() else ""
    imports_to_add: dict[str, set[str]] = defaultdict(set)
    for objs in groups.values():
        if len(objs) <= min_occurrences:
            continue
        canonical = objs[0]
        content += f"\n\n# Moved from {canonical['reference_file']}\n{canonical['source_code']}\n"
        for obj in objs:
            imports_to_add[obj["reference_file"]].add(obj["object_name"])
    shared_module.write_text(content, encoding="utf-8")
    print(f"[+] Created/Updated {shared_module}")
    module_name = shared_module.stem
    for ref_file, names in imports_to_add.items():
        target = Path(ref_file)
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        deletions = [
            obj
            for objs in groups.values()
            if len(objs) > min_occurrences
            for obj in objs
            if obj["reference_file"] == ref_file
        ]
        deletions.sort(key=lambda x: x["start_line"], reverse=True)
        for obj in deletions:
            start = obj["start_line"] - 1
            end = obj["end_line"]
            del lines[start:end]
        last_import = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                last_import = i
        insert_at = last_import + 1
        for name in sorted(names):
            lines.insert(insert_at, f"from {module_name} import {name}\n")
            insert_at += 1
        target.write_text("".join(lines), encoding="utf-8")
    print(f"[+] Refactored {len(imports_to_add)} files.")


def find_fuzzy_duplicates(
    objects: list[dict[str, Any]],
    output_path: Path,
    similarity_threshold: float,
) -> None:
    try:
        from rapidfuzz import fuzz
        import ssdeep
    except ImportError:
        logger.error("Please install dependencies for --fuzzy: pip install ssdeep rapidfuzz")
        return
    print("[*] Calculating fuzzy similarities (this may take a while for large codebases)...")
    n = len(objects)
    pairs: list[dict[str, Any]] = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = objects[i], objects[j]
            if a["content_hash"] == b["content_hash"]:
                continue
            a_ssdeep = a.get("ssdeep_hash") or ssdeep.hash(a["source_code"])
            b_ssdeep = b.get("ssdeep_hash") or ssdeep.hash(b["source_code"])
            score = ssdeep.compare(a_ssdeep, b_ssdeep)
            if score <= 0:
                continue
            ratio = fuzz.ratio(a["source_code"], b["source_code"])
            if ratio > similarity_threshold:
                pairs.append({
                    "object_1": {
                        "type": a["object_type"],
                        "name": a["object_name"],
                        "file": a["reference_file"],
                        "source_code": a["source_code"],
                    },
                    "object_2": {
                        "type": b["object_type"],
                        "name": b["object_name"],
                        "file": b["reference_file"],
                        "source_code": b["source_code"],
                    },
                    "similarity_percentage": round(ratio, 2),
                    "ssdeep_score": score,
                })
    output_path.write_text(json.dumps(pairs, indent=4), encoding="utf-8")
    print(f"[+] Saved {len(pairs)} fuzzy duplicate pairs to {output_path}")


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.exists():
        print(f"Error: Path '{root}' not found")
        return 1
    shared = (Path(args.shared_module)).resolve() if args.refactor else None
    excludes = [Path(__file__).resolve()]
    if shared is not None:
        excludes.append(shared)
    files = iter_python_files(root, exclude_paths=excludes)
    print(f"[*] Found {len(files)} files. Starting parallel extraction...")
    workers = MAX_WORKERS or multiprocessing.cpu_count()
    if workers > 1 and len(files) > 1:
        with multiprocessing.Pool(processes=workers) as pool:
            results = pool.map(_scan_worker, [str(f) for f in files])
    else:
        results = [_scan_worker(str(f)) for f in files]
    objects = [obj for batch in results for obj in batch]
    print(f"[*] Extracted {len(objects)} total objects.")
    groups = find_exact_duplicates(objects)
    save_scan_reports(groups, Path(args.output_dir))
    if args.refactor:
        print(f"[*] Refactoring duplicates with >{args.threshold} appearances...")
        refactor_to_shared_module(groups, Path(args.shared_module), args.threshold)
    if args.fuzzy:
        find_fuzzy_duplicates(
            objects,
            Path(args.output_dir) / "fuzzy_duplicates.json",
            args.similarity,
        )
    return 0


def extract_definitions(
    path: Path,
) -> dict[tuple[str, str, str], tuple[str, dict[str, Any]]]:
    _src, tree = parse_file(path)
    if tree is None:
        return {}
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            key = ("function", node.name, ast.unparse(node))
            found[key] = {"name": node.name, "type": "function", "node": node}
        elif isinstance(node, ast.ClassDef):
            key = ("class", node.name, ast.unparse(node))
            found[key] = {"name": node.name, "type": "class", "node": node}
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            key = ("constant", name, ast.unparse(node))
            found[key] = {"name": name, "type": "constant", "node": node}
    return {k: (str(path), rec) for k, rec in found.items()}


def _consolidate_worker(
    path_str: str,
) -> dict[tuple[str, str, str], tuple[str, dict[str, Any]]]:
    return extract_definitions(Path(path_str))


def strip_definition_from_source(source: str, name: str, kind: str, source_code: str, module_name: str) -> str:
    tree = ast.parse(source)
    new_body: list[ast.AST] = []
    removed = False
    for node in tree.body:
        if kind in ("function", "class") and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name and ast.unparse(node) == source_code:
                removed = True
                continue
        elif (
            kind == "constant"
            and isinstance(node, ast.Assign)
            and (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name
                and ast.unparse(node) == source_code
            )
        ):
            removed = True
            continue
        new_body.append(node)
    if not removed:
        return source
    import_node = ast.ImportFrom(module=module_name, names=[ast.alias(name=name, asname=None)], level=0)
    new_body.insert(0, import_node)
    tree.body = new_body
    new_source = ast.unparse(tree)
    ast.parse(new_source)
    return new_source


def cmd_consolidate(args: argparse.Namespace) -> int:
    root = Path(args.path)
    shared_module = root / args.shared_module
    self_path = Path(__file__).resolve()
    files = iter_python_files(root, exclude_paths=[self_path, shared_module])
    if not files:
        print("🔍 No Python files found to scan.")
        return 0
    print(f"🔍 Scanning {len(files)} files concurrently...")
    groups: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    workers = MAX_WORKERS or os.cpu_count() or 1
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_consolidate_worker, str(f)): f for f in files}
        for fut in concurrent.futures.as_completed(futures):
            for key, (path_str, rec) in fut.result().items():
                groups[key].append((path_str, rec))
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dup_groups:
        print("🎉 Success! No repeated functions, classes, or constants were detected.")
        return 0
    print(f"⚠️  Detected {len(dup_groups)} repeated structural definitions:\n")
    sources_to_move: list[str] = []
    per_file: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for (kind, name, source), occurrences in dup_groups.items():
        paths = [occ[0] for occ in occurrences]
        print(f"[{kind.upper()}] '{name}' is repeated in {len(paths)} files:")
        for p in paths:
            print(f"   -> {p}")
        print()
        if args.move:
            sources_to_move.append(source)
            for path_str, _rec in occurrences:
                per_file[path_str].append((name, kind, source))
    if not args.move:
        return 0
    print("🛠️  Processing Consolidation (-m flag active)...")
    existing = shared_module.read_text(encoding="utf-8") if shared_module.exists() else ""
    merged = existing + "\n\n" + "\n\n".join(sources_to_move)
    try:
        ast.parse(merged)
        shared_module.write_text(merged, encoding="utf-8")
        print(f"✅ Extracted duplicate definitions safely written to: {shared_module}")
    except Exception as e:
        print(f"❌ Aborted: Merged definitions inside {shared_module} failed AST parsing: {e}")
        return 1
    module_name = shared_module.stem
    updated = 0
    for path_str, defs in per_file.items():
        try:
            p = Path(path_str)
            src = p.read_text(encoding="utf-8")
            for name, kind, source in defs:
                src = strip_definition_from_source(src, name, kind, source, module_name)
            p.write_text(src, encoding="utf-8")
            print(f"✅ In-place code updated & verified: {path_str}")
            updated += 1
        except Exception as e:
            print(f"❌ Failed to parse or modify file safely {path_str}: {e}. Skipping.")
    print(f"\n📊 Refactor complete. Adjusted and verified {updated} files.")
    return 0


def _hash_function(node: ast.FunctionDef, lines: list[str]) -> str:
    start = node.lineno - 1
    end = node.end_lineno if node.end_lineno is not None else start + 1
    body_lines = lines[start:end]
    body_start = 0
    for i, line in enumerate(body_lines):
        if ":" in line and not line.strip().startswith("@"):
            body_start = i + 1
            break
    sig = ast.dump(node.args)
    if node.returns:
        sig += ast.dump(node.returns)
    body = clean_source(body_lines[body_start:])
    return hashlib.md5(f"{sig}\n{body}".encode()).hexdigest()


def analyze_file_functions(path: Path) -> Optional[dict[str, dict[str, Any]]]:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, OSError):
        return None
    lines = src.splitlines(keepends=True)
    result: dict[str, dict[str, Any]] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef):
            h = _hash_function(node, lines)
            if h:
                result[node.name] = {
                    "name": node.name,
                    "hash": h,
                    "lineno": node.lineno,
                    "end_lineno": node.end_lineno,
                }
    return result


def _prune_worker(path_str: str, ref_hashes: dict[str, str], apply: bool) -> dict[str, Any]:
    path = Path(path_str)
    defs = analyze_file_functions(path)
    if defs is None or not defs:
        return {"file": path, "status": "skipped", "duplicates": []}
    dups: list[dict[str, Any]] = []
    for name, info in defs.items():
        if info["hash"] in ref_hashes:
            dups.append({
                "name": name,
                "lineno": info["lineno"],
                "end_lineno": info["end_lineno"],
                "ref_name": ref_hashes[info["hash"]],
            })
    if not dups:
        return {"file": path, "status": "ok", "duplicates": []}
    if not apply:
        return {"file": path, "status": "found", "duplicates": dups}
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        dups.sort(key=lambda d: d["lineno"], reverse=True)
        removed: list[str] = []
        for dup in dups:
            start = dup["lineno"] - 1
            end = dup["end_lineno"]
            while start > 0 and (lines[start - 1].strip().startswith("@") or lines[start - 1].strip() == ""):
                start -= 1
            del lines[start:end]
            removed.append(dup["name"])
        path.write_text("".join(lines), encoding="utf-8")
        return {"file": path, "status": "updated", "duplicates": removed}
    except Exception as e:
        return {"file": path, "status": "error", "error": str(e), "duplicates": []}


def _collect_prune_targets(inputs: list[str]) -> list[Path]:
    files: set[Path] = set()
    if not inputs:
        files.update(Path().rglob("*.py"))
    else:
        for item in inputs:
            p = Path(item)
            if p.is_file() and p.suffix == ".py":
                files.add(p)
            elif p.is_dir():
                files.update(p.rglob("*.py"))
    return sorted(files)


def cmd_prune(args: argparse.Namespace) -> int:
    ref_path = Path(args.reference)
    if not ref_path.exists():
        logger.error(f"❌ Reference file not found: {ref_path}")
        return 1
    if ref_path.suffix != ".py":
        logger.error("❌ Reference must be a .py file")
        return 1
    print(f"📖 Analyzing reference: {ref_path}")
    ref_funcs = analyze_file_functions(ref_path)
    if ref_funcs is None:
        logger.error("❌ Failed to parse reference file")
        return 1
    if not ref_funcs:
        logger.warning("⚠️  No functions found in reference")
        return 1
    ref_hashes = {info["hash"]: info["name"] for info in ref_funcs.values()}
    print(f"  Found {len(ref_hashes)} functions")
    targets = [t for t in _collect_prune_targets(args.inputs) if t.resolve() != ref_path.resolve()]
    if not targets:
        logger.warning("⚠️  No target files found")
        return 0
    action = "applying" if args.apply else "scanning"
    print(f"\n🔍 {action} {len(targets)} file(s)...")
    print("-" * 40)
    found_count = 0
    removed_count = 0
    workers = MAX_WORKERS or 8
    with multiprocessing.Pool(processes=workers) as pool:
        async_results = [pool.apply_async(_prune_worker, (str(f), ref_hashes, args.apply)) for f in targets]
        for ar in async_results:
            res = ar.get()
            status = res["status"]
            if status == "skipped":
                print(f"⊘  {res['file']}")
            elif status == "ok":
                print(f"✅ {res['file']}")
            elif status == "found":
                found_count += len(res["duplicates"])
                names = ", ".join(d["name"] for d in res["duplicates"])
                logger.warning(f"⚠️  {res['file']}: {names}")
            elif status == "updated":
                removed_count += len(res["duplicates"])
                names = ", ".join(res["duplicates"])
                print(f"✂️  {res['file']}: removed {names}")
            elif status == "error":
                logger.error(f"❌ {res['file']}: {res['error']}")
    print("-" * 40)
    if args.apply:
        print(f"✅ Removed {removed_count} duplicate(s)")
    else:
        print(f"ℹ️  Found {found_count} duplicate function(s)")
        print("   Run with -a/--apply to remove")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dedup_tool.py",
        description="Unified detector/refactorer for duplicate Python objects.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "single",
        help="Find / remove duplicate functions in ONE file (dedupfunc.py).",
    )
    p.add_argument("file", help="Python file to analyze.")
    p.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Interactively remove duplicates (keep one per group).",
    )
    p.add_argument(
        "--backup",
        action="store_true",
        help="Create a .backup copy before removing (implies removal flow).",
    )
    p.set_defaults(func=cmd_single)
    p = sub.add_parser(
        "scan",
        help="Recursive exact/fuzzy duplicate scanner (dup_detector.py).",
    )
    p.add_argument("path", nargs="?", default=".", help="Root path (default: .).")
    p.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="Directory for JSON reports (default: .).",
    )
    p.add_argument(
        "--refactor",
        action="store_true",
        help="Move heavy duplicates into the shared module.",
    )
    p.add_argument(
        "--shared-module",
        default="utils.py",
        help="Shared module path for --refactor (default: utils.py).",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=5,
        help="Refactor only duplicates appearing MORE than N times (default: 5).",
    )
    p.add_argument(
        "--fuzzy",
        action="store_true",
        help="Also compute fuzzy duplicate pairs (requires ssdeep + rapidfuzz).",
    )
    p.add_argument(
        "--similarity",
        type=float,
        default=50.0,
        help="Fuzzy similarity threshold in %% (default: 50).",
    )
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser(
        "consolidate",
        help="Consolidate exact duplicates into a shared module (find_dup_func_class_const.py).",
    )
    p.add_argument("path", nargs="?", default=".", help="Root path (default: .).")
    p.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="Actually move duplicates and rewrite imports.",
    )
    p.add_argument(
        "--shared-module",
        default="dh.py",
        help="Shared module filename (default: dh.py).",
    )
    p.set_defaults(func=cmd_consolidate)
    p = sub.add_parser(
        "prune",
        help="Remove functions matching a reference file (remove_duplicate_functions.py).",
    )
    p.add_argument("reference", help="Reference file (functions to keep).")
    p.add_argument(
        "inputs",
        nargs="*",
        help="Target files/directories (default: current directory).",
    )
    p.add_argument("-a", "--apply", action="store_true", help="Apply changes (default: dry-run).")
    p.set_defaults(func=cmd_prune)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
