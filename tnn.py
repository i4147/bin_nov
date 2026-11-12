#!/data/data/com.termux/files/usr/bin/env python
"""Replace the tabs of text files with four spaces.
Walks the given paths (or all non-binary files below the current directory), rewrites the files that contain a tab and reports the ones that were updated.
Usage: tnn [paths ...] Examples: $ tnn src/ Runs as a script (``python tnn.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from dh import cprint, get_nobinary, mpf


def process_file(path: str | Path) -> None:
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    if "\t" not in content:
        return
    new_content = content.replace("\t", "    ")
    if new_content == content:
        cprint(f"{path.name} (no change)", "grey")
        return
    path.write_text(new_content, encoding="utf-8")
    cprint(f"{path.name} (updated)", "cyan")


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = []
    if args:
        for arg in args:
            p = Path(arg)
            if p.is_file():
                files.append(p)
            elif p.is_dir():
                files.extend(get_nobinary(p))
    else:
        files = get_nobinary(cwd)
    if len(files) == 1:
        process_file(files[0])
        sys.exit(1)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
