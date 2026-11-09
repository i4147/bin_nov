#!/data/data/com.termux/files/usr/bin/env python
"""Write the missing pip dependencies to ``requirements.txt``.
Runs ``pip check``, parses the ``requires X, which is not installed`` lines, resolves the names against the installed package list (cached for the day, ``--no-cache``/``--refresh-cache``) and appends them to the requirements file, skipping the blacklisted packages.
Usage: reqmaker [--refresh-cache] [--no-cache] Options: --refresh-cache Force refresh the installed-packages cache.
--no-cache Do not read or write the installed-packages cache.
Examples: $ reqmaker Command-line script: run ``python reqmaker.py`` or the ``reqmaker`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
from datetime import date
from pathlib import Path
import re
import subprocess
import sys
import time


REQ = Path("requirements.txt")
CACHE_FILE = Path(".installed_packages_cache.txt")
BLACKLIST = {
    "pydantic-core",
    "h5py",
    "jiter",
    "jupyter-server-ydoc",
    "scipy",
    "rpds-py",
    "nh3",
    "pandas",
    "torch",
    "torchvision",
    "scikit-learn",
    "pynacl",
    "gensim",
    "spacy",
    "onnx",
    "torchaudio",
    "selenium",
    "jupyter-ydoc",
    "tensorflow",
}

PIP_CHECK_RE = re.compile(
    r"^(\S+)\s+.*requires\s+([^,]+),\s+which is not installed\.$",
)


def _list_installed_packages() -> list[str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        print(f"Failed to list installed packages: {e}", file=sys.stderr)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _same_day(a: float, b: float) -> bool:
    return date.fromtimestamp(a) == date.fromtimestamp(b)


def _cached_installed_list(cache_file: Path, refresh: bool = False) -> list[str]:
    if not refresh and cache_file.is_file():
        try:
            if _same_day(cache_file.stat().st_mtime, time.time()):
                return [line.strip() for line in cache_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            pass
    fresh = _list_installed_packages()
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            "\n".join(fresh) + ("\n" if fresh else ""),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"Warning: could not write cache {cache_file}: {e}", file=sys.stderr)
    return fresh


def save_to_req(packages: list[str]) -> None:
    content = "\n".join(packages)
    if content:
        content += "\n"
    REQ.write_text(content, encoding="utf-8")


def run_pip_check() -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        print(f"Failed to run pip check: {e}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def parse_pip_check(output: str) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = defaultdict(list)
    for line in output.splitlines():
        match = PIP_CHECK_RE.match(line.strip())
        if not match:
            continue
        requirer = match.group(1)
        missing_pkg = match.group(2).strip()
        if missing_pkg in BLACKLIST:
            continue
        missing[missing_pkg].append(requirer)
    return dict(missing)


def format_deptree(missing_deps: dict[str, list[str]]) -> None:
    if not missing_deps:
        print("No missing dependencies found.")
        return
    print("required packages:")
    for pkg in sorted(missing_deps):
        requirers = ", ".join(sorted(set(missing_deps[pkg])))
        print(f"  - {pkg} --> {requirers}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect missing pip dependencies and write them to requirements.txt.",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force refresh the installed-packages cache.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the installed-packages cache.",
    )
    args = parser.parse_args()

    if args.no_cache:
        installed = _list_installed_packages()
    else:
        installed = _cached_installed_list(CACHE_FILE, refresh=args.refresh_cache)
    print(f"Installed packages: {len(installed)}")

    output = run_pip_check()
    if not output or "No broken requirements found" in output:
        print("No missing dependencies.")
        save_to_req([])
        return 0

    missing_deps = parse_pip_check(output)

    installed_bases = {pkg.split("==", 1)[0].lower() for pkg in installed}
    for name in list(missing_deps.keys()):
        if name.lower() in installed_bases:
            print(
                f"Warning: {name!r} reported missing but present in installed list; skipping",
                file=sys.stderr,
            )
            del missing_deps[name]

    format_deptree(missing_deps)
    save_to_req(sorted(missing_deps.keys()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
