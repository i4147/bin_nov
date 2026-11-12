#!/data/data/com.termux/files/usr/bin/env python
"""Make scripts and binaries below the current directory executable.
Adds the execute bits to the files that need them: scripts with a shebang, files in ``bin``/``sbin``/``.bin`` folders and shared objects, using a worker pool for the chmod calls.
Usage: mkx [paths ...] Examples: $ mkx Runs as a script (``python mkx.py``) in the directory it should act on; the module can also be imported.
"""

import multiprocessing as mp
import os
from pathlib import Path
import re

from dh import MAX_WORKERS

SUFFIXES: set[str] = {".py", ".sh", ".bash", ".pl", ".rb", ".pyw", ".txt"}
BIN_DIRS: set[str] = {"sbin", "bin", ".bin"}
SO_RE = re.compile(r".*\.so(?:\.\d+)*$")


def has_shebang(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def is_binary(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def is_executable(p: Path) -> bool:
    try:
        return bool(p.stat().st_mode & 0o100)
    except OSError:
        return False


def chmod_x(p: Path) -> bool:
    try:
        p.chmod(p.stat().st_mode | 0o111)
        return True
    except OSError:
        return False


def should_execute(p: Path) -> bool:
    if has_shebang(p):
        return True
    if p.parent.name in BIN_DIRS:
        return True
    if SO_RE.match(p.name):
        return True
    return bool(not p.suffix and is_binary(p))


def process(path_str: str):
    p = Path(path_str)
    try:
        if not p.is_file():
            return path_str, False, None
        if is_executable(p):
            return path_str, False, None
        if not should_execute(p):
            return path_str, False, None
        if os.name != "posix":
            return path_str, False, None
        if chmod_x(p):
            return path_str, True, None
        return path_str, False, "chmod failed"
    except Exception as e:
        return path_str, False, str(e)


def main() -> int:
    root = Path.cwd()
    self_path = Path(__file__).resolve()
    files = []
    for p in root.rglob("*"):
        if ".git" in p.parts:
            continue
        if p.is_symlink():
            continue
        try:
            if p.resolve() == self_path:
                continue
        except OSError:
            pass
        if p.is_file():
            files.append(str(p))
    if not files:
        print("No files found.")
        return 0
    changed = errors = 0
    with mp.Pool(processes=MAX_WORKERS) as pool:
        results = [pool.apply_async(process, (f,)) for f in files]
        for r in results:
            path_str, did_change, err = r.get()
            if err:
                errors += 1
                print(f"ERROR {path_str}: {err}")
                continue
            if did_change:
                changed += 1
                print(f"[+] Made executable: {path_str}")
    print(f"Done. changed={changed} errors={errors}")
    if os.name != "posix":
        print("Note: non-POSIX system — executable bits not applied.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
