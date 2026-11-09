#!/usr/bin/env python3
"""Heuristic in-place type-annotation adder for the bin_nov scripts.

This is the tool that produced the annotations in PR #1 (409 files, ~3000 new
hints).  It reads a Python file, infers parameter / return / module-constant /
instance-attribute types from the source itself, and then inserts the
annotations with *surgical string edits* so that nothing else in the file
moves (no reformatting, no AST round-trip).

Inference sources, in order of trust
------------------------------------
1. literal / ``None`` defaults  (``x=3`` -> ``int``, ``items=[]`` -> ``list[Any]``)
2. *hard* usage evidence        (``p.read_text()`` -> ``Path``, ``s.strip()`` -> ``str``,
                                 ``f.read()`` -> ``TextIO``, ``len(x)`` iterated -> ``Iterable``)
3. return / yield expressions   (tuple shapes, ``ast.dump``-able expressions, ``None``)
4. same-file definitions        (``self.helper()`` -> the helper's inferred return type,
                                 ``Foo()`` -> ``Foo``, ``Path(p)`` -> ``Path``)
5. base classes                 (``ast.NodeVisitor``, ``cst.CSTTransformer``, ``HTMLParser`` ...)
6. name conventions             (last resort: ``*_count`` -> ``int``, ``*_path`` -> ``Path``)

When none of these fire the parameter/return is left **unannotated** instead of
guessed.

Safety
------
Every file is re-parsed after editing and compared with the original AST after
all annotations are stripped (and ``AnnAssign(x: T = v)`` normalised back to
``Assign(x = v)``).  Any file that would change semantically is rejected
verbatim.  ``compile()`` is run as a second gate.

Usage
-----
    python arena/infer_annotations.py [--write] [--json out.json] [--limit N] FILE.py ...
    python arena/infer_annotations.py --write *.py          # whole repo
    python arena/run_all.py                                 # batched driver

Notes
-----
* Only single-line, paren-free aliases of ``from typing import ...`` are merged;
  multi-line typing imports get a fresh import line next to the stdlib block.
* A ``from __future__ import annotations`` line is added only when a PEP 604
  union (``X | None``) was inserted into a file that did not use one before.
* ``self`` / ``cls`` are never annotated (PEP 484/673).
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

BUILTIN_TYPES = {
    "int", "float", "complex", "bool", "str", "bytes", "bytearray", "list",
    "dict", "set", "frozenset", "tuple", "object", "type", "range", "None",
}

NUMERIC = {"int", "float", "complex"}

TYPING_NAMES = {
    "Any", "Optional", "Union", "Iterator", "Iterable", "Sequence", "Mapping",
    "MutableMapping", "TextIO", "IO", "Callable", "Generator", "AsyncIterator",
    "AsyncIterable", "NoReturn", "TypeVar", "ClassVar", "Final", "Literal",
    "TypedDict", "NamedTuple", "Protocol", "overload", "cast", "TYPE_CHECKING",
    "Self",
}

LITERAL_TYPE = {str: "str", int: "int", float: "float", bool: "bool", bytes: "bytes", complex: "complex"}

# attribute -> weight; used only when the receiver is the parameter itself
PATH_ATTRS = {
    "resolve": 6, "read_text": 8, "write_text": 8, "read_bytes": 8, "write_bytes": 8,
    "is_file": 6, "is_dir": 6, "exists": 6, "unlink": 8, "mkdir": 8, "rglob": 8,
    "iterdir": 8, "relative_to": 6, "as_posix": 6, "expanduser": 6, "with_suffix": 6,
    "with_name": 6, "is_symlink": 6, "glob": 6, "is_absolute": 6, "touch": 6, "chmod": 6,
}
STR_ATTRS = {
    "strip": 4, "lstrip": 4, "rstrip": 4, "splitlines": 5, "split": 3, "rsplit": 3,
    "lower": 5, "upper": 5, "casefold": 5, "startswith": 6, "endswith": 6, "zfill": 4,
    "isdigit": 5, "isalpha": 5, "isalnum": 5, "isspace": 5, "capitalize": 4, "title": 4,
    "removesuffix": 4, "removeprefix": 4,
}
RICH_STR_ATTRS = {"replace": 3, "join": 4, "encode": 5, "format": 2, "partition": 3, "rpartition": 3}
BYTES_ATTRS = {"decode": 8}
DICT_ATTRS = {"keys": 5, "setdefault": 6, "popitem": 5, "get": 2}
RICH_DICT_ATTRS = {"items": 3, "values": 2, "update": 2}
LIST_ATTRS = {"append": 5, "extend": 4, "insert": 4, "sort": 4, "reverse": 4}
SET_ATTRS = {"discard": 5, "issubset": 5, "issuperset": 5, "symmetric_difference": 5}
FILE_ATTRS = {
    "readlines": 8, "readline": 6, "writelines": 8, "fileno": 8, "readable": 8,
    "writable": 8, "truncate": 8, "read": 4, "write": 3, "seek": 4, "tell": 3,
}
FILE_STR_ATTRS = {"seek": 3, "tell": 3, "read": 2, "write": 2}
MATCH_ATTRS = {"groupdict": 8, "groups": 8, "span": 6}
MATCH_RICH = {"group": 4, "expand": 3}
PROCESS_ATTRS = {"returncode": 8, "check_returncode": 8}
PROCESS_RICH = {"stdout": 3, "stderr": 3}
REQUESTS_ATTRS = {"json": 4, "raise_for_status": 8, "content": 6, "iter_content": 8, "text": 2, "status_code": 6}
DT_ATTRS = {"isoformat": 8, "timestamp": 8, "strftime": 6}

NAME_HINTS = {
    # strings
    "name": "str", "text": "str", "msg": "str", "message": "str", "content": "str",
    "pattern": "str", "line": "str", "label": "str", "prefix": "str", "suffix": "str",
    "encoding": "str", "mode": "str", "command": "str", "cmd": "str", "title": "str",
    "url": "str", "html": "str", "markdown": "str", "source": "str", "target_lang": "str",
    "lang": "str", "lang_code": "str", "revision": "str", "branch": "str", "ref": "str",
    "rev": "str", "sha": "str", "hash": "str", "token": "str", "key": "str",
    "separator": "str", "sep": "str", "delimiter": "str", "ext": "str", "extension": "str",
    "new": "str", "old": "str", "reason": "str", "description": "str", "version": "str",
    "tag": "str", "username": "str", "email": "str", "password": "str", "host": "str",
    "user": "str", "dir_name": "str", "file_name": "str", "basename": "str", "module": "str",
    "attr": "str", "attribute": "str", "method": "str", "column": "str", "comment": "str",
    "raw": "str", "text_data": "str", "body": "str", "html_text": "str", "snippet": "str",
    "query": "str", "prompt": "str", "answer": "str", "response": "str",
    # ints / floats
    "count": "int", "n": "int", "num": "int", "index": "int", "idx": "int", "size": "int",
    "limit": "int", "offset": "int", "port": "int", "width": "int", "height": "int",
    "depth": "int", "level": "int", "retries": "int", "max_workers": "int", "workers": "int",
    "line_no": "int", "lineno": "int", "line_number": "int", "min_size": "int",
    "max_size": "int", "chunk_size": "int", "compression_level": "int",
    "min_mb": "float", "max_mb": "float", "mb": "float", "kb": "float", "gb": "float",
    "size_bytes": "int", "num_bytes": "int", "total_bytes": "int", "total_size": "int",
    "total_files": "int", "file_count": "int", "saved_count": "int", "n_files": "int",
    "radius": "float", "sigma": "float", "alpha": "float", "beta": "float", "gamma": "float",
    "spaces": "int", "indent": "int", "indent_size": "int", "tab_size": "int",
    "start": "int", "stop": "int", "step": "int", "end": "int", "r": "int", "g": "int", "b": "int",
    "timeout": "float", "delay": "float", "interval": "float", "ratio": "float",
    "threshold": "float", "factor": "float", "scale": "float", "ratio_threshold": "float",
    # bools
    "verbose": "bool", "debug": "bool", "force": "bool", "enabled": "bool", "flag": "bool",
    "dry_run": "bool", "recursive": "bool", "quiet": "bool", "check": "bool",
    "capture": "bool", "overwrite": "bool", "include_hidden": "bool", "follow_links": "bool",
    "as_bytes": "bool", "append": "bool", "create": "bool", "strict": "bool",
    "absolute": "bool", "verify": "bool", "show": "bool", "write": "bool", "read": "bool",
    "clean": "bool", "sync": "bool", "trim": "bool", "use_cache": "bool",
}

PATH_LIKE_NAMES = {
    "path", "src", "dst", "dest", "target", "dest_path", "src_path", "base", "base_dir",
    "root", "directory", "dir", "folder", "folder_path", "file", "file_name", "filename",
    "file_path", "filepath", "out", "output", "output_path", "output_dir", "input",
    "input_path", "source_path", "dest_dir", "target_dir", "workdir", "repo", "repo_path",
    "archive_path", "patch_file", "meta_file", "cwd", "parent", "archive", "src_dir",
    "tmpdir", "tmp_dir", "tempdir", "repos_dir", "clone_dir", "wheel_path", "script_path",
    "out_dir", "base_path", "trash_dir", "cache_dir", "temp_path",
}

EXPR_NAME_HINTS = {
    "node": "ast.AST", "expr": "ast.expr", "stmt": "ast.stmt", "tree": "ast.AST",
    "module_node": "ast.Module", "call_node": "ast.Call", "func_node": "ast.FunctionDef",
    "assign": "ast.Assign", "body": "ast.Module", "arguments": "ast.arguments",
    "arg_node": "ast.arg", "exc_node": "ast.ExceptHandler",
}

CONTAINER_NAMES = {
    "items": "list", "values": "list", "entries": "list", "files": "list", "paths": "list",
    "lines": "list", "rows": "list", "results": "list", "found": "list", "matches": "list",
    "ids": "list", "names": "list", "suffixes": "list", "parts": "list", "tokens": "list",
    "words": "list", "sents": "list", "sentences": "list", "records": "list", "args": "list",
    "argv": "list", "elts": "list", "elements": "list", "members": "list", "exts": "list",
    "keys": "list", "errors": "list", "warnings": "list", "targets": "list",
    "kwargs": "dict", "options": "dict", "config": "dict", "configs": "dict", "params": "dict",
    "settings": "dict", "env": "dict", "metadata": "dict", "mapping": "dict",
}

CALLABLE_HINTS = {
    "callback", "func", "handler", "hook", "keyfunc", "predicate", "action", "convert",
    "transform", "factory", "visitor",
}

BUILTIN_CALL_RETURNS = {
    "len": "int", "int": "int", "float": "float", "str": "str", "bool": "bool",
    "bytes": "bytes", "bytearray": "bytearray", "repr": "str", "sorted": "list[Any]",
    "reversed": "list[Any]", "range": "range", "enumerate": "enumerate[Any]",
    "zip": "zip[Any]", "iter": "Iterator[Any]", "open": "TextIO", "input": "str",
    "isinstance": "bool", "issubclass": "bool", "hasattr": "bool", "callable": "bool",
    "print": "None", "type": "type", "id": "int", "ord": "int", "chr": "str",
    "round": "int", "format": "str", "any": "bool", "all": "bool", "hex": "str",
    "oct": "str", "bin": "str", "hash": "int", "exit": "None", "quit": "None",
    "sum": "Any", "max": "Any", "min": "Any", "abs": "Any", "divmod": "tuple[int, int]",
    "getattr": "Any", "next": "Any", "vars": "dict[str, Any]", "dir": "list[str]",
    "globals": "dict[str, Any]", "locals": "dict[str, Any]", "super": "Any",
    "property": "property", "staticmethod": "Any", "classmethod": "Any", "eval": "Any",
    "exec": "None", "compile": "Any", "memoryview": "memoryview", "object": "object",
    "frozenset": "frozenset[Any]", "tuple": "tuple[Any, ...]", "list": "list[Any]",
    "dict": "dict[Any, Any]", "set": "set[Any]",
}

CONTAINER_CALLS = {
    "list": "list[Any]", "dict": "dict[str, Any]", "set": "set[Any]",
    "tuple": "tuple[Any, ...]", "frozenset": "frozenset[Any]",
    "OrderedDict": "Any", "defaultdict": "Any",
}

METHOD_RETURNS = {
    # str
    "strip": "str", "lstrip": "str", "rstrip": "str", "lower": "str", "upper": "str",
    "casefold": "str", "replace": "str", "join": "str", "encode": "bytes", "decode": "str",
    "format": "str", "title": "str", "capitalize": "str", "removeprefix": "str",
    "removesuffix": "str", "zfill": "str", "center": "str", "ljust": "str", "rjust": "str",
    "expandtabs": "str", "splitlines": "list[str]", "partition": "tuple[str, str, str]",
    "rpartition": "tuple[str, str, str]", "split": "list[str]", "rsplit": "list[str]",
    # bool-ish
    "startswith": "bool", "endswith": "bool", "isdigit": "bool", "isalpha": "bool",
    "isalnum": "bool", "isspace": "bool", "is_file": "bool", "is_dir": "bool",
    "exists": "bool", "is_symlink": "bool", "is_absolute": "bool", "is_relative": "bool",
    "isupper": "bool", "islower": "bool", "istitle": "bool", "isnumeric": "bool",
    "isdecimal": "bool", "isidentifier": "bool", "isprintable": "bool",
    # numbers
    "find": "int", "rfind": "int", "count": "int", "index": "int", "getsize": "int",
    # hashing
    "hexdigest": "str", "digest": "bytes",
    # Path
    "resolve": "Path", "absolute": "Path", "expanduser": "Path", "joinpath": "Path",
    "with_suffix": "Path", "with_name": "Path", "relative_to": "Path", "read_text": "str",
    "read_bytes": "bytes", "write_text": "int", "write_bytes": "int", "as_posix": "str",
    "as_uri": "str", "iterdir": "Iterator[Path]", "rglob": "Iterator[Path]",
    "glob": "Iterator[Path]", "stat": "Any", "open": "TextIO",
    "readlines": "list[str]", "readline": "str", "read": "str",
    # dict / list / file
    "keys": "Any", "values": "Any", "items": "Any", "get": "Any", "pop": "Any",
    "setdefault": "Any", "copy": "Any", "append": "None", "extend": "None", "insert": "None",
    "sort": "None", "reverse": "None", "update": "None", "add": "None", "discard": "None",
    "remove": "None", "clear": "None", "close": "None", "flush": "None", "seek": "int",
    "tell": "int", "write": "int", "writelines": "None", "match": "Any", "search": "Any",
    "fullmatch": "Any", "findall": "Any", "finditer": "Any", "sub": "str", "compile": "Any",
    "dumps": "str", "dump": "None", "loads": "Any", "load": "Any", "run": "Any",
    "check_output": "bytes",
}

MODULE_CALL_RETURNS = {
    ("subprocess", "run"): "subprocess.CompletedProcess[str]",
    ("subprocess", "check_output"): "bytes",
    ("subprocess", "check_call"): "int",
    ("os", "getcwd"): "str",
    ("os", "listdir"): "list[str]",
    ("os", "makedirs"): "None",
    ("os", "remove"): "None",
    ("os", "unlink"): "None",
    ("os", "rename"): "None",
    ("os", "chdir"): "None",
    ("os", "path", "join"): "str",
    ("os", "path", "exists"): "bool",
    ("os", "path", "isfile"): "bool",
    ("os", "path", "isdir"): "bool",
    ("os", "path", "basename"): "str",
    ("os", "path", "dirname"): "str",
    ("os", "path", "abspath"): "str",
    ("json", "dumps"): "str",
    ("json", "dump"): "None",
    ("json", "loads"): "Any",
    ("json", "load"): "Any",
    ("re", "sub"): "str",
    ("re", "match"): "re.Match[str] | None",
    ("re", "search"): "re.Match[str] | None",
    ("re", "fullmatch"): "re.Match[str] | None",
    ("re", "compile"): "re.Pattern[str]",
    ("re", "findall"): "list[Any]",
    ("re", "escape"): "str",
    ("shutil", "which"): "str | None",
    ("shutil", "rmtree"): "None",
    ("shutil", "copy"): "str",
    ("shutil", "copy2"): "str",
    ("shutil", "copyfile"): "str",
    ("shutil", "move"): "str",
    ("tempfile", "mkdtemp"): "str",
    ("tempfile", "mkstemp"): "tuple[int, str]",
    ("time", "time"): "float",
    ("time", "strftime"): "str",
    ("pathlib", "Path"): "Path",
}

MODULE_ATTR_RETURNS = {
    ("os.path", "join"): "str",
    ("os.path", "basename"): "str",
    ("os.path", "dirname"): "str",
    ("os.path", "abspath"): "str",
    ("os.path", "realpath"): "str",
    ("os.path", "expanduser"): "str",
    ("os.path", "relpath"): "str",
    ("os.path", "normpath"): "str",
    ("os.path", "splitext"): "tuple[str, str]",
    ("os.path", "exists"): "bool",
    ("os.path", "isfile"): "bool",
    ("os.path", "isdir"): "bool",
    ("os.path", "islink"): "bool",
    ("os.path", "getsize"): "int",
    ("os", "getcwd"): "str",
    ("os", "listdir"): "list[str]",
}

DUNDER_RETURNS = {
    "__init__": "None", "__post_init__": "None", "__str__": "str", "__repr__": "str",
    "__format__": "str", "__eq__": "bool", "__ne__": "bool", "__lt__": "bool",
    "__le__": "bool", "__gt__": "bool", "__ge__": "bool", "__contains__": "bool",
    "__bool__": "bool", "__hash__": "int", "__len__": "int", "__index__": "int",
    "__next__": "Any",
}

BASE_METHOD_RETURNS = {
    ("ast.NodeVisitor", "generic_visit"): "None",
    ("ast.NodeTransformer", "generic_visit"): "ast.AST | None",
    ("cst.CSTVisitor", "generic_visit"): "bool | None",
    ("cst.CSTTransformer", "generic_visit"): "cst.CSTNode",
    ("HTMLParser", "getpos"): "tuple[int, int]",
    ("HTMLParser", "feed"): "None",
    ("HTMLParser", "close"): "None",
    ("HTMLParser", "handle_starttag"): "None",
    ("HTMLParser", "handle_endtag"): "None",
    ("HTMLParser", "handle_data"): "None",
    ("HTMLParser", "handle_comment"): "None",
    ("HTMLParser", "handle_decl"): "None",
    ("HTMLParser", "handle_pi"): "None",
    ("HTMLParser", "handle_entityref"): "None",
    ("HTMLParser", "handle_charref"): "None",
    ("HTMLParser", "handle_startendtag"): "None",
}

TS_ATTRS = {
    "type", "children", "start_byte", "end_byte", "start_point", "end_point", "text",
    "child_by_field_name", "is_named", "named_children", "grammar_name", "has_error",
    "child_count", "field_name_for_child", "prev_named_sibling", "next_named_sibling",
}
AST_ATTRS = {
    "body", "targets", "id", "lineno", "col_offset", "annotation", "returns",
    "decorator_list", "elts", "name", "func", "args", "value", "op", "attr", "ctx",
    "orelse", "handlers", "test", "iter", "generators", "keys", "values", "slice",
    "level", "module", "names",
}

PRIORITIES = [
    "Response", "CompletedProcess", "datetime", "Match", "Path", "TextIO", "FileLike",
    "bytes", "set", "list", "dict", "Mapping", "Sequence", "Iterable", "Callable",
    "float", "int", "bool", "str",
]

ANY_ONLY = {
    "Any", "tuple[Any, ...]", "dict[Any, Any]", "list[tuple[Any, ...]]", "Iterator[Any]",
    "list", "dict", "set", "tuple", "list[Any]", "set[Any]", "dict[str, Any]",
}

PEP604_RE = re.compile(r"\|\s*None\b")

STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()


# --------------------------------------------------------------------------- #
# file context
# --------------------------------------------------------------------------- #
@dataclass
class FileCtx:
    """Everything the inference needs to know about one file."""

    path: Path
    text: str
    encoding: str
    tree: ast.Module
    future_annotations: bool
    style: str  # "pep604" | "optional"
    defined_names: set[str] = field(default_factory=set)
    needed: set[str] = field(default_factory=set)  # typing names to import
    need_re: bool = False
    need_path: bool = False
    lines: list[str] = field(default_factory=list)
    module_consts: dict[str, str] = field(default_factory=dict)
    # per function / class state
    class_bases: list[str] = field(default_factory=list)
    class_attr_types: dict[str, str] = field(default_factory=dict)
    class_method_returns: dict[str, str] = field(default_factory=dict)
    class_method_returns_all: dict[str, dict[str, str]] = field(default_factory=dict)
    module_classes: dict[str, set[str]] = field(default_factory=dict)
    method_returns: dict[str, str] = field(default_factory=dict)
    local_returns: dict[str, str] = field(default_factory=dict)
    local_types: dict[str, str] = field(default_factory=dict)
    func: ast.AST | None = None
    param_types: dict[str, str] = field(default_factory=dict)

    def has_name(self, name: str) -> bool:
        return name in self.defined_names or name in BUILTIN_TYPES

    def can_use(self, name: str) -> bool:
        if name in BUILTIN_TYPES:
            return True
        if name.startswith(("cst.", "re.", "subprocess.")):
            return True
        return name in self.defined_names

    def request(self, typ: str) -> str:
        """Register the imports *typ* needs and return it unchanged."""
        for name in TYPING_NAMES:
            if re.search(rf"(?<![\w.]){name}(?![\w.])", typ):
                self.needed.add(name)
        if re.search(r"(?<![\w.])Path(?![\w.])", typ) and not self.has_name("Path"):
            self.need_path = True
        if re.search(r"(?<![\w.])re\.", typ):
            self.need_re = True
        return typ

    def has_annotation(self, line: int, col: int) -> bool:
        """True when the source right after (line, col) already shows a return arrow."""
        if not (1 <= line <= len(self.lines)):
            return False
        return self.lines[line - 1][col:].lstrip().startswith("->")


def load_ctx(path: Path) -> FileCtx:
    raw = path.read_bytes()
    enc, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    text = raw.decode(enc)
    tree = ast.parse(text)
    future = any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    n_opt = text.count("Optional[") + text.count("Union[")
    n_604 = len(PEP604_RE.findall(text))
    style = "pep604" if future or n_604 >= n_opt else "optional"
    ctx = FileCtx(path=path, text=text, encoding=enc, tree=tree,
                  future_annotations=future, style=style)
    ctx.lines = text.splitlines(keepends=True)
    collect_names(ctx)
    collect_module_consts(ctx)
    return ctx


def collect_names(ctx: FileCtx) -> None:
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                ctx.defined_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                ctx.defined_names.add(a.asname or a.name)
    for node in ast.walk(ctx.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ctx.defined_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        ctx.defined_names.add(x.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ctx.defined_names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    ctx.defined_names.add(n.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for n in ast.walk(item.optional_vars):
                        if isinstance(n, ast.Name):
                            ctx.defined_names.add(n.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            ctx.defined_names.add(node.name)
        elif isinstance(node, ast.arg):
            ctx.defined_names.add(node.arg)


def collect_module_consts(ctx: FileCtx) -> None:
    for node in ctx.tree.body:
        if isinstance(node, ast.Assign):
            typ = infer_expr(ctx, node.value)
            for t in node.targets:
                if isinstance(t, ast.Name) and typ:
                    ctx.module_consts[t.id] = typ
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.annotation is not None:
            ctx.module_consts[node.target.id] = ast.unparse(node.annotation)


# --------------------------------------------------------------------------- #
# type expression helpers
# --------------------------------------------------------------------------- #
def split_top_level(typ: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in typ:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _split_args(inner: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in inner:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def merge_generics(a: str, b: str) -> str | None:
    if a == b:
        return a
    ma, mb = re.fullmatch(r"(\w+)\[(.+)\]", a), re.fullmatch(r"(\w+)\[(.+)\]", b)
    if ma and mb and ma.group(1) == mb.group(1):
        base = ma.group(1)
        ea = [p.strip() for p in _split_args(ma.group(2))]
        eb = [p.strip() for p in _split_args(mb.group(2))]
        if len(ea) == len(eb):
            merged = []
            for x, y in zip(ea, eb):
                if x == y:
                    merged.append(x)
                elif "Any" in (x, y):
                    merged.append(y if x == "Any" else x)
                elif {x, y} <= NUMERIC:
                    merged.append("float")
                else:
                    return None
            return f"{base}[{', '.join(merged)}]"
    if {a, b} <= NUMERIC:
        return "float" if "float" in (a, b) else "int"
    if "Any" in (a, b):
        return b if a == "Any" else a
    return None


def join_types(ctx: FileCtx, types: list[str]) -> str:
    """Join several inferred types into a single annotation."""
    flat: list[str] = []
    for t in types:
        if t:
            flat.extend(split_top_level(t))
    if any(t.startswith("tuple[") for t in flat) and (len(set(flat)) > 1 or len(flat) > 1):
        flat = [t for t in flat if not t.startswith("tuple[")] or ["tuple[Any, ...]"]
    seen: list[str] = []
    for t in flat:
        if t not in seen:
            seen.append(t)
    if not seen:
        return ""
    if len(seen) > 1 and any(t.startswith("tuple[") for t in seen):
        seen = [t for t in seen if not t.startswith("tuple[")] or ["tuple[Any, ...]"]
    has_none = "None" in seen
    rest = [t for t in seen if t != "None"]
    if not rest:
        return "None"
    if set(rest) <= NUMERIC and len(rest) > 1:
        rest = ["float" if "float" in rest else "int"]
    if len(rest) > 2:
        rest = ["Any"]
    elif len(rest) == 2:
        merged = merge_generics(rest[0], rest[1])
        rest = [merged] if merged else ["Any"]
    inner = rest[0]
    if not has_none:
        return inner
    return _union_none(ctx, inner)


def _union_none(ctx: FileCtx, typ: str) -> str:
    if typ == "Any" or typ.endswith("| None") or typ.startswith("Optional["):
        return typ
    if ctx.style == "pep604":
        return f"{typ} | None"
    ctx.needed.add("Optional")
    if "|" in typ:
        ctx.needed.add("Union")
        return f"Optional[Union[{typ}]]"
    return f"Optional[{typ}]"


# --------------------------------------------------------------------------- #
# expression inference
# --------------------------------------------------------------------------- #
def infer_expr(ctx: FileCtx, node: ast.AST | None, depth: int = 0) -> str:
    """Best-effort type of an expression node (``""`` when unknown)."""
    if node is None or depth > 6:
        return ""
    if ctx.func is not None and isinstance(node, ast.Name):
        local = ctx.local_types.get(node.id)
        if local and node.id not in ctx.param_types:
            return local
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "None"
        if node.value is Ellipsis:
            return ""
        if type(node.value) is str:
            return "str"
        for py, pyann in LITERAL_TYPE.items():
            if type(node.value) is py:
                return pyann
        return ""
    if isinstance(node, ast.JoinedStr):
        return "str"
    if isinstance(node, ast.FormattedValue):
        return infer_expr(ctx, node.value, depth + 1)
    if isinstance(node, ast.Name):
        if node.id in ("True", "False"):
            return "bool"
        if node.id == "None":
            return "None"
        t = (
            ctx.param_types.get(node.id)
            or ctx.module_consts.get(node.id)
            or ctx.local_returns.get(node.id)
        )
        if not t and ctx.func is not None:
            for arg in ast.walk(ctx.func.args):
                if isinstance(arg, ast.arg) and arg.arg == node.id and arg.annotation is not None:
                    t = ast.unparse(arg.annotation)
                    ctx.param_types[node.id] = t
                    break
        if t:
            return t
        if node.id in ("__file__", "__name__"):
            return "str"
        return ""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        elts = [e for e in (infer_expr(ctx, x, depth + 1) for x in node.elts) if e]
        base = {ast.List: "list", ast.Tuple: "tuple", ast.Set: "set"}[type(node)]
        if base == "tuple":
            if not elts:
                return "tuple[Any, ...]"
            if len(elts) == 1:
                return "tuple[Any, ...]" if "|" in elts[0] else f"tuple[{elts[0]},]"
            if len(elts) <= 4 and all("|" not in e for e in elts):
                return f"tuple[{', '.join(elts)}]"
            if len(set(elts)) == 1 and "|" not in elts[0]:
                return f"tuple[{elts[0]}, ...]"
            return "tuple[Any, ...]"
        inner = join_types(ctx, elts)
        if not elts or not inner or "|" in inner:
            return f"{base}[Any]"
        return f"{base}[{inner}]"
    if isinstance(node, ast.Dict):
        if not node.keys:
            return "dict[str, Any]"
        ks = [k for k in (infer_expr(ctx, x, depth + 1) for x in node.keys if x is not None) if k]
        vs = [v for v in (infer_expr(ctx, x, depth + 1) for x in node.values) if v]
        kt = join_types(ctx, ks) or "Any"
        vt = join_types(ctx, vs) or "Any"
        if "|" in kt:
            kt = "Any"
        if "|" in vt:
            vt = "Any"
        return f"dict[{kt}, {vt}]"
    if isinstance(node, ast.ListComp):
        inner = _comp_inner(ctx, node, depth)
        return f"list[{inner}]" if inner else "list[Any]"
    if isinstance(node, ast.SetComp):
        inner = _comp_inner(ctx, node, depth)
        return f"set[{inner}]" if inner else "set[Any]"
    if isinstance(node, ast.DictComp):
        k = infer_expr(ctx, node.key, depth + 1) or "Any"
        v = infer_expr(ctx, node.value, depth + 1) or "Any"
        return f"dict[{k if '|' not in k else 'Any'}, {v if '|' not in v else 'Any'}]"
    if isinstance(node, ast.GeneratorExp):
        inner = _comp_inner(ctx, node, depth)
        return f"Iterator[{inner}]" if inner else "Iterator[Any]"
    if isinstance(node, ast.BinOp):
        left = infer_expr(ctx, node.left, depth + 1)
        right = infer_expr(ctx, node.right, depth + 1)
        if left == "str" or right == "str":
            if isinstance(node.op, (ast.Add, ast.Mod)):
                return "str"
        if not left and isinstance(node.left, ast.Subscript) and right in NUMERIC:
            return right
        if left in NUMERIC and right in NUMERIC:
            if isinstance(node.op, ast.Div) or "float" in (left, right):
                return "float"
            return left or right
        return ""
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return "bool"
        inner = infer_expr(ctx, node.operand, depth + 1)
        return inner if inner in NUMERIC else ""
    if isinstance(node, ast.BoolOp):
        parts = [p for p in (infer_expr(ctx, v, depth + 1) for v in node.values) if p]
        if parts and all(p == "bool" for p in parts):
            return "bool"
        return join_types(ctx, parts)
    if isinstance(node, ast.Compare):
        return "bool"
    if isinstance(node, ast.IfExp):
        return join_types(
            ctx,
            [infer_expr(ctx, node.body, depth + 1), infer_expr(ctx, node.orelse, depth + 1)],
        )
    if isinstance(node, ast.NamedExpr):
        return infer_expr(ctx, node.value, depth + 1)
    if isinstance(node, ast.Await):
        return infer_expr(ctx, node.value, depth + 1)
    if isinstance(node, ast.Subscript):
        base = infer_expr(ctx, node.value, depth + 1)
        if base.startswith("dict["):
            parts = _split_args(base[len("dict["):-1])
            return parts[-1].strip() if len(parts) == 2 else "Any"
        m = re.fullmatch(r"(\w+)\[(.+)\]", base or "")
        if m:
            name, inner = m.group(1), m.group(2)
            if name == "dict":
                return _split_args(inner)[-1].strip() if "," in inner else "Any"
            if name in ("list", "set"):
                return inner if "|" not in inner else "Any"
            if name == "tuple":
                return _split_args(inner)[0].strip()
            if name in ("str", "bytes"):
                return "str"
        return ""
    if isinstance(node, ast.Attribute) and dotted_name(node) in ("sys.stderr", "sys.stdout", "sys.stdin"):
        return "TextIO"
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and not node.args and node.func.id in CONTAINER_CALLS:
            if not any(kw.arg == "default_factory" for kw in node.keywords):
                return CONTAINER_CALLS[node.func.id]
        return infer_call(ctx, node, depth)
    if isinstance(node, ast.Attribute):
        return infer_attr(ctx, node, depth)
    if isinstance(node, ast.Starred):
        return infer_element(ctx, node)
    return ""


def _comp_inner(ctx: FileCtx, node: ast.AST, depth: int) -> str:
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return infer_expr(ctx, node.elt, depth + 1)
    if isinstance(node, ast.DictComp):
        return infer_expr(ctx, node.value, depth + 1)
    return ""


def infer_call(ctx: FileCtx, node: ast.Call, depth: int) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
        if name == "Path":
            ctx.need_path = True
            return "Path"
        if name in BUILTIN_CALL_RETURNS:
            return BUILTIN_CALL_RETURNS[name]
        if name in ctx.module_classes:
            return name
        if name in ctx.param_types and "Callable" in ctx.param_types[name]:
            return "Any"
        return ""
    if not isinstance(func, ast.Attribute):
        return ""
    dotted = dotted_name(func)
    if dotted and dotted in ctx.module_consts:
        return ctx.module_consts[dotted]
    parts = dotted.split(".") if dotted else []
    for i in range(len(parts), 0, -1):
        key = tuple(parts[:i])
        if key in MODULE_CALL_RETURNS:
            return MODULE_CALL_RETURNS[key]
    attr = func.attr
    recv = infer_expr(ctx, func.value, depth + 1)
    if recv == "Path" and attr in METHOD_RETURNS:
        return METHOD_RETURNS[attr]
    if recv.startswith("str") and attr in METHOD_RETURNS:
        return METHOD_RETURNS[attr]
    if recv == "bytes":
        if attr == "decode":
            return "str"
        if attr == "hex":
            return "str"
    if recv.startswith("re.Match") or recv == "Any":
        if attr in ("group", "expand"):
            return "str"
        if attr == "groupdict":
            return "Any"
        if attr == "groups":
            return "tuple[Any, ...]"
        if attr == "span":
            return "tuple[int, int]"
        if attr in ("start", "end"):
            return "int"
    if recv.startswith("dict"):
        if attr == "get":
            # `mapping.get(key, default)` is as typed as its default
            for kw in node.keywords:
                if kw.arg == "default" and kw.value is not None:
                    default_t = infer_expr(ctx, kw.value)
                    if default_t and "|" not in default_t:
                        return default_t
            if len(node.args) >= 2:
                default_t = infer_expr(ctx, node.args[1])
                if default_t and "|" not in default_t:
                    return default_t
            return "Any"
    if dotted in MODULE_ATTR_RETURNS:
        return MODULE_ATTR_RETURNS[dotted]
    if "." in dotted:
        mod = dotted.rsplit(".", 1)[0]
        if (mod, attr) in METHOD_RETURNS and mod in ("str", "Path"):
            return METHOD_RETURNS[attr]
    if isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
        local = ctx.class_method_returns.get(attr)
        if local:
            return local
    if isinstance(func.value, ast.Name) and func.value.id in ctx.module_classes:
        cls_returns = ctx.class_method_returns_all.get(func.value.id, {})
        if attr in cls_returns:
            return cls_returns[attr]
    if (
        isinstance(func.value, ast.Attribute)
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "self"
        and attr in ctx.method_returns
    ):
        return ctx.method_returns[attr]
    if attr in METHOD_RETURNS:
        return METHOD_RETURNS[attr]
    # a method of a class defined in this very file: use its return type
    cls_returns = ctx.class_method_returns_all.get(recv, {})
    if attr in cls_returns:
        return cls_returns[attr]
    return ""


def infer_attr(ctx: FileCtx, node: ast.Attribute, depth: int) -> str:
    dotted = dotted_name(node)
    if dotted in ctx.module_consts:
        return ctx.module_consts[dotted]
    if dotted in ("os.sep", "os.linesep", "os.pathsep", "sys.platform", "sys.executable", "sys.version"):
        return "str"
    if dotted == "sys.argv":
        return "list[str]"
    recv = infer_expr(ctx, node.value, depth + 1)
    attr = node.attr
    if recv == "Path":
        if attr == "parent":
            return "Path"
        if attr in ("name", "stem", "suffix", "anchor", "drive"):
            return "str"
        if attr == "suffixes":
            return "list[str]"
        if attr == "parts":
            return "tuple[str, ...]"
    if recv == "subprocess.CompletedProcess[str]":
        if attr == "returncode":
            return "int"
        if attr in ("stdout", "stderr"):
            return "str"
    if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
        return ctx.class_attr_types.get(attr, "")
    return ""


def dotted_name(node: ast.AST) -> str:
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def iterable_element(typ: str) -> str:
    """Element type of an iterable annotation (``Iterator[Path]`` -> ``Path``)."""
    m = re.fullmatch(
        r"(?:\w+\.)?(?:Iterator|Iterable|Sequence|list|set|tuple|Generator|AsyncIterator)\[(.+)\]",
        typ or "",
    )
    if not m:
        return ""
    inner = m.group(1)
    if inner.startswith(("tuple[", "dict[")):
        return ""
    first = inner.split(",")[0].strip()
    if not first or first == "Any" or "|" in first:
        return ""
    return first


def infer_element(ctx: FileCtx, node: ast.AST | None) -> str:
    """Type of the *element* produced for a yielded / appended expression."""
    if node is None:
        return ""
    if isinstance(node, ast.Starred):
        return iterable_element(infer_expr(ctx, node.value)) or infer_expr(ctx, node.value)
    typ = infer_expr(ctx, node)
    return iterable_element(typ) or typ


# --------------------------------------------------------------------------- #
# parameter inference
# --------------------------------------------------------------------------- #
class Evidence:
    """Weighted usage evidence for one parameter.

    ``hard`` holds near-certain signals (``p.read_text()`` -> Path, ``s.strip()``
    -> str); ``counts`` also keeps hints any object could satisfy (f-strings,
    comparisons, arithmetic).
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.hard: dict[str, int] = {}

    def add(self, t: str, w: int = 1, hard: bool = False) -> None:
        if not t:
            return
        self.counts[t] = self.counts.get(t, 0) + w
        if hard:
            self.hard[t] = self.hard.get(t, 0) + w

    def score(self, t: str) -> int:
        return self.counts.get(t, 0)

    def signals(self) -> set[str]:
        return {k for k, v in self.counts.items() if v > 0 and k != "Any"}


