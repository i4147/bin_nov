#!/data/data/com.termux/files/usr/bin/env python
"""Run pylint over Python files with a compact message template.
Calls pylint with ``--persistent=n --reports=n --output-format=parseable`` and a ``file:line:column:object:message:id`` template on the given files (directories are expanded) or on every Python file below the current directory.
Usage: run_pylint [paths ...] Examples: $ run_pylint src/ Runs as a script (``python run_pylint.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from dh import get_pyfiles, runcmd

CHUNK_SIZE: int = 1024 * 1024


def process_file(path: Path) -> None:
    path = Path(path)
    cmd = [
        "pylint",
        f"{path!s}",
        "--persistent=n",
        "--reports=n",
        "--output-format=parseable",
        "--msg-template='{C}:{line}:{column}:{obj}:{msg}:{msg_id}'",
        str(path),
    ]
    return runcmd(cmd, show_output=True)


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = []
    if args:
        for arg in args:
            p = Path(arg)
            if p.is_file():
                files.append(p)
            if p.is_dir():
                files.extend(get_pyfiles(p))
    else:
        files = get_pyfiles(cwd)
    for f in files:
        process_file(f)


if __name__ == "__main__":
    raise SystemExit(main())
