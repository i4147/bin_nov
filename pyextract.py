#!/data/data/com.termux/files/usr/bin/env python
"""Extract the functions, classes and constants of Python sources and archives.
Parses with ast, LibCST or tree-sitter (``--parser``), writes the entities with the per-entity, per-type, per-folder, JSON or SQLite layout (``--layout``), skips the tests with ``--skip-tests`` and also reads the ``.py`` members of zip, wheel, tar and zstd archives.
Usage: pyextract {extract,nodes,sexpr,funcnames} [options] [paths ...] Positional arguments: paths ...
Files/dirs to scan (default: current directory).
file Subcommands: extract Extract code entities.
nodes Dump top-level tree-sitter nodes per folder.
sexpr Print tree-sitter S-expression for a file.
funcnames List function names in a Python file.
Options: -d, --dir Root directory (alternative to positional `paths`).
-o, --output Output directory (default: ./output).
--parser Parsing backend (default: ast).
--layout Output layout (default: per-entity).
--sqlite-path SQLite file (used by --layout sqlite).
--include-nested Include methods and nested defs/classes.
--constants-only Extract only UPPERCASE constants.
--skip-tests Skip test/tests/examples directories.
--archives Also walk .zip/.whl/.tar[.gz]/.zst archives.
--imports-file Write a global_imports.py aggregating imports.
...
plus 8 more; run ``pyextract --help`` for the full list.
Examples: $ pyextract extract -o out/ src/ --layout by-type Command-line script: run ``python pyextract.py`` or the ``pyextract`` command.
The module is importable as well."""

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass, field
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
from typing import Iterable, Iterator, Optional
import zipfile

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


try:
    import tree_sitter
    import tree_sitter_python

    HAS_TS = True
except Exception:
    HAS_TS = False
try:
    import libcst as cst

    HAS_LIBCST = True
except Exception:
    HAS_LIBCST = False
try:
    import zstandard as zstd

    HAS_ZSTD = True
except Exception:
    HAS_ZSTD = False
log = logger
ARCHIVE_EXTS = (
    ".whl",
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.zst",
    ".tar.xz",
    ".zst",
)
UPPER_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
DEFAULT_SKIP_TESTS = {"test", "tests", "examples"}


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


def _content_looks_python(text: str) -> bool:
    head = text[:512]
    if head.startswith("#!") and "python" in head.split("\n", 1)[0].lower():
        return True
    return any(kw in head for kw in ("import ", "def ", "class ", "if __name__"))


def _path_looks_python(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(512)
    except OSError:
        return False
    first = head.splitlines()[0] if head else b""
    if first.startswith(b"#!") and b"python" in first.lower():
        return True
    return any(kw in head for kw in (b"import ", b"def ", b"class ", b"if __name__"))


def is_archive(path: Path) -> bool:
    n = path.name.lower()
    return any(n.endswith(e) for e in ARCHIVE_EXTS)


def discover(
    root: Path, skip_dirs: set[str], skip_tests: bool, include_archives: bool
) -> tuple[list[Path], list[Path]]:
    skip = set(skip_dirs)
    if skip_tests:
        skip |= DEFAULT_SKIP_TESTS
    py_files: list[Path] = []
    archives: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not Path(dirpath, d).is_symlink()]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.is_symlink():
                continue
            if p.suffix == ".py":
                py_files.append(p)
            elif not p.suffix and _path_looks_python(p):
                py_files.append(p)
            elif include_archives and is_archive(p):
                archives.append(p)
    return sorted(py_files), sorted(archives)


def iter_archive_python(archive: Path) -> Iterator[tuple[str, str]]:
    n = archive.name.lower()
    if n.endswith((".whl", ".zip")):
        try:
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if info.filename.endswith("/"):
                        continue
                    if Path(info.filename).suffix != ".py":
                        continue
                    try:
                        yield info.filename, zf.read(info).decode("utf-8", "replace")
                    except Exception:
                        continue
        except (zipfile.BadZipFile, OSError) as e:
            log.error("bad zip {}: {}", archive, e)
        return
    try:
        if n.endswith(".zst"):
            if not HAS_ZSTD:
                log.warning("zstandard not installed, skipping {}", archive)
                return
            raw = zstd.ZstdDecompressor().decompress(archive.read_bytes())
            tf = tarfile.open(fileobj=io.BytesIO(raw))
        else:
            tf = tarfile.open(archive, "r:*")
    except (tarfile.TarError, OSError) as e:
        log.error("cannot open {}: {}", archive, e)
        return
    with tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            if Path(member.name).suffix != ".py":
                continue
            try:
                f = tf.extractfile(member)
                if f is None:
                    continue
                yield member.name, f.read().decode("utf-8", "replace")
            except Exception:
                continue


