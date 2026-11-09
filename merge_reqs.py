#!/data/data/com.termux/files/usr/bin/env python
"""Generate a ``requirements.txt`` from a project tree.
The ``scan`` subcommand collects the imports (ast or regex, notebooks and archives included), resolves them against the stdlib, the local modules and a package mapping and writes the requirements; ``metadata`` extracts the ``Requires-Dist`` lines of the installed ``METADATA`` files.
A file cache and a pip package list keep the runs fast.
Usage: merge_reqs {scan,metadata} [-d DIR] [-o requirements.txt] [--pip-file PATH] Subcommands: scan Scan a tree for imports and write requirements.
metadata Extract Requires-Dist lines from METADATA files.
Options: --ignore Directories to skip (default: common VCS/build/cache dirs) -v, --verbose Verbose logging -d, --directory Root directory (default: .) -o, --output Output file (default: requirements.txt) -p, --pip-file -m, --mapping Module=package mapping file (optional) --stdlib-file Extra stdlib names file (optional) --stdlib-source Where to get stdlib list (default: both) --extractor Import extraction strategy (default: ast) --include-archives Scan .zip/.whl/.tar.* archives (default: on) ...
plus 14 more; run ``merge_reqs --help`` for the full list.
Examples: $ merge_reqs scan -d .
-o requirements.txt Command-line script: run ``python merge_reqs.py`` or the ``merge_reqs`` command.
The module is importable as well."""

import argparse
import ast
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from typing import Any, Iterable, Iterator, Optional, Sequence, TextIO
import zipfile

from dh import MAX_WORKERS


try:
    from tqdm import tqdm as _tqdm
except Exception:

    def _tqdm(iterable=None, **kwargs: Any):
        return iterable if iterable is not None else []


def _hash_bytes(data: bytes) -> str:
    try:
        import xxhash

        return xxhash.xxh64(data).hexdigest()
    except Exception:
        return hashlib.sha256(data).hexdigest()


DEFAULT_PIP_FILE: str = "/sdcard/data/pip.txt"
DEFAULT_CACHE_FILE: str = ".reqcache.json"
DEFAULT_IGNORE: list[str] = [
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    ".tox",
    ".eggs",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "build",
    "dist",
    "site-packages",
]
ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".zip",
    ".whl",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".tar.bz2",
    ".tar.zst",
)
_STDLIB_FALLBACK: set[str] = {
    "abc",
    "aifc",
    "argparse",
    "array",
    "ast",
    "asynchat",
    "asyncio",
    "asyncore",
    "atexit",
    "audioop",
    "base64",
    "bdb",
    "binascii",
    "binhex",
    "bisect",
    "builtins",
    "bz2",
    "cProfile",
    "calendar",
    "cgi",
    "cgitb",
    "chunk",
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
    "dummy_thread",
    "dummy_threading",
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
    "formatter",
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
    "msilib",
    "msvcrt",
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
    "symbol",
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
    "tomllib",
    "trace",
    "traceback",
    "tracemalloc",
    "tty",
    "turtle",
    "turtledemo",
    "types",
    "typing",
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
    "__future__",
    "__main__",
}
_BLOCKLIST: set[str] = {
    "pip",
    "setuptools",
    "wheel",
    "distribute",
    "easy_install",
    "apt",
    "apt_pkg",
    "gi",
    "dbus",
    "__future__",
    "__main__",
    "__init__",
}


def norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def is_valid_module_name(name: str) -> bool:
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return False
    if name.startswith("__") and name.endswith("__"):
        return False
    return norm(name) not in {norm(b) for b in _BLOCKLIST}


