#!/data/data/com.termux/files/usr/bin/env python
"""Move wheels that contain no code into an ``empty_wheels`` folder.
A ``.whl`` is empty when it has no ``.py``/``.pyi``/``.so`` member; those files are reported and moved into ``./empty_wheels``, invalid archives are skipped with a warning.
Usage: find_empty_wheels Examples: $ find_empty_wheels Runs as a script (``python find_empty_wheels.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import zipfile


def is_empty_wheel(whl_path: Path) -> bool | None:
    try:
        with zipfile.ZipFile(whl_path, "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith((".py", ".so", ".pyi")):
                    return False
    except zipfile.BadZipFile:
        print(f"Warning: {whl_path} is not a valid ZIP file. Skipping.")
        return False
    return True


def main() -> None:
    empty_wheels = []
    cwd = Path.cwd()
    target = cwd / "empty_wheels"
    for whl in cwd.rglob("*.whl"):
        if whl.is_file():
            is_empty = is_empty_wheel(whl)
            if is_empty:
                empty_wheels.append(whl)
    if empty_wheels:
        print(f"\nFound {len(empty_wheels)} empty wheel(s).")
        target.mkdir(exist_ok=True)
        for k in empty_wheels:
            print(k.relative_to(cwd))
            new_path = target / k.name
            k.rename(new_path)
    else:
        print("No empty wheels found.")


if __name__ == "__main__":
    raise SystemExit(main())
