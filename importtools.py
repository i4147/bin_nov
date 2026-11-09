#!/data/data/com.termux/files/usr/bin/env python
"""Python import hygiene toolkit.
Subcommands: ``check-missing`` finds the used names without an import (spec, stdlib or mapped strategy) and can insert them, ``check-position`` flags the imports placed after code or inside a scope, ``add-import`` prepends an import, ``check-load`` imports the files to catch runtime errors, ``find-py2`` detects the Python-2 import styles and ``transform`` rewrites ``import m; m.x`` as ``from m import x``.
Usage: importtools {check-missing,check-position,add-import,check-load,find-py2,transform} [options] Positional arguments: name Module name to import files ...
file Subcommands: check-missing Find missing imports (spec/stdlib/mapped).
check-position Find imports that appear after code / inside scopes.
add-import Prepend 'import NAME' to every .py file.
check-load Import each given file to detect runtime errors.
find-py2 Detect Python-2-style imports via tree-sitter.
transform Rewrite 'import m; m.x' -> 'from m import x'.
Options: -d, --directory --strategy Spec=find_spec (default), stdlib=fixed set, mapped=stdlib + from-import suggestions -a, --autofix -e, --exclude Extra directory name(s) to exclude --deep Use chk_imports.py semantics (nested imports counted, autofix possible).
-o, --output --shebang Shebang to insert when the file has none.
--threshold --no-color Examples: $ importtools check-missing -d src -a Command-line script: run ``python importtools.py`` or the ``importtools`` command.
The module is importable as well."""

import argparse
import ast
from importlib.machinery import SourceFileLoader
from importlib.util import find_spec
from multiprocessing import Pool
import os
from pathlib import Path
import random
import string
import sys
import textwrap
import traceback
from typing import Any, Iterable, Iterator

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger


