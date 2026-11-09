#!/data/data/com.termux/files/usr/bin/env python
"""Shrink CSS files in place with ``cleancss`` and report the space that was freed.
Runs ``cleancss -O2`` (keeping only duplicate-rule removal) over the files named on the command line, or over every ``.css`` file below the current directory, skipping single-line files, and prints the size difference per file plus the total.
Usage: ccss [files ...] Examples: $ ccss styles.css Runs as a script (``python ccss.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import cprint, fsz, get_files, gsz, mpf, runcmd


def process_file(path) -> bool:
    path = Path(path)
    before = gsz(path)
    if not path.exists():
        return False
    if len(path.read_text().splitlines()) == 1:
        return False
    print(f"{path.name}", end=" ")
    cmd = [
        "cleancss",
        "-O2",
        "all:off;removeDuplicateRules:on",
        str(path),
        "-o",
        str(path),
    ]
    res, _, _err = runcmd(cmd, show_output=True)
    if not res:
        after = gsz(path)
        diffsize = before - after
        if not diffsize:
            cprint("[NO CHANGE]", "white")
        if diffsize:
            ratio = after / before * 40
            cprint(f"[OK] - {fsz(diffsize)} {abs(ratio):.1f}%", "cyan")
        return True
    cprint("[ERROR]", "red")
    return False


def main() -> None:
    args = sys.argv[1:]
    cwd = Path.cwd()
    before = gsz(cwd)
    files = [Path(p) for p in args] if args else get_files(cwd, ext=[".css", ".min.css"])
    _ = mpf(process_file, files)
    diff_size = before - gsz(cwd)
    cprint(f"space freed : {fsz(diff_size)}", "green")


if __name__ == "__main__":
    raise SystemExit(main())