def collect_param_evidence(ctx: FileCtx, func: ast.AST, param: str) -> Evidence:
    ev = Evidence()

    def is_param(n: ast.AST) -> bool:
        return isinstance(n, ast.Name) and n.id == param

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.Lambda,)) and node is not func:
            return
        if isinstance(node, ast.Attribute) and is_param(node.value):
            attr = node.attr
            for table, typ, hard in (
                (PATH_ATTRS, "Path", True),
                (STR_ATTRS, "str", True),
                (RICH_STR_ATTRS, "str", True),
                (BYTES_ATTRS, "bytes", True),
                (DICT_ATTRS, "dict", True),
                (RICH_DICT_ATTRS, "dict", True),
                (LIST_ATTRS, "list", True),
                (SET_ATTRS, "set", True),
                (FILE_ATTRS, "TextIO", True),
                (FILE_STR_ATTRS, "str", False),
                (MATCH_ATTRS, "Match", True),
                (MATCH_RICH, "Match", False),
                (PROCESS_ATTRS, "CompletedProcess", True),
                (PROCESS_RICH, "CompletedProcess", False),
                (REQUESTS_ATTRS, "Response", True),
                (DT_ATTRS, "datetime", True),
            ):
                if attr in table:
                    ev.add(typ, table[attr], hard=hard)
        if isinstance(node, ast.Call):
            f = node.func
            passed = any(is_param(a) for a in node.args) or any(
                kw.value is not None and is_param(kw.value) for kw in node.keywords
            )
            if passed:
                if isinstance(f, ast.Name):
                    fn = f.id
                    table = {
                        "Path": ("Path", 12, True), "open": ("Path", 6, True),
                        "str": ("str", 8, False), "int": ("int", 8, True),
                        "float": ("float", 8, True), "bool": ("bool", 8, True),
                        "bytes": ("bytes", 8, True), "print": ("str", 2, False),
                        "repr": ("str", 2, False), "range": ("int", 4, False),
                    }
                    if fn in table:
                        typ, w, hard = table[fn]
                        ev.add(typ, w, hard=hard)
                    elif fn in ("len", "sorted", "list", "tuple", "set", "enumerate", "iter"):
                        ev.add("Sequence", 3)
                elif isinstance(f, ast.Attribute):
                    dotted = dotted_name(f)
                    if dotted in ("os.path.join", "os.path.basename", "os.path.dirname",
                                  "os.path.abspath", "os.path.normpath"):
                        ev.add("str", 6, hard=True)
                    elif dotted in ("json.dump", "json.load", "pickle.dump", "pickle.load"):
                        ev.add("FileLike", 8, hard=True)
                    elif dotted in ("shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.move"):
                        ev.add("Path", 5, hard=True)
                        ev.add("str", 2)
                    elif dotted in ("subprocess.run", "subprocess.check_output",
                                    "subprocess.Popen", "subprocess.call", "subprocess.check_call"):
                        ev.add("Command", 8, hard=True)
                    elif f.attr == "decode" and is_param(f.value):
                        ev.add("bytes", 10, hard=True)
                    elif f.attr == "encode" and is_param(f.value):
                        ev.add("str", 10, hard=True)
                    elif f.attr in ("exists", "is_file", "is_dir", "mkdir", "unlink"):
                        ev.add("Path", 10, hard=True)
            if isinstance(f, ast.Name) and f.id == param:
                ev.add("Callable", 10, hard=True)
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if is_param(t):
                        ev.add("Any", 6)
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            for i, o in enumerate(operands):
                if is_param(o):
                    for j, other in enumerate(operands):
                        if j == i or not isinstance(other, ast.Constant):
                            continue
                        t = LITERAL_TYPE.get(type(other.value))
                        if t and t != "bool":
                            ev.add(t, 3)
        if isinstance(node, ast.BinOp):
            for side, other in ((node.left, node.right), (node.right, node.left)):
                if is_param(side) and isinstance(other, ast.Constant):
                    t = LITERAL_TYPE.get(type(other.value))
                    if t in ("int", "str", "float"):
                        ev.add(t, 4)
        if isinstance(node, (ast.For, ast.AsyncFor)) and is_param(node.iter):
            ev.add("Iterable", 4, hard=True)
            target_names = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id in target_names:
                    if sub.attr in PATH_ATTRS:
                        ev.add("PathElem", 3)
                    elif sub.attr in STR_ATTRS:
                        ev.add("StrElem", 3)
        if isinstance(node, ast.comprehension) and is_param(node.iter):
            ev.add("Iterable", 4, hard=True)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args and is_param(node.args[0]):
            if node.func.id in ("iter", "list", "sorted", "set", "tuple", "enumerate", "reversed", "any", "all"):
                ev.add("Iterable", 5, hard=True)
        if isinstance(node, ast.Subscript) and is_param(node.value):
            idx = node.slice
            if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
                ev.add("Mapping", 5, hard=True)
                ev.add("dict", 5, hard=True)
            elif isinstance(idx, (ast.Slice, ast.Constant)):
                ev.add("Sequence", 4)
        if isinstance(node, ast.FormattedValue) and is_param(node.value):
            ev.add("str", 3)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in getattr(func, "body", []):
        visit(stmt)
    return ev


