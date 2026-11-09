#!/data/data/com.termux/files/usr/bin/env python
"""Sanity check installed Termux Python packages."""


import argparse
from fnmatch import fnmatch
import importlib.metadata
from pathlib import Path
from typing import Any, Iterable

from loguru import logger


def parse_arguments():
    parser = argparse.ArgumentParser(description="Sanity check installed Termux Python packages.")
    parser.add_argument(
        "-i",
        "--ignore",
        action="append",
        default=[],
        help="Glob pattern or filename to ignore when checking package files (e.g., '*.md'). Can be used multiple times.",
    )
    return parser.parse_args()


def should_ignore_file(path: Path, ignore_patterns: list[str]) -> bool:
    if path.suffix == ".pyc":
        return True
    name = path.name
    return any(fnmatch(name, pattern) for pattern in ignore_patterns)


def check_package_files(dist: Any, ignore_patterns: list[str]) -> list[str]:
    missing_files = []
    if dist.files is None:
        return missing_files
    for package_file in dist.files:
        path = Path(dist.locate_file(package_file))
        if should_ignore_file(path, ignore_patterns):
            continue
        if not path.exists():
            missing_files.append(str(package_file))
    return missing_files


def check_package_dependencies(dist: Any, installed_map: dict[str, str]) -> tuple[list[str], list[str]]:
    broken_deps = []
    clean_reqs_for_file = []
    if dist.requires is None:
        return broken_deps, clean_reqs_for_file
    for req_str in dist.requires:
        if "extra ==" in req_str or "extra =" in req_str:
            continue
        parts = req_str.split(";")
        base_requirement = parts[0].strip()
        dep_name = base_requirement
        for op in ["<", ">", "=", "!", "~"]:
            if op in dep_name:
                dep_name = dep_name.split(op)[0].strip()
        if not dep_name:
            continue
        if dep_name.lower() not in installed_map:
            broken_deps.append(base_requirement)
            clean_reqs_for_file.append(base_requirement)
    return broken_deps, clean_reqs_for_file


def main() -> None:
    args = parse_arguments()
    logger.info("Starting Termux site-packages verification scan...\n")
    distributions = list(importlib.metadata.distributions())
    installed_map = {d.metadata["Name"].lower(): d.version for d in distributions}
    unique_missing_deps = set()
    corrupted_packages_count = 0
    broken_deps_count = 0
    for dist in distributions:
        pkg_name = dist.metadata["Name"]
        pkg_version = dist.version
        origin_path = getattr(dist, "_path", "Unknown Environment Space")
        missing_files = check_package_files(dist, args.ignore)
        missing_deps, file_deps = check_package_dependencies(dist, installed_map)
        if missing_files or missing_deps:
            print(f"📦 PACKAGE: {pkg_name} ({pkg_version})")
            print(f"   Location Target: {origin_path}")
            if missing_files:
                corrupted_packages_count += 1
                print(f"   ❌ Missing Files ({len(missing_files)}):")
                for f in missing_files[:5]:
                    print(f"      - {f}")
                if len(missing_files) > 5:
                    print(f"      - ... and {len(missing_files) - 5} more files missing.")
            if missing_deps:
                broken_deps_count += 1
                print("   ⚠️  Unresolved Core Dependencies:")
                for dep in missing_deps:
                    print(f"      - Missing requirement: {dep}")
                for clean_dep in file_deps:
                    unique_missing_deps.add(clean_dep)
            print("-" * 40)
    if unique_missing_deps:
        req_file = Path("requirements.txt")
        with open(req_file, "w", encoding="utf-8") as f:
            for dep in sorted(unique_missing_deps):
                f.write(f"{dep}\n")
        logger.info(f"📝 Saved {len(unique_missing_deps)} unique main dependencies to: {req_file.resolve()}")
    logger.info("=== SCAN SUMMARY ===")
    logger.info(f"Total packages evaluated: {len(distributions)}")
    logger.info(f"Packages with missing files: {corrupted_packages_count}")
    logger.info(f"Packages with missing dependencies: {broken_deps_count}")


if __name__ == "__main__":
    raise SystemExit(main())