def _slice_ast_node(node: ast.AST, source_lines: list[str]) -> str:
    start = node.lineno - 1
    decs = getattr(node, "decorator_list", None) or []
    if decs:
        start = min(start, *(d.lineno - 1 for d in decs))
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    end = min(end, len(source_lines))
    if start >= end:
        return ""
    lines = source_lines[start:end]
    first_col = node.col_offset
    if decs:
        first_col = min(first_col, *(d.col_offset for d in decs))
    if first_col and len(lines[0]) > first_col:
        lines[0] = lines[0][first_col:]
    return "".join(lines)


class _ASTExtractor(ast.NodeVisitor):
    def __init__(self, source: str, path: str, include_nested: bool, constants_only: bool) -> None:
        self.source_lines = source.splitlines(keepends=True)
        self.path = path
        self.include_nested = include_nested
        self.constants_only = constants_only
        self.entities: list[Entity] = []
        self.imports: list[str] = []
        self._class_stack: list[str] = []
        self._scope_stack: list[str] = []

    def _slice(self, node: ast.AST) -> str:
        return _slice_ast_node(node, self.source_lines)

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
        parent = "_".join(self._class_stack)
        full = "_".join(self._class_stack + [node.name])
        top = not self._class_stack and not self._scope_stack
        if not self.constants_only and (top or self.include_nested):
            self.entities.append(
                Entity(
                    name=node.name,
                    full_name=full,
                    type="class",
                    source=self._slice(node),
                    path=self.path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    parent=parent,
                )
            )
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node: ast.AST) -> None:
        parent = "_".join(self._class_stack)
        full = "_".join(self._class_stack + self._scope_stack + [node.name])
        in_class = bool(self._class_stack)
        top = not in_class and not self._scope_stack
        if not self.constants_only and (top or self.include_nested):
            self.entities.append(
                Entity(
                    name=node.name,
                    full_name=full,
                    type="function",
                    source=self._slice(node),
                    path=self.path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    parent=parent,
                )
            )
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        top = not self._class_stack and not self._scope_stack
        if top or self.include_nested:
            for t in node.targets:
                if isinstance(t, ast.Name) and UPPER_RE.match(t.id):
                    self.entities.append(
                        Entity(
                            name=t.id,
                            full_name=t.id,
                            type="constant",
                            source=self._slice(node),
                            path=self.path,
                            line_start=node.lineno,
                            line_end=node.end_lineno or node.lineno,
                        )
                    )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        top = not self._class_stack and not self._scope_stack
        if (
            (top or self.include_nested)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and UPPER_RE.match(node.target.id)
        ):
            self.entities.append(
                Entity(
                    name=node.target.id,
                    full_name=node.target.id,
                    type="constant",
                    source=self._slice(node),
                    path=self.path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                )
            )
        self.generic_visit(node)


def extract_ast(
    source: str, path: str, include_nested: bool = False, constants_only: bool = False
) -> tuple[list[Entity], list[str]]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        log.warning("syntax error in {}: {}", path, e)
        return [], []
    ex = _ASTExtractor(source, path, include_nested, constants_only)
    ex.visit(tree)
    for e in ex.entities:
        if not e.parent:
            e.imports = list(ex.imports)
    return ex.entities, ex.imports


