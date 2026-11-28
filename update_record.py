#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from loguru import logger

SITE_PACKAGES = Path("/data/data/com.termux/files/usr/lib/python3.14/site-packages")
RECORD_NAME = "RECORD"


class PkgProcessor(Protocol):
    def __call__(self, pkg_name: str) -> tuple[str, list[str], list[str]]: ...


@dataclass(slots=True)
class PkgInfo:
    name: str
    dist_info: Path
    record_path: Path


def detect_site_packages() -> Path:
    if hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    ):
        return (
            Path(sys.prefix)
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    return SITE_PACKAGES


def find_pkg(pkg_name: str, site_packages: Path) -> PkgInfo | None:
    dist_info = site_packages / f"{pkg_name.replace('-', '_')}-*.dist-info"
    matches = list(site_packages.glob(dist_info.name))
    if not matches:
        return None
    record_path = matches[0] / RECORD_NAME
    if not record_path.exists():
        return None
    return PkgInfo(pkg_name, matches[0], record_path)


def hash_file(path: Path) -> str:
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def parse_record(record_path: Path) -> list[tuple[str, str | None, str | None]]:
    entries: list[tuple[str, str | None, str | None]] = []
    for line in record_path.read_text().splitlines():
        parts = line.split(",")
        if len(parts) >= 1:
            path = parts[0]
            hash_val = parts[1] if len(parts) > 1 and parts[1] else None
            size = parts[2] if len(parts) > 2 and parts[2] else None
            entries.append((path, hash_val, size))
    return entries


def process_pkg(pkg_name: str, site_packages: Path) -> tuple[str, list[str], list[str]]:
    errors: list[str] = []
    removed: list[str] = []

    pkg_info = find_pkg(pkg_name, site_packages)
    if pkg_info is None:
        return (
            pkg_name,
            [f"Package {pkg_name} not found or has no RECORD file"],
            removed,
        )

    entries = parse_record(pkg_info.record_path)
    new_entries: list[tuple[str, str | None, str | None]] = []

    for path, hash_val, size in entries:
        full_path = site_packages / path

        if path.endswith(".pyc"):
            removed.append(path)
            continue

        if not full_path.exists():
            removed.append(path)
            errors.append(f"Missing file: {path}")
            continue

        if full_path.is_file():
            try:
                new_hash = f"sha256={hash_file(full_path)}"
                new_size = str(full_path.stat().st_size)
                new_entries.append((path, new_hash, new_size))
            except OSError as e:
                errors.append(f"Failed to hash {path}: {e}")
                new_entries.append((path, hash_val, size))
        else:
            new_entries.append((path, hash_val, size))

    if new_entries:
        lines = []
        for path, hash_val, size in new_entries:
            parts = [path]
            if hash_val:
                parts.append(hash_val)
            if size:
                parts.append(size)
            lines.append(",".join(parts))
        pkg_info.record_path.write_text("\n".join(lines) + "\n")

    return pkg_name, errors, removed


def process_all(site_packages: Path) -> None:
    dist_infos = list(site_packages.glob("*.dist-info"))
    pkg_names = [d.name.replace(".dist-info", "") for d in dist_infos]

    with ProcessPoolExecutor() as executor:
        futures = {
            executor.submit(process_pkg, name, site_packages): name
            for name in pkg_names
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                pkg_name, errors, removed = future.result()
                if errors:
                    for err in errors:
                        logger.warning(f"[{pkg_name}] {err}")
                if removed:
                    logger.info(
                        f"[{pkg_name}] Removed {len(removed)} entries: {', '.join(removed[:5])}{'...' if len(removed) > 5 else ''}"
                    )
                else:
                    logger.success(f"[{pkg_name}] Updated successfully")
            except Exception as e:
                logger.error(f"[{name}] Processing failed: {e}")


def process_single(pkg_name: str, site_packages: Path) -> None:
    pkg_name, errors, removed = process_pkg(pkg_name, site_packages)
    if errors:
        for err in errors:
            logger.warning(f"[{pkg_name}] {err}")
    if removed:
        logger.info(
            f"[{pkg_name}] Removed {len(removed)} entries: {', '.join(removed[:5])}{'...' if len(removed) > 5 else ''}"
        )
    else:
        logger.success(f"[{pkg_name}] Updated successfully")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update RECORD files for installed packages"
    )
    parser.add_argument("pkg", nargs="?", help="Package name to process")
    parser.add_argument(
        "-a", "--all", action="store_true", help="Process all installed packages"
    )
    args = parser.parse_args()

    site_packages = detect_site_packages()
    logger.info(f"Using site-packages: {site_packages}")

    if args.all:
        process_all(site_packages)
    elif args.pkg:
        process_single(args.pkg, site_packages)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