def evidence_to_type(ev: Evidence, ctx: FileCtx) -> str:
    counts = {k: v for k, v in ev.counts.items() if k != "Any"}
    hard = {k: v for k, v in ev.hard.items() if k != "Any"}
    pool = hard or counts
    if not pool:
        return ""
    best = sorted(
        pool.items(),
        key=lambda kv: (-kv[1], PRIORITIES.index(kv[0]) if kv[0] in PRIORITIES else 99),
    )[0][0]
    mapping = {
        "Path": "Path", "Command": "str", "TextIO": "TextIO", "FileLike": "TextIO",
        "Match": "re.Match[str]", "Response": "Any",
        "CompletedProcess": "subprocess.CompletedProcess[str]", "datetime": "Any",
        "dict": "dict[str, Any]", "Mapping": "dict[str, Any]", "list": "list[Any]",
        "set": "set[Any]", "Sequence": "Sequence[Any]", "Iterable": "Iterable[Any]",
        "Callable": "Callable[..., Any]",
    }
    typ = mapping.get(best, best)
    if typ == "Path" and not ctx.has_name("Path"):
        return "str"
    if typ == "Iterable[Any]" and ev.score("PathElem"):
        typ = "Sequence[Path]" if ctx.has_name("Path") else "Sequence[str]"
    return typ


