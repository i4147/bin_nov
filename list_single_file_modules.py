#!/data/data/com.termux/files/usr/bin/env python
"""List system-site distributions that install exactly one module: a top-level .py or a single-folder package with only __init__.py."""

from __future__ import annotations

import json
import sys
import sysconfig
from importlib.metadata import Distribution
from multiprocessing import Pool
from pathlib import Path

from loguru import logger

WORKERS = 4


def system_site_dirs() -> list[Path]:
    """Return purelib/platlib of the base interpreter, ignoring any active venv."""
    scheme_vars = {"base": sys.base_prefix, "platbase": sys.base_prefix}
    dirs = {
        Path(sysconfig.get_path("purelib", vars=scheme_vars)),
        Path(sysconfig.get_path("platlib", vars=scheme_vars)),
    }
    return sorted(d for d in dirs if d.is_dir())


def find_dist_infos(site_dirs: list[Path]) -> list[Path]:
    found: set[Path] = set()
    for site in site_dirs:
        found.update(site.glob("*.dist-info"))
        found.update(site.glob("*.egg-info"))
    return sorted(found)


def parse_record(dist: Distribution) -> list[str] | None:
    record = dist.read_text("RECORD")
    if record is None:
        return None
    return [
        line.split(",", 1)[0].strip() for line in record.splitlines() if line.strip()
    ]


def is_editable(dist: Distribution, entries: list[str]) -> bool:
    """Detect PEP 660 editable installs via direct_url.json or setuptools editable artifacts."""
    direct_url = dist.read_text("direct_url.json")
    if direct_url:
        try:
            if json.loads(direct_url).get("dir_info", {}).get("editable", False):
                return True
        except json.JSONDecodeError:
            logger.warning(f"Unparseable direct_url.json in {dist.metadata['Name']}")
    return any(
        entry.startswith("__editable__")
        or (entry.startswith("__editable___") and entry.endswith("_finder.py"))
        for entry in entries
    )


def installed_module_files(entries: list[str]) -> list[str]:
    """Files the distribution installs into site-packages, excluding its own metadata and bytecode."""
    kept = []
    for path in entries:
        if path.startswith(".."):  # console scripts outside site-packages
            continue
        parts = Path(path).parts
        if not parts:
            continue
        head = parts[0]
        if head.endswith((".dist-info", ".egg-info", ".data")):
            continue
        if "__pycache__" in parts:
            continue
        kept.append(path)
    return kept


def describe_single_module(path: str) -> str | None:
    """Return a label if the file is a qualifying single module, otherwise None."""
    parts = Path(path).parts
    if len(parts) == 1 and path.endswith(".py"):
        return path
    if len(parts) == 2 and parts[1] == "__init__.py":
        return path
    return None


def inspect_dist(dist_path: Path) -> tuple[str, str, str] | None:
    try:
        dist = Distribution.at(dist_path)
        name = dist.metadata["Name"] or dist_path.stem
        entries = parse_record(dist)
        if entries is None:
            return None
        if is_editable(dist, entries):
            logger.debug(f"Skipping editable install: {name}")
            return None
        files = installed_module_files(entries)
        if len(files) != 1:
            return None
        label = describe_single_module(files[0])
        if label is None:
            return None
        return name, dist.version, label
    except Exception:
        logger.exception(f"Failed to inspect {dist_path}")
        return None


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    site_dirs = system_site_dirs()
    logger.info(f"Scanning {len(site_dirs)} system site dir(s)")
    dist_paths = find_dist_infos(site_dirs)
    logger.info(f"Found {len(dist_paths)} distribution metadata folders")

    matches: dict[str, tuple[str, str]] = {}
    with Pool(WORKERS) as pool:
        for result in pool.imap_unordered(inspect_dist, dist_paths):
            if result is None:
                continue
            name, version, label = result
            matches[name.lower()] = (version, label)

    for name in sorted(matches):
        version, label = matches[name]
        print(f"{name}=={version} -> {label}")

    logger.success(f"{len(matches)} package(s) installing exactly one module")


if __name__ == "__main__":
    main()
