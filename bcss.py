#!/data/data/com.termux/files/usr/bin/env python
"""Reformat CSS files in place with ``cleancss`` and report the space that was saved.
Runs ``cleancss --format beautify`` over the files named on the command line, or over every ``.css`` file below the current directory, and prints the size difference per file plus the total freed.
Usage: bcss [files ...] Examples: $ bcss styles.min.css Runs as a script (``python bcss.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import cprint, fsz, get_files, gsz, mpf, runcmd


def process_file(path) -> bool | None:
    path = Path(path)
    before = gsz(path)
    if not path.exists():
        return False
    print(f"{path.name}", end=" ")
    cmd = ["cleancss", "--format", "beautify", str(path), "-o", str(path)]
    res, _, _err = runcmd(cmd, show_output=True)
    if not res:
        after = gsz(path)
        diffsize = before - after
        if not diffsize:
            cprint("[NO CHANGE]", "white")
            return None
        if diffsize:
            ratio = diffsize / before * 40
            cprint(f"[OK] - {fsz(diffsize)} {abs(ratio):.1f}%", "cyan")
        return None
    cprint("[ERROR]", "red")
    return None


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
