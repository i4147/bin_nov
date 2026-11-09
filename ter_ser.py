#!/data/data/com.termux/files/usr/bin/env python
"""Minify JavaScript files in place with terser.
Runs ``terser --compress --mangle`` over the ``.js``/``.jsx``/``.jsm``/``.jsc`` files, skipping the ones that are already ``.min.js`` or the notebook bundles, and prints the size reduction for each of them in parallel.
Usage: ter_ser [paths ...] Examples: $ ter_ser static/js Runs as a script (``python ter_ser.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import sys

from dh import get_files, gsz, mpf, rrs, runcmd


EXT: list[str] = [".js", ".jsx", ".jsm", ".jsc"]


def safe_run(path: Path) -> bool:
    cmd = ["terser", "--compress", "--mangle", "--", str(path)]
    res, txt, err = runcmd(cmd, show_output=False)
    if res != 0:
        print(f"Error running terser: {err}", file=sys.stderr)
        return False
    path.write_text(txt, encoding="utf8")
    return True


def process_file(path: Path) -> None:
    path = Path(path)
    if path.name.endswith(".min.js"):
        return
    if "site-packages" in path.parts and "notebook" in path.parts:
        return
    before = gsz(path)
    if not path.exists() or not before:
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) == 1:
        del lines, before
        return
    if safe_run(path):
        after = gsz(path)
        rrs(path, before, after)
    return


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
                files.extend(get_files(p), ext=EXT)
    else:
        files = get_files(cwd, ext=EXT)
    if len(files) == 1:
        process_file(files[0])
        sys.exit(0)
    mpf(process_file, files)


if __name__ == "__main__":
    raise SystemExit(main())
