#!/data/data/com.termux/files/usr/bin/env python
"""Build a wheel for every ``setup.py`` found below the current directory.
Runs ``python setup.py bdist_wheel`` in the parent directory of each ``setup.py`` with a worker pool and finally lists the ``.whl`` files that were produced.
Usage: buildr Examples: $ buildr Runs as a script (``python buildr.py``) in the directory it should act on; the module can also be imported."""

from os import chdir as os_chdir
from pathlib import Path

from dh import get_files, mpf, runcmd


def process_file(path_str: str) -> None:
    path = Path(path_str)
    os_chdir(path.parent)
    cmd = ["python", "setup.py", "bdist_wheel"]
    ret, _, _ = runcmd(cmd)
    if ret != 0:
        print(f"Error building wheel for {path}")


if __name__ == "__main__":
    cwd = Path.cwd()
    files = get_files(cwd)
    targets = [str(path) for path in files if path.name == "setup.py"]
    mpf(process_file, targets)
    whl_files = get_files(cwd, ext=[".whl"])
    if whl_files:
        for k in whl_files:
            print(k)
