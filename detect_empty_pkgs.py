#!/data/data/com.termux/files/usr/bin/env python
"""Find the empty or useless Python archives (tar.gz, wheel, zip).
Opens each archive of the current directory and flags the ones without code (no files, only metadata, only ``setup.py``, only ``__pycache__``, only docs/tests, import-only modules or trivially small content), moving them into ``./empty`` with ``-a``.
Usage: detect_empty_pkgs [-a] Options: -a, --apply Actually move the archives (default is dry-run) Examples: $ detect_empty_pkgs -a Command-line script: run ``python detect_empty_pkgs.py`` or the ``detect_empty_pkgs`` command.
The module is importable as well."""

from __future__ import annotations

import argparse
import ast
import multiprocessing as mp
from pathlib import Path
import shutil
import tarfile
import zipfile
from typing import Any, Iterator

from dh import MAX_WORKERS


MIN_ARCHIVE_BYTES: int = 300
TARGET_GLOBS: tuple[str, str, str] = ("*.tar.gz", "*.whl", "*.zip")
METADATA_NAMES: set[str] = {
    "PKG-INFO",
    "METADATA",
    "setup.cfg",
    "pyproject.toml",
    "setup.py",
    "MANIFEST.in",
    "requirements.txt",
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
    "LICENSE.rst",
    "COPYING",
    "README",
    "README.md",
    "README.rst",
    "README.txt",
    "CHANGELOG",
    "CHANGELOG.md",
    "CHANGELOG.rst",
    "CHANGES",
    "CHANGES.md",
    "AUTHORS",
    "AUTHORS.md",
    "NOTICE",
    "TODO",
    "RECORD",
    "WHEEL",
}
NON_CODE_PREFIXES: tuple[str, ...] = (
    "docs/",
    "doc/",
    "examples/",
    "example/",
    "tests/",
    "test/",
    "testing/",
    "benchmarks/",
    "benchmark/",
)


class _Member:
    __slots__ = ("_read", "name", "size")

    def __init__(self, name: str, size: int, read: bool) -> None:
        self.name = name
        self.size = size
        self._read = read

    def read(self) -> bytes:
        data = self._read()
        return data if data is not None else b""


class Archive:
    def __init__(self, path: Path) -> None:
        self.path = path
        lower = path.name.lower()
        if lower.endswith(".tar.gz"):
            self.kind = "tar"
        elif lower.endswith((".zip", ".whl")):
            self.kind = "zip"
        else:
            msg = f"unsupported archive: {path}"
            raise ValueError(msg)
        self._fh = None

    def __enter__(self):
        if self.kind == "tar":
            self._fh = tarfile.open(self.path, "r:gz")
        else:
            self._fh = zipfile.ZipFile(self.path, "r")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def files(self) -> Iterator[_Member]:
        fh = self._fh
        if self.kind == "tar":
            for m in fh.getmembers():
                if not m.isfile():
                    continue
                yield _Member(
                    m.name,
                    m.size,
                    lambda m=m, fh=fh: fh.extractfile(m).read(),
                )
        else:
            for info in fh.infolist():
                if info.is_dir():
                    continue
                yield _Member(
                    info.filename,
                    info.file_size,
                    lambda n=info.filename, fh=fh: fh.read(n),
                )


def _is_py(member: _Member) -> bool:
    return member.name.endswith(".py")


def _basename(member: _Member) -> str:
    return member.name.replace("\\", "/").rsplit("/", 1)[-1]


def _check_no_files(members: list[Any]) -> str | None:
    if not members:
        return "no files in archive"
    return None


def _check_no_py(members: list[Any]) -> str | None:
    if not any(_is_py(m) for m in members):
        return "no .py files"
    return None


def _check_only_setup_py(members: list[Any]) -> str | None:
    py = [m for m in members if _is_py(m)]
    if len(py) == 1 and _basename(py[0]) == "setup.py":
        return "only setup.py"
    return None


def _check_all_non_setup_empty(members: list[Any]) -> str | None:
    py = [m for m in members if _is_py(m)]
    non_setup = [m for m in py if _basename(m) != "setup.py"]
    if not non_setup:
        return None
    for m in non_setup:
        if m.read().strip():
            return None
    return "all non-setup .py files are empty"


def _check_only_metadata(members: list[Any]) -> str | None:
    if not members:
        return None
    for m in members:
        base = _basename(m)
        if base in METADATA_NAMES:
            continue
        if _is_py(m) and not m.read().strip():
            continue
        return None
    return "only metadata files"