DEFAULT_SHEBANG: str = "#!/data/data/com.termux/files/usr/bin/python\n"
DEFAULT_JOBS: int = 8
DEFAULT_PY2_THRESHOLD: int = 85
STDLIB_MODULES: frozenset[str] = frozenset({
    "abc",
    "argparse",
    "array",
    "ast",
    "asyncio",
    "base64",
    "bisect",
    "builtins",
    "bz2",
    "calendar",
    "cmath",
    "cmd",
    "code",
    "codecs",
    "codeop",
    "collections",
    "colorsys",
    "compileall",
    "concurrent",
    "configparser",
    "contextlib",
    "contextvars",
    "copy",
    "copyreg",
    "cProfile",
    "crypt",
    "csv",
    "ctypes",
    "curses",
    "dataclasses",
    "datetime",
    "dbm",
    "decimal",
    "difflib",
    "dis",
    "distutils",
    "doctest",
    "email",
    "encodings",
    "ensurepip",
    "enum",
    "errno",
    "faulthandler",
    "fcntl",
    "filecmp",
    "fileinput",
    "fnmatch",
    "fractions",
    "ftplib",
    "functools",
    "gc",
    "getopt",
    "getpass",
    "gettext",
    "glob",
    "graphlib",
    "grp",
    "gzip",
    "hashlib",
    "heapq",
    "hmac",
    "html",
    "http",
    "idlelib",
    "imaplib",
    "imghdr",
    "imp",
    "importlib",
    "inspect",
    "io",
    "ipaddress",
    "itertools",
    "json",
    "keyword",
    "lib2to3",
    "linecache",
    "locale",
    "logging",
    "lzma",
    "mailbox",
    "mailcap",
    "marshal",
    "math",
    "mimetypes",
    "mmap",
    "modulefinder",
    "multiprocessing",
    "netrc",
    "nis",
    "nntplib",
    "numbers",
    "operator",
    "optparse",
    "os",
    "ossaudiodev",
    "parser",
    "pathlib",
    "pdb",
    "pickle",
    "pickletools",
    "pipes",
    "pkgutil",
    "platform",
    "plistlib",
    "poplib",
    "posix",
    "posixpath",
    "pprint",
    "profile",
    "pstats",
    "pty",
    "pwd",
    "py_compile",
    "pyclbr",
    "pydoc",
    "queue",
    "quopri",
    "random",
    "re",
    "readline",
    "reprlib",
    "resource",
    "rlcompleter",
    "runpy",
    "sched",
    "secrets",
    "select",
    "selectors",
    "shelve",
    "shlex",
    "shutil",
    "signal",
    "site",
    "smtpd",
    "smtplib",
    "sndhdr",
    "socket",
    "socketserver",
    "spwd",
    "sqlite3",
    "ssl",
    "stat",
    "statistics",
    "string",
    "stringprep",
    "struct",
    "subprocess",
    "sunau",
    "symtable",
    "sys",
    "sysconfig",
    "syslog",
    "tabnanny",
    "tarfile",
    "telnetlib",
    "tempfile",
    "termios",
    "test",
    "textwrap",
    "threading",
    "time",
    "timeit",
    "tkinter",
    "token",
    "tokenize",
    "trace",
    "traceback",
    "tracemalloc",
    "tty",
    "turtle",
    "turtledemo",
    "types",
    "typing",
    "typing_extensions",
    "unicodedata",
    "unittest",
    "urllib",
    "uu",
    "uuid",
    "venv",
    "warnings",
    "wave",
    "weakref",
    "webbrowser",
    "winreg",
    "winsound",
    "wsgiref",
    "xdrlib",
    "xml",
    "xmlrpc",
    "zipapp",
    "zipfile",
    "zipimport",
    "zlib",
    "zoneinfo",
})
BUILTIN_NAMES: frozenset[str] = frozenset({
    "print",
    "len",
    "range",
    "str",
    "int",
    "float",
    "list",
    "dict",
    "set",
    "tuple",
    "bool",
    "bytes",
    "bytearray",
    "object",
    "type",
    "super",
    "property",
    "classmethod",
    "staticmethod",
    "open",
    "input",
    "enumerate",
    "zip",
    "map",
    "filter",
    "sorted",
    "reversed",
    "sum",
    "min",
    "max",
    "all",
    "any",
    "abs",
    "round",
    "pow",
    "divmod",
    "hex",
    "oct",
    "bin",
    "ord",
    "chr",
    "ascii",
    "repr",
    "format",
    "hash",
    "id",
    "isinstance",
    "issubclass",
    "callable",
    "iter",
    "next",
    "compile",
    "eval",
    "exec",
    "globals",
    "locals",
    "vars",
    "dir",
    "help",
    "getattr",
    "setattr",
    "delattr",
    "hasattr",
    "Exception",
    "BaseException",
    "ValueError",
    "TypeError",
    "RuntimeError",
    "KeyError",
    "IndexError",
    "AttributeError",
    "NameError",
    "IOError",
    "OSError",
    "ImportError",
    "ModuleNotFoundError",
    "StopIteration",
    "GeneratorExit",
    "KeyboardInterrupt",
    "SystemExit",
    "NotImplemented",
    "Ellipsis",
    "None",
    "True",
    "False",
    "__name__",
    "__doc__",
    "__package__",
    "__file__",
    "__cached__",
    "__loader__",
    "__spec__",
    "self",
    "cls",
})
IGNORED_AUTOFIX: frozenset[str] = frozenset({"imp", "cmd", "keyword", "token"})
STDLIB_MEMBER_MAP: dict[str, set[str]] = {
    "os": {
        "path",
        "environ",
        "getenv",
        "listdir",
        "walk",
        "remove",
        "rename",
        "mkdir",
        "makedirs",
        "chdir",
        "getcwd",
        "sep",
        "linesep",
    },
    "sys": {
        "argv",
        "path",
        "stdin",
        "stdout",
        "stderr",
        "exit",
        "version",
        "platform",
        "executable",
        "modules",
    },
    "math": {
        "sqrt",
        "ceil",
        "floor",
        "sin",
        "cos",
        "tan",
        "pi",
        "e",
        "log",
        "log10",
        "exp",
        "pow",
        "fabs",
        "factorial",
    },
    "random": {
        "random",
        "randint",
        "choice",
        "shuffle",
        "sample",
        "uniform",
        "seed",
        "randrange",
    },
    "datetime": {"datetime", "date", "time", "timedelta", "timezone"},
    "json": {"dumps", "loads", "dump", "load"},
    "collections": {"defaultdict", "OrderedDict", "Counter", "deque", "namedtuple"},
    "itertools": {
        "chain",
        "cycle",
        "repeat",
        "count",
        "islice",
        "groupby",
        "combinations",
        "permutations",
        "product",
    },
    "functools": {"reduce", "partial", "lru_cache", "wraps", "cache"},
    "pathlib": {"Path", "PurePath", "PurePosixPath", "PureWindowsPath"},
    "re": {"match", "search", "findall", "sub", "compile", "split", "escape"},
    "argparse": {"ArgumentParser", "Namespace"},
    "logging": {
        "debug",
        "info",
        "warning",
        "error",
        "critical",
        "getLogger",
        "basicConfig",
    },
    "statistics": {"mean", "median", "mode", "stdev", "variance"},
    "typing": {
        "List",
        "Dict",
        "Set",
        "Tuple",
        "Optional",
        "Union",
        "Any",
        "Callable",
        "Iterator",
    },
    "decimal": {"Decimal"},
    "fractions": {"Fraction"},
    "hashlib": {"md5", "sha1", "sha256", "sha512"},
    "subprocess": {"run", "Popen", "call", "check_output"},
    "shutil": {"copy", "copy2", "move", "rmtree", "make_archive"},
    "tempfile": {"NamedTemporaryFile", "TemporaryFile", "mkdtemp"},
    "glob": {"glob"},
    "time": {"time", "sleep", "ctime", "localtime", "gmtime", "strftime", "strptime"},
}