def name_based_type(ctx: FileCtx, name: str) -> str:
    """Weak, name-only fallback used when usage gives no signal."""
    lname = name.lower()
    if lname in EXPR_NAME_HINTS and ctx.has_name("ast"):
        return EXPR_NAME_HINTS[lname]
    if lname in ("node", "original_node", "updated_node") and ctx.has_name("cst"):
        return "cst.CSTNode"
    if lname in ("tree",) and ctx.has_name("cst"):
        return "cst.Module"
    if lname in ("event", "lock", "detector", "translator", "client", "session", "backend",
                 "engine", "browser", "page", "driver", "log", "console", "progress",
                 "bar", "task", "instance", "inst", "worker", "pool", "executor",
                 "manager", "app", "frame", "img", "image", "tiles", "arr", "array"):
        return "Any"
    if lname in ("it", "iterable", "iterator", "seq", "sequence", "gen", "generator", "stream", "rows"):
        return "Iterable[Any]"
    if lname.endswith(("_bytes", "_blob", "_buf", "_buffer", "_raw")) or lname in (
        "data_bytes", "byte_content", "src_bytes", "raw_bytes", "blob", "buf", "buffer",
    ):
        return "bytes"
    if lname in ("paths", "dirs", "files", "folders", "repos", "archives", "sources", "destinations"):
        return "list[Path]" if ctx.has_name("Path") else "list[str]"
    if lname in ("filename", "file_name", "fname", "basename", "dirname", "path_str",
                 "str_path", "root_dir", "trash_dir", "dest_str", "src_str", "target_str"):
        return "Path" if lname.endswith("_dir") and ctx.has_name("Path") else "str"
    if lname in PATH_LIKE_NAMES:
        return "Path" if ctx.has_name("Path") else "str"
    if lname.endswith(("_path", "_file", "_folder")):
        return "Path" if ctx.has_name("Path") else "str"
    if lname.endswith("_dir"):
        return "Path" if ctx.has_name("Path") else "str"
    if lname in CONTAINER_NAMES:
        base = CONTAINER_NAMES[lname]
        if base == "list":
            return "list[str]" if lname == "argv" else "list[Any]"
        if base == "set":
            return "set[str]"
        return "dict[str, Any]"
    if lname in ("args", "argv", "arguments", "cli_args", "extra_args"):
        return "list[str]" if lname == "argv" else "list[Any]"
    if lname in NAME_HINTS:
        return NAME_HINTS[lname]
    if lname in ("pypi_index", "index", "cache", "lookup", "table", "registry", "packages",
                 "imports", "deps"):
        return "dict[str, Any]"
    if lname.endswith(("_name", "_text", "_str", "_msg", "_message", "_url", "_key", "_pattern",
                       "_label", "_title", "_lang", "_mode", "_prefix", "_suffix", "_ext",
                       "_encoding", "_sep", "_tag", "_comment", "_reason", "_revision",
                       "_branch", "_ref", "_sha", "_hash", "_token", "_user", "_username",
                       "_email", "_host", "_version", "_format")):
        return "str"
    if lname.endswith(("_count", "_num", "_size", "_len", "_idx", "_index", "_level", "_depth",
                       "_limit", "_port", "_number", "_width", "_height", "_offset", "_n")):
        return "int"
    if lname.endswith(("_flag", "_enabled", "_allowed")):
        return "bool"
    if lname.startswith(("is_", "has_", "should_", "use_", "allow_", "can_", "enable_")):
        return "bool"
    if lname in ("signum", "sig", "_signum", "signal_num", "signo"):
        return "int"
    if lname in ("cls", "klass", "kls"):
        return "Any"
    if lname in CALLABLE_HINTS:
        return "Callable[..., Any]"
    if lname.startswith(("list_", "all_")):
        return "list[Any]"
    if lname.startswith(("dict_", "map_")):
        return "dict[str, Any]"
    return ""


