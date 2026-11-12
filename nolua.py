#!/data/data/com.termux/files/usr/bin/env python
"""List the sub-directories of the current directory that contain no Lua files.
Recursively checks each top-level folder for ``.lua`` sources and prints the names of the ones that have none.
``move_to_start`` copies such a folder into the vim plugin ``start`` directory and removes the original.
Usage: nolua Examples: $ nolua Runs as a script (``python nolua.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path

from dh import get_files


def has_lua(root_dir: Path) -> bool:
    lua_files = []
    lua_files = get_files(root_dir, ext=[".lua"])
    return bool(lua_files)


def move_to_start(src: Path) -> None:
    dest = "/data/data/com.termux/files/home/.vim/pack/plugins/start"
    if not Path(dest).exists():
        Path(dest).mkdir(exist_ok=True)
    import shutil

    target_path = f"{dest}/{src.name}"
    shutil.copytree(str(src), target_path)
    shutil.rmtree(src)


if __name__ == "__main__":
    cwd = Path.cwd()
    for dirpath in cwd.iterdir():
        if not dirpath.is_dir():
            continue
        if not has_lua(dirpath):
            print(f" - {dirpath.name}")
