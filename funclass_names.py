#!/data/data/com.termux/files/usr/bin/env python
"""Rank the function and class names used in a tree by how often they are referenced.
Scans Python sources, wheels and tarballs (including ``.tar.zst``), collects the defined functions/classes and every name or attribute use, and writes ``func.json`` and ``class.json`` sorted by usage count.
Generic names such as ``main`` or ``run`` are ignored.
Usage: funclass_names Examples: $ funclass_names Runs as a script (``python funclass_names.py``) in the directory it should act on; the module can also be imported.
"""

import ast
from collections import defaultdict
import json
import multiprocessing as mp
from pathlib import Path
import sys
import tarfile
from typing import Iterator
import zipfile

from dh import WorkerResult
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

SKIP_NAMES: set[str] = {
    "main",
    "app",
    "run",
    "start",
    "cli",
    "entry_point",
    "entrypoint",
    "__init__",
    "__main__",
    "__call__",
    "__repr__",
    "__str__",
    "__eq__",
    "__ne__",
    "__lt__",
    "__gt__",
    "__le__",
    "__ge__",
    "__hash__",
    "__len__",
    "__iter__",
    "__next__",
    "__getitem__",
    "__setitem__",
    "__delitem__",
    "__contains__",
    "__enter__",
    "__exit__",
    "__bool__",
    "__int__",
    "__float__",
    "__add__",
    "__sub__",
    "__mul__",
    "__truediv__",
    "__floordiv__",
    "__mod__",
    "__pow__",
    "__and__",
    "__or__",
    "__xor__",
    "__invert__",
    "__neg__",
    "__pos__",
    "__abs__",
    "__getattr__",
    "__setattr__",
    "__delattr__",
    "__get__",
    "__set__",
    "__delete__",
    "__new__",
    "__init_subclass__",
    "__class_getitem__",
    "__format__",
    "__reduce__",
    "__copy__",
    "__deepcopy__",
    "setup",
    "teardown",
    "setUp",
    "tearDown",
    "setUpClass",
    "tearDownClass",
    "setUpModule",
    "tearDownModule",
    "get",
    "set",
    "put",
    "post",
    "delete",
    "patch",
    "head",
    "options",
    "update",
    "create",
    "read",
    "write",
    "list",
    "add",
    "remove",
    "clear",
    "load",
    "save",
    "parse",
    "build",
    "make",
    "init",
    "close",
    "open",
    "test",
    "process",
    "handle",
    "execute",
    "apply",
    "call",
    "invoke",
    "reset",
    "copy",
    "clone",
    "merge",
    "split",
    "join",
    "filter",
    "map",
    "reduce",
    "validate",
    "check",
    "is_valid",
    "to_dict",
    "from_dict",
    "to_json",
    "from_json",
    "to_string",
    "to_str",
    "serialize",
    "deserialize",
    "encode",
    "decode",
    "format",
    "print",
    "log",
    "debug",
    "info",
    "warn",
    "warning",
    "error",
    "critical",
    "exception",
    "wrapper",
    "decorator",
    "inner",
    "outer",
    "callback",
    "handler",
    "hook",
    "lambda_func",
}
EXCLUDED_DIRS: set[str] = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    "node_modules",
    "build",
    "dist",
    ".eggs",
    ".idea",
    ".vscode",
}
ARCHIVE_SUFFIXES: tuple[str, ...] = (".whl", ".tar.gz", ".tar.xz", ".tar.zst")
HAS_NATIVE_ZSTD: bool = sys.version_info >= (3, 14)


def _is_relevant(path: Path) -> bool:
    name = path.name
    return name.endswith(".py") or name.endswith(ARCHIVE_SUFFIXES)


def _iter_zip_python(path: Path, messages: list[str]) -> Iterator[tuple[str, str]]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for member in zf.namelist():
                if member.endswith(".py"):
                    try:
                        source = zf.read(member).decode("utf-8")
                        yield f"{path}::{member}", source
                    except (UnicodeDecodeError, KeyError) as e:
                        messages.append(f"[skip] {path}::{member}: {e}")
    except (zipfile.BadZipFile, OSError) as e:
        messages.append(f"[skip] {path}: {e}")


def _iter_tar_python(path: Path, messages: list[str]) -> Iterator[tuple[str, str]]:
    if path.name.endswith(".tar.gz"):
        mode = "r:gz"
    elif path.name.endswith(".tar.xz"):
        mode = "r:xz"
    else:
        return
    try:
        with tarfile.open(path, mode) as tf:
            for member in tf.getmembers():
                if member.isfile() and member.name.endswith(".py"):
                    try:
                        f = tf.extractfile(member)
                        if f is None:
                            continue
                        source = f.read().decode("utf-8")
                        yield f"{path}::{member.name}", source
                    except (UnicodeDecodeError, KeyError) as e:
                        messages.append(f"[skip] {path}::{member.name}: {e}")
    except (tarfile.TarError, OSError) as e:
        messages.append(f"[skip] {path}: {e}")


