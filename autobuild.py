#!/data/data/com.termux/files/usr/bin/env python
"""Build every Python package directory below the current directory into wheels.
A directory counts as a package when it holds ``setup.py``, ``setup.cfg`` or ``pyproject.toml``.
Wheels are written to ``./wheels`` by ``python -m build --wheel``, one worker per package, with a success/failure summary at the end.
Usage: autobuild Examples: $ autobuild Runs as a script (``python autobuild.py``) in the directory it should act on; the module can also be imported."""

from multiprocessing import Pool
from pathlib import Path
import subprocess
import sys

from dh import MAX_WORKERS


CURRENT_DIR = Path.cwd()
OUTPUT_DIR = CURRENT_DIR / "wheels"


def is_package_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    return (path / "setup.py").exists() or (path / "pyproject.toml").exists() or (path / "setup.cfg").exists()


def build_wheel(pkg_dir: Path) -> tuple[str, bool, str]:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(OUTPUT_DIR),
                str(pkg_dir),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return (pkg_dir.name, True, "OK")
        else:
            return (pkg_dir.name, False, result.stderr.strip()[-500:])
    except Exception as e:
        return (pkg_dir.name, False, f"Exception: {e}")


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    pkg_dirs = sorted(p for p in CURRENT_DIR.iterdir() if is_package_dir(p) and p != OUTPUT_DIR)
    if not pkg_dirs:
        print("No package directories found in", CURRENT_DIR)
        return
    print(f"Found {len(pkg_dirs)} package(s):")
    for p in pkg_dirs:
        print("  -", p.name)
    print(f"\nBuilding wheels into: {OUTPUT_DIR}")
    print(f"Using {MAX_WORKERS} workers\n")
    results = []
    with Pool(processes=MAX_WORKERS) as pool:
        async_results = [(pkg_dir, pool.apply_async(build_wheel, (pkg_dir,))) for pkg_dir in pkg_dirs]
        for pkg_dir, ar in async_results:
            name, ok, msg = ar.get()
            status = "✓" if ok else "✗"
            print(f"[{status}] {name}: {msg if not ok else 'built'}")
            results.append((name, ok))
    success = sum(1 for _, ok in results if ok)
    failed = len(results) - success
    print(f"\nDone. Success: {success}, Failed: {failed}")


if __name__ == "__main__":
    main()
