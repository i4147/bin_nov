#!/data/data/com.termux/files/usr/bin/env python
"""Delete the empty files below the current directory.
Collects the zero-byte files (``__init__.py`` excluded), lists them and removes them, printing how many were deleted and how many failed.
Usage: rmempty Examples: $ rmempty Runs as a script (``python rmempty.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

from dh import cprint, get_files


def wait_for_keypress(timeout: float) -> bool:
    if timeout <= 0:
        return False
    import select

    sys.stdout.flush()
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if r:
        sys.stdin.readline()
        return True
    return False


def main() -> int:
    cwd = Path.cwd()
    zeroto100 = "-z" in sys.argv
    files = get_files(cwd)
    empty_files = [p for p in files if p.stat().st_size == 0 and p.name != "__init__.py"]
    found = len(empty_files)
    if not found:
        cprint("no empty files found", "cyan")
        sys.exit(0)
    cprint(f"{found} empty files found.", "cyan")
    for empty_file in empty_files:
        cprint(f"    - {empty_file.relative_to(cwd)}", "yellow")
        empty_file.unlink()

    if zeroto100:
        small_files = [p for p in files if p.stat().st_size and p.stat().st_size < 100]
    for f in small_files:
        cprint(f"    - {f.relative_to(cwd)}", "blue")
        f.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