def _check_only_pyc(members: list[Any]) -> str | None:
    if any(_is_py(m) for m in members):
        return None
    has_compiled = any(m.name.endswith((".pyc", ".pyo")) or "__pycache__" in m.name for m in members)
    if has_compiled and all(m.name.endswith((".pyc", ".pyo")) or "__pycache__" in m.name for m in members):
        return "only compiled Python files"
    return None


def _check_only_docs_tests(members: list[Any]) -> str | None:
    if not members:
        return None
    for m in members:
        parts = m.name.split("/", 1)
        rel = parts[1] if len(parts) > 1 else parts[0]
        if rel.startswith(NON_CODE_PREFIXES):
            continue
        if "/" not in rel and rel in METADATA_NAMES:
            continue
        return None
    return "only docs/tests/examples"


def _py_is_trivial(src: str) -> bool:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        return False
    return True


def _check_imports_only(members: list[Any]) -> str | None:
    py = [m for m in members if _is_py(m)]
    if not py:
        return None
    saw_non_empty = False
    for m in py:
        if _basename(m) == "setup.py":
            continue
        src = m.read().decode("utf-8", "replace")
        if not src.strip():
            continue
        if not _py_is_trivial(src):
            return None
        saw_non_empty = True
    if saw_non_empty:
        return "all .py files contain only imports/docstrings"
    return None


def _check_tiny(members: list[Any]) -> str | None:
    total = sum(m.size for m in members)
    if total < MIN_ARCHIVE_BYTES:
        return f"archive content too small ({total} bytes)"
    return None


_CHECKS: tuple[Any, ...] = (
    _check_no_files,
    _check_no_py,
    _check_only_setup_py,
    _check_all_non_setup_empty,
    _check_only_metadata,
    _check_only_pyc,
    _check_only_docs_tests,
    _check_imports_only,
    _check_tiny,
)


def analyze(path: Path):
    try:
        with Archive(path) as ar:
            members = list(ar.files())
            for check in _CHECKS:
                reason = check(members)
                if reason:
                    return path, reason
            return path, None
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError, ValueError) as e:
        return path, f"invalid archive ({type(e).__name__})"


def _split_archive_name(name: str):
    lower = name.lower()
    for compound in (".tar.gz",):
        if lower.endswith(compound):
            return name[: -len(compound)], name[-len(compound) :]
    p = Path(name)
    return p.stem, p.suffix


def _unique_dest(directory: Path, name: str) -> Path:
    dest = directory / name
    if not dest.exists():
        return dest
    stem, suffix = _split_archive_name(name)
    i = 1
    while True:
        dest = directory / f"{stem}.{i}{suffix}"
        if not dest.exists():
            return dest
        i += 1


def parse_args():
    ap = argparse.ArgumentParser(
        description=(
            "Find empty/useless .tar.gz / .whl / .zip Python packages and (optionally) move them into ./empty/ ."
        ),
    )
    ap.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="actually move the archives (default is dry-run)",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cwd = Path.cwd()
    empty_dir = cwd / "empty"
    if args.apply:
        empty_dir.mkdir(exist_ok=True)
    targets = sorted({
        p for pattern in TARGET_GLOBS for p in cwd.rglob(pattern) if p.is_file() and empty_dir not in p.parents
    })
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanning {len(targets)} archive(s) with {MAX_WORKERS} workers\n")
    matched = 0
    moved = 0
    with mp.Pool(MAX_WORKERS) as pool:
        for path, reason in pool.imap_unordered(analyze, targets, chunksize=1):
            if reason is None:
                continue
            matched += 1
            rel = path.relative_to(cwd)
            if not args.apply:
                print(f"WOULD MOVE  [{reason}]  {rel}")
                continue
            dest = _unique_dest(empty_dir, path.name)
            try:
                shutil.move(str(path), str(dest))
            except OSError as e:
                print(f"! failed to move {rel}: {e}")
                continue
            moved += 1
            print(f"MOVED  [{reason}]  {rel}  ->  empty/{dest.name}")
    print()
    if args.apply:
        print(f"Moved {moved} of {len(targets)} archives into {empty_dir}")
    else:
        print(f"Dry run: {matched} of {len(targets)} archives would be moved. Re-run with -a / --apply to do it.")


if __name__ == "__main__":
    main()
