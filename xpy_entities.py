#!/data/data/com.termux/files/usr/bin/env python
"""Extract the classes, functions, constants and comments of Python sources with an ast/tree-sitter/libcst backend.
Subcommands: ``extract`` writes the entities per kind to ``output/`` as Python, JSON, text or SQLite files, ``nodes``/``sexpr``/``funcnames``/``constants`` print the tree-sitter views, and ``--scope``, ``--kinds``, ``--layout`` and ``--archives`` control what is included (zip, wheel, tar and zstd archives are scanned too).
Usage: xpy_entities {extract,nodes,sexpr,funcnames,constants} [paths ...] Positional arguments: paths ...
Files or directories (default: cwd) file Subcommands: extract Extract entities from files/dirs/archives nodes Extract tree-sitter node bodies sexpr Print tree-sitter S-expression funcnames List function names in a file constants Extract module-level UPPER_CASE constants Options: --backend Parsing backend (default: ast) --layout Output directory layout (default: by-type) --format Output format (default: py) --kinds Comma-separated entity types to keep --scope 'all' records methods + nested; 'top-level' only module level -o, --output Output directory (default: ./output) --db-path SQLite path for --format db --global-imports Also write output/global_imports.py --archives Also scan .zip/.whl/.tar/.tar.gz/.tar.zst archives --toc Also write TOC.txt summarising extracted entities ...
plus 2 more; run ``xpy_entities --help`` for the full list.
Examples: $ xpy_entities extract src/ --backend libcst Command-line script: run ``python xpy_entities.py`` or the ``xpy_entities`` command.
The module is importable as well."""

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tarfile
import textwrap
from typing import Iterable, Iterator
import zipfile

from dh import SKIP_DIRS


try:
    import libcst as cst

    HAS_LIBCST = True
except ImportError:
    cst = None
    HAS_LIBCST = False
try:
    import tree_sitter
    import tree_sitter_python as tspython

    HAS_TS = True
except ImportError:
    tree_sitter = None
    tspython = None
    HAS_TS = False
try:
    import zstd

    HAS_ZSTD = True
except ImportError:
    zstd = None
    HAS_ZSTD = False
UPPER_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".whl",
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.zst",
    ".tar.xz",
    ".zst",
)
STDLIB_IMPORTS: dict[str, str] = {
    "List": "from typing import List",
    "Dict": "from typing import Dict",
    "Optional": "from typing import Optional",
    "Tuple": "from typing import Tuple",
    "Set": "from typing import Set",
    "Any": "from typing import Any",
    "Union": "from typing import Union",
    "Callable": "from typing import Callable",
    "Type": "from typing import Type",
    "ClassVar": "from typing import ClassVar",
    "Final": "from typing import Final",
    "Literal": "from typing import Literal",
    "Generator": "from typing import Generator",
    "Iterator": "from typing import Iterator",
    "Iterable": "from typing import Iterable",
    "Sequence": "from typing import Sequence",
    "Mapping": "from typing import Mapping",
    "TypeVar": "from typing import TypeVar",
    "overload": "from typing import overload",
    "cast": "from typing import cast",
    "TYPE_CHECKING": "from typing import TYPE_CHECKING",
    "Protocol": "from typing import Protocol",
    "runtime_checkable": "from typing import runtime_checkable",
    "dataclass": "from dataclasses import dataclass",
    "field": "from dataclasses import field",
    "Path": "from pathlib import Path",
    "datetime": "from datetime import datetime",
    "date": "from datetime import date",
    "timedelta": "from datetime import timedelta",
    "Enum": "from enum import Enum",
    "auto": "from enum import auto",
    "IntEnum": "from enum import IntEnum",
    "ABC": "from abc import ABC",
    "abstractmethod": "from abc import abstractmethod",
    "defaultdict": "from collections import defaultdict",
    "OrderedDict": "from collections import OrderedDict",
    "Counter": "from collections import Counter",
    "deque": "from collections import deque",
    "namedtuple": "from collections import namedtuple",
    "contextmanager": "from contextlib import contextmanager",
    "asynccontextmanager": "from contextlib import asynccontextmanager",
    "partial": "from functools import partial",
    "wraps": "from functools import wraps",
    "lru_cache": "from functools import lru_cache",
    "cached_property": "from functools import cached_property",
    "StringIO": "from io import StringIO",
    "BytesIO": "from io import BytesIO",
    "re": "import re",
    "json": "import json",
    "os": "import os",
    "sys": "import sys",
    "math": "import math",
    "time": "import time",
    "copy": "import copy",
    "abc": "import abc",
    "functools": "import functools",
    "itertools": "import itertools",
    "collections": "import collections",
    "logging": "import logging",
    "traceback": "import traceback",
    "threading": "import threading",
    "asyncio": "import asyncio",
    "subprocess": "import subprocess",
    "tempfile": "import tempfile",
    "hashlib": "import hashlib",
    "base64": "import base64",
    "struct": "import struct",
    "io": "import io",
    "socket": "import socket",
    "uuid": "import uuid",
    "warnings": "import warnings",
    "weakref": "import weakref",
    "inspect": "import inspect",
    "textwrap": "import textwrap",
    "string": "import string",
    "random": "import random",
    "heapq": "import heapq",
    "bisect": "import bisect",
    "array": "import array",
    "pickle": "import pickle",
    "shelve": "import shelve",
    "sqlite3": "import sqlite3",
    "csv": "import csv",
    "configparser": "import configparser",
    "argparse": "import argparse",
    "shutil": "import shutil",
    "glob": "import glob",
    "fnmatch": "import fnmatch",
    "stat": "import stat",
    "platform": "import platform",
    "signal": "import signal",
    "atexit": "import atexit",
    "pprint": "import pprint",
    "unittest": "import unittest",
    "dataclasses": "import dataclasses",
}