def param_message_percent(func: ast.AST, name: str) -> bool:
    """True when the parameter is used as the left side of a ``%`` interpolation."""
    for node in ast.walk(func):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = node.left
            if isinstance(left, ast.Name) and left.id == name:
                right = node.right
                if isinstance(right, (ast.Tuple, ast.Dict, ast.List, ast.Name)):
                    return True
                if isinstance(right, ast.Constant) and isinstance(right.value, tuple):
                    return True
    return False


def infer_default(ctx: FileCtx, default: ast.AST | None) -> str:
    if default is None:
        return ""
    if isinstance(default, ast.Constant):
        if default.value is None:
            return ""
        for py, pyann in LITERAL_TYPE.items():
            if type(default.value) is py:
                return pyann
        return ""
    if isinstance(default, ast.UnaryOp) and isinstance(default.op, (ast.USub, ast.UAdd)):
        return infer_expr(ctx, default.operand)
    if isinstance(default, ast.Name):
        if default.id in ("True", "False"):
            return "bool"
        if default.id == "None":
            return ""
        return ctx.module_consts.get(default.id, "")
    if isinstance(default, ast.Attribute):
        return infer_attr(ctx, default)
    if isinstance(default, (ast.Tuple, ast.List, ast.Set)):
        return infer_expr(ctx, default)
    if isinstance(default, ast.Call) and isinstance(default.func, ast.Name):
        fname = default.func.id
        if fname in ("list", "set") and not default.args:
            return f"{fname}[Any]"
        if fname == "dict" and not default.args:
            return "dict[str, Any]"
        if fname == "tuple" and not default.args:
            return "tuple[Any, ...]"
        if fname == "Path":
            return "Path"
    return infer_expr(ctx, default)


def is_override(func: ast.AST) -> bool:
    return any(
        (isinstance(d, ast.Name) and d.id == "override")
        or (isinstance(d, ast.Attribute) and d.attr == "override")
        for d in getattr(func, "decorator_list", [])
    )


def _visitor_kind(func: ast.AST) -> str:
    """``visit_FunctionDef`` -> ``FunctionDef`` (the node class of that visitor hook)."""
    name = getattr(func, "name", "")
    for prefix in ("visit_", "leave_"):
        if name.startswith(prefix) and len(name) > len(prefix):
            kind = name[len(prefix):]
            if kind[:1].isupper():
                return kind
    return ""


def looks_like_ts_node(func: ast.AST, name: str) -> bool:
    ts = ast_s = 0
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == name:
            if node.attr in TS_ATTRS:
                ts += 1
            elif node.attr in AST_ATTRS:
                ast_s += 1
    return ts > ast_s


def infer_param_type(
    ctx: FileCtx,
    func: ast.AST,
    param: ast.arg,
    default: ast.AST | None,
    default_repr: str,
    is_first: bool,
    in_class: bool,
) -> str:
    name = param.arg
    if name in ("self", "cls", "mcs") and (in_class or is_first):
        return ""  # PEP 484/673: leave self/cls unannotated
    if in_class and name in ("original_node", "updated_node", "node") and ctx.has_name("cst"):
        kind = _visitor_kind(func)
        if kind and (name != "node" or ctx.class_bases):
            return f"cst.{kind}"
        return "cst.CSTNode"
    if in_class and name == "node" and any("NodeVisitor" in b or "NodeTransformer" in b for b in ctx.class_bases):
        if ctx.has_name("ast"):
            return f"ast.{_visitor_kind(func) or 'AST'}"
        return "ast.AST"
    if name in ("node", "root_node", "child", "parent_node") and looks_like_ts_node(func, name):
        return "Any"
    if default_repr in ("__VARARG__", "__KWARG__"):
        if name.startswith("_"):
            return "Any"
        typ = evidence_to_type(collect_param_evidence(ctx, func, name), ctx)
        return typ if typ in ("str", "int", "float", "bool", "bytes") else "Any"
    if is_override(func):
        return ""

    ev = collect_param_evidence(ctx, func, name)
    default_type = infer_default(ctx, default) if default is not None else ""
    ev_type = evidence_to_type(ev, ctx)
    none_default = isinstance(default, ast.Constant) and default.value is None
    container_default = (
        default_type if default_type in ("list[Any]", "dict[str, Any]", "set[Any]",
                                         "tuple[Any, ...]", "frozenset[Any]", "dict[Any, Any]")
        else ""
    )
    if default_type.startswith("dict[") and default_type != "dict[str, Any]":
        default_type = ""

    # a message that is `%`-formatted gets `Any`
    if param_message_percent(func, name):
        return "Any"

    # 1) the default value is the strongest signal - but `x=None` gets its type
    #    from usage, and a str default clearly used as a path becomes Path
    if default_type:
        if default_type == "str" and ev.score("Path") >= 10:
            return ctx.request("Path")
        if default_type in ("str", "int", "float", "bytes") and ev_type and ev_type != default_type:
            return ctx.request(_union_none(ctx, ev_type) if none_default else ev_type)
        if default_type == "bool":
            if ev.score("str") >= 10 and ev.score("int") >= 10:
                return "Any"
            return "bool"
        if default_type == "Any":
            default_type = ""
        else:
            return ctx.request(default_type)

    # 2) names (strong hints such as *_count / *_path beat incidental usage) -
    #    but a one-letter name carries almost no information ('b' is not a bytes
    #    buffer just because it starts with b), so hard usage evidence wins there
    named = name_based_type(ctx, name)
    if named and len(name) <= 2 and ev.hard:
        hard_type = evidence_to_type(ev, ctx)
        if hard_type and hard_type != "Any":
            named = ""
    if named:
        if none_default and named != "Any":
            return ctx.request(_union_none(ctx, named))
        return ctx.request(named)

    # 3) usage evidence
    if ev_type:
        if ev_type == "dict[str, Any]" and CONTAINER_NAMES.get(name) == "list":
            return "list[Any]"
        if len(ev.signals()) >= 3 and (
            (ev.score("Path") >= 6 and ev.score("list") >= 6)
            or (ev.score("str") >= 6 and ev.score("list") >= 6)
            or (ev.score("dict") >= 6 and ev.score("list") >= 6)
            or (ev.score("TextIO") >= 6 and ev.score("list") >= 6)
        ):
            return "Any"
        if none_default:
            return ctx.request(_union_none(ctx, ev_type))
        return ctx.request(ev_type)
    if container_default:
        return ctx.request(container_default)
    if none_default and name in ("timezone", "tz"):
        return _union_none(ctx, "str")
    if name.startswith("_"):
        return ""
    return "Any"


