#!/data/data/com.termux/files/usr/bin/env python
"""Extract the RAR archives below a directory, including their split parts.
Finds the ``.rar`` files, resolves the ``.partN.rar``/``.rNN`` companions, extracts each of them in a process pool into a folder next to the archive and reports the resulting size.
Usage: xrar [DIRECTORY] Examples: $ xrar ~/Downloads Runs as a script (``python xrar.py``) in the directory it should act on; the module can also be imported.
"""

import contextlib
import multiprocessing as mp
from pathlib import Path
import sys
from typing import Iterable

import rarfile


def find_rar_files(root: Path) -> list[Path]:
    files = [p for p in root.rglob("*.rar") if p.is_file()]
    result: list[Path] = []
    for p in files:
        name = p.stem
        if ".part" in name:
            _, _, suffix = name.partition(".part")
            num = suffix.lstrip("0") or "0"
            if num != "1":
                continue
        result.append(p)
    return sorted(result)


def related_parts(rar_path: Path) -> list[Path]:
    base = rar_path.stem.split(".part")[0]
    parts: list[Path] = []
    for sibling in rar_path.parent.iterdir():
        if sibling.is_file() and sibling.suffix.lower() == ".rar" and sibling.stem.split(".part")[0] == base:
            parts.append(sibling)
    return parts


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def extract_one(rar_path: Path) -> tuple[Path, bool, str, int, int]:
    base = rar_path.stem.split(".part")[0]
    out_dir = rar_path.parent / base
    parts = related_parts(rar_path)
    input_size = sum(p.stat().st_size for p in parts)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with rarfile.RarFile(rar_path) as rf:
            rf.extractall(path=out_dir)
        output_size = dir_size(out_dir)
    except Exception as e:
        return (rar_path, False, str(e), input_size, 0)
    for f in parts:
        with contextlib.suppress(OSError):
            f.unlink()
    return (rar_path, True, str(out_dir), input_size, output_size)


def main(root: str = ".") -> None:
    root_path = Path(root).resolve()
    rar_files = find_rar_files(root_path)
    if not rar_files:
        print("No .rar files found.")
        return
    with mp.Pool(processes=mp.cpu_count()) as pool:
        results: Iterable[tuple[Path, bool, str, int, int]] = pool.map(extract_one, rar_files)
    results = list(results)
    max_len = max(len(p.name) for p, _, _, _, _ in results)
    for path, ok, msg, in_size, out_size in results:
        if not ok:
            print(f"[FAIL] {path.name:<{max_len}}   {msg}")
            continue
        ratio = (in_size / out_size * 100) if out_size > 0 else 0.0
        print(f"{path.name:<{max_len}}   {ratio:.0f}%")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