@dataclass
class Entity:
    name: str
    full_name: str
    type: str
    source: str
    path: str
    imports: list[str] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    docstring: str = ""
    parent: str = ""
    decorators: list[str] = field(default_factory=list)
    value: str = ""


def infer_imports(source: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name, imp in STDLIB_IMPORTS.items():
        if re.search(rf"\b{re.escape(name)}\b", source) and imp not in seen:
            out.append(imp)
            seen.add(imp)
    return out


def safe_name(s: str) -> str:
    s = re.sub(r"[^\w\-.]", "_", s)
    s = s.strip(". ")
    return s or "unnamed"


def unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem, suf = base.stem, base.suffix
    i = 1
    while True:
        p = base.with_name(f"{stem}_{i}{suf}")
        if not p.exists():
            return p
        i += 1


def parse_kinds(spec: str) -> set[str]:
    kinds = {k.strip() for k in spec.split(",") if k.strip()}
    if "function" in kinds:
        kinds.add("method")
    return kinds


class ASTExtractor(ast.NodeVisitor):
    def __init__(self, source: str, path: str, scope: str = "all") -> None:
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.path = path
        self.scope = scope
        self.entities: list[Entity] = []
        self.imports: list[str] = []
        self._class_stack: list[str] = []

    def _slice(self, node: ast.AST) -> str:
        start = getattr(node, "lineno", 1) - 1
        end = getattr(node, "end_lineno", None) or getattr(node, "lineno", 1)
        seg = self.lines[start:end]
        col = getattr(node, "col_offset", 0) or 0
        out: list[str] = []
        for i, line in enumerate(seg):
            if i == 0 and len(line) > col:
                out.append(line[col:])
            else:
                out.append(line)
        return "".join(out)

    def _record_import(self, node: ast.AST) -> None:
        try:
            src = ast.unparse(node)
        except Exception:
            return
        if src not in self.imports:
            self.imports.append(src)

    def visit_Import(self, node: ast.Import) -> None:
        self._record_import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._record_import(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        parent = self._class_stack[-1] if self._class_stack else ""
        src = self._slice(node)
        full = f"{parent}_{node.name}" if parent else node.name
        self.entities.append(
            Entity(
                name=node.name,
                full_name=full,
                type="class",
                source=src,
                path=self.path,
                imports=infer_imports(src),
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=ast.get_docstring(node) or "",
                parent=parent,
                decorators=[ast.unparse(d) for d in node.decorator_list],
            )
        )
        if self.scope == "all":
            self._class_stack.append(node.name)
            for child in node.body:
                self.visit(child)
            self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node) -> None:
        in_class = bool(self._class_stack)
        parent = self._class_stack[-1] if in_class else ""
        full = f"{parent}_{node.name}" if parent else node.name
        src = self._slice(node)
        self.entities.append(
            Entity(
                name=node.name,
                full_name=full,
                type="method" if in_class else "function",
                source=src,
                path=self.path,
                imports=infer_imports(src),
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                docstring=ast.get_docstring(node) or "",
                parent=parent,
                decorators=[ast.unparse(d) for d in node.decorator_list],
            )
        )
        if self.scope == "all" and not in_class:
            for child in node.body:
                self.visit(child)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._class_stack:
            return
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        name = node.targets[0].id
        if not UPPER_RE.match(name):
            return
        try:
            value = ast.unparse(node.value)
        except Exception:
            value = ""
        self.entities.append(
            Entity(
                name=name,
                full_name=name,
                type="constant",
                source=self._slice(node),
                path=self.path,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                value=value,
            )
        )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._class_stack:
            return
        if not isinstance(node.target, ast.Name):
            return
        name = node.target.id
        if not UPPER_RE.match(name):
            return
        value = ""
        if node.value is not None:
            try:
                value = ast.unparse(node.value)
            except Exception:
                value = ""
        self.entities.append(
            Entity(
                name=name,
                full_name=name,
                type="constant",
                source=self._slice(node),
                path=self.path,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                value=value,
            )
        )


def extract_with_ast(source: str, path: str, scope: str) -> tuple[list[Entity], list[str]]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        print(f"[warn] syntax error in {path}: {exc}", file=sys.stderr)
        return [], []
    v = ASTExtractor(source, path, scope=scope)
    v.visit(tree)
    return v.entities, v.imports


_TS_PARSER = None


def _ts_parser():
    global _TS_PARSER
    if not HAS_TS:
        msg = "tree-sitter / tree-sitter-python not installed"
        raise RuntimeError(msg)
    if _TS_PARSER is None:
        lang = tree_sitter.Language(tspython.language())
        _TS_PARSER = tree_sitter.Parser(lang)
    return _TS_PARSER


def extract_with_tree_sitter(source: str, path: str) -> tuple[list[Entity], list[str]]:
    parser = _ts_parser()
    data = source.encode("utf-8")
    tree = parser.parse(data)
    root = tree.root_node
    out: list[Entity] = []

    def text(n) -> str:
        return data[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    def get_name(n) -> str:
        for c in n.children:
            if c.type == "identifier":
                return text(c)
        return ""

    def get_decorators(n) -> list[str]:
        decs: list[str] = []
        prev = n.prev_sibling
        while prev is not None and prev.type == "decorator":
            decs.append(text(prev))
            prev = prev.prev_sibling
        return list(reversed(decs))

    def walk(node) -> None:
        for child in node.children:
            t = child.type
            if t in ("function_definition", "class_definition"):
                name = get_name(child)
                if not name:
                    continue
                etype = "function" if t == "function_definition" else "class"
                body = text(child)
                out.append(
                    Entity(
                        name=name,
                        full_name=name,
                        type=etype,
                        source=body,
                        path=path,
                        imports=infer_imports(body),
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        decorators=get_decorators(child),
                    )
                )
                continue
            if t == "expression_statement":
                for sub in child.children:
                    if sub.type == "assignment":
                        left = sub.child_by_field_name("left")
                        if left is not None and left.type == "identifier":
                            nm = text(left)
                            if UPPER_RE.match(nm):
                                out.append(
                                    Entity(
                                        name=nm,
                                        full_name=nm,
                                        type="constant",
                                        source=text(sub),
                                        path=path,
                                        line_start=sub.start_point[0] + 1,
                                        line_end=sub.end_point[0] + 1,
                                    )
                                )

    walk(root)
    return out, []


def extract_with_libcst(source: str, path: str) -> tuple[list[Entity], list[str]]:
    if not HAS_LIBCST:
        msg = "libcst not installed"
        raise RuntimeError(msg)
    try:
        module = cst.parse_module(source)
    except Exception as exc:
        print(f"[warn] libcst failed on {path}: {exc}", file=sys.stderr)
        return [], []
    entities: list[Entity] = []
    imports: list[str] = []

    class V(cst.CSTVisitor):
        def visit_Import(self, node) -> None:
            imports.append(cst.Module([node]).code.strip())

        def visit_ImportFrom(self, node) -> None:
            imports.append(cst.Module([node]).code.strip())

        def visit_FunctionDef(self, node) -> bool:
            code = cst.Module([node]).code
            entities.append(
                Entity(
                    name=node.name.value,
                    full_name=node.name.value,
                    type="function",
                    source=code,
                    path=path,
                    imports=infer_imports(code),
                )
            )
            return False

        def visit_ClassDef(self, node) -> bool:
            code = cst.Module([node]).code
            entities.append(
                Entity(
                    name=node.name.value,
                    full_name=node.name.value,
                    type="class",
                    source=code,
                    path=path,
                    imports=infer_imports(code),
                )
            )
            return False

        def visit_Assign(self, node) -> None:
            for t in node.targets:
                if isinstance(t.target, cst.Name) and UPPER_RE.match(t.target.value):
                    code = cst.Module([node]).code.strip()
                    entities.append(
                        Entity(
                            name=t.target.value,
                            full_name=t.target.value,
                            type="constant",
                            source=code,
                            path=path,
                        )
                    )

    module.visit(V())
    return entities, imports


def extract_entities(source: str, path: str, backend: str, scope: str) -> tuple[list[Entity], list[str]]:
    if backend == "ast":
        return extract_with_ast(source, path, scope)
    if backend == "libcst":
        return extract_with_libcst(source, path)
    if backend == "tree-sitter":
        return extract_with_tree_sitter(source, path)
    msg = f"unknown backend: {backend}"
    raise ValueError(msg)


def find_python_files(
    roots: Iterable[Path],
    include_archives: bool = False,
    skip_dirs: set[str] | None = None,
) -> tuple[list[Path], list[Path]]:
    skip_dirs = skip_dirs or SKIP_DIRS
    pys: list[Path] = []
    archives: list[Path] = []
    for root in roots:
        if root.is_file():
            if root.suffix == ".py":
                pys.append(root)
            elif include_archives and any(str(root).lower().endswith(s) for s in ARCHIVE_SUFFIXES):
                archives.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix == ".py":
                    pys.append(p)
                elif include_archives and any(str(p).lower().endswith(s) for s in ARCHIVE_SUFFIXES):
                    archives.append(p)
    return pys, archives


def read_zip_members(path: Path) -> Iterator[tuple[str, str]]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.filename.endswith("/") or not info.filename.endswith(".py"):
                    continue
                try:
                    text = zf.read(info).decode("utf-8", errors="replace")
                except Exception:
                    continue
                yield f"{path.name}::{info.filename}", text
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"[warn] bad zip {path}: {exc}", file=sys.stderr)


def read_tar_members(path: Path) -> Iterator[tuple[str, str]]:
    try:
        if path.name.lower().endswith((".tar.zst", ".zst")):
            if not HAS_ZSTD:
                print(f"[warn] zstd missing, skipping {path}", file=sys.stderr)
                return
            raw = zstd.decompress(path.read_bytes())
            tf = tarfile.open(fileobj=io.BytesIO(raw))
        else:
            tf = tarfile.open(path, mode="r:*")
    except Exception as exc:
        print(f"[warn] cannot open tar {path}: {exc}", file=sys.stderr)
        return
    with tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(".py"):
                continue
            try:
                f = tf.extractfile(m)
                if f is None:
                    continue
                text = f.read().decode("utf-8", errors="replace")
            except Exception:
                continue
            yield f"{path.name}::{m.name}", text


def read_archive_members(path: Path) -> Iterator[tuple[str, str]]:
    low = path.name.lower()
    if low.endswith((".whl", ".zip")):
        yield from read_zip_members(path)
    else:
        yield from read_tar_members(path)


def write_py_entity(
    entity: Entity,
    out_dir: Path,
    layout: str,
    include_imports: bool = True,
) -> Path:
    if layout == "by-type":
        target_dir = out_dir / entity.type
    elif layout == "by-folder":
        folder = Path(entity.path).parent.name or "root"
        target_dir = out_dir / safe_name(folder)
    elif layout == "by-file":
        target_dir = out_dir / safe_name(Path(entity.path).stem)
    else:
        target_dir = out_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = unique_path(target_dir / f"{safe_name(entity.full_name)}.py")
    lines: list[str] = []
    if include_imports and entity.imports and not entity.parent:
        lines.extend(entity.imports)
        lines.append("")
    if entity.decorators:
        lines.extend(entity.decorators)
    lines.append(entity.source.rstrip("\n"))
    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def write_json_metadata(entity: Entity, py_path: Path) -> Path:
    meta_path = py_path.with_name(py_path.stem + "_metadata.json")
    meta_path.write_text(
        json.dumps(
            {
                "name": entity.name,
                "full_name": entity.full_name,
                "type": entity.type,
                "path": entity.path,
                "line_start": entity.line_start,
                "line_end": entity.line_end,
                "docstring": entity.docstring,
                "parent": entity.parent,
                "imports": entity.imports,
                "decorators": entity.decorators,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return meta_path


def write_txt_entities(entities: list[Entity], out_dir: Path) -> None:
    by_kind: dict[str, dict[str, list[Entity]]] = {
        "function": defaultdict(list),
        "method": defaultdict(list),
        "class": defaultdict(list),
        "constant": defaultdict(list),
    }
    for e in entities:
        by_kind.setdefault(e.type, defaultdict(list))[e.path].append(e)
    for kind, files in by_kind.items():
        if not files:
            continue
        kdir = out_dir / kind
        kdir.mkdir(parents=True, exist_ok=True)
        unique: set[str] = set()
        for path_str, ents in files.items():
            fname = safe_name(Path(path_str).stem) + ".txt"
            with (kdir / fname).open("w", encoding="utf-8") as f:
                for e in sorted(ents, key=lambda x: x.name):
                    f.write(f"{e.name} (line {e.line_start})\n")
                    unique.add(e.name)
        with (kdir / "unique.txt").open("w", encoding="utf-8") as f:
            for name in sorted(unique):
                f.write(name + "\n")


def write_db(entities: list[Entity], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, full_name TEXT, type TEXT, source TEXT,
            path TEXT, parent TEXT, line_start INTEGER, line_end INTEGER,
            docstring TEXT, value TEXT
        )
        """)
    for e in entities:
        cur.execute(
            "INSERT INTO entities "
            "(name, full_name, type, source, path, parent, line_start, line_end, docstring, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                e.name,
                e.full_name,
                e.type,
                e.source,
                e.path,
                e.parent,
                e.line_start,
                e.line_end,
                e.docstring,
                e.value,
            ),
        )
    conn.commit()
    conn.close()


def write_global_imports(imports: Iterable[str], out_dir: Path) -> Path:
    out = out_dir / "global_imports.py"
    uniq = sorted(set(imports))
    header = "# Global imports collected from all processed files\n\n"
    out.write_text(header + "\n".join(uniq) + "\n", encoding="utf-8")
    return out


def cmd_extract(args: argparse.Namespace) -> int:
    roots = [p.resolve() for p in (args.paths or [Path.cwd()])]
    roots = [p for p in roots if p.exists()]
    if not roots:
        print("No valid input paths.", file=sys.stderr)
        return 1
    pys, archives = find_python_files(roots, include_archives=args.archives)
    print(
        f"Discovered {len(pys)} python file(s), {len(archives)} archive(s).",
        file=sys.stderr,
    )
    kinds = parse_kinds(args.kinds)
    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    all_entities: list[Entity] = []
    all_imports: list[str] = []

    def process(name: str, text: str) -> None:
        ents, imps = extract_entities(text, name, args.backend, args.scope)
        all_entities.extend(e for e in ents if e.type in kinds)
        all_imports.extend(imps)

    for p in pys:
        try:
            process(str(p), p.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            print(f"[warn] cannot read {p}: {exc}", file=sys.stderr)
    for a in archives:
        for name, text in read_archive_members(a):
            process(name, text)
    print(f"Extracted {len(all_entities)} entities.", file=sys.stderr)
    if args.format in ("py", "py+json"):
        for e in all_entities:
            py_path = write_py_entity(e, out_dir, args.layout)
            if args.format == "py+json":
                write_json_metadata(e, py_path)
    if args.format == "txt":
        write_txt_entities(all_entities, out_dir)
    if args.format == "json":
        (out_dir / "entities.json").write_text(
            json.dumps([e.__dict__ for e in all_entities], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if args.format == "db":
        write_db(all_entities, args.db_path)
        print(f"Wrote {len(all_entities)} rows to {args.db_path}", file=sys.stderr)
    if args.global_imports:
        p = write_global_imports(all_imports, out_dir)
        print(f"Global imports -> {p}", file=sys.stderr)
    if args.toc:
        toc = out_dir / "TOC.txt"
        with toc.open("w", encoding="utf-8") as f:
            for e in sorted(all_entities, key=lambda x: (x.type, x.full_name)):
                f.write(f"{e.type:9} {e.full_name}   ({e.path})\n")
        print(f"TOC -> {toc}", file=sys.stderr)
    counts: dict[str, int] = defaultdict(int)
    for e in all_entities:
        counts[e.type] += 1
    print("=" * 40)
    print("EXTRACTION SUMMARY")
    print("-" * 40)
    for kind, n in sorted(counts.items()):
        print(f"  {kind:<12}: {n}")
    print(f"  {'total':<12}: {len(all_entities)}")
    print("-" * 40)
    return 0


NODE_KIND_MAP: dict[str, set[str]] = {
    "class": {"class_definition"},
    "func": {"function_definition"},
    "comments": {"comment"},
    "all": {"class_definition", "function_definition", "comment"},
}


def _extract_nodes(source: bytes, kind: str) -> list[str]:
    parser = _ts_parser()
    tree = parser.parse(source)
    root = tree.root_node

    def text(n) -> str:
        return source[n.start_byte : n.end_byte].decode("utf-8", errors="replace")

    results: list[str] = []
    if kind == "docstrings":
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "class_definition"):
                body = n.child_by_field_name("body")
                if body is not None and body.children:
                    first = body.children[0]
                    if first.type == "expression_statement" and first.children:
                        s = first.children[0]
                        if s.type == "string":
                            results.append(text(s))
                stack.extend(n.children)
                continue
            stack.extend(n.children)
        return results
    targets = NODE_KIND_MAP.get(kind, NODE_KIND_MAP["func"])
    for child in root.children:
        if child.type in targets:
            results.append(text(child))
    return results


def cmd_nodes(args: argparse.Namespace) -> int:
    if not HAS_TS:
        print(
            "tree-sitter required for 'nodes'. Install tree-sitter + tree-sitter-python.",
            file=sys.stderr,
        )
        return 1
    roots = [p.resolve() for p in (args.paths or [Path.cwd()])]
    pys, _ = find_python_files(roots)
    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    by_folder: dict[Path, list[str]] = defaultdict(list)
    for p in pys:
        try:
            src = p.read_bytes()
        except OSError:
            continue
        nodes = _extract_nodes(src, args.kind)
        if nodes:
            by_folder[p.parent].append("\n\n".join(nodes))
    for folder, chunks in by_folder.items():
        rel = folder.name or "root"
        target = out_dir / safe_name(rel) / "imports.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n\n".join(chunks), encoding="utf-8")
    print(
        f"Done. kind={args.kind} processed {len(by_folder)} folder(s) -> {out_dir}/",
        file=sys.stderr,
    )
    return 0


def cmd_sexpr(args: argparse.Namespace) -> int:
    if not HAS_TS:
        print("tree-sitter required for 'sexpr'.", file=sys.stderr)
        return 1
    parser = _ts_parser()
    data = args.file.read_bytes()
    tree = parser.parse(data)

    def render(n) -> str:
        if n.child_count == 0:
            return f'({n.type} "{data[n.start_byte : n.end_byte].decode("utf-8", errors="replace")}")'
        children = " ".join(render(c) for c in n.children)
        return f"({n.type} {children})"

    print(render(tree.root_node))
    return 0


def cmd_funcnames(args: argparse.Namespace) -> int:
    try:
        tree = ast.parse(args.file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Error: file '{args.file}' not found.", file=sys.stderr)
        return 1
    except SyntaxError as exc:
        print(f"Error: syntax error in '{args.file}': {exc}", file=sys.stderr)
        return 1
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if args.skip_main and node.name == "main":
                continue
            names.append(node.name)
    if names:
        label = " (excluding 'main')" if args.skip_main else ""
        print(f"Functions found{label}:")
        for n in names:
            print(f"  - {n}")
    else:
        print("No functions found.")
    return 0


def cmd_constants(args: argparse.Namespace) -> int:
    roots = [p.resolve() for p in (args.paths or [Path.cwd()])]
    pys, _ = find_python_files(roots)
    found: list[Entity] = []
    for p in pys:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ents, _ = extract_with_ast(text, str(p), scope="top-level")
        found.extend(e for e in ents if e.type == "constant")
    if args.format == "list":
        for e in found:
            print(f"{e.name} = {e.value}   # {e.path}:{e.line_start}")
        print(f"\nTotal constants found: {len(found)}")
        return 0
    out_dir = args.output.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "const.py"
    seen: set[str] = set()
    with out.open("w", encoding="utf-8") as f:
        f.write("# Automatically generated constants file\n\n")
        for e in sorted(found, key=lambda x: x.name):
            line = f"{e.name} = {e.value}"
            if line in seen:
                continue
            seen.add(line)
            f.write(f"# From: {e.path}:{e.line_start}\n")
            f.write(line + "\n\n")
    print(f"Wrote {len(seen)} unique constants to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="merged.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Unified Python code-entity extractor.
            Subcommands
            -----------
              extract     Extract functions / classes / constants (files, dirs, archives).
              nodes       Extract tree-sitter node bodies (functions / classes / comments / docstrings).
              sexpr       Print the tree-sitter S-expression for one file.
              funcnames   List function names defined in one file.
              constants   Extract only module-level UPPER_CASE constants.
            """),
    )
    sub = ap.add_subparsers(dest="command", required=True)
    ep = sub.add_parser("extract", help="Extract entities from files/dirs/archives")
    ep.add_argument("paths", nargs="*", type=Path, help="Files or directories (default: cwd)")
    ep.add_argument(
        "--backend",
        choices=["ast", "libcst", "tree-sitter"],
        default="ast",
        help="Parsing backend (default: ast)",
    )
    ep.add_argument(
        "--layout",
        choices=["by-type", "by-folder", "by-file", "flat"],
        default="by-type",
        help="Output directory layout (default: by-type)",
    )
    ep.add_argument(
        "--format",
        choices=["py", "txt", "json", "db", "py+json"],
        default="py",
        help="Output format (default: py)",
    )
    ep.add_argument(
        "--kinds",
        default="function,class,constant,method",
        help="Comma-separated entity types to keep",
    )
    ep.add_argument(
        "--scope",
        choices=["all", "top-level"],
        default="all",
        help="'all' records methods + nested; 'top-level' only module level",
    )
    ep.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="Output directory (default: ./output)",
    )
    ep.add_argument(
        "--db-path",
        type=Path,
        default=Path("ext.db"),
        help="SQLite path for --format db",
    )
    ep.add_argument(
        "--global-imports",
        action="store_true",
        help="Also write output/global_imports.py",
    )
    ep.add_argument(
        "--archives",
        action="store_true",
        help="Also scan .zip/.whl/.tar/.tar.gz/.tar.zst archives",
    )
    ep.add_argument(
        "--toc",
        action="store_true",
        help="Also write TOC.txt summarising extracted entities",
    )
    ep.set_defaults(func=cmd_extract)
    np = sub.add_parser("nodes", help="Extract tree-sitter node bodies")
    np.add_argument("paths", nargs="*", type=Path)
    np.add_argument(
        "--kind",
        choices=["class", "func", "docstrings", "comments", "all"],
        default="func",
    )
    np.add_argument("-o", "--output", type=Path, default=Path("output"))
    np.set_defaults(func=cmd_nodes)
    sp = sub.add_parser("sexpr", help="Print tree-sitter S-expression")
    sp.add_argument("file", type=Path)
    sp.set_defaults(func=cmd_sexpr)
    fp = sub.add_parser("funcnames", help="List function names in a file")
    fp.add_argument("file", type=Path)
    fp.add_argument(
        "--skip-main",
        action="store_true",
        default=True,
        help="Exclude a function literally named 'main' (default: on)",
    )
    fp.set_defaults(func=cmd_funcnames)
    cp = sub.add_parser("constants", help="Extract module-level UPPER_CASE constants")
    cp.add_argument("paths", nargs="*", type=Path)
    cp.add_argument("--format", choices=["py", "list"], default="py")
    cp.add_argument("-o", "--output", type=Path, default=Path("output"))
    cp.set_defaults(func=cmd_constants)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
