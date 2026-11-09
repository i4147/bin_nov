#!/data/data/com.termux/files/usr/bin/env python
"""Print every directory on ``PATH``, one per line.
Splits ``$PATH`` on colons and prints the entries in order, which makes it easy to spot duplicates or stale directories.
Usage: pathcmd Examples: $ pathcmd Runs as a script (``python pathcmd.py``) in the directory it should act on; the module can also be imported."""

import os


if __name__ == "__main__":
    p_env = os.environ.get("PATH")
    pdirs = p_env.split(":")
    for p in pdirs:
        print(p)
