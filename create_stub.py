#!/data/data/com.termux/files/usr/bin/env python
"""Generate ``.pyi`` stub files for Python modules with ``stubgen``.
Runs ``stubgen`` on the files named on the command line, or on every ``.py`` file below the current directory, skipping modules whose stub already exists and summarising the files that still have no stub.
Usage: create_stub [files ...] Examples: $ create_stub pkg/ Runs as a script (``python create_stub.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import subprocess
import sys

from dh import get_files, mpf


def process_file(path: Path) -> None:
    path = Path(path)
    print(f"processing {path.name}")
    stubfile = path.with_suffix(".pyi")
    if stubfile.exists():
        print(f"[SKIP] {path.name} (stub already exists)")
        return
    cmd = ["stubgen", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print(f"[OK] {path.name}")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {path.name}")
        print(f"  {e.stderr}")


def main() -> None:
    cwd = Path.cwd()
    args = sys.argv[1:]
    files = [Path(p) for p in args] if args else get_files(cwd, ext=[".py"])
    if len(files) == 1:
        process_file(files[0])
        sys.exit(0)
    mpf(process_file, files)
    stubless = []
    for f in files:
        stubpath = f.with_suffix(".pyi")
        if not stubpath.exists():
            stubless.append(f)
    if stubless:
        print("\nFiles without generated stubs:")
        for k in stubless:
            print(f" - {k.name}")


if __name__ == "__main__":
    raise SystemExit(main())