def _extract_libcst_impl(
    source: str, path: str, include_nested: bool, constants_only: bool
) -> tuple[list[Entity], list[str]]:
    try:
        module = cst.parse_module(source)
    except Exception as e:
        log.warning("libcst parse error in {}: {}", path, e)
        return [], []
    entities: list[Entity] = []
    imports: list[str] = []

    def _code(node) -> str:
        return cst.Module(body=[node]).code.strip()

    class V(cst.CSTVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []
            self.scope_stack: list[str] = []

        def visit_Import(self, node) -> None:
            if not self.class_stack and not self.scope_stack:
                c = _code(node)
                if c not in imports:
                    imports.append(c)

        def visit_ImportFrom(self, node) -> None:
            if not self.class_stack and not self.scope_stack:
                c = _code(node)
                if c not in imports:
                    imports.append(c)

        def visit_ClassDef(self, node) -> None:
            parent = "_".join(self.class_stack)
            full = "_".join(self.class_stack + [node.name.value])
            top = not self.class_stack and not self.scope_stack
            if not constants_only and (top or include_nested):
                entities.append(
                    Entity(
                        name=node.name.value,
                        full_name=full,
                        type="class",
                        source=_code(node),
                        path=path,
                        parent=parent,
                    )
                )
            self.class_stack.append(node.name.value)

        def leave_ClassDef(self, node) -> None:
            self.class_stack.pop()

        def visit_FunctionDef(self, node) -> None:
            parent = "_".join(self.class_stack)
            full = "_".join(self.class_stack + self.scope_stack + [node.name.value])
            in_class = bool(self.class_stack)
            top = not in_class and not self.scope_stack
            if not constants_only and (top or include_nested):
                entities.append(
                    Entity(
                        name=node.name.value,
                        full_name=full,
                        type="function",
                        source=_code(node),
                        path=path,
                        parent=parent,
                    )
                )
            self.scope_stack.append(node.name.value)

        def leave_FunctionDef(self, node) -> None:
            self.scope_stack.pop()

        def visit_Assign(self, node) -> None:
            top = not self.class_stack and not self.scope_stack
            if not (top or include_nested):
                return
            for target in node.targets:
                t = target.target
                if isinstance(t, cst.Name) and UPPER_RE.match(t.value):
                    entities.append(
                        Entity(
                            name=t.value,
                            full_name=t.value,
                            type="constant",
                            source=_code(node),
                            path=path,
                        )
                    )

    module.visit(V())
    for e in entities:
        if not e.parent:
            e.imports = list(imports)
    return entities, imports


def extract_libcst(
    source: str, path: str, include_nested: bool = False, constants_only: bool = False
) -> tuple[list[Entity], list[str]]:
    if not HAS_LIBCST:
        log.error("libcst is not installed — cannot use --parser libcst")
        return [], []
    return _extract_libcst_impl(source, path, include_nested, constants_only)


_TS_PARSER = None


def _get_ts_parser():
    global _TS_PARSER
    if _TS_PARSER is not None:
        return _TS_PARSER
    lang = tree_sitter.Language(tree_sitter_python.language())
    try:
        _TS_PARSER = tree_sitter.Parser(lang)
    except TypeError:
        p = tree_sitter.Parser()
        p.set_language(lang)
        _TS_PARSER = p
    return _TS_PARSER


def extract_treesitter(
    source: str, path: str, include_nested: bool = False, constants_only: bool = False
) -> tuple[list[Entity], list[str]]:
    if not HAS_TS:
        log.error("tree-sitter is not installed — cannot use --parser treesitter")
        return [], []
    parser = _get_ts_parser()
    data = source.encode("utf-8", "replace")
    tree = parser.parse(data)
    entities: list[Entity] = []
    imports: list[str] = []

    def text(n) -> str:
        return data[n.start_byte : n.end_byte].decode("utf-8", "replace")

    def name_of(n) -> Optional[str]:
        f = n.child_by_field_name("name")
        return text(f) if f else None

    def walk(node, class_stack: list[str], func_stack: list[str]) -> None:
        for child in node.children:
            t = child.type
            src_node = child
            def_node = child
            if t == "decorated_definition":
                inner = next(
                    (c for c in child.children if c.type in ("function_definition", "class_definition")),
                    None,
                )
                if inner is None:
                    walk(child, class_stack, func_stack)
                    continue
                src_node, def_node = child, inner
                t = inner.type
            if t == "class_definition":
                nm = name_of(def_node) or "<anon>"
                parent = "_".join(class_stack)
                top = not class_stack and not func_stack
                if not constants_only and (top or include_nested):
                    entities.append(
                        Entity(
                            name=nm,
                            full_name="_".join(class_stack + [nm]),
                            type="class",
                            source=text(src_node),
                            path=path,
                            parent=parent,
                        )
                    )
                walk(def_node, class_stack + [nm], func_stack)
                continue
            if t == "function_definition":
                nm = name_of(def_node) or "<anon>"
                parent = "_".join(class_stack)
                in_class = bool(class_stack)
                top = not in_class and not func_stack
                if not constants_only and (top or include_nested):
                    entities.append(
                        Entity(
                            name=nm,
                            full_name="_".join(class_stack + func_stack + [nm]),
                            type="function",
                            source=text(src_node),
                            path=path,
                            parent=parent,
                        )
                    )
                walk(def_node, class_stack, func_stack + [nm])
                continue
            if t in ("import_statement", "import_from_statement"):
                if not class_stack and not func_stack:
                    imports.append(text(child))
                continue
            if t == "expression_statement" and ((not class_stack and not func_stack) or include_nested):
                for sub in child.children:
                    if sub.type == "assignment":
                        left = sub.child_by_field_name("left")
                        if left is not None and left.type == "identifier":
                            nm = text(left)
                            if UPPER_RE.match(nm):
                                entities.append(
                                    Entity(
                                        name=nm,
                                        full_name=nm,
                                        type="constant",
                                        source=text(child),
                                        path=path,
                                    )
                                )
                continue
            walk(child, class_stack, func_stack)

    walk(tree.root_node, [], [])
    for e in entities:
        if not e.parent:
            e.imports = list(imports)
    return entities, imports


def _extract(
    source: str, path: str, parser_name: str, include_nested: bool, constants_only: bool
) -> tuple[list[Entity], list[str]]:
    if parser_name == "ast":
        return extract_ast(source, path, include_nested, constants_only)
    if parser_name == "libcst":
        return extract_libcst(source, path, include_nested, constants_only)
    if parser_name == "treesitter":
        return extract_treesitter(source, path, include_nested, constants_only)
    msg = f"unknown parser: {parser_name}"
    raise ValueError(msg)


def _dedupe_entities(entities: list[Entity]) -> list[Entity]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Entity] = []
    for e in entities:
        key = (e.name, e.source.strip(), e.path.split("::")[0])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w\-.]", "_", s) or "unnamed"


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    p = directory / f"{stem}{suffix}"
    i = 1
    while p.exists():
        p = directory / f"{stem}_{i}{suffix}"
        i += 1
    return p