def _iter_tar_zst_native(path: Path, messages: list[str]) -> Iterator[tuple[str, str]]:
    try:
        with tarfile.open(path, "r:zst") as tf:
            for member in tf.getmembers():
                if member.isfile() and member.name.endswith(".py"):
                    try:
                        f = tf.extractfile(member)
                        if f is None:
                            continue
                        source = f.read().decode("utf-8")
                        yield f"{path}::{member.name}", source
                    except (UnicodeDecodeError, KeyError) as e:
                        messages.append(f"[skip] {path}::{member.name}: {e}")
    except (tarfile.TarError, OSError) as e:
        messages.append(f"[skip] {path}: {e}")


def _iter_tar_zst_zstandard(path: Path, messages: list[str]) -> Iterator[tuple[str, str]]:
    try:
        import zstandard
    except ImportError:
        messages.append(f"[skip] {path}: zstandard module not installed")
        return
    try:
        with zstandard.open(path, "rb") as f, tarfile.open(fileobj=f, mode="r:") as tf:
            for member in tf.getmembers():
                if member.isfile() and member.name.endswith(".py"):
                    try:
                        fobj = tf.extractfile(member)
                        if fobj is None:
                            continue
                        source = fobj.read().decode("utf-8")
                        yield f"{path}::{member.name}", source
                    except (UnicodeDecodeError, KeyError) as e:
                        messages.append(f"[skip] {path}::{member.name}: {e}")
    except (tarfile.TarError, OSError, zstandard.ZstdError) as e:
        messages.append(f"[skip] {path}: {e}")


def _iter_tar_zst_python(path: Path, messages: list[str]) -> Iterator[tuple[str, str]]:
    if HAS_NATIVE_ZSTD:
        yield from _iter_tar_zst_native(path, messages)
    else:
        yield from _iter_tar_zst_zstandard(path, messages)


def _iter_sources_from_path(path: Path, messages: list[str]) -> Iterator[tuple[str, str]]:
    name = path.name
    if name.endswith(".py"):
        try:
            yield str(path), path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            messages.append(f"[skip] {path}: {e}")
    elif name.endswith(".whl"):
        yield from _iter_zip_python(path, messages)
    elif name.endswith((".tar.gz", ".tar.xz")):
        yield from _iter_tar_python(path, messages)
    elif name.endswith(".tar.zst"):
        yield from _iter_tar_zst_python(path, messages)


def _collect_paths(directory: Path, messages: list[str]) -> list[Path]:
    results: list[Path] = []
    stack: list[Path] = [directory]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError as e:
            messages.append(f"[skip] {current}: {e}")
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in EXCLUDED_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file() and _is_relevant(entry):
                results.append(entry)
    return results


def process_path(path: Path) -> WorkerResult:
    func_defs: dict[str, int] = defaultdict(int)
    class_defs: dict[str, int] = defaultdict(int)
    name_uses: dict[str, int] = defaultdict(int)
    attr_uses: dict[str, int] = defaultdict(int)
    messages: list[str] = []
    for name, source in _iter_sources_from_path(path, messages):
        try:
            tree = ast.parse(source, filename=name)
        except SyntaxError as e:
            messages.append(f"[skip] {name}: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name not in SKIP_NAMES:
                    func_defs[node.name] += 1
            elif isinstance(node, ast.ClassDef):
                if node.name not in SKIP_NAMES:
                    class_defs[node.name] += 1
            elif isinstance(node, ast.Name):
                name_uses[node.id] += 1
            elif isinstance(node, ast.Attribute):
                attr_uses[node.attr] += 1
    return (
        dict(func_defs),
        dict(class_defs),
        dict(name_uses),
        dict(attr_uses),
        messages,
    )


def write_json(path: Path, data: list[dict[str, int]]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    directory = Path()
    console = Console()
    func_defs: dict[str, int] = defaultdict(int)
    class_defs: dict[str, int] = defaultdict(int)
    name_uses: dict[str, int] = defaultdict(int)
    attr_uses: dict[str, int] = defaultdict(int)
    with Progress(
        TextColumn("[bold blue]Scanning"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("scan", total=None)
        paths = _collect_paths(directory, [])
        progress.update(task, total=len(paths))
        with mp.Pool() as pool:
            for fd, cd, nu, au, msgs in pool.imap_unordered(process_path, paths, chunksize=8):
                progress.update(task, advance=1)
                for msg in msgs:
                    progress.console.log(msg)
                for k, v in fd.items():
                    func_defs[k] += v
                for k, v in cd.items():
                    class_defs[k] += v
                for k, v in nu.items():
                    name_uses[k] += v
                for k, v in au.items():
                    attr_uses[k] += v

    def usage(n: str) -> int:
        return name_uses.get(n, 0) + attr_uses.get(n, 0)

    func_json = [{name: usage(name)} for name in func_defs]
    class_json = [{name: usage(name)} for name in class_defs]
    func_json.sort(key=lambda d: next(iter(d.values())), reverse=True)
    class_json.sort(key=lambda d: next(iter(d.values())), reverse=True)
    write_json(Path("func.json"), func_json)
    write_json(Path("class.json"), class_json)
    console.print(f"Wrote func.json ({len(func_json)} entries)")
    console.print(f"Wrote class.json ({len(class_json)} entries)")


if __name__ == "__main__":
    main()