def _make_logger():
    try:
        from loguru import logger as _log

        return _log
    except ImportError:
        return logger


LOG = _make_logger()


def iter_py_files(root: Path, *, skip_dirs: Iterable[str] = SKIP_DIRS) -> Iterator[Path]:
    root = Path(root)
    skip = set(skip_dirs)
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix == ".py" and not p.is_symlink():
                yield p


def parse_file(path: Path) -> ast.Module | None:
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
        return ast.parse(src, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def read_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines), encoding="utf-8")


def collect_names_from_tree(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    imported: set[str] = set()
    assigned: set[str] = set()
    used: set[str] = set()

    def add_target(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            assigned.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for elt in ast.walk(t):
                if isinstance(elt, ast.Name):
                    assigned.add(elt.id)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            for n in node.names:
                if n.name != "*":
                    imported.add(n.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                add_target(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            add_target(node.target)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assigned.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for a in args.args + args.posonlyargs + args.kwonlyargs:
                    assigned.add(a.arg)
                if args.vararg:
                    assigned.add(args.vararg.arg)
                if args.kwarg:
                    assigned.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    return imported, assigned, used


def top_import_block_end(lines: list[str]) -> int:
    end = 0
    in_doc = False
    for i, line in enumerate(lines):
        s = line.strip()
        if in_doc:
            if '"""' in s or "'''" in s:
                in_doc = False
                end = i + 1
            continue
        if not s or s.startswith("#"):
            end = i + 1
            continue
        if s.startswith(('"""', "'''")):
            if s.count('"""') == 2 or s.count("'''") == 2:
                end = i + 1
                continue
            in_doc = True
            continue
        if s.startswith(("import ", "from ")):
            end = i + 1
            continue
        break
    return end


def insert_imports(path: Path, imports: list[str]) -> bool:
    src = read_source(path)
    if src is None:
        return False
    lines = src.split("\n")
    pos = top_import_block_end(lines)
    block = [f"{imp}\n" if not imp.endswith("\n") else imp for imp in imports]
    new = "\n".join(lines[:pos] + [imp.rstrip("\n") for imp in imports] + lines[pos:])
    path.write_text(new, encoding="utf-8")
    return True


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _detect_missing(path: Path, strategy: str) -> list[str]:
    tree = parse_file(path)
    if tree is None:
        return []
    imported, assigned, used = collect_names_from_tree(tree)
    suggestions: list[str] = []
    seen_modules: set[str] = set()
    for name in sorted(used):
        if name in imported or name in assigned:
            continue
        if name in BUILTIN_NAMES or name in IGNORED_AUTOFIX:
            continue
        if name.startswith("_"):
            continue
        if strategy == "spec":
            try:
                if find_spec(name) is not None:
                    suggestions.append(f"import {name}")
            except (ImportError, ModuleNotFoundError, ValueError):
                pass
        elif strategy == "stdlib":
            if name in STDLIB_MODULES:
                suggestions.append(f"import {name}")
        elif strategy == "mapped":
            if name in STDLIB_MODULES:
                suggestions.append(f"import {name}")
            else:
                for module, members in STDLIB_MEMBER_MAP.items():
                    if name in members and module not in imported:
                        suggestions.append(f"from {module} import {name}")
                        break
    out: list[str] = []
    seen: set[str] = set()
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _fix_missing(path: Path, missing: list[str]) -> bool:
    if not missing:
        return False
    return insert_imports(path, missing)


def _worker_check_missing(args: tuple[Path, str]) -> tuple[Path, list[str]]:
    path, strategy = args
    return path, _detect_missing(path, strategy)


def cmd_check_missing(ns: argparse.Namespace) -> int:
    root: Path = ns.directory.resolve()
    if not root.is_dir():
        LOG.error(f"Not a directory: {root}")
        return 1
    files = list(iter_py_files(root, SKIP_DIRS - set(ns.exclude or set())))
    if not files:
        print("No Python files found.")
        return 0
    print(f"Scanning {len(files)} Python file(s) with {MAX_WORKERS} worker(s) (strategy={ns.strategy})...")
    with Pool(processes=MAX_WORKERS) as pool:
        results = pool.map(_worker_check_missing, [(f, ns.strategy) for f in files])
    total = 0
    fixed = 0
    for path, missing in results:
        if not missing:
            continue
        total += len(missing)
        print(f"\n{rel(path, root)}:")
        for imp in missing:
            print(f"  - {imp}")
        if ns.autofix:
            if _fix_missing(path, missing):
                print("  ✓ Fixed")
                fixed += 1
            else:
                print("  ✗ Failed to fix")
    print("\n" + "=" * 40)
    print(f"Total missing imports: {total}")
    if ns.autofix:
        print(f"Files fixed: {fixed}")
    return 1 if total and not ns.autofix else 0


def _position_basic(path: Path) -> list[str]:
    src = read_source(path)
    if src is None:
        return []
    tree = parse_file(path)
    if tree is None:
        return []
    seen_non_import = False
    offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if seen_non_import:
                offenders.append(f"Line {node.lineno}: import after code")
            continue
        seen_non_import = True
    return offenders


class _ParentMapper(ast.NodeVisitor):
    def __init__(self) -> None:
        self.parents: dict[ast.AST, ast.AST] = {}

    def visit(self, node: ast.AST) -> Any:
        for child in ast.iter_child_nodes(node):
            self.parents[child] = node
        return super().visit(node)


_NESTING_NODES = (
    ast.Try,
    ast.If,
    ast.With,
    ast.AsyncWith,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.For,
    ast.AsyncFor,
    ast.While,
)


def _is_nested(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur: ast.AST | None = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, _NESTING_NODES):
            return True
    return False


def _position_deep(path: Path) -> list[tuple[int, int, str]]:
    src = read_source(path)
    if src is None:
        return []
    tree = parse_file(path)
    if tree is None:
        return []
    mapper = _ParentMapper()
    mapper.visit(tree)
    parents = mapper.parents
    lines = src.split("\n")
    head = top_import_block_end(lines)
    offenders: list[tuple[int, int, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if node.lineno <= head:
            continue
        if _is_nested(node, parents):
            continue
        end = node.end_lineno or node.lineno
        text = "\n".join(lines[node.lineno - 1 : end])
        offenders.append((node.lineno, end, text))
    return offenders


def _autofix_position(path: Path, offenders: list[tuple[int, int, str]]) -> bool:
    src = read_source(path)
    if src is None:
        return False
    lines = src.split("\n")
    imports_text = [t for _, _, t in offenders]
    for start, end, _ in sorted(offenders, reverse=True):
        del lines[start - 1 : end]
    pos = top_import_block_end(lines)
    new = lines[:pos] + imports_text + [""] + lines[pos:]
    path.write_text("\n".join(new), encoding="utf-8")
    return True


def cmd_check_position(ns: argparse.Namespace) -> int:
    root: Path = ns.directory.resolve()
    if not root.exists():
        LOG.error(f"Does not exist: {root}")
        return 1
    files = list(iter_py_files(root))
    if not files:
        print("No Python files found.")
        return 0
    scanned = offenders_files = fixed = 0
    report: list[str] = []
    for path in files:
        if ns.deep:
            offenders = _position_deep(path)
            details = [f"  Line {s}: {t.strip().splitlines()[0]}" for s, _, t in offenders]
        else:
            offenders = _position_basic(path)
            details = [f"  {d}" for d in offenders]
        if not offenders:
            continue
        offenders_files += 1
        scanned += len(offenders)
        relpath = rel(path, root)
        print(f"\n{relpath}:")
        for d in details:
            print(d)
        report.append(f"File: {relpath}\n" + "\n".join(details))
        if ns.autofix and ns.deep and _autofix_position(path, offenders):
            fixed += 1
            print(f"  [FIXED] moved {len(offenders)} import(s)")
    print("\n" + "=" * 40)
    print(f"Files with misplaced imports: {offenders_files}")
    print(f"Total misplaced imports: {scanned}")
    if ns.autofix:
        print(f"Files fixed: {fixed}")
    if ns.output:
        out = Path(ns.output)
        if report:
            out.write_text("\n\n".join(report) + "\n", encoding="utf-8")
        else:
            out.write_text("No misplaced imports found!\n", encoding="utf-8")
        print(f"Report saved to: {out}")
    return 1 if offenders_files and not ns.autofix else 0


def _add_import_to_file(path: Path, name: str, shebang: str) -> None:
    if not path.exists() or path.is_symlink():
        return
    print(f"processing {path}")
    src = read_source(path)
    if src is None:
        return
    lines = src.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        new = [lines[0], f"import {name}\n"] + lines[1:]
    else:
        new = [shebang, f"import {name}\n"] + lines
    path.write_text("".join(new), encoding="utf-8")


def cmd_add_import(ns: argparse.Namespace) -> int:
    root: Path = ns.directory.resolve()
    for p in iter_py_files(root):
        _add_import_to_file(p, ns.name, ns.shebang)
    return 0


def cmd_check_load(ns: argparse.Namespace) -> int:
    had_error = False
    for p in ns.files:
        try:
            alias = "".join(random.choice(string.ascii_letters) for _ in range(20))
            SourceFileLoader(alias, p).load_module()
        except Exception:
            had_error = True
            print(p)
            traceback.print_exc()
            print()
    return 1 if had_error else 0


def _cprint(text: str, color: str = "cyan", *, enabled: bool = True) -> None:
    if not enabled:
        print(text)
        return
    palette = {
        "cyan": "\033[36m",
        "yellow": "\033[33m",
        "green": "\033[32m",
        "red": "\033[31m",
    }
    code = palette.get(color, "")
    reset = "\033[0m" if code else ""
    print(f"{code}{text}{reset}")


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def cmd_find_py2(ns: argparse.Namespace) -> int:
    try:
        from rapidfuzz import fuzz
        from tree_sitter import Language, Parser
        import tree_sitter_python as tsp
    except ImportError as exc:
        LOG.error(f"find-py2 needs tree_sitter, tree_sitter_python, rapidfuzz: {exc}")
        return 2
    root: Path = ns.directory.resolve()
    parser = Parser()
    parser.language = Language(tsp.language())
    import_node_types = {"import_statement", "import_from_statement"}
    stdlib_lc = {m.lower() for m in STDLIB_MODULES}
    stdlib_list = sorted(stdlib_lc)
    for path in iter_py_files(root):
        if path.is_symlink():
            continue
        try:
            src = path.read_bytes()
        except OSError:
            continue
        tree = parser.parse(src)
        found: list[str] = []
        for node in tree.root_node.children:
            if node.type not in import_node_types:
                continue
            text = src[node.start_byte : node.end_byte].decode(errors="ignore")
            name = _extract_module_name(text)
            if name and not name.startswith("_") and name not in found:
                found.append(name)
        for name in sorted(set(found)):
            low = name.lower()
            if low in stdlib_lc and low not in {"io", "os", "pathlib", "ast", "urllib"}:
                _cprint(rel(path, root), "cyan", enabled=not ns.no_color)
                continue
            for candidate in stdlib_list:
                score = fuzz.ratio(low, candidate)
                if (
                    score > ns.threshold
                    and len(low) > 3
                    and len(candidate) > 3
                    and low
                    not in {
                        "io",
                        "os",
                        "pathlib",
                        "urllib",
                        "tkinter",
                        "pickle",
                        "string",
                        "queue",
                        "urllib3",
                        "configparser",
                        "copyreg",
                        "httplib2",
                    }
                ):
                    _cprint(rel(path, root), "yellow", enabled=not ns.no_color)
                    _cprint(
                        f"{low} / {candidate} / {score}",
                        "green",
                        enabled=not ns.no_color,
                    )
                    break
    return 0


def _extract_module_name(text: str) -> str:
    text = text.strip()
    if text.startswith("import "):
        name = text[len("import ") :]
    elif text.startswith("from "):
        name = text[len("from ") :]
    else:
        return ""
    if name.startswith("."):
        return ""
    for sep in (" as ", ".", " import"):
        if sep in name:
            name = name.split(sep)[0]
    return name.strip()


class _ImportTransformer(ast.NodeTransformer):
    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.module_to_names: dict[str, set[str]] = {}
        self.modified = False
        self._analyze()

    def _analyze(self) -> None:
        bare_modules: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not alias.asname:
                        bare_modules.add(alias.name)
        if not bare_modules:
            return
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bare_modules:
                self.module_to_names.setdefault(node.value.id, set()).add(node.attr)

    def visit_Import(self, node: ast.Import) -> Any:
        new_nodes: list[ast.stmt] = []
        for alias in node.names:
            if not alias.asname and alias.name in self.module_to_names:
                self.modified = True
                names = sorted(self.module_to_names[alias.name])
                new_nodes.append(
                    ast.ImportFrom(
                        module=alias.name,
                        names=[ast.alias(name=n, asname=None) for n in names],
                        level=0,
                    )
                )
            else:
                new_nodes.append(ast.Import(names=[alias]))
        if not new_nodes:
            return None
        return new_nodes if len(new_nodes) > 1 else new_nodes[0]

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if isinstance(node.value, ast.Name) and node.value.id in self.module_to_names:
            self.modified = True
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return self.generic_visit(node)


def cmd_transform(ns: argparse.Namespace) -> int:
    path = Path(ns.file)
    if not path.exists() or path.suffix != ".py":
        print(f"Error: invalid Python file '{path}'", file=sys.stderr)
        return 1
    src = read_source(path)
    if src is None:
        print(f"Error: cannot read '{path}'", file=sys.stderr)
        return 1
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"Error: syntax error: {exc}", file=sys.stderr)
        return 1
    transformer = _ImportTransformer(tree)
    new_tree = transformer.visit(tree)
    if transformer.modified:
        ast.fix_missing_locations(new_tree)
        path.write_text(ast.unparse(new_tree), encoding="utf-8")
        print(f"✓ Successfully transformed imports in '{path}'.")
    else:
        print(f"No transformations needed for '{path}'.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="import_tools.py",
        description="Unified Python import hygiene toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Mapping from original scripts:
          addimport.py              ->  add-import NAME
          check4missing_imports.py  ->  check-missing --strategy spec
          find_missing_imports.py   ->  check-missing --strategy stdlib
          fix_stdlib_imports.py     ->  check-missing --strategy mapped
          chk_imports.py            ->  check-position --deep [--autofix]
          imdetector.py             ->  check-position
          check_imports.py          ->  check-load FILES...
          find_py2_imports.py       ->  find-py2
          transformimports.py       ->  transform FILE
        """),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    cm = sub.add_parser("check-missing", help="Find missing imports (spec/stdlib/mapped).")
    cm.add_argument("-d", "--directory", type=Path, default=Path.cwd())
    cm.add_argument(
        "--strategy",
        choices=("spec", "stdlib", "mapped"),
        default="spec",
        help="spec=find_spec (default), stdlib=fixed set, mapped=stdlib + from-import suggestions",
    )
    cm.add_argument("-a", "--autofix", action="store_true")
    cm.add_argument(
        "-e",
        "--exclude",
        action="append",
        default=[],
        help="Extra directory name(s) to exclude",
    )
    cm.set_defaults(func=cmd_check_missing)
    cp = sub.add_parser("check-position", help="Find imports that appear after code / inside scopes.")
    cp.add_argument("-d", "--directory", type=Path, default=Path.cwd())
    cp.add_argument(
        "--deep",
        action="store_true",
        help="Use chk_imports.py semantics (nested imports counted, autofix possible).",
    )
    cp.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Move misplaced imports to top (requires --deep).",
    )
    cp.add_argument("-o", "--output", type=str, default=None)
    cp.set_defaults(func=cmd_check_position)
    ai = sub.add_parser("add-import", help="Prepend 'import NAME' to every .py file.")
    ai.add_argument("name", help="Module name to import")
    ai.add_argument("-d", "--directory", type=Path, default=Path.cwd())
    ai.add_argument(
        "--shebang",
        default=DEFAULT_SHEBANG,
        help="Shebang to insert when the file has none.",
    )
    ai.set_defaults(func=cmd_add_import)
    cl = sub.add_parser("check-load", help="Import each given file to detect runtime errors.")
    cl.add_argument("files", nargs="+")
    cl.set_defaults(func=cmd_check_load)
    fp = sub.add_parser("find-py2", help="Detect Python-2-style imports via tree-sitter.")
    fp.add_argument("-d", "--directory", type=Path, default=Path.cwd())
    fp.add_argument("--threshold", type=int, default=DEFAULT_PY2_THRESHOLD)
    fp.add_argument("--no-color", action="store_true")
    fp.set_defaults(func=cmd_find_py2)
    tr = sub.add_parser("transform", help="Rewrite 'import m; m.x' -> 'from m import x'.")
    tr.add_argument("file")
    tr.set_defaults(func=cmd_transform)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
