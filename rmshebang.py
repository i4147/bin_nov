#!/data/data/com.termux/files/usr/bin/env python
"""Remove the shebang line from Python files.
Deletes the first line when it starts with ``#!/`` and rewrites the files in a spawn-based pool, printing the space that was freed.
Usage: rmshebang [files ...] Examples: $ rmshebang pkg/ Runs as a script (``python rmshebang.py``) in the directory it should act on; the module can also be imported."""

from collections import deque
from multiprocessing import get_context
from pathlib import Path
import sys

from dh import fsz, get_files, gsz


MAX_QUEUE: int = 16


def process_file(path: Path) -> None:
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        new_lines = []
        if lines[0].startswith("#!/"):
            new_lines = lines[1:]
            content = "\n".join(new_lines)
            path.write_text(content, encoding="utf-8")
            print(f"{path.name} updated.")
            return
        return
    except Exception:
        pass


def main() -> None:
    cwd = Path.cwd()
    before = gsz(cwd)
    args = sys.argv[1:]
    files = [Path(arg) for arg in args] if args else get_files(cwd, ext=[".py"])
    with get_context("spawn").Pool(8) as pool:
        pending = deque()
        for f in files:
            pending.append(pool.apply_async(process_file, (f,)))
            if len(pending) > MAX_QUEUE:
                pending.popleft().get()
        while pending:
            pending.popleft().get()
    diffsize = before - gsz(cwd)
    print(f"space saved: {fsz(diffsize)}")


if __name__ == "__main__":
    raise SystemExit(main())
