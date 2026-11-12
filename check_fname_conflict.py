#!/data/data/com.termux/files/usr/bin/env python
"""Flag ``.py`` file names that shadow a standard-library or installed third-party module.
Compares the stem of every Python file below the current directory against :data:`sys.stdlib_module_names` and against the distribution names listed in ``/sdcard/data/pip.txt`` (``pip freeze`` output), and exits with status 1 when a conflict is found.
Usage: check_fname_conflict Examples: $ check_fname_conflict Runs as a script (``python check_fname_conflict.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import sys

PIP_LIST: Path = Path("/sdcard/data/pip.txt")
TARGET_DIR = Path.cwd()


def load_third_party(path: Path) -> set[str]:
    names: set[str] = set()
    if not path.is_file():
        print(f"⚠️  {path} not found — skipping 3rd-party check", file=sys.stderr)
        return names
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==")[0].split(">=")[0].split("<=")[0]
        name = name.split()[0].strip()
        name = name.replace("-", "_").lower()
        if name:
            names.add(name)
    return names


def main() -> int:
    stdlib = {n.lower() for n in getattr(sys, "stdlib_module_names", set())}
    third_party = load_third_party(PIP_LIST)
    if not stdlib:
        print(
            "⚠️  sys.stdlib_module_names unavailable (needs Python 3.10+)",
            file=sys.stderr,
        )
    print(f"📂 Scanning: {TARGET_DIR}")
    print(f"📦 stdlib entries: {len(stdlib)} | 3rd-party entries: {len(third_party)}\n")
    conflicts = 0
    for py in sorted(TARGET_DIR.rglob("*.py")):
        stem = py.stem.lower()
        hits = []
        if stem in stdlib:
            hits.append("stdlib")
        if stem in third_party:
            hits.append("3rd-party")
        if hits:
            conflicts += 1
            rel = py.relative_to(TARGET_DIR)
            print(f"❌ {rel}  →  conflicts with: {', '.join(hits)}")
    if conflicts == 0:
        print("✅ No conflicts found.")
    else:
        print(f"\n⚠️  {conflicts} conflicting file(s).")
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
