#!/data/data/com.termux/files/usr/bin/env python
"""Refactor the camelCase identifiers of a Python tree to snake_case.
Collects the symbols with LibCST, rewrites the functions, classes, variables and parameters (``--targets``), can also rename the ``.py`` files, caches the work, snapshots an undo file and offers ``--undo``, ``--dry-run``, ``--diff`` and ``--exclude``.
Usage: camel2snake [options] [paths ...] Positional arguments: paths ...
Options: --targets --dry-run --diff --no-cache --no-undo --undo Restore from undo file and exit --rename-modules Rename camelCase .py filenames --cache-file --undo-file --exclude Regex to exclude paths Examples: $ camel2snake --diff pkg/ Command-line script: run ``python camel2snake.py`` or the ``camel2snake`` command.
The module is importable as well."""

import argparse
import base64
import difflib
import hashlib
import io
import json
import mmap
from multiprocessing import Pool
import os
from pathlib import Path
import re
import sys
import tokenize
from typing import Iterator, NamedTuple

from atomicio import atomic_write
from dh import MAX_WORKERS, SKIP_DIRS
import libcst as cst
from loguru import logger


CAMEL_RE = re.compile(r"^(?P<pre>_*)(?P<body>[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+)(?P<post>_*)$")
PARTIAL_RE = re.compile(r"[a-z][A-Z]")
SNAKE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
CACHE_FILE = ".camel_cache.json"
UNDO_FILE = ".camel_undo.json"
DUNDER_RE = re.compile(r"^__.*__$")
GETATTR_FUNCS = frozenset({"getattr", "setattr", "delattr", "hasattr"})
ALLOWED_TARGETS = ("function", "class", "variable", "param")


def to_snake(name: str) -> str | None:
    if not name or DUNDER_RE.match(name):
        return None
    m = CAMEL_RE.match(name)
    if m:
        pre, body, post = m.group("pre"), m.group("body"), m.group("post")
    else:
        if not PARTIAL_RE.search(name):
            return None
        pre = ""
        post = ""
        body = name
        while body and body[0] == "_":
            pre += "_"
            body = body[1:]
        while body and body[-1] == "_":
            post = "_" + post
            body = body[:-1]
        if not body:
            return None
    snake = SNAKE_BOUNDARY_RE.sub("_", body).lower()
    if snake == name:
        return None
    return f"{pre}{snake}{post}"


def to_pascal(name: str) -> str | None:
    if not name or name.startswith("__") or DUNDER_RE.match(name):
        return None
    leading = len(name) - len(name.lstrip("_"))
    trailing = len(name) - len(name.rstrip("_"))
    core = name[leading : len(name) - trailing] if trailing else name[leading:]
    if not core or core[0].isupper():
        return None
    s = to_snake(core)
    if s is None:
        return None
    parts = [p for p in s.split("_") if p]
    if not parts:
        return None
    pascal = "".join(p.capitalize() for p in parts)
    if pascal == core:
        return None
    return "_" * leading + pascal + "_" * trailing


def read_bytes(path: Path) -> bytes:
    with path.open("rb") as f:
        if os.fstat(f.fileno()).st_size == 0:
            return b""
        try:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return mm[:]
        except (ValueError, OSError):
            return f.read()


def detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        enc, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return enc
    except (SyntaxError, LookupError):
        return "utf-8"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_json(path: Path, obj: dict) -> None:
    try:
        path.write_text(json.dumps(obj, sort_keys=True), "utf-8")
    except OSError as exc:
        logger.warning("state write failed {}: {}", path, exc)


def load_cache(path: Path) -> dict[str, str]:
    return {k: v for k, v in load_json(path).items() if isinstance(v, str)}


def load_undo(path: Path) -> dict[str, dict]:
    return load_json(path)


def snapshot_undo(store: dict[str, dict], path: Path, data: bytes, encoding: str) -> None:
    key = str(path.resolve())
    if key in store:
        return
    store[key] = {"encoding": encoding, "data": base64.b64encode(data).decode("ascii")}