def load_pip_packages(path: str | os.PathLike | None) -> set[str]:
    out: set[str] = set()
    if not path:
        return out
    p = Path(path)
    if not p.exists():
        print(f"[!] pip list not found: {p}", file=sys.stderr)
        return out
    with p.open(encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            head = re.split(r"[=!<>;\[\s@]", line, maxsplit=1)[0].strip()
            if head:
                out.add(norm(head))
    print(f"[i] Loaded {len(out)} packages from {p}")
    return out


def load_mapping(path: str | os.PathLike | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path:
        return out
    p = Path(path)
    if not p.exists():
        print(f"[!] mapping file not found: {p}", file=sys.stderr)
        return out
    with p.open(encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            mod, pkg = line.split("=", 1)
            out[norm(mod)] = pkg.strip()
    print(f"[i] Loaded {len(out)} module->package mappings")
    return out


def load_stdlib(source: str, extra_file: str | os.PathLike | None) -> set[str]:
    mods: set[str] = set()
    if source in ("embedded", "both"):
        mods |= _STDLIB_FALLBACK
    if source in ("python", "both"):
        mods |= set(sys.builtin_module_names)
        mods |= set(getattr(sys, "stdlib_module_names", ()))
    if extra_file:
        p = Path(extra_file)
        if p.exists():
            with p.open(encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        mods.add(line.split(".", 1)[0])
    return mods


_SHEBANG_RE = re.compile(r"^#!.*python", re.IGNORECASE)


def looks_like_python_script(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    first = head.split(b"\n", 1)[0].decode("utf-8", "ignore")
    if _SHEBANG_RE.match(first):
        return True
    return bool(re.search(rb"\b(import|from)\b", head))


def iter_candidate_files(
    root: str | os.PathLike,
    ignore: Iterable[str],
    include_archives: bool = True,
    include_notebooks: bool = True,
) -> Iterator[Path]:
    ignore_set = set(ignore)
    root_path = Path(root)
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in ignore_set and not d.endswith(".dist-info")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            low = name.lower()
            if low.endswith((".py", ".pyw")):
                yield p
            elif include_notebooks and low.endswith(".ipynb"):
                yield p
            elif include_archives and low.endswith(ARCHIVE_SUFFIXES):
                yield p
            elif p.suffix == "" and looks_like_python_script(p):
                yield p


def detect_local_modules(root: str | os.PathLike, ignore: Iterable[str]) -> set[str]:
    local: set[str] = set()
    ignore_set = set(ignore)
    for dirpath, dirnames, filenames in os.walk(Path(root)):
        dirnames[:] = [d for d in dirnames if d not in ignore_set and not d.endswith(".dist-info")]
        if "__init__.py" in filenames:
            local.add(Path(dirpath).name)
        for name in filenames:
            if name.endswith((".py", ".pyw")) and name != "__init__.py":
                local.add(Path(name).stem)
    return {n for n in local if n}


_EMPTY_RESULT = lambda: {
    "imports": set(),
    "star_modules": set(),
    "dynamic": set(),
    "relative": set(),
}
_IMP_RE = re.compile(r"^\s*import\s+([A-Za-z0-9_.*\s,]+)")
_FROM_RE = re.compile(r"^\s*from\s+([A-Za-z0-9_.]+)\s+import")
_DYN_RE = re.compile(r'(?:import_module|__import__)\(\s*[\'"]([\w.]+)[\'"]\s*\)')


def extract_ast(source: str) -> dict[str, set[str]]:
    res = _EMPTY_RESULT()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        for m in _DYN_RE.finditer(source):
            res["dynamic"].add(m.group(1).split(".", 1)[0])
        return res
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                res["imports"].add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                res["relative"].add(node.module.split(".", 1)[0] if node.module else ".")
                continue
            if node.module:
                if any(a.name == "*" for a in node.names):
                    res["star_modules"].add(node.module)
                else:
                    res["imports"].add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Name)
                and fn.id == "__import__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                res["dynamic"].add(node.args[0].value.split(".", 1)[0])
            elif (
                isinstance(fn, ast.Attribute)
                and fn.attr == "import_module"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "importlib"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                res["dynamic"].add(node.args[0].value.split(".", 1)[0])
    return res


def extract_regex(source: str) -> dict[str, set[str]]:
    res = _EMPTY_RESULT()
    for line in source.splitlines():
        line = line.split("#", 1)[0].strip()
        m = _IMP_RE.match(line)
        if m:
            for part in m.group(1).split(","):
                part = part.strip().split()[0] if part.strip() else ""
                if part and part != "*":
                    res["imports"].add(part.split(".", 1)[0])
        m = _FROM_RE.match(line)
        if m:
            mod = m.group(1).strip()
            if mod and mod != "__future__":
                res["imports"].add(mod.split(".", 1)[0])
    return res


def extract_imports(source: str, extractor: str = "ast") -> dict[str, set[str]]:
    if extractor == "regex":
        return extract_regex(source)
    return extract_ast(source)


def _merge_into(dst: dict[str, set[str]], src: dict[str, set[str]]) -> None:
    for k in dst:
        dst[k] |= src.get(k, set())


def _extract_from_notebook(text: str, extractor: str) -> dict[str, set[str]]:
    res = _EMPTY_RESULT()
    try:
        nb = json.loads(text)
    except json.JSONDecodeError:
        return res
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source", []))
        _merge_into(res, extract_imports(src, extractor))
    return res


def _extract_from_zip(path: Path, extractor: str) -> dict[str, set[str]]:
    res = _EMPTY_RESULT()
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.namelist():
                if not info.endswith((".py", ".pyw")):
                    continue
                try:
                    text = zf.read(info).decode("utf-8", "ignore")
                except Exception:
                    continue
                _merge_into(res, extract_imports(text, extractor))
    except Exception:
        pass
    return res


def _extract_from_tar(path: Path, extractor: str) -> dict[str, set[str]]:
    res = _EMPTY_RESULT()
    low = path.name.lower()
    if low.endswith(".tar.zst"):
        try:
            import zstandard
        except ImportError:
            print(f"[!] skipping {path}: zstandard not installed", file=sys.stderr)
            return res
        try:
            with path.open("rb") as fh:
                dctx = zstandard.ZstdDecompressor()
                with dctx.stream_reader(fh) as reader, tarfile.open(fileobj=reader, mode="r|") as tf:
                    for m in tf:
                        if m.isfile() and m.name.endswith((".py", ".pyw")):
                            f = tf.extractfile(m)
                            if not f:
                                continue
                            _merge_into(
                                res,
                                extract_imports(
                                    f.read().decode("utf-8", "ignore"),
                                    extractor,
                                ),
                            )
        except Exception:
            pass
        return res
    try:
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                if m.isfile() and m.name.endswith((".py", ".pyw")):
                    f = tf.extractfile(m)
                    if not f:
                        continue
                    _merge_into(
                        res,
                        extract_imports(f.read().decode("utf-8", "ignore"), extractor),
                    )
    except Exception:
        pass
    return res


def process_path(path: str | os.PathLike, extractor: str = "ast") -> dict[str, set[str]]:
    p = Path(path)
    low = p.name.lower()
    if p.suffix in (".py", ".pyw") or (p.suffix == "" and p.is_file()):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return _EMPTY_RESULT()
        return extract_imports(text, extractor)
    if low.endswith(".ipynb"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return _EMPTY_RESULT()
        return _extract_from_notebook(text, extractor)
    if low.endswith((".zip", ".whl")):
        return _extract_from_zip(p, extractor)
    if low.endswith(ARCHIVE_SUFFIXES):
        return _extract_from_tar(p, extractor)
    return _EMPTY_RESULT()


def _file_cache_key(path: Path) -> tuple[float, str]:
    try:
        st = path.stat()
        with path.open("rb") as f:
            head = f.read(65536)
        return st.st_mtime, _hash_bytes(head + str(st.st_size).encode())
    except OSError:
        return 0.0, "0"


def load_cache(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except OSError:
        pass


def _result_to_json(res: dict[str, set[str]]) -> dict[str, list[str]]:
    return {k: sorted(v) for k, v in res.items()}


def _result_from_json(obj: dict[str, list[str]]) -> dict[str, set[str]]:
    return {k: set(v) for k, v in obj.items()}


_WORKER_STATE: dict[str, Any] = {}


def _init_worker(extractor: str, cache_file: Optional[str]) -> None:
    _WORKER_STATE["extractor"] = extractor
    _WORKER_STATE["cache_file"] = cache_file
    if cache_file and Path(cache_file).exists():
        _WORKER_STATE["cache"] = load_cache(Path(cache_file))
    else:
        _WORKER_STATE["cache"] = {}


def _worker_one(path_str: str) -> tuple[str, dict[str, set[str]]]:
    extractor = _WORKER_STATE.get("extractor", "ast")
    cache = _WORKER_STATE.get("cache") or {}
    p = Path(path_str)
    key = str(p.resolve())
    if cache:
        entry = cache.get(key)
        if entry:
            try:
                mtime, h = _file_cache_key(p)
                if entry.get("mtime") == mtime and entry.get("hash") == h:
                    return path_str, _result_from_json(entry.get("result", {}))
            except Exception:
                pass
    return path_str, process_path(p, extractor)


def filter_packages(
    imports: set[str],
    stdlib: set[str],
    local: set[str],
    pip_pkgs: set[str],
    mapping: dict[str, str],
    installed: Optional[set[str]],
    include_unknown: bool,
) -> set[str]:
    out: set[str] = set()
    stdlib_n = {norm(s) for s in stdlib}
    local_n = {norm(l) for l in local}
    pip_n = {norm(p) for p in pip_pkgs}
    installed_n = {norm(p) for p in installed} if installed else None
    for imp in imports:
        if not imp or not is_valid_module_name(imp):
            continue
        n = norm(imp)
        if n in stdlib_n or imp in stdlib:
            continue
        if n in local_n or imp in local:
            continue
        if installed_n and n in installed_n:
            continue
        target = mapping.get(n) or mapping.get(imp)
        cand_n = norm(target) if target else n
        if pip_n:
            if cand_n in pip_n or n in pip_n:
                out.add(target or imp)
            elif include_unknown:
                out.add(target or imp)
        else:
            out.add(target or imp)
    return out


def run_scan(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.exists():
        print(f"[!] directory not found: {root}", file=sys.stderr)
        return 2
    print("[i] Loading resources...")
    pip_pkgs = load_pip_packages(args.pip_file)
    mapping = load_mapping(args.mapping)
    stdlib = load_stdlib(args.stdlib_source, args.stdlib_file)
    print(f"[i] Scanning {root} (stdlib={len(stdlib)} modules)")
    files = list(
        iter_candidate_files(
            root,
            ignore=args.ignore,
            include_archives=args.include_archives,
            include_notebooks=args.include_notebooks,
        )
    )
    print(f"[i] Found {len(files)} candidate files")
    if not files:
        print("[!] nothing to scan")
        return 0
    local = detect_local_modules(root, args.ignore)
    print(f"[i] Detected {len(local)} local modules")
    installed: Optional[set[str]] = None
    if args.check_installed:
        installed = _pip_freeze(args.pip_cmd)
        if installed is not None:
            print(f"[i] {len(installed)} packages installed via `{args.pip_cmd} freeze`")
    cache: dict[str, Any] = {}
    cache_path = Path(args.cache) if args.cache else None
    if cache_path and not args.no_cache and cache_path.exists():
        cache = load_cache(cache_path)
        print(f"[i] Loaded cache ({len(cache)} entries)")
    results: list[tuple[str, dict[str, set[str]]]] = []
    workers = max(1, MAX_WORKERS)
    if workers > 1 and len(files) > 1:
        with mp.Pool(
            workers,
            initializer=_init_worker,
            initargs=(
                args.extractor,
                str(cache_path) if cache_path and not args.no_cache else None,
            ),
        ) as pool:
            it = pool.imap_unordered(_worker_one, [str(f) for f in files])
            for item in _tqdm(it, total=len(files), desc="Processing"):
                results.append(item)
    else:
        _init_worker(args.extractor, str(cache_path) if cache_path else None)
        for f in _tqdm(files, desc="Processing"):
            results.append(_worker_one(str(f)))
    all_imports: set[str] = set()
    all_relative: set[str] = set()
    for _, res in results:
        all_imports |= res.get("imports", set())
        all_imports |= {d.split(".", 1)[0] for d in res.get("dynamic", set())}
        all_relative |= res.get("relative", set())
    print(f"[i] {len(all_imports)} unique imports found")
    filtered = filter_packages(
        imports=all_imports - all_relative,
        stdlib=stdlib,
        local=local,
        pip_pkgs=pip_pkgs,
        mapping=mapping,
        installed=installed,
        include_unknown=args.include_unknown,
    )
    if args.format == "flat":
        flat = sorted(all_imports - all_relative, key=str.lower)
        _write_lines(args.output, flat)
        print(f"[✓] Wrote {len(flat)} imports to {args.output} (flat)")
    else:
        pkgs = sorted(filtered, key=str.lower)
        if args.dry_run:
            print(f"[dry-run] Would write {len(pkgs)} packages:")
            for p in pkgs:
                print("   ", p)
        else:
            _write_lines(args.output, pkgs)
            print(f"[✓] Wrote {len(pkgs)} packages to {args.output}")
    if cache_path and not args.no_cache:
        for path_str, res in results:
            p = Path(path_str)
            try:
                mtime, h = _file_cache_key(p)
            except Exception:
                mtime, h = 0.0, "0"
            cache[str(p.resolve())] = {
                "mtime": mtime,
                "hash": h,
                "result": _result_to_json(res),
            }
        save_cache(cache_path, cache)
    return 0


def _write_lines(path: str | os.PathLike, lines: Sequence[str]) -> None:
    with Path(path).open("w", encoding="utf-8") as f:
        f.writelines(f"{line}\n" for line in lines)


def _pip_freeze(pip_cmd: str) -> Optional[set[str]]:
    try:
        proc = subprocess.run(
            [pip_cmd, "freeze"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except FileNotFoundError:
        print(f"[!] `{pip_cmd}` not found; skipping installed check", file=sys.stderr)
        return None
    out: set[str] = set()
    for raw in proc.stdout.splitlines():
        line = raw.decode("utf-8", "ignore").strip()
        if not line or line.startswith(("#", "-e")):
            continue
        name = line.split("==")[0].split("@")[0].split("[")[0].strip()
        if name:
            out.add(norm(name))
    return out


_METADATA_RE = re.compile(r"^Requires-Dist:\s*([^\s;]+)")


def _parse_metadata(path: Path) -> list[str]:
    reqs: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = _METADATA_RE.match(line)
                if m:
                    reqs.append(m.group(1))
    except OSError:
        pass
    return reqs


def run_metadata(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.exists():
        print(f"[!] directory not found: {root}", file=sys.stderr)
        return 2
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in set(args.ignore)]
        for name in filenames:
            if name == args.metadata_name:
                found.extend(_parse_metadata(Path(dirpath) / name))
    if not found:
        print("No dependencies found in METADATA files.")
        return 0
    out = Path(args.output)
    mode = "a" if args.append else "w"
    with out.open(mode, encoding="utf-8") as f:
        for r in found:
            f.write(r + "\n")
    print(f"[✓] {len(found)} requirements {'appended to' if args.append else 'written to'} {out}")
    return 0


def _add_common_ignore(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--ignore",
        nargs="*",
        default=list(DEFAULT_IGNORE),
        help="Directories to skip (default: common VCS/build/cache dirs)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="merge_reqs.py",
        description="Unified requirements.txt generator (merges 10 scripts).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="Scan a tree for imports and write requirements.")
    s.add_argument("-d", "--directory", default=".", help="Root directory (default: .)")
    s.add_argument(
        "-o",
        "--output",
        default="requirements.txt",
        help="Output file (default: requirements.txt)",
    )
    s.add_argument(
        "-p",
        "--pip-file",
        default=DEFAULT_PIP_FILE,
        help=f"Offline pip package list (default: {DEFAULT_PIP_FILE})",
    )
    s.add_argument("-m", "--mapping", default=None, help="module=package mapping file (optional)")
    s.add_argument("--stdlib-file", default=None, help="Extra stdlib names file (optional)")
    s.add_argument(
        "--stdlib-source",
        choices=["embedded", "python", "both"],
        default="both",
        help="Where to get stdlib list (default: both)",
    )
    s.add_argument(
        "--extractor",
        choices=["ast", "regex"],
        default="ast",
        help="Import extraction strategy (default: ast)",
    )
    s.add_argument(
        "--include-archives",
        dest="include_archives",
        action="store_true",
        default=True,
        help="Scan .zip/.whl/.tar.* archives (default: on)",
    )
    s.add_argument(
        "--no-archives",
        dest="include_archives",
        action="store_false",
        help="Do not scan archives",
    )
    s.add_argument(
        "--include-notebooks",
        dest="include_notebooks",
        action="store_true",
        default=True,
        help="Scan .ipynb notebooks (default: on)",
    )
    s.add_argument(
        "--no-notebooks",
        dest="include_notebooks",
        action="store_false",
        help="Do not scan notebooks",
    )
    s.add_argument(
        "--include-unknown",
        action="store_true",
        help="Include packages not in the pip list",
    )
    s.add_argument(
        "--check-installed",
        action="store_true",
        help="Skip packages already installed (pip freeze)",
    )
    s.add_argument(
        "--pip-cmd",
        default="pip",
        help="Command used for the installed-check (default: pip)",
    )
    s.add_argument("--cache", default=None, help=f"Enable cache file (e.g. {DEFAULT_CACHE_FILE})")
    s.add_argument("--no-cache", action="store_true", help="Disable cache even if --cache is set")
    s.add_argument("--clear-cache", action="store_true", help="Delete the cache file and exit")
    s.add_argument(
        "--format",
        choices=["requirements", "flat"],
        default="requirements",
        help="Output format: 'requirements' (filtered) or 'flat' (every non-stdlib import; imz3.py style)",
    )
    s.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the result but do not write anything",
    )
    _add_common_ignore(s)
    s.set_defaults(func=_dispatch_scan)
    md = sub.add_parser("metadata", help="Extract Requires-Dist lines from METADATA files.")
    md.add_argument("-d", "--directory", default=".", help="Root directory")
    md.add_argument(
        "-o",
        "--output",
        default="/sdcard/requirements.txt",
        help="Output file (default: /sdcard/requirements.txt)",
    )
    md.add_argument(
        "--metadata-name",
        default="METADATA",
        help="Filename to search for (default: METADATA)",
    )
    md.add_argument(
        "--append",
        dest="append",
        action="store_true",
        default=True,
        help="Append to the output file (default: on, matches reqr.py)",
    )
    md.add_argument(
        "--no-append",
        dest="append",
        action="store_false",
        help="Overwrite the output file",
    )
    _add_common_ignore(md)
    md.set_defaults(func=run_metadata)
    return p


def _dispatch_scan(args: argparse.Namespace) -> int:
    if args.clear_cache:
        if args.cache and Path(args.cache).exists():
            Path(args.cache).unlink()
            print(f"[i] Cache cleared: {args.cache}")
        return 0
    return run_scan(args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[!] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