# --------------------------------------------------------------------------- #
# function return inference
# --------------------------------------------------------------------------- #
class ReturnCollector(ast.NodeVisitor):
    """Collects the ``return`` / ``yield`` statements of one function."""

    def __init__(self) -> None:
        self.returns: list[ast.Return] = []
        self.yields: list[ast.AST] = []
        self.has_yield = False
        self.depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self.depth > 0:
            return  # nested functions have their own returns
        self.depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self.depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(node)
        self.generic_visit(node)

    def visit_Yield(self, node: ast.Yield) -> None:
        self.has_yield = True
        self.yields.append(node)
        self.generic_visit(node)

    def visit_YieldFrom(self, node: ast.YieldFrom) -> None:
        self.has_yield = True
        self.yields.append(node)
        self.generic_visit(node)


def _is_exit_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    dotted = dotted_name(node.func)
    return dotted in ("sys.exit", "os._exit", "parser.error", "ap.error") or dotted.endswith((".exit", ".error"))


def stmt_falls_through(node: ast.stmt) -> bool:
    if isinstance(node, (ast.Return, ast.Raise)):
        return False
    if isinstance(node, ast.If):
        if not node.orelse:
            return True
        return block_falls_through(node.body) or block_falls_through(node.orelse)
    if isinstance(node, ast.While):
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            if not any(isinstance(n, ast.Break) for n in ast.walk(node)):
                return False
        return True
    if isinstance(node, ast.With):
        return block_falls_through(node.body)
    if isinstance(node, ast.Try):
        if node.finalbody and not block_falls_through(node.finalbody):
            return False
        ok = block_falls_through(node.body)
        if node.handlers:
            ok = ok or any(block_falls_through(h.body) for h in node.handlers)
        else:
            ok = True
        if node.orelse:
            ok = ok or block_falls_through(node.orelse)
        return ok
    if isinstance(node, ast.Match):
        return any(block_falls_through(c.body) for c in node.cases) or not node.cases
    return True


def block_falls_through(body: list[ast.stmt]) -> bool:
    if not body:
        return True
    last = body[-1]
    if isinstance(last, ast.Expr) and _is_exit_call(last.value):
        return False
    return stmt_falls_through(last)


def always_raises(body: list[ast.stmt]) -> bool:
    """True when the only way out of the function is an exception."""
    if not body or len(body) > 2:
        return False
    last = body[-1]
    if isinstance(last, ast.Raise):
        return True
    if isinstance(last, ast.If):
        return always_raises(last.body) and always_raises(last.orelse)
    if isinstance(last, (ast.With, ast.AsyncWith)) and last.body:
        return isinstance(last.body[-1], ast.Raise)
    return False


def base_return_type(bases: list[str], name: str, is_leave: bool) -> str:
    for b in bases:
        if (b, name) in BASE_METHOD_RETURNS:
            return BASE_METHOD_RETURNS[(b, name)]
        if b.endswith(("NodeTransformer", "NodeVisitor")) and name.startswith("visit_"):
            return ""
        if b.endswith("CSTTransformer") and name.startswith("leave_"):
            return "cst.CSTNode"
        if b.endswith(("CSTTransformer", "CSTVisitor")) and name.startswith("visit_"):
            return "bool | None"
        if b.endswith("FileSystemEventHandler") and name.startswith(("on_", "on_any_event")):
            return "None"
    return ""


def infer_factory_return(ctx: FileCtx, func: ast.AST) -> str:
    """A function that only defines and returns a closure returns ``Callable``."""
    body = getattr(func, "body", [])
    if len(body) < 2:
        return ""
    last = body[-1]
    if not (isinstance(last, ast.Return) and isinstance(last.value, ast.Name)):
        return ""
    name = last.value.id
    for stmt in body[:-1]:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return "Callable[..., Any]"
    return ""


def infer_return_type(ctx: FileCtx, func: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool) -> str:
    name = func.name
    if in_class and ctx.class_bases:
        bt = base_return_type(ctx.class_bases, name, False)
        if bt:
            return bt
    factory = infer_factory_return(ctx, func)
    if factory:
        return factory
    if in_class and name in DUNDER_RETURNS:
        return DUNDER_RETURNS[name]
    rc = ReturnCollector()
    for stmt in func.body:
        rc.visit(stmt)
    if rc.has_yield:
        inner_types = []
        for y in rc.yields:
            if isinstance(y, ast.Yield) and y.value is not None:
                inner_types.append(infer_element(ctx, y.value))
            elif isinstance(y, ast.YieldFrom):
                t = infer_expr(ctx, y.value)
                m = re.fullmatch(r"(?:list|set|tuple)\[(.+)\]", t or "")
                if m:
                    inner_types.append(m.group(1))
        inner = join_types(ctx, [t for t in inner_types if t])
        if inner == "tuple[Any]":
            inner = "tuple[Any, ...]"
        if not inner or inner == "Any" or "|" in inner:
            return ""
        return ctx.request(f"Iterator[{inner}]")
    if not rc.returns:
        return "NoReturn" if always_raises(func.body) else "None"

    inferred: list[str] = []
    unknowns = 0
    for r in rc.returns:
        if r.value is None:
            inferred.append("None")
            continue
        t = infer_expr(ctx, r.value)
        if t:
            inferred.append(t)
        else:
            unknowns += 1
    if unknowns:
        known = {t for t in inferred if t != "None"}
        if len(known) == 1 and not ({"list[Any]", "dict[str, Any]"} & known):
            return ""
        shaped = [r for r in rc.returns if r.value is not None and isinstance(r.value, (ast.Tuple, ast.List))]
        if shaped and len(shaped) == len([r for r in rc.returns if r.value is not None]):
            arity = {len(r.value.elts) for r in shaped}
            arity_n = arity.pop() if arity else 0
            if arity_n >= 2:
                members = [infer_expr(ctx, e) or "Any" for e in shaped[0].value.elts]
                return "tuple[" + ", ".join(members) + "]"
            if arity_n == 1:
                return "tuple[Any, ...]"
        # a `return` we cannot type: give up rather than guess wrongly
        return ""
    typ = join_types(ctx, inferred)
    if typ == "tuple[Any]":
        typ = "tuple[Any, ...]"
    if not typ or typ in ANY_ONLY:
        return ""
    if typ == "bool" and len(rc.returns) > 1:
        flagged = [r for r in rc.returns if r.value is not None and infer_expr(ctx, r.value) == "bool"]
        if len(flagged) < len(rc.returns):
            return ""
    if block_falls_through(func.body) and typ != "None":
        typ = join_types(ctx, [typ, "None"])
    return typ


# --------------------------------------------------------------------------- #
# token level edits
# --------------------------------------------------------------------------- #
SKIP_TOKENS = {
    tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
    tokenize.COMMENT, tokenize.ENCODING, tokenize.ENDMARKER,
}


@dataclass
class Edit:
    line: int
    col: int
    text: str
    kind: str
    detail: str

    def key(self) -> tuple[int, int]:
        return (self.line, self.col)


def _char_at(ctx: FileCtx, line: int, col: int) -> str:
    if 1 <= line <= len(ctx.lines):
        s = ctx.lines[line - 1]
        if 0 <= col < len(s):
            return s[col]
    return ""


def collect_func_edits(ctx: FileCtx, tokens: list, func: ast.FunctionDef | ast.AsyncFunctionDef,
                       edits: list[Edit], in_class: bool) -> None:
    """Emit one edit per missing parameter annotation and one for the return."""
    idx = next(
        (i for i, t in enumerate(tokens)
         if t.type == tokenize.NAME and t.string == "def"
         and t.start[0] == func.lineno and t.start[1] >= func.col_offset),
        -1,
    )
    if idx < 0:
        return
    open_i = next(
        (i for i in range(idx + 1, len(tokens))
         if tokens[i].type == tokenize.OP and tokens[i].string in ("(", ":")),
        -1,
    )
    if open_i < 0 or tokens[open_i].string != "(":
        return
    depth = 0
    segments: list[list] = []
    cur: list = []
    close_tok = None
    for i in range(open_i, len(tokens)):
        tok = tokens[i]
        if tok.type == tokenize.OP and tok.string in "([{":
            depth += 1
            if depth == 1 and i == open_i:
                continue
            cur.append(tok)
            continue
        if tok.type == tokenize.OP and tok.string in ")]}":
            depth -= 1
            if depth == 0:
                if cur:
                    segments.append(cur)
                close_tok = tok
                break
            cur.append(tok)
            continue
        if depth == 1 and tok.type == tokenize.OP and tok.string == ",":
            segments.append(cur)
            cur = []
            continue
        if depth == 1 and tok.type in (tokenize.NL, tokenize.COMMENT):
            continue
        cur.append(tok)
    if close_tok is None:
        return

    a = func.args
    ordered: list[tuple[ast.arg, str]] = [(p, "pos") for p in a.posonlyargs + a.args]
    if a.vararg:
        ordered.append((a.vararg, "vararg"))
    ordered.extend((p, "kwonly") for p in a.kwonlyargs)
    if a.kwarg:
        ordered.append((a.kwarg, "kwarg"))
    defaults: dict[str, ast.AST] = {}
    pos_params = a.posonlyargs + a.args
    for p, d in zip(pos_params[len(pos_params) - len(a.defaults):], a.defaults):
        defaults[p.arg] = d
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        if d is not None:
            defaults[p.arg] = d

    types_for_params: dict[str, str] = {}
    seen: set[str] = set()
    for p, kind in ordered:
        if p.arg in seen:
            continue
        seen.add(p.arg)
        is_first = len(seen) == 1 and kind == "pos"
        dflt = defaults.get(p.arg)
        dflt_repr = {"vararg": "__VARARG__", "kwarg": "__KWARG__"}.get(kind, "")
        if dflt_repr:
            dflt = None
        typ = infer_param_type(ctx, func, p, dflt, dflt_repr, is_first, in_class)
        if typ:
            prefix = {"vararg": "*", "kwarg": "**"}.get(kind, "")
            types_for_params[prefix + p.arg] = typ

    for seg in segments:
        seg_tokens = [t for t in seg if t.type not in SKIP_TOKENS]
        if not seg_tokens or all(t.type == tokenize.OP and t.string in ("*", "/") for t in seg_tokens):
            continue
        j = 1 if seg_tokens[0].type == tokenize.OP and seg_tokens[0].string in ("*", "**") else 0
        if j >= len(seg_tokens) or seg_tokens[j].type != tokenize.NAME:
            continue
        name_tok = seg_tokens[j]
        if any(t.type == tokenize.NAME and t.string == "def" for t in seg_tokens):
            continue
        after = seg_tokens[j + 1:]
        par = next((i for i, t in enumerate(after) if t.type == tokenize.OP and t.string == "("), None)
        if par is not None and not any(t.type == tokenize.OP and t.string in "=,)" for t in after[par + 1:]):
            continue  # `def f(arg)` is the wrapper/call form, not a parameter
        star = seg_tokens[0].string if j else ""
        lookup = star + name_tok.string if star else name_tok.string
        if lookup not in types_for_params:
            continue
        if any(t.type == tokenize.OP and t.string == ":" for t in after):
            continue  # already annotated
        typ = types_for_params[lookup]
        edits.append(Edit(name_tok.end[0], name_tok.end[1], f": {typ}", "param",
                          f"{func.name}({lookup}): {typ}"))
        eq = next((t for t in after if t.type == tokenize.OP and t.string == "="), None)
        if eq is not None:
            if _char_at(ctx, eq.start[0], eq.start[1] - 1) != " ":
                edits.append(Edit(eq.start[0], eq.start[1], " ", "param", f"{func.name}({lookup}) spacing"))
            if _char_at(ctx, eq.end[0], eq.end[1]) not in (" ", "\t", "\n", "\r", ""):
                edits.append(Edit(eq.end[0], eq.end[1], " ", "param", f"{func.name}({lookup}) spacing"))

    if func.returns is None:
        rt = infer_return_type(ctx, func, in_class)
        if rt and not ctx.has_annotation(close_tok.end[0], close_tok.end[1]):
            edits.append(Edit(close_tok.end[0], close_tok.end[1], f" -> {ctx.request(rt)}",
                              "return", f"{func.name} -> {rt}"))


