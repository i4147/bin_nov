#!/data/data/com.termux/files/usr/bin/env python
"""Build a ``requirements.txt`` from the imports a project actually uses.
Parses the Python files and notebooks of the target tree, maps the imported module names to distributions with the local PyPI index (``/sdcard/data/pip.json``) plus a cached online lookup, and writes the requirement lines (optionally pinned to the installed versions).
``--ignore-dir``/``--ignore-file``/``--exclude`` filter the scan, ``--dry-run`` and ``--stdout`` avoid touching the file.
Usage: imports4 [-p PATH] [--pin] [--stdout] [--dry-run] Options: -p, --path Path to target directory --python Python interpreter to query pip from --proxy HTTP(S) proxy for PyPI --pin --exclude --include --ignore-dir --ignore-file --include-optional --keep-installed ...
plus 5 more; run ``imports4 --help`` for the full list.
Examples: $ imports4 -p ~/project --pin $ imports4 --stdout > requirements.txt Command-line script: run ``python imports4.py`` or the ``imports4`` command.
The module is importable as well."""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable

from dh import PKG_MAPPING, SKIP_DIRS, STDLIB
import requests
from yarg import json2package
from yarg.exceptions import HTTPError

PYPI_INDEX_PATH: str = "/sdcard/data/pip.json"
CACHE_DIR = Path.home() / ".cache" / "piplist"
CACHE_TTL: int = 86400
OPTIONAL_EXC: tuple[str, str] = ("ImportError", "ModuleNotFoundError")
log = logging.getLogger("piplist")
_MISSING = object()


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


IMPORT_TO_PKG: dict[str, Any] = {}
for _pkg, _imp in PKG_MAPPING.items():
    IMPORT_TO_PKG.setdefault(_imp, _pkg)
STDLIB_SET: set[Any] = set(STDLIB) if not isinstance(STDLIB, set) else STDLIB


def cache_key(module: str) -> str:
    return hashlib.sha256(module.encode("utf-8")).hexdigest()[:16]


def read_cache(module: str) -> object:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / (cache_key(module) + ".json")
    if not p.exists():
        return _MISSING
    try:
        data = json.loads(p.read_text())
    except Exception:
        return _MISSING
    if time.time() - data.get("ts", 0) > CACHE_TTL:
        return _MISSING
    return data.get("value", _MISSING)


