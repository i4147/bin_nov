#!/data/data/com.termux/files/usr/bin/env python
"""Format web and Markdown files in place with prettier.
Runs ``prettier -w`` over the given files, or over every ``.html``/``.js``/``.ts``/``.tsx``/``.md``/``.scss``/``.coffee`` file below the current directory, skipping empty and single-line files and rewriting the Android ``/storage/emulated/0`` prefix to ``/sdcard``.
Files are processed in parallel.
Usage: pret [files ...] Examples: $ pret src/ Runs as a script (``python pret.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import get_files, mpf, runcmd


def process_file(path: str | Path) -> tuple[bool, Path]:
    path = Path(path)
    if not path.exists() or not path.stat().st_size:
        return (False, path)
    if sum(1 for _ in path.open()) == 1:
        return (False, path)
    ret = runcmd(
        ["prettier", "-w", str(path).replace("/storage/emulated/0", "/sdcard")],
        show_output=True,
    )
    if not ret:
        return (True, path)
    return (False, path)


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = list(
        [Path(f) for f in args]
        if args
        else get_files(
            cwd,
            e=[
                ".html",
                ".htm",
                ".js",
                ".jsx",
                ".ts",
                ".tsx",
                ".md",
                ".jsm",
                ".scss",
                ".tsm",
                ".coffee",
            ],
        )
    )
    if len(files) == 1:
        process_file(files[0])
        sys.exit(1)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