def restore_undo(store: dict[str, dict]) -> int:
    count = 0
    for key, entry in store.items():
        p = Path(key)
        try:
            data = base64.b64decode(entry["data"])
        except (KeyError, ValueError) as exc:
            logger.error("undo entry invalid {}: {}", key, exc)
            raise
        atomic_write(p, data)
        count += 1
    return count


def iter_py_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix == ".py" and not root.is_symlink():
            yield root
        return
    seen: set[tuple[int, int]] = set()
    stack: list[Path] = [root]
    while stack:
        cur = stack.pop()
        try:
            st = cur.stat()
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(cur.iterdir())
        except OSError as exc:
            logger.warning("skip {}: {}", cur, exc)
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRS:
                    continue
                stack.append(entry)
            elif entry.suffix == ".py":
                yield entry


def unified_diff(path: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


class SymbolCollector(cst.CSTVisitor):
    def __init__(self, targets: set[str]) -> None:
        self.targets = targets
        self.renames: dict[str, str] = {}

    def _add(self, name: str, new: str | None) -> None:
        if new and new != name and name not in self.renames:
            self.renames[name] = new

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if "function" in self.targets:
            self._add(node.name.value, to_snake(node.name.value))
        return True

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if "class" in self.targets:
            self._add(node.name.value, to_pascal(node.name.value))
        return True

    def visit_Param(self, node: cst.Param) -> bool:
        if "param" in self.targets and node.name is not None:
            self._add(node.name.value, to_snake(node.name.value))
        return True

    def visit_AssignTarget(self, node: cst.AssignTarget) -> bool:
        if "variable" in self.targets and isinstance(node.target, cst.Name):
            self._add(node.target.value, to_snake(node.target.value))
        return True

    def visit_AnnAssign(self, node: cst.AnnAssign) -> bool:
        if "variable" in self.targets and isinstance(node.target, cst.Name):
            self._add(node.target.value, to_snake(node.target.value))
        return True

    def visit_AugAssign(self, node: cst.AugAssign) -> bool:
        if "variable" in self.targets and isinstance(node.target, cst.Name):
            self._add(node.target.value, to_snake(node.target.value))
        return True

    def visit_For(self, node: cst.For) -> bool:
        if "variable" in self.targets and isinstance(node.target, cst.Name):
            self._add(node.target.value, to_snake(node.target.value))
        return True

    def visit_WithItem(self, node: cst.WithItem) -> bool:
        if "variable" in self.targets and isinstance(node.asname, cst.AsName):
            if isinstance(node.asname.name, cst.Name):
                self._add(node.asname.name.value, to_snake(node.asname.name.value))
        return True


class RenameTransformer(cst.CSTTransformer):
    def __init__(self, renames: dict[str, str], module_renames: dict[str, str]) -> None:
        self.renames = renames
        self.module_renames = module_renames
        self.count = 0
        self._str_ctx = 0
        self._in_import = 0

    def _in_all_assign(self, node: cst.Assign) -> bool:
        return any(isinstance(tgt.target, cst.Name) and tgt.target.value == "__all__" for tgt in node.targets)

    def visit_Assign(self, node: cst.Assign) -> bool:
        if self._in_all_assign(node):
            self._str_ctx += 1
        return True

    def leave_Assign(self, orig: cst.Assign, updated: cst.Assign) -> cst.Assign:
        if self._in_all_assign(orig):
            self._str_ctx -= 1
        return updated

    def visit_AugAssign(self, node: cst.AugAssign) -> bool:
        if isinstance(node.target, cst.Name) and node.target.value == "__all__":
            self._str_ctx += 1
        return True

    def leave_AugAssign(self, orig: cst.AugAssign, updated: cst.AugAssign) -> cst.AugAssign:
        if isinstance(orig.target, cst.Name) and orig.target.value == "__all__":
            self._str_ctx -= 1
        return updated

    def visit_Call(self, node: cst.Call) -> bool:
        if isinstance(node.func, cst.Name) and node.func.value in GETATTR_FUNCS:
            self._str_ctx += 1
        return True

    def leave_Call(self, orig: cst.Call, updated: cst.Call) -> cst.Call:
        if isinstance(orig.func, cst.Name) and orig.func.value in GETATTR_FUNCS:
            self._str_ctx -= 1
        return updated

    def visit_Import(self, node: cst.Import) -> bool:
        self._in_import += 1
        return True

    def leave_Import(self, orig: cst.Import, updated: cst.Import) -> cst.Import:
        self._in_import -= 1
        new_names = []
        changed = False
        for alias in updated.names:
            new_name = self._rename_module_ref(alias.name)
            if new_name is not alias.name:
                new_names.append(alias.with_changes(name=new_name))
                changed = True
            else:
                new_names.append(alias)
        if changed:
            return updated.with_changes(names=new_names)
        return updated

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        self._in_import += 1
        return True

    def leave_ImportFrom(self, orig: cst.ImportFrom, updated: cst.ImportFrom) -> cst.ImportFrom:
        self._in_import -= 1
        if updated.module is not None:
            new_module = self._rename_module_ref(updated.module)
            if new_module is not updated.module:
                updated = updated.with_changes(module=new_module)
        if isinstance(updated.names, cst.ImportStar):
            return updated
        new_names = []
        changed = False
        for alias in updated.names:
            if isinstance(alias.name, cst.Name) and alias.name.value in self.renames:
                new_alias = alias.with_changes(name=alias.name.with_changes(value=self.renames[alias.name.value]))
                new_names.append(new_alias)
                changed = True
                self.count += 1
            else:
                new_names.append(alias)
        if changed:
            return updated.with_changes(names=new_names)
        return updated

    def _rename_module_ref(self, node: cst.BaseExpression) -> cst.BaseExpression:
        if isinstance(node, cst.Name):
            if node.value in self.module_renames:
                self.count += 1
                return node.with_changes(value=self.module_renames[node.value])
            return node
        if isinstance(node, cst.Attribute):
            new_value = self._rename_module_ref(node.value)
            new_attr = node.attr
            if node.attr.value in self.module_renames:
                new_attr = node.attr.with_changes(value=self.module_renames[node.attr.value])
            if new_value is not node.value or new_attr is not node.attr:
                self.count += 1
                return node.with_changes(value=new_value, attr=new_attr)
            return node
        return node

    def leave_Name(self, orig: cst.Name, updated: cst.Name) -> cst.Name:
        if self._in_import > 0:
            return updated
        if updated.value in self.renames:
            self.count += 1
            return updated.with_changes(value=self.renames[updated.value])
        return updated

    def leave_SimpleString(self, orig: cst.SimpleString, updated: cst.SimpleString) -> cst.SimpleString:
        if self._str_ctx <= 0:
            return updated
        try:
            val = updated.evaluated_value
        except Exception:
            return updated
        if not isinstance(val, str) or val not in self.renames:
            return updated
        new_val = self.renames[val]
        q = updated.quote
        if updated.prefix or "\\" in new_val or "\n" in new_val or q in new_val:
            return updated
        self.count += 1
        return updated.with_changes(value=f"{q}{new_val}{q}")


class FileResult(NamedTuple):
    path: Path
    count: int
    new_hash: str | None
    undo_entry: tuple[Path, bytes, str] | None
    error: str | None
    diff: str


def process_file(
    payload: tuple[Path, dict[str, str], dict[str, str], str | None, bool, bool, bool],
) -> FileResult:
    path, renames, module_renames, cached_hash, apply, dry_run, want_diff = payload
    try:
        data = read_bytes(path)
    except OSError as exc:
        return FileResult(path, 0, None, None, f"read failed: {exc}", "")
    h = hashlib.sha256(data).hexdigest()
    if not dry_run and cached_hash is not None and cached_hash == h:
        return FileResult(path, 0, h, None, None, "")
    if not renames and not module_renames:
        return FileResult(path, 0, h, None, None, "")
    encoding = detect_encoding(data)
    try:
        source = data.decode(encoding)
    except UnicodeDecodeError as exc:
        return FileResult(path, 0, None, None, f"decode failed: {exc}", "")
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return FileResult(path, 0, None, None, f"parse error: {exc}", "")
    transformer = RenameTransformer(renames, module_renames)
    try:
        new_module = module.visit(transformer)
    except Exception as exc:
        return FileResult(path, 0, None, None, f"transform failed: {exc}", "")
    new_source = new_module.code
    if new_source == source:
        return FileResult(path, 0, h, None, None, "")
    count = transformer.count
    diff = unified_diff(path, source, new_source) if want_diff else ""
    if dry_run:
        return FileResult(path, count, None, None, None, diff)
    new_data = new_source.encode(encoding)
    new_hash = hashlib.sha256(new_data).hexdigest()
    if apply:
        try:
            atomic_write(path, new_data)
        except OSError as exc:
            return FileResult(path, 0, None, None, f"write failed: {exc}", "")
    return FileResult(path, count, new_hash, (path, data, encoding), None, "")


def collect_symbols(path: Path, targets: set[str]) -> dict[str, str]:
    try:
        data = read_bytes(path)
    except OSError:
        return {}
    encoding = detect_encoding(data)
    try:
        source = data.decode(encoding)
    except UnicodeDecodeError:
        return {}
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        return {}
    c = SymbolCollector(targets)
    try:
        module.visit(c)
    except Exception:
        return {}
    return c.renames


def collect_worker(payload: tuple[Path, set[str]]) -> tuple[Path, dict[str, str]]:
    path, targets = payload
    return path, collect_symbols(path, targets)


def detect_module_renames(files: list[Path]) -> dict[str, str]:
    renames: dict[str, str] = {}
    for f in files:
        stem = f.stem
        if stem == "__init__":
            continue
        new = to_snake(stem)
        if not new:
            continue
        if stem in renames and renames[stem] != new:
            logger.warning("module rename conflict for {}: {} vs {}", stem, renames[stem], new)
            continue
        renames[stem] = new
    return renames


def apply_module_file_renames(
    files: list[Path], module_renames: dict[str, str], apply: bool
) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    for f in files:
        stem = f.stem
        if stem not in module_renames:
            continue
        new_path = f.with_name(module_renames[stem] + f.suffix)
        if new_path == f:
            continue
        if new_path.exists():
            logger.warning("module rename target exists, skip: {} -> {}", f, new_path)
            continue
        moves.append((f, new_path))
    if apply:
        for old, new in moves:
            try:
                old.rename(new)
            except OSError as exc:
                logger.error("module rename failed {} -> {}: {}", old, new, exc)
                raise
    return moves


def gather(targets: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for t in targets:
        p = t.resolve()
        if not p.exists():
            logger.error("path does not exist: {}", t)
            raise SystemExit(1)
        for f in iter_py_files(p):
            rp = f.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(f)
    return out


def parse_targets(value: str) -> set[str]:
    parts = {p.strip() for p in value.split(",") if p.strip()}
    invalid = parts - set(ALLOWED_TARGETS)
    if invalid:
        msg = f"invalid targets: {sorted(invalid)}"
        raise argparse.ArgumentTypeError(msg)
    return parts


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level}</level>: {message}")
    parser = argparse.ArgumentParser(description="Refactor camelCase identifiers to snake_case.")
    parser.add_argument("paths", nargs="*", type=Path, default=[Path.cwd()])
    parser.add_argument("--targets", type=parse_targets, default=set(ALLOWED_TARGETS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--no-undo", action="store_true")
    parser.add_argument("--undo", action="store_true", help="restore from undo file and exit")
    parser.add_argument("--rename-modules", action="store_true", help="rename camelCase .py filenames")
    parser.add_argument("--cache-file", type=Path, default=None)
    parser.add_argument("--undo-file", type=Path, default=None)
    parser.add_argument("--exclude", action="append", default=[], help="regex to exclude paths")
    args = parser.parse_args()
    root = args.paths[0].resolve() if args.paths else Path.cwd().resolve()
    cache_path = args.cache_file or (root if root.is_dir() else root.parent) / CACHE_FILE
    undo_path = args.undo_file or (root if root.is_dir() else root.parent) / UNDO_FILE
    if args.undo:
        store = load_undo(undo_path)
        if not store:
            logger.info("no undo entries at {}", undo_path)
            return 0
        n = restore_undo(store)
        logger.info("restored {} file(s) from {}", n, undo_path)
        undo_path.unlink(missing_ok=True)
        return 0
    files = gather(args.paths)
    if args.exclude:
        import re as _re

        pats = [_re.compile(p) for p in args.exclude]
        files = [f for f in files if not any(p.search(str(f)) for p in pats)]
    if not files:
        logger.info("no python files found")
        return 0
    cache = {} if args.no_cache else load_cache(cache_path)
    undo_store = {} if args.no_undo else load_undo(undo_path)
    logger.info("collecting symbols from {} file(s)", len(files))
    combined: dict[str, str] = {}
    conflicts: set[str] = set()
    payloads = [(f, args.targets) for f in files]
    if len(files) >= 64 and MAX_WORKERS > 1:
        with Pool(processes=MAX_WORKERS) as pool:
            for path, mapping in pool.imap_unordered(collect_worker, payloads, chunksize=16):
                for k, v in mapping.items():
                    if k in combined and combined[k] != v and k not in conflicts:
                        logger.warning("rename conflict for {}: {} vs {}", k, combined[k], v)
                        conflicts.add(k)
                        continue
                    combined.setdefault(k, v)
    else:
        for path, mapping in (collect_worker(p) for p in payloads):
            for k, v in mapping.items():
                if k in combined and combined[k] != v and k not in conflicts:
                    logger.warning("rename conflict for {}: {} vs {}", k, combined[k], v)
                    conflicts.add(k)
                    continue
                combined.setdefault(k, v)
    for k in conflicts:
        combined.pop(k, None)
    module_renames = detect_module_renames(files) if args.rename_modules else {}
    if not combined and not module_renames:
        logger.info("nothing to rename")
        return 0
    logger.info(
        "applying {} symbol rename(s), {} module rename(s)",
        len(combined),
        len(module_renames),
    )
    apply = not args.dry_run
    payloads = [(f, combined, module_renames, cache.get(str(f)), apply, args.dry_run, args.diff) for f in files]
    total_files = 0
    total_edits = 0
    errors = 0
    if len(files) >= 64 and MAX_WORKERS > 1:
        with Pool(processes=MAX_WORKERS) as pool:
            results = pool.imap_unordered(process_file, payloads, chunksize=8)
            for res in results:
                errors += _handle_result(res, apply, args.no_undo, cache, undo_store, args.diff)
                if res.count:
                    total_files += 1
                    total_edits += res.count
    else:
        for res in map(process_file, payloads):
            errors += _handle_result(res, apply, args.no_undo, cache, undo_store, args.diff)
            if res.count:
                total_files += 1
                total_edits += res.count
    if module_renames and apply:
        moves = apply_module_file_renames(files, module_renames, apply)
        for old, new in moves:
            logger.info("module renamed {} -> {}", old, new)
    if not args.dry_run and not args.no_cache:
        save_json(cache_path, cache)
    if not args.dry_run and not args.no_undo and undo_store:
        save_json(undo_path, undo_store)
    logger.info(
        "done: {} file(s) modified, {} rename(s), {} error(s)",
        total_files,
        total_edits,
        errors,
    )
    return 1 if errors else 0


def _handle_result(
    res: FileResult,
    apply: bool,
    no_undo: bool,
    cache: dict[str, str],
    undo_store: dict[str, dict],
    diff_requested: bool,
) -> int:
    if res.error:
        logger.error("{}: {}", res.path, res.error)
        return 1
    if res.diff and diff_requested:
        sys.stdout.write(res.diff)
    if res.count:
        logger.info("{}: {} rename(s)", res.path, res.count)
    if apply and res.new_hash is not None:
        cache[str(res.path)] = res.new_hash
    if apply and not no_undo and res.undo_entry is not None:
        p, data, enc = res.undo_entry
        snapshot_undo(undo_store, p, data, enc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
