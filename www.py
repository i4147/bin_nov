#!/data/data/com.termux/files/usr/bin/env python
"""Move every ``.whl`` below the current directory to ``/sdcard/whl``.
Finds the wheels recursively, overwrites the targets of the same name in ``/sdcard/whl`` and deletes the source copies.
Usage: www Examples: $ www Runs as a script (``python www.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path

if __name__ == "__main__":
    cwd = Path.cwd()
    for path in cwd.rglob("*.whl"):
        fname = path.name
        target_dir = Path("/sdcard/whl")
        if not target_dir.exists():
            target_dir.mkdir(exist_ok=True)
        target_path = target_dir / fname
        if target_path.exists():
            target_path.unlink()
        data = path.read_bytes()
        target_path.write_bytes(data)
        path.unlink()
        print(f"{path.name} -> {target_path.name}")
    print("done")
