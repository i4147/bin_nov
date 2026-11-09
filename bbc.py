#!/data/data/com.termux/files/usr/bin/env python
"""Delete Python build leftovers below the current directory.
Removes ``build``, ``dist`` and ``target`` directories, ``*.egg-info`` directories, stray ``PKG-INFO`` files and auto-generated ``setup.cfg`` files (the ones that only contain an empty ``[egg_info]`` section).
Usage: bbc Examples: $ bbc Runs as a script (``python bbc.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import shutil


L1 = "[egg_info]"
L2 = "tag_build = "
L3 = "tag_date = 0"
SETUPCFG = """[egg_info]
tag_build =
tag_date = 0
"""


def is_setupcfg(fn: Path) -> bool:
    content = fn.read_text(encoding="utf8")
    if content == SETUPCFG:
        return True
    lines = content.splitlines(keepends=False)
    return bool(lines[0] == L1 and lines[1] == L2 and lines[2] == L3)


if __name__ == "__main__":
    cwd = Path.cwd()
    for item in cwd.rglob("*"):
        if item.is_dir() and item.name in ("build", "dist", "target"):
            shutil.rmtree(str(item))
            print(f"{item.name} removed.")
        if item.is_dir() and item.name.endswith("egg-info"):
            shutil.rmtree(str(item))
            print(f"{item.name} removed.")
        if item.is_file() and item.name == "PKG-INFO":
            item.unlink()
            print(f"{item.name} removed.")
        if item.is_file() and item.name == "setup.cfg" and is_setupcfg(item):
            item.unlink()
            print(f"{item.name} removed.")
