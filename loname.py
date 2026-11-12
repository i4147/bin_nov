#!/data/data/com.termux/files/usr/bin/env python
"""Normalize the file names of the current directory to lowercase snake style.
Lower-cases the stem, drops punctuation other than ``_``/``-``, replaces spaces with underscores, collapses repeats and lower-cases the extension, keeping the existing files under a unique name when a rename would collide.
Usage: loname Examples: $ loname Runs as a script (``python loname.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import re
import string
import sys

from dh import unique_path

recursive: bool = "-r" in sys.argv


_CYAN: str = "\033[36m"
_RESET: str = "\033[0m"

_PUNCT_TO_REMOVE: str = string.punctuation.replace("_", "").replace("-", "").replace(":", "")
_TRANSLATE_TABLE = str.maketrans(dict.fromkeys(_PUNCT_TO_REMOVE) | {" ": "_"})
_COLLAPSE_RE = re.compile(r"[_:\-]+")


def process_file(path: Path) -> None:
    stem = path.stem
    suffix = path.suffix
    stem = stem.lower()
    stem = stem.translate(_TRANSLATE_TABLE)
    stem = _COLLAPSE_RE.sub("_", stem)
    stem = stem.strip("/")
    if not stem:
        stem = "unnamed"
    nn = stem + suffix.lower()
    if nn == path.name:
        return
    new_path = path.with_name(nn)
    if new_path.exists():
        new_path = unique_path(new_path)
    path.rename(new_path)


def main() -> None:
    cwd = Path.cwd()
    for path in cwd.iterdir():
        if path.is_symlink():
            continue
        process_file(path)


if __name__ == "__main__":
    raise SystemExit(main())
