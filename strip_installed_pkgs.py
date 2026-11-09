#!/data/data/com.termux/files/usr/bin/env python
"""Remove the already-installed packages and the stdlib names from a requirements file.
Normalises the requirement names, drops the ones that are installed or part of the standard library, sorts the result and rewrites the file in place, printing how many entries were removed.
Usage: strip_installed_pkgs [requirements.txt] Examples: $ strip_installed_pkgs Runs as a script (``python strip_installed_pkgs.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import STDLIB, get_installed_pkgs


def read_requirements(filename) -> list[str]:
    req_file = Path(filename)
    with req_file.open(encoding="utf-8") as f:
        return [line.strip().replace("-", "_").lower() for line in f if line.strip() and not line.startswith("#")]


def strip_installed_from_requirements(fname: str) -> None:
    installed = get_installed_pkgs()
    installed = [p.lower().replace("-", "_") for p in installed if p]
    lines = read_requirements(fname)
    new_lines = [line for line in lines if line not in installed]
    new_lines = [line for line in new_lines if line not in STDLIB]
    new_lines = sorted(set(new_lines))
    Path(fname).write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    removed = len(lines) - len(new_lines)
    print(f"Removed {removed} packages")


if __name__ == "__main__":
    fn = "requirements.txt"
    if len(sys.argv) > 1:
        fn = sys.argv[1]
    strip_installed_from_requirements(fn)