def write_cache(module: str, value: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / (cache_key(module) + ".json")
    with contextlib.suppress(Exception):
        p.write_text(json.dumps({"ts": time.time(), "value": value}))


def load_pypi_index(path=PYPI_INDEX_PATH):
    try:
        data = json.loads(Path(path).read_text())
    except Exception as e:
        log.warning("could not load PyPI index %s: %s", path, e)
        return set()
    keys = data.keys() if isinstance(data, dict) else data
    return {normalize(k) for k in keys}


def get_installed_packages(python=None):
    python = python or sys.executable
    cmd = [python, "-m", "pip", "freeze"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, _ = proc.communicate()
    result = {}
    for line in stdout.splitlines():
        line = line.decode("utf-8", errors="ignore").strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            name, version = line.split("==", 1)
        else:
            name, version = line, None
        result[normalize(name)] = (name, version)
    return result


def get_imports_info(module: str, pypi_server="https://pypi.python.org/pypi/", proxy=None) -> str | None:
    try:
        response = requests.get("{0}{1}/json".format(pypi_server, module), proxies=proxy, timeout=15)
        if response.status_code == 200:
            if hasattr(response.content, "decode"):
                data = json2package(response.content.decode())
            else:
                data = json2package(response.content)
        elif response.status_code >= 300:
            raise HTTPError(status_code=response.status_code, reason=response.reason)
    except HTTPError:
        return None
    except requests.RequestException as e:
        log.debug("network error for %s: %s", module, e)
        return None
    return str(module) + "==" + str(data.latest_release_id)


def get_imports_info_cached(module: str, proxy=None):
    cached = read_cache(module)
    if cached is not _MISSING:
        return cached
    result = get_imports_info(module, proxy=proxy)
    write_cache(module, result)
    return result


def extract_imports_from_source(source: str, include_optional=False):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found = []
    stack = [(tree, False)]
    while stack:
        node, optional = stack.pop()
        in_optional = optional
        if isinstance(node, ast.Try):
            for h in node.handlers:
                t = h.type
                if t is None:
                    in_optional = True
                elif isinstance(t, ast.Name) and t.id in OPTIONAL_EXC:
                    in_optional = True
                elif isinstance(t, ast.Tuple):
                    for e in t.elts:
                        if isinstance(e, ast.Name) and e.id in OPTIONAL_EXC:
                            in_optional = True
        if isinstance(node, ast.Import):
            for alias in node.names:
                n = alias.name.split(".")[0]
                if include_optional or not in_optional:
                    found.append(n)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            n = node.module.split(".")[0]
            if include_optional or not in_optional:
                found.append(n)
        for child in ast.iter_child_nodes(node):
            stack.append((child, in_optional))
    return found


def extract_imports_from_notebook(path: Path, include_optional=False):
    try:
        nb = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    found = []
    for cell in nb.get("cells", []):
        src = cell.get("source", [])
        if isinstance(src, list):
            src = "".join(src)
        if not src:
            continue
        found.extend(extract_imports_from_source(src, include_optional))
    return found


def get_project_imports(directory: Path, ignore_dirs: Iterable[Any], include_optional=False):
    results = []
    ignore = set(ignore_dirs)
    for path, subdirs, files in os.walk(directory):
        subdirs[:] = [d for d in subdirs if d not in ignore and not d.startswith(".")]
        for name in files:
            full = os.path.join(path, name)
            if name.endswith(".py"):
                try:
                    source = Path(full).read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for m in extract_imports_from_source(source, include_optional):
                    results.append((m, full))
            elif name.endswith(".ipynb"):
                for m in extract_imports_from_notebook(full, include_optional):
                    results.append((m, full))
    return results


def is_local_module(name: str, source_file: Path) -> bool:
    d = os.path.dirname(os.path.abspath(source_file))
    if os.path.exists(os.path.join(d, name + ".py")):
        return True
    if os.path.exists(os.path.join(d, name, "__init__.py")):
        return True
    cur = d
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        if os.path.exists(os.path.join(parent, name, "__init__.py")):
            return True
        cur = parent
    return False


def apply_pin(name: str, version: str, style: str):
    if style == "none" or not version:
        return name
    if style == "exact":
        return name + "==" + version
    if style == "compatible":
        return name + "~=" + version
    if style == "minimal":
        return name + ">=" + version
    return name


def resolve_online(pkg: Any, index: dict[str, Any], proxy: Any):
    if index and normalize(pkg) not in index:
        return None
    return get_imports_info_cached(pkg, proxy=proxy)


def read_ignores(path: Path):
    out = set()
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    except Exception as e:
        log.warning("could not read ignore file %s: %s", path, e)
    return out


def init(args: list[Any]) -> int:
    level = logging.WARNING if args["quiet"] else (logging.DEBUG if args["verbose"] else logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    root = args["path"] or os.curdir
    if not os.path.isdir(root):
        log.error("path does not exist: %s", root)
        return 2
    ignore_dirs = SKIP_DIRS | set(args["ignore_dir"] or [])
    exclude = {normalize(e) for e in (args["exclude"] or [])}
    if args["ignore_file"]:
        exclude |= {normalize(x) for x in read_ignores(args["ignore_file"])}
    include_only = {normalize(i) for i in (args["include"] or [])}
    raw = get_project_imports(root, ignore_dirs, include_optional=args["include_optional"])
    seen = {}
    for name, src in raw:
        if name in STDLIB_SET:
            continue
        if is_local_module(name, src):
            continue
        seen.setdefault(name, src)
    pkgs = []
    for name in sorted(seen):
        pkg = IMPORT_TO_PKG.get(name, name)
        if pkg not in pkgs:
            pkgs.append(pkg)
    if exclude:
        pkgs = [p for p in pkgs if normalize(p) not in exclude]
    if include_only:
        pkgs = [p for p in pkgs if normalize(p) in include_only]
    index = load_pypi_index()
    installed = get_installed_packages(args["python"])
    results = {}
    online = []
    for pkg in pkgs:
        norm = normalize(pkg)
        if norm in installed:
            results[pkg] = installed[norm]
        else:
            online.append(pkg)
    if online:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(resolve_online, p, index, args["proxy"]): p for p in online}
            for fut in concurrent.futures.as_completed(futures):
                pkg = futures[fut]
                try:
                    info = fut.result()
                except Exception as e:
                    log.debug("lookup error %s: %s", pkg, e)
                    info = None
                if info and "==" in info:
                    n, v = info.split("==", 1)
                    results[pkg] = (n, v)
                elif index and normalize(pkg) not in index:
                    log.warning("%s not on PyPI, skipping", pkg)
                else:
                    log.warning("could not resolve version for %s", pkg)
                    results[pkg] = (pkg, None)
    lines = []
    for pkg in results:
        if args["strip_installed"] and normalize(pkg) in installed:
            continue
        name, version = results[pkg]
        lines.append(apply_pin(name, version, args["pin"]))
    lines = sorted(set(lines), key=str.lower)
    if args["stdout"] or args["dry_run"]:
        sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
        return 0
    target = os.path.join(args["path"], "requirements.txt") if args["path"] else "requirements.txt"
    if os.path.exists(target) and not args["overwrite"]:
        try:
            existing = [l.strip() for l in Path(target).read_text().splitlines() if l.strip() and not l.startswith("#")]
        except Exception:
            existing = []
        merged = set(lines) | set(existing)
        lines = sorted(merged, key=str.lower)
    Path(target).write_text("\n".join(lines) + ("\n" if lines else ""))
    log.info("wrote %d entries to %s", len(lines), target)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--path", type=str, help="Path to target directory")
    ap.add_argument("--python", type=str, help="Python interpreter to query pip from")
    ap.add_argument("--proxy", type=str, help="HTTP(S) proxy for PyPI")
    ap.add_argument(
        "--pin",
        choices=["exact", "compatible", "minimal", "none"],
        default="exact",
    )
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--include", nargs="*", default=[])
    ap.add_argument("--ignore-dir", nargs="*", default=[])
    ap.add_argument("--ignore-file", type=str)
    ap.add_argument("--include-optional", action="store_true")
    ap.add_argument("--keep-installed", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = vars(ap.parse_args())
    args["strip_installed"] = not args["keep_installed"]
    try:
        sys.exit(init(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
