#!/data/data/com.termux/files/usr/bin/env python
"""Page through the files of the current directory.
Reads every file (``-r`` includes the subdirectories), prints a header per file and shows the whole collection through ``pydoc.pager``.
Usage: qv [-r] Options: -r, --recursive Include files in subdirectories Examples: $ qv -r Command-line script: run ``python qv.py`` or the ``qv`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import pydoc


def collect_files(root: Path, recursive: bool) -> list[Path]:
    paths = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        (path for path in paths if path.is_file()),
        key=lambda path: str(path).lower(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="View files in the current directory with paging.")
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="include files in subdirectories",
    )
    args = parser.parse_args()
    root = Path.cwd()
    files = collect_files(root, args.recursive)
    if not files:
        print("No files found.")
        return
    output = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            content = f"[Could not read file: {exc}]"
        output.append(f"\n{'=' * 40}")
        output.append(f"FILE: {path.relative_to(root)}")
        output.append("=" * 40)
        output.append(content)
    pydoc.pager("\n".join(output))


if __name__ == "__main__":
    raise SystemExit(main())