def ctor_heavy(cls_node: ast.ClassDef | None) -> bool:
    """Only classes whose ``__init__`` mostly stores constructor parameters get
    instance-attribute annotations (keeps framework subclasses untouched)."""
    if cls_node is None or any(_base_is_risky(b) for b in cls_node.bases):
        return False
    init = next((n for n in cls_node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
    if init is None:
        return False
    params = {a.arg for a in init.args.args + init.args.kwonlyargs}
    stores = param_stores = 0
    for stmt in ast.walk(init):
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    stores += 1
                    if isinstance(stmt.value, ast.Name) and stmt.value.id in params:
                        param_stores += 1
    return stores > 0 and param_stores / stores >= 0.5


def _base_is_risky(base: ast.AST) -> bool:
    name = ast.unparse(base)
    return any(r in name for r in (
        "NamedTuple", "TypedDict", "Enum", "Protocol", "App", "Widget", "ModalScreen",
        "Static", "Structure", "BaseTransformer", "urwid", "StreamResponse",
        "ScrollableContainer", "str",
    ))


def class_annotated_attrs(cls_node: ast.ClassDef | None) -> set[str]:
    if cls_node is None:
        return set()
    out: set[str] = set()
    for stmt in ast.walk(cls_node):
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, (ast.Name, ast.Attribute)):
                    out.add(getattr(t, "attr", None) or getattr(t, "id", ""))
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            out.add(getattr(target, "attr", None) or getattr(target, "id", ""))
    return out


def collect_attr_edits(ctx: FileCtx, node: ast.Assign, edits: list[Edit], annotated: set[str]) -> None:
    """``self.x = <expr>`` inside ``__init__`` -> ``self.x: T = <expr>``."""
    if len(node.targets) != 1:
        return
    tgt = node.targets[0]
    if not isinstance(tgt, ast.Attribute) or not isinstance(tgt.value, ast.Name) or tgt.value.id != "self":
        return
    if tgt.attr in annotated:
        return
    typ = infer_expr(ctx, node.value)
    if not typ or typ == "None" or "|" in typ or tgt.end_lineno != tgt.lineno:
        return
    edits.append(Edit(tgt.end_lineno, tgt.end_col_offset, f": {ctx.request(typ)}", "attr", f"self.{tgt.attr}: {typ}"))
    annotated.add(tgt.attr)


DUNDER_CONST_OK = {"__version__", "__all__"}


def collect_module_edits(ctx: FileCtx, edits: list[Edit]) -> None:
    for node in ctx.tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        if tgt.id.startswith("__") and tgt.id.endswith("__") and tgt.id not in DUNDER_CONST_OK:
            continue
        typ = infer_expr(ctx, node.value)
        if not typ or typ == "None" or (("|" in typ) and ctx.style != "pep604"):
            continue
        if node.lineno != tgt.lineno:
            continue
        if isinstance(node.value, ast.Call) and not (
            isinstance(node.value.func, ast.Name) and node.value.func.id == "Path" and ctx.has_name("Path")
        ):
            continue
        if typ == "tuple[Any]":
            typ = "tuple[Any, ...]"
        edits.append(Edit(tgt.end_lineno, tgt.end_col_offset, f": {ctx.request(typ)}", "module", f"{tgt.id}: {typ}"))


# --------------------------------------------------------------------------- #
# imports
# --------------------------------------------------------------------------- #
def add_imports(ctx: FileCtx, text: str) -> tuple[str, list[str]]:
    """Add the missing ``typing`` / ``pathlib`` / ``re`` imports."""
    added: list[str] = []
    names = sorted(n for n in ctx.needed if n)
    if not names and not ctx.need_path and not ctx.need_re:
        return text, added
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)

    # 1) a single-line `from typing import ...` can absorb the new names
    typing_line = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "typing" and node.level == 0:
            seg = ast.get_source_segment(text, node) or ""
            if "\n" not in seg and "(" not in seg and " as " not in seg:
                typing_line = node.lineno
                break
    remaining = list(names)
    if typing_line is not None:
        line_text = lines[typing_line - 1]
        m = re.match(r"^from typing import (.+?)(\r?\n)?$", line_text)
        if m:
            existing = [n.strip() for n in m.group(1).split(",") if n.strip()]
            merged = sorted(set(existing) | set(remaining), key=lambda s: (s.lower(), s))
            end = "\r\n" if line_text.endswith("\r\n") else "\n"
            lines[typing_line - 1] = f"from typing import {', '.join(merged)}{end}"
            added.append(f"typing: {', '.join(remaining)}")
            text = "".join(lines)
            remaining = []
            tree = ast.parse(text)
            lines = text.splitlines(keepends=True)

    new_lines: list[str] = []
    if remaining:
        joined = ", ".join(sorted(remaining, key=lambda s: (s.lower(), s)))
        new_lines.append(f"from typing import {joined}\n")
        added.append(f"from typing import {joined}")
    if ctx.need_re and not any(
        isinstance(n, ast.Import) and any(a.name == "re" for a in n.names) for n in ast.walk(tree)
    ):
        new_lines.append("import re\n")
        added.append("import re")
    if ctx.need_path and not any(
        (isinstance(n, ast.ImportFrom) and n.module == "pathlib" and any(a.name == "Path" for a in n.names))
        or (isinstance(n, ast.Import) and any(a.name == "pathlib" for a in n.names))
        for n in ast.walk(tree)
    ) and "class Path" not in text:
        new_lines.append("from pathlib import Path\n")
        added.append("from pathlib import Path")
    if not new_lines:
        return "".join(lines), added

    # 2) the block goes after the last stdlib import, before the third party ones
    insert_line = 0
    last_stdlib_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top = ((node.module if isinstance(node, ast.ImportFrom) else node.names[0].name) or "").split(".")[0]
            if top in STDLIB_MODULES or top == "__future__":
                last_stdlib_end = max(last_stdlib_end, node.end_lineno or node.lineno)
            insert_line = max(insert_line, node.end_lineno or node.lineno)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            insert_line = max(insert_line, node.end_lineno or node.lineno)
        else:
            break
    insert_line = last_stdlib_end or insert_line
    block = "".join(new_lines)
    if insert_line:
        if not lines[insert_line - 1].endswith(("\n", "\r")):
            block = "\n" + block
        lines.insert(insert_line, block)
    else:
        lines[0:0] = new_lines
    return "".join(lines), added


def add_future_import(text: str) -> str:
    """Insert ``from __future__ import annotations`` (after the docstring/shebang)."""
    if "from __future__ import annotations" in text:
        return text
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    insert_after = 0
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) \
            and isinstance(tree.body[0].value.value, str):
        insert_after = tree.body[0].end_lineno
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_after = max(insert_after, node.end_lineno)
    if insert_after:
        lines.insert(insert_after, "\nfrom __future__ import annotations\n")
    else:
        pos = 0
        for i, line in enumerate(lines):
            if line.startswith("#!") or re.match(r"^#.*coding[:=]", line):
                pos = i + 1
            else:
                break
        lines.insert(pos, "from __future__ import annotations\n")
    return "".join(lines)


def annotations_in(text: str) -> set[str]:
    """Names referenced by the annotations of *text*."""
    tree = ast.parse(text)
    used: set[str] = set()

    def add(node: ast.AST | None) -> None:
        if node is None:
            return
        for sub in ast.walk(node):
            root = sub
            if isinstance(sub, ast.Attribute):
                while isinstance(root, ast.Attribute):
                    root = root.value
            if isinstance(root, ast.Name):
                used.add(root.id)
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                for m in re.finditer(r"[A-Za-z_][\w.]*", sub.value):
                    used.add(m.group(0).split(".")[0])

    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            add(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.returns)
        elif isinstance(node, ast.AnnAssign):
            add(node.annotation)
    return used


