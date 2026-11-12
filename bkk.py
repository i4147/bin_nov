#!/data/data/com.termux/files/usr/bin/env python
"""Delete every ``*.bak`` file under the current directory.
Walks the tree, prints each backup file relative to the current directory and unlinks it.
Usage: bkk Examples: $ bkk Runs as a script (``python bkk.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path

if __name__ == "__main__":
    cwd = Path.cwd()
    for r, _d, files in cwd.walk():
        for file in files:
            path = Path(r) / file
            if path.is_file() and path.name.endswith(".bak"):
                print(path.relative_to(cwd))
                path.unlink()