def write_per_entity(entities: list[Entity], out_dir: Path, write_metadata: bool = False) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = defaultdict(int)
    for e in entities:
        sub = out_dir / e.type
        sub.mkdir(parents=True, exist_ok=True)
        fp = _unique_path(sub, _safe_name(e.full_name), ".py")
        body = ""
        if e.imports and not e.parent:
            body += "\n".join(e.imports) + "\n\n"
        body += e.source
        if not body.endswith("\n"):
            body += "\n"
        try:
            ast.parse(body)
        except SyntaxError:
            log.debug("generated file {} has parse warnings (kept)", fp)
        fp.write_text(body, encoding="utf-8")
        counts[e.type] += 1
        if write_metadata:
            meta = {
                "name": e.name,
                "full_name": e.full_name,
                "type": e.type,
                "path": e.path,
                "line_start": e.line_start,
                "line_end": e.line_end,
                "parent": e.parent,
                "docstring": e.docstring,
                "imports": e.imports,
                "decorators": e.decorators,
            }
            (sub / (fp.stem + ".json")).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return counts


def write_by_type(entities: list[Entity], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    def _dump(fname: str, es: list[Entity]) -> None:
        body = "\n\n".join(e.source.rstrip() for e in es)
        if body and not body.endswith("\n"):
            body += "\n"
        (out_dir / fname).write_text(body, encoding="utf-8")

    _dump("classes.py", [e for e in entities if e.type == "class" and not e.parent])
    _dump("nested_classes.py", [e for e in entities if e.type == "class" and e.parent])
    _dump("functions.py", [e for e in entities if e.type == "function" and not e.parent])
    _dump(
        "nested_functions.py",
        [e for e in entities if e.type == "function" and e.parent],
    )
    _dump("const.py", [e for e in entities if e.type == "constant"])


def write_per_folder(entities: list[Entity], out_dir: Path, toc: bool = False) -> None:
    cwd = Path.cwd().resolve()
    groups: dict[Path, list[Entity]] = defaultdict(list)
    for e in entities:
        src = Path(e.path.split("::")[0])
        try:
            rel = src.resolve().parent.relative_to(cwd)
        except (ValueError, OSError):
            rel = Path(src.parent.name or ".")
        groups[rel].append(e)
    for rel, ents in groups.items():
        target_dir = out_dir / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        parts: list[str] = ["#!/usr/bin/env python", ""]
        if toc:
            by_file: dict[str, list[Entity]] = defaultdict(list)
            for e in ents:
                by_file[e.path].append(e)
            parts += ["#" + "=" * 76, "# TABLE OF CONTENTS", "#" + "=" * 76, ""]
            for src, es in sorted(by_file.items()):
                parts.append(f"# File: {src}")
                parts.append(f"#   Functions: {sum(1 for x in es if x.type == 'function')}")
                parts.append(f"#   Classes:   {sum(1 for x in es if x.type == 'class')}")
                parts.append("")
        for e in ents:
            parts += [
                "",
                "#" + "=" * 76,
                f"# File: {e.path}",
                f"# {e.type.title()}: {e.name}",
                "#" + "=" * 76,
                e.source,
            ]
        (target_dir / "definitions.py").write_text("\n".join(parts), encoding="utf-8")


def write_lists(entities: list[Entity], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    by_type: dict[str, dict[str, list[Entity]]] = defaultdict(lambda: defaultdict(list))
    for e in entities:
        by_type[e.type][e.path].append(e)
    for t, files in by_type.items():
        sub = out_dir / t
        sub.mkdir(parents=True, exist_ok=True)
        unique: set[str] = set()
        for src, es in files.items():
            stem = Path(src.split("::")[0]).stem or "unnamed"
            (sub / f"{stem}.txt").write_text(
                "\n".join(e.name for e in sorted(es, key=lambda x: x.name)) + "\n",
                encoding="utf-8",
            )
            unique.update(e.name for e in es)
        (sub / "unique.txt").write_text("\n".join(sorted(unique)) + "\n", encoding="utf-8")


def write_sqlite(entities: list[Entity], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, full_name TEXT, type TEXT,
            code TEXT, path TEXT,
            is_constant BOOLEAN, is_class BOOLEAN, is_function BOOLEAN
        )
    """)
    for e in entities:
        cur.execute(
            "INSERT INTO entities (name, full_name, type, code, path, "
            "is_constant, is_class, is_function) VALUES (?,?,?,?,?,?,?,?)",
            (
                e.name,
                e.full_name,
                e.type,
                e.source,
                e.path,
                e.type == "constant",
                e.type == "class",
                e.type == "function",
            ),
        )
    conn.commit()
    conn.close()


def write_imports_file(imports: Iterable[str], out_dir: Path) -> None:
    unique = sorted(set(imports))
    body = "# Global imports collected from all processed files\n\n" + "\n".join(unique) + "\n"
    (out_dir / "global_imports.py").write_text(body, encoding="utf-8")


def _worker(item: tuple) -> tuple:
    path_str, parser_name, include_nested, constants_only, is_archive = item
    p = Path(path_str)
    entities: list[Entity] = []
    imports: list[str] = []
    err: Optional[str] = None
    try:
        if is_archive:
            for member, source in iter_archive_python(p):
                full = f"{p.name}::{member}"
                es, im = _extract(source, full, parser_name, include_nested, constants_only)
                entities.extend(es)
                imports.extend(im)
        else:
            source = p.read_text(encoding="utf-8", errors="replace")
            es, im = _extract(source, str(p), parser_name, include_nested, constants_only)
            entities.extend(es)
            imports.extend(im)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return path_str, entities, imports, err


def cmd_extract(args: argparse.Namespace) -> int:
    if args.parser == "treesitter" and not HAS_TS:
        log.error("--parser treesitter requires tree-sitter & tree-sitter-python")
        return 2
    if args.parser == "libcst" and not HAS_LIBCST:
        log.error("--parser libcst requires libcst")
        return 2
    roots: list[Path] = []
    if args.paths:
        roots = [p for p in args.paths if p.exists()]
    elif args.dir:
        roots = [args.dir]
    else:
        roots = [Path.cwd()]
    if not roots:
        log.error("no valid input paths")
        return 2
    out_dir = Path(args.output).resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    skip = set(SKIP_DIRS) | set(args.exclude or [])
    py_files: list[Path] = []
    archives: list[Path] = []
    for r in roots:
        if r.is_file():
            if r.suffix == ".py":
                py_files.append(r)
            elif args.archives and is_archive(r):
                archives.append(r)
        else:
            pfs, ars = discover(r, skip, args.skip_tests, args.archives)
            py_files.extend(pfs)
            archives.extend(ars)
    log.info("Found {} Python file(s), {} archive(s)", len(py_files), len(archives))
    if not py_files and not archives:
        log.warning("nothing to process")
        return 0
    work: list[tuple] = [(str(p), args.parser, args.include_nested, args.constants_only, False) for p in py_files]
    work += [(str(p), args.parser, args.include_nested, args.constants_only, True) for p in archives]
    all_entities: list[Entity] = []
    all_imports: list[str] = []
    if MAX_WORKERS <= 1 or len(work) <= 1:
        for item in work:
            path_str, ents, imps, err = _worker(item)
            if err:
                log.error("{}: {}", path_str, err)
            else:
                log.info("processed {} ({} entities)", path_str, len(ents))
            all_entities.extend(ents)
            all_imports.extend(imps)
    else:
        with mp.Pool(processes=MAX_WORKERS) as pool:
            for path_str, ents, imps, err in pool.imap_unordered(_worker, work):
                if err:
                    log.error("{}: {}", path_str, err)
                else:
                    log.info("processed {} ({} entities)", path_str, len(ents))
                all_entities.extend(ents)
                all_imports.extend(imps)
    if args.dedupe:
        before = len(all_entities)
        all_entities = _dedupe_entities(all_entities)
        log.info("dedupe: {} -> {}", before, len(all_entities))
    all_entities.sort(key=lambda e: (e.type, e.full_name, e.path))
    log.info("total entities: {}", len(all_entities))
    if args.layout == "per-entity":
        write_per_entity(all_entities, out_dir, write_metadata=args.metadata)
    elif args.layout == "by-type":
        write_by_type(all_entities, out_dir)
    elif args.layout == "per-folder":
        write_per_folder(all_entities, out_dir, toc=args.toc)
    elif args.layout == "lists":
        write_lists(all_entities, out_dir)
    elif args.layout == "sqlite":
        write_sqlite(all_entities, Path(args.sqlite_path))
    if args.imports_file and args.layout != "lists":
        write_imports_file(all_imports, out_dir)
    print("=" * 40)
    print("EXTRACTION SUMMARY")
    print("-" * 40)
    by_type: dict[str, int] = defaultdict(int)
    for e in all_entities:
        by_type[e.type] += 1
    for t, c in sorted(by_type.items()):
        print(f"  {t:<12}: {c}")
    print(f"  {'total':<12}: {len(all_entities)}")
    print(f"  output       : {out_dir}")
    return 0


NODE_KINDS: dict[str, set[str]] = {
    "class": {"class_definition"},
    "func": {"function_definition"},
    "docstrings": {"function_docstrings", "class_docstrings"},
    "comments": {"comment", "expression_statement"},
}
NODE_KINDS["all"] = set().union(*NODE_KINDS.values())


def cmd_nodes(args: argparse.Namespace) -> int:
    if not HAS_TS:
        log.error("`nodes` requires tree-sitter & tree-sitter-python")
        return 2
    kinds = NODE_KINDS[args.kind]
    parser = _get_ts_parser()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cwd = Path.cwd().resolve()
    paths = args.paths or [Path.cwd()]
    targets: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            targets.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*.py")):
                if any(part.startswith(".") for part in f.parts):
                    continue
                if SKIP_DIRS.intersection(f.parts):
                    continue
                try:
                    if out.resolve() in f.resolve().parents:
                        continue
                except OSError:
                    pass
                targets.append(f)
    grouped: dict[Path, list[str]] = defaultdict(list)
    for py in targets:
        try:
            data = py.read_bytes()
        except OSError:
            continue
        tree = parser.parse(data)
        nodes = [
            data[c.start_byte : c.end_byte].decode("utf-8", "replace")
            for c in tree.root_node.children
            if c.type in kinds
        ]
        if nodes:
            grouped[py.parent].append("\n".join(nodes))
    for folder, chunks in grouped.items():
        try:
            rel = folder.resolve().relative_to(cwd)
        except ValueError:
            rel = Path(folder.name)
        target = out / rel / "imports.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n\n".join(chunks), encoding="utf-8")
    print(f"✨ Wrote {len(grouped)} folder file(s) under {out}")
    return 0


def _node_to_sexpr(node, data: bytes) -> str:
    if node.child_count == 0:
        txt = data[node.start_byte : node.end_byte].decode("utf-8", "replace")
        return f'({node.type} "{txt}")'
    inner = " ".join(_node_to_sexpr(c, data) for c in node.children)
    return f"({node.type} {inner})"


def cmd_sexpr(args: argparse.Namespace) -> int:
    if not HAS_TS:
        log.error("`sexpr` requires tree-sitter & tree-sitter-python")
        return 2
    p = Path(args.file)
    data = p.read_bytes()
    tree = _get_ts_parser().parse(data)
    print(_node_to_sexpr(tree.root_node, data))
    return 0


def cmd_funcnames(args: argparse.Namespace) -> int:
    p = Path(args.file)
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    except (OSError, SyntaxError) as e:
        print(f"Error: {e}")
        return 1
    names = [
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and not (args.skip_main and n.name == "main")
    ]
    if not names:
        print("No functions found.")
        return 0
    for n in names:
        print(n)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyextract",
        description="Extract functions / classes / constants from Python files, directories and archives.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("extract", help="Extract code entities.")
    e.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files/dirs to scan (default: current directory).",
    )
    e.add_argument(
        "-d",
        "--dir",
        type=Path,
        help="Root directory (alternative to positional `paths`).",
    )
    e.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="Output directory (default: ./output).",
    )
    e.add_argument(
        "--parser",
        choices=("ast", "libcst", "treesitter"),
        default="ast",
        help="Parsing backend (default: ast).",
    )
    e.add_argument(
        "--layout",
        choices=("per-entity", "by-type", "per-folder", "lists", "sqlite"),
        default="per-entity",
        help="Output layout (default: per-entity).",
    )
    e.add_argument(
        "--sqlite-path",
        type=Path,
        default=Path("ext.db"),
        help="SQLite file (used by --layout sqlite).",
    )
    e.add_argument(
        "--include-nested",
        action="store_true",
        help="Include methods and nested defs/classes.",
    )
    e.add_argument(
        "--constants-only",
        action="store_true",
        help="Extract only UPPERCASE constants.",
    )
    e.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip test/tests/examples directories.",
    )
    e.add_argument(
        "--archives",
        action="store_true",
        help="Also walk .zip/.whl/.tar[.gz]/.zst archives.",
    )
    e.add_argument(
        "--imports-file",
        action="store_true",
        help="Write a global_imports.py aggregating imports.",
    )
    e.add_argument(
        "--metadata",
        action="store_true",
        help="Write a JSON sidecar for each entity (per-entity layout).",
    )
    e.add_argument(
        "--toc",
        action="store_true",
        help="Add a table of contents (per-folder layout).",
    )
    e.add_argument(
        "--dedupe",
        action="store_true",
        help="Drop duplicate entities (same name + source).",
    )
    e.add_argument(
        "--clean",
        action="store_true",
        help="Delete the output directory before writing.",
    )
    e.add_argument("--exclude", nargs="*", default=[], help="Extra directory names to skip.")
    e.set_defaults(func=cmd_extract)
    n = sub.add_parser("nodes", help="Dump top-level tree-sitter nodes per folder.")
    n.add_argument("paths", nargs="*", type=Path, help="Files/dirs (default: current directory).")
    n.add_argument(
        "--kind",
        choices=tuple(NODE_KINDS.keys()),
        default="func",
        help="Which node type to keep (default: func).",
    )
    n.add_argument(
        "--out",
        type=Path,
        default=Path("output"),
        help="Output directory (default: ./output).",
    )
    n.set_defaults(func=cmd_nodes)
    s = sub.add_parser("sexpr", help="Print tree-sitter S-expression for a file.")
    s.add_argument("file", type=Path)
    s.set_defaults(func=cmd_sexpr)
    f = sub.add_parser("funcnames", help="List function names in a Python file.")
    f.add_argument("file", type=Path)
    f.add_argument("--skip-main", action="store_true", help="Skip the function named `main`.")
    f.set_defaults(func=cmd_funcnames)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
