#!/data/data/com.termux/files/usr/bin/env python
"""Classify PyPI packages as pure Python or as shipping native extensions.
Reads the package names from a file, queries the PyPI JSON API for the available wheel tags (in a worker pool) and writes ``pure_python.txt``, ``native_extensions.txt`` and ``not_found.txt`` with the classification.
Usage: purepy PACKAGES.txt [--pool-method map|starmap|imap_unordered|apply_async] Positional arguments: package_list Path to a file with one package per line.
Options: --pool-method Multiprocessing pool method to use for PyPI queries.
Examples: $ purepy packages.txt Command-line script: run ``python purepy.py`` or the ``purepy`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
from multiprocessing.pool import AsyncResult, Pool
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from dh import MAX_WORKERS
from loguru import logger
import requests


POOL_METHODS: Final[tuple[str, ...]] = (
    "map",
    "starmap",
    "imap_unordered",
    "apply_async",
)
PYPI_URL: Final[str] = "https://pypi.org/pypi/{name}/json"
REQUEST_TIMEOUT: Final[int] = 10
NATIVE_MARKERS: Final[tuple[str, ...]] = (
    ".so",
    ".pyd",
    ".dll",
    "win_amd64",
    "manylinux",
    "macosx",
)
PURE_OUTPUT: Final[Path] = Path("pure_python.txt")
NATIVE_OUTPUT: Final[Path] = Path("native_extensions.txt")
MISSING_OUTPUT: Final[Path] = Path("not_found.txt")
PackageResult: TypeAlias = tuple[str, str]


def has_native_wheels(info: dict[str, Any]) -> bool:
    urls = info.get("urls", [])
    if not isinstance(urls, list):
        return False
    for entry in urls:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename", "")).lower()
        if any(marker in filename for marker in NATIVE_MARKERS):
            return True
    return False


def check_package(name: str) -> PackageResult:
    url = PYPI_URL.format(name=name)
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return name, "not_found"
        info = resp.json()
        if not isinstance(info, dict):
            return name, "not_found"
        if has_native_wheels(info):
            return name, "native"
        return name, "pure"
    except Exception as exc:
        logger.debug(f"Error querying {name}: {exc}")
        return name, "not_found"


def _check_package_tuple(item: tuple[str]) -> PackageResult:
    return check_package(item[0])


def _run_pool(packages: Sequence[str], method: str) -> list[PackageResult]:
    with Pool(processes=MAX_WORKERS) as pool:
        if method == "map":
            return pool.map(_check_package_tuple, [(pkg,) for pkg in packages])
        if method == "starmap":
            return pool.starmap(check_package, [(pkg,) for pkg in packages])
        if method == "imap_unordered":
            return list(pool.imap_unordered(_check_package_tuple, [(pkg,) for pkg in packages]))
        if method == "apply_async":
            async_results: list[AsyncResult[PackageResult]] = [
                pool.apply_async(check_package, (pkg,)) for pkg in packages
            ]
            return [result.get() for result in async_results]
    msg = f"Unsupported pool method: {method}"
    raise ValueError(msg)


def load_packages(path: Path) -> list[str]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        logger.error(f"Error reading {path}: {exc}")
        return []


def write_lines(path: Path, lines: Sequence[str]) -> None:
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.error(f"Error writing {path}: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_list", help="Path to a file with one package per line.")
    parser.add_argument(
        "--pool-method",
        choices=POOL_METHODS,
        default="map",
        help="Multiprocessing pool method to use for PyPI queries.",
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    pool_method: str = args.pool_method
    infile: Path = Path(args.package_list)
    if not infile.exists():
        logger.error(f"Package list not found: {infile}")
        return 1
    packages = load_packages(infile)
    if not packages:
        logger.warning("No packages to check.")
        return 0
    pure: set[str] = set()
    native: set[str] = set()
    missing: set[str] = set()
    for pkg, result in _run_pool(packages, pool_method):
        if result == "pure":
            pure.add(pkg)
        elif result == "native":
            native.add(pkg)
        else:
            missing.add(pkg)
    write_lines(PURE_OUTPUT, sorted(pure))
    write_lines(NATIVE_OUTPUT, sorted(native))
    write_lines(MISSING_OUTPUT, sorted(missing))
    logger.info("Done!")
    logger.info(f"Pure Python: {len(pure)}")
    logger.info(f"Native-required: {len(native)}")
    logger.info(f"Not found: {len(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
