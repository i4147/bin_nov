#!/data/data/com.termux/files/usr/bin/env python
"""Create the ``.so.0``/``.so.1`` symlinks of the libraries in ``~/.local/lib``.
Skips the files that are already symlinks or carry a version suffix, derives the ``libfoo.so`` base name, repairs the wrong links and points ``libfoo.so.0`` and ``libfoo.so.1`` at it, which is what the loaders of the Termux builds expect.
Usage: symlink_so Examples: $ symlink_so Runs as a script (``python symlink_so.py``) in the directory it should act on; the module can also be imported."""

import glob
import os
from pathlib import Path
import re


def should_skip(so_path: Path) -> bool:
    if so_path.is_symlink():
        return True
    name = so_path.name
    if name.endswith((".0", ".1")):
        return True
    return bool(re.search(r"\.so\.\d+(\.\d+)+$", name))


def get_base_name(so_path: Path):
    name = so_path.name
    match = re.match(r"(.+\.so)(?:\.\d+)*$", name)
    if match:
        return match.group(1)
    return name


def create_symlinks() -> None:
    lib_dir = Path.home() / ".local" / "lib"
    if not lib_dir.exists():
        print(f"Error: {lib_dir} does not exist")
        return
    so_files = glob.glob(str(lib_dir / "*.so"))
    so_files.extend(glob.glob(str(lib_dir / "*.so.*")))
    processed_bases = set()
    for so_file in sorted(so_files):
        so_path = Path(so_file)
        if should_skip(so_path):
            continue
        abs_path = so_path.resolve()
        base_name = get_base_name(so_path)
        if base_name in processed_bases:
            continue
        processed_bases.add(base_name)
        for version in [".0", ".1"]:
            symlink_name = base_name + version
            symlink_path = lib_dir / symlink_name
            if symlink_path.is_symlink():
                target = os.readlink(symlink_path)
                if target == base_name or Path(target).resolve() == abs_path:
                    print(f"Exists: {symlink_path} -> {target} (correct)")
                    continue
                else:
                    symlink_path.unlink()
            elif symlink_path.exists():
                symlink_path.unlink()
            try:
                relative_path = base_name
                symlink_path.symlink_to(relative_path)
                print(f"Created: {symlink_path} -> {relative_path}")
            except Exception as e:
                print(f"Failed to create {symlink_path}: {e}")


if __name__ == "__main__":
    create_symlinks()
