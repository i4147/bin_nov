#!/data/data/com.termux/files/usr/bin/env python
"""Build a wheel from the current directory into ``/sdcard/whl``.
Changes to the parent directory and runs ``wheel pack`` on the current one with ``-d /sdcard/whl`` as the destination.
Usage: ww Examples: $ cd mypkg && ww Runs as a script (``python ww.py``) in the directory it should act on; the module can also be imported."""

import os
from pathlib import Path
import subprocess


if __name__ == "__main__":
    target_dir = Path.cwd().resolve()
    os.chdir(target_dir.parent)
    subprocess.run(["wheel", "pack", str(target_dir), "-d", "/sdcard/whl"], check=False)
