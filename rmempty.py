#!/data/data/com.termux/files/usr/bin/env python
"""Delete empty files below the current directory (-z also <100-byte files).

Skips __init__.py; lists every match and unlinks them in place. Permanent deletion with no confirmation prompt.
"""

from __future__ import annotations
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


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def main() -> int:
    cwd = Path.cwd()
    zeroto100 = "-z" in sys.argv
    files = get_files(cwd)

    empty_files: list[Path] = [p for p in files if p.name != "__init__.py" and _size(p) == 0]

    small_files: list[Path] = []
    if zeroto100:
        small_files = [p for p in files if p.name != "__init__.py" and 0 < _size(p) < 100]

    if not empty_files and not small_files:
        cprint("no empty or small files found", color="cyan")
        return 0

    if empty_files:
        cprint(f"{len(empty_files)} empty files found.", color="cyan")
        for f in empty_files:
            cprint(f"    - {f.relative_to(cwd)}", color="yellow")
            f.unlink()

    if small_files:
        cprint(f"{len(small_files)} small files (<100 bytes) found.", color="cyan")
        for f in small_files:
            cprint(f"    - {f.relative_to(cwd)}", color="blue")
            f.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