def repair_imports(text: str) -> tuple[str, list[str]]:
    """Make sure every name used by an annotation is importable."""
    import builtins

    used = annotations_in(text)
    tree = ast.parse(text)
    defined: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                defined.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        defined.add(x.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
    missing = {n for n in used if n not in defined and n in TYPING_NAMES}

    class _Ctx:
        def __init__(self) -> None:
            self.needed = set(missing)
            self.need_path = "Path" in used and "Path" not in defined
            self.need_re = "re" not in defined and any(n.startswith("re.") for n in used)

    if not missing and not _Ctx().need_path and not _Ctx().need_re:
        return text, []
    return add_imports(_Ctx(), text)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# per-file driver
# --------------------------------------------------------------------------- #
def collect_local_types(ctx: FileCtx, func: ast.AST) -> dict[str, str]:
    """``name = <expr>`` locals of one function, typed column-wise first."""
    out: dict[str, str] = {}
    for stmt in ast.walk(func):
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    typ = infer_expr(ctx, stmt.value)
                    if typ and typ != "Any":
                        if t.id not in out:
                            out[t.id] = typ
                        elif out[t.id] != typ:
                            merged = merge_generics(out[t.id], typ)
                            if merged:
                                out[t.id] = merged
                            elif {out[t.id], typ} <= NUMERIC:
                                out[t.id] = "float"
                            else:
                                out[t.id] = "Any"
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.annotation is not None:
            out[stmt.target.id] = ast.unparse(stmt.annotation)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            elem = iterable_element(infer_expr(ctx, stmt.iter))
            if elem:
                for name_node in ast.walk(stmt.target):
                    if isinstance(name_node, ast.Name):
                        out.setdefault(name_node.id, elem)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is None:
                    continue
                typ = infer_with_item(ctx, item.context_expr)
                if typ:
                    for name_node in ast.walk(item.optional_vars):
                        if isinstance(name_node, ast.Name):
                            out.setdefault(name_node.id, typ)
    return out


def infer_with_item(ctx: FileCtx, expr: ast.AST) -> str:
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Name) and expr.func.id == "open":
            return "TextIO"
        if isinstance(expr.func, ast.Attribute) and expr.func.attr == "open":
            return "TextIO"
    typ = infer_expr(ctx, expr)
    return "" if typ.startswith(("list", "dict", "set", "tuple")) else typ


def infer_local_returns(ctx: FileCtx, func: ast.AST) -> dict[str, str]:
    """Return types of the closures defined directly inside *func*."""
    out: dict[str, str] = {}
    for stmt in getattr(func, "body", []):
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is None:
                rt = infer_return_type(ctx, node, False)
                if rt:
                    out.setdefault(node.name, rt)
    return out


def local_method_returns(ctx: FileCtx, cls_node: ast.ClassDef) -> dict[str, str]:
    """Return types declared/inferred for this very class' methods."""
    out: dict[str, str] = {}
    saved_bases, saved_attr = ctx.class_bases, ctx.class_attr_types
    ctx.class_bases = [ast.unparse(b) for b in cls_node.bases]
    for stmt in cls_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.annotation is not None:
            ctx.class_attr_types[stmt.target.id] = ast.unparse(stmt.annotation)
    for stmt in cls_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.returns is not None:
                out[stmt.name] = ast.unparse(stmt.returns)
            else:
                rt = infer_return_type(ctx, stmt, True)
                if rt:
                    out[stmt.name] = rt
    ctx.class_bases, ctx.class_attr_types = saved_bases, saved_attr
    return out


def collect_module_classes(ctx: FileCtx) -> dict[str, set[str]]:
    """class name -> method names defined in this file."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(ctx.tree):
        if isinstance(node, ast.ClassDef):
            out.setdefault(node.name, set()).update(
                s.name for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return out


def collect_all_class_returns(ctx: FileCtx) -> dict[str, dict[str, str]]:
    return {
        node.name: local_method_returns(ctx, node)
        for node in ast.walk(ctx.tree)
        if isinstance(node, ast.ClassDef)
    }


def collect_method_returns(ctx: FileCtx) -> dict[str, str]:
    """Most common inferred return type per method name (for ``self.<m>()`` calls)."""
    seen: dict[str, list[str]] = {}

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                saved = ctx.class_bases
                ctx.class_bases = [ast.unparse(b) for b in node.bases]
                walk(node.body)
                ctx.class_bases = saved
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is None:
                    rt = infer_return_type(ctx, node, True)
                    if rt and rt not in ("None", "Any"):
                        seen.setdefault(node.name, []).append(rt)
                walk([s for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))])

    walk(ctx.tree.body)
    out = {}
    for name, types in seen.items():
        counts: dict[str, int] = {}
        for t in types:
            counts[t] = counts.get(t, 0) + 1
        out[name] = max(counts.items(), key=lambda kv: kv[1])[0]
    return out


def annotate_source(ctx: FileCtx) -> tuple[str, list[Edit], list[str]]:
    """Return the annotated source, the list of edits and the imports added."""
    edits: list[Edit] = []
    tokens = list(tokenize.generate_tokens(io.StringIO(ctx.text).readline))
    collect_module_edits(ctx, edits)

    def handle_func(node: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool,
                    cls_node: ast.ClassDef | None) -> None:
        ctx.func = node
        ctx.param_types = {
            a.arg: ast.unparse(a.annotation)
            for a in ast.walk(node.args)
            if isinstance(a, ast.arg) and a.annotation is not None
        }
        ctx.local_returns = infer_local_returns(ctx, node)
        ctx.class_attr_types = {}
        if cls_node is not None:
            for stmt in cls_node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.annotation is not None:
                    ctx.class_attr_types[stmt.target.id] = ast.unparse(stmt.annotation)
        ctx.local_types = collect_local_types(ctx, node)
        collect_func_edits(ctx, tokens, node, edits, in_class)
        if cls_node is not None and node.name == "__init__" and ctor_heavy(cls_node):
            annotated = class_annotated_attrs(cls_node)
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Assign):
                        collect_attr_edits(ctx, sub, edits, annotated)
        handle_body(node.body, in_class, cls_node)

    def handle_body(body: list[ast.stmt], in_class: bool, cls_node: ast.ClassDef | None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                handle_func(node, in_class, cls_node)
            elif isinstance(node, ast.ClassDef):
                saved_bases, saved_returns = ctx.class_bases, ctx.class_method_returns
                ctx.class_bases = [ast.unparse(b) for b in node.bases]
                ctx.class_method_returns = local_method_returns(ctx, node)
                handle_body(node.body, True, node)
                ctx.class_bases, ctx.class_method_returns = saved_bases, saved_returns
            else:
                for attr in ("body", "orelse", "finalbody"):
                    sub = getattr(node, attr, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                        handle_body(sub, in_class, cls_node)
                for h in getattr(node, "handlers", []) or []:
                    handle_body(h.body, in_class, cls_node)
                for c in getattr(node, "cases", []) or []:
                    handle_body(c.body, in_class, cls_node)

    handle_body(ctx.tree.body, False, None)

    uniq: dict[tuple[int, int], Edit] = {}
    for e in edits:
        if e.key() in uniq and uniq[e.key()].text == e.text:
            continue
        if e.key() in uniq and e.kind != "param":
            continue
        uniq[e.key()] = e
    final = sorted(uniq.values(), key=lambda e: (e.line, e.col), reverse=True)
    lines = ctx.text.splitlines(keepends=True)
    for e in final:
        if 1 <= e.line <= len(lines):
            line = lines[e.line - 1]
            lines[e.line - 1] = line[: e.col] + e.text + line[e.col:]
    text = "".join(lines)
    imports_added: list[str] = []
    if ctx.needed or ctx.need_path or ctx.need_re:
        text, imports_added = add_imports(ctx, text)
    if any("|" in e.text for e in final) and not ctx.future_annotations and ctx.style == "pep604":
        text = add_future_import(text)
        imports_added.append("from __future__ import annotations")
    return text, final, imports_added


def _normalize(tree: ast.AST) -> str:
    """AST dump with every annotation stripped and ``AnnAssign`` folded to ``Assign``."""
    import copy

    tree = copy.deepcopy(tree)
    for n in ast.walk(tree):
        if hasattr(n, "annotation"):
            n.annotation = None  # type: ignore[attr-defined]
        if hasattr(n, "returns"):
            n.returns = None  # type: ignore[attr-defined]
    for parent in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(parent, attr, None)
            if isinstance(body, list) and body:
                out = []
                for stmt in body:
                    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                        out.append(ast.Assign(targets=[stmt.target], value=stmt.value))
                    elif isinstance(stmt, ast.AnnAssign):
                        continue
                    else:
                        out.append(stmt)
                setattr(parent, attr, out)
    tree.body = [s for s in tree.body if not isinstance(s, (ast.Import, ast.ImportFrom))]
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def check_equivalence(old: ast.AST, new_text: str) -> tuple[bool, str]:
    try:
        new = ast.parse(new_text)
    except SyntaxError as e:
        return False, f"syntax error after edit: {e}"
    if _normalize(old) != _normalize(new):
        return False, "AST changed beyond annotations"
    return True, ""


def process_file(path: Path, write: bool) -> dict:
    info: dict = {"file": str(path), "status": "", "edits": [], "imports": [], "error": ""}
    try:
        ctx = load_ctx(path)
    except Exception as e:  # noqa: BLE001 - report, never crash the batch
        info["status"] = "parse-error"
        info["error"] = str(e)
        return info
    try:
        ctx.module_classes = collect_module_classes(ctx)
        ctx.class_method_returns_all = collect_all_class_returns(ctx)
        ctx.method_returns = collect_method_returns(ctx)
        new_text, edits, imports = annotate_source(ctx)
        # second pass: the return types of same-file methods are now known
        ctx.needed, ctx.need_path, ctx.need_re = set(), False, False
        ctx.method_returns = collect_method_returns(ctx)
        new_text2, edits2, imports2 = annotate_source(ctx)
        if len(edits2) > len(edits):
            new_text, edits, imports = new_text2, edits2, imports2
        if edits:
            repaired, extra = repair_imports(new_text)
            if extra and repaired != new_text:
                new_text = repaired
                imports = imports + extra
    except Exception as e:  # noqa: BLE001
        info["status"] = "infer-error"
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    if not edits:
        info["status"] = "no-change"
        return info
    ok, err = check_equivalence(ctx.tree, new_text)
    if not ok:
        info["status"] = "rejected"
        info["error"] = err
        info["edits"] = [f"{e.kind}:{e.detail}" for e in edits]
        return info
    try:
        compile(new_text, str(path), "exec")
    except SyntaxError as e:
        info["status"] = "compile-error"
        info["error"] = str(e)
        return info
    info.update(status="changed", edits=[f"{e.kind}:{e.detail}" for e in edits], imports=imports)
    if write:
        path.write_bytes(new_text.encode(ctx.encoding))
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help=".py files to annotate")
    ap.add_argument("--write", action="store_true", help="write the changes in place")
    ap.add_argument("--json", default="", help="write a JSON report here")
    ap.add_argument("--limit", type=int, default=0, help="only the first N files")
    args = ap.parse_args(argv)
    files = [Path(f) for f in args.files]
    if args.limit:
        files = files[: args.limit]
    results = []
    for f in files:
        r = process_file(f, args.write)
        results.append(r)
        if r["status"] in ("rejected", "compile-error", "infer-error", "parse-error"):
            print(f"!! {f.name}: {r['status']}: {r['error']}", file=sys.stderr)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    changed = sum(1 for r in results if r["status"] == "changed")
    edits = sum(len(r["edits"]) for r in results)
    print(f"processed {len(results)} files, changed {changed}, edits {edits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
