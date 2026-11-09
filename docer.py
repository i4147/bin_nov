#!/data/data/com.termux/files/usr/bin/env python
"""Move a document into ``/sdcard/doc``.
Copies the given file into ``/sdcard/doc`` (creating the directory when needed) and deletes the original; an existing destination aborts the move instead of overwriting.
Usage: docer FILE Examples: $ docer invoice.pdf Runs as a script (``python docer.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys


def process_file(path) -> None:
    content = path.read_bytes()
    target_dir = Path("/sdcard/doc")
    if not target_dir.exists():
        target_dir.mkdir(exist_ok=True)
    target_path = target_dir / path.name
    if not target_path.exists():
        target_path.write_bytes(content)
        path.unlink()
        print("done.")
    else:
        print(f"target file : {target_path.name} exists. remove it and try again")


if __name__ == "__main__":
    fn = Path(sys.argv[1])
    process_file(fn)
