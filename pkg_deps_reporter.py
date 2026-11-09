#!/data/data/com.termux/files/usr/bin/env python
"""Report the dependencies of the installed packages by reading their ``METADATA``.
Parses the ``Requires-Dist`` lines of a package's dist-info (``-d`` sets the site-packages root) or of every installed package with ``-a``, and prints or writes the dependency report (``-w``).
Usage: pkg_deps_reporter [package] [-a] [-w report.txt] [-d DIR] Positional arguments: package Name of the specific target package to analyze.
Options: -a, --all Process and report dependencies for all installed packages in the tree.
-w, --write Save report to a file.
Defaults to 'dependency_report.txt' if no filename given.
-d, --directory Root path of the site-packages directory to parse (Defaults to current folder '.').
Examples: $ pkg_deps_reporter requests -w deps.txt Command-line script: run ``python pkg_deps_reporter.py`` or the ``pkg_deps_reporter`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re
import sys
from typing import Any, Iterable


def parse_metadata_dependencies(metadata_path: Path) -> dict:
    pkg_info = {"name": "Unknown", "version": "Unknown", "dependencies": []}
    req_pattern = re.compile(r"^Requires-Dist:\s*(.+)$", re.IGNORECASE)
    name_pattern = re.compile(r"^Name:\s*(.+)$", re.IGNORECASE)
    version_pattern = re.compile(r"^Version:\s*(.+)$", re.IGNORECASE)
    try:
        with open(metadata_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip():
                    break
                name_match = name_pattern.match(line)
                if name_match:
                    pkg_info["name"] = name_match.group(1).strip()
                    continue
                version_match = version_pattern.match(line)
                if version_match:
                    pkg_info["version"] = version_match.group(1).strip()
                    continue
                req_match = req_pattern.match(line)
                if req_match:
                    pkg_info["dependencies"].append(req_match.group(1).strip())
    except Exception as e:
        print(f"Error parsing metadata file {metadata_path}: {e}", file=sys.stderr)
    return pkg_info


def find_package_metadata(target_pkg: str, root_dir: Path) -> Path | None:
    normalized_target = target_pkg.lower().replace("-", "_")
    for meta_path in root_dir.rglob("METADATA"):
        parent_name = meta_path.parent.name.lower().replace("-", "_")
        if parent_name.startswith(f"{normalized_target}-"):
            return meta_path
    return None


def generate_report(pkgs_data: list[dict]) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("PYTHON PACKAGE DEPENDENCY DIAGNOSTIC REPORT")
    lines.append("=" * 60)
    for pkg in sorted(pkgs_data, key=lambda x: x["name"].lower()):
        lines.append(f"\n📦 Package: {pkg['name']} (v{pkg['version']})")
        if pkg["dependencies"]:
            lines.append("  └── Dependencies:")
            for dep in sorted(pkg["dependencies"]):
                lines.append(f"      ├── {dep}")
        else:
            lines.append("  └── Dependencies: None")
    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report dependencies of installed Python packages by parsing local METADATA files."
    )
    parser.add_argument("package", nargs="?", help="Name of the specific target package to analyze.")
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Process and report dependencies for all installed packages in the tree.",
    )
    parser.add_argument(
        "-w",
        "--write",
        nargs="?",
        const="dependency_report.txt",
        help="Save report to a file. Defaults to 'dependency_report.txt' if no filename given.",
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=".",
        help="Root path of the site-packages directory to parse (Defaults to current folder '.').",
    )
    args = parser.parse_args()
    root_path = Path(args.directory).resolve()
    if not args.all and not args.package:
        parser.error("You must either specify a target package name or pass the -a/--all flag.")
    pkgs_to_report = []
    if args.all:
        print(f"Scanning target directory '{root_path}' for all package distributions...", file=sys.stderr)
        metadata_files = list(root_path.rglob("METADATA"))
        if not metadata_files:
            print("No METADATA layout fields found in this directory tree.", file=sys.stderr)
            sys.exit(1)
        for meta_path in metadata_files:
            pkgs_to_report.append(parse_metadata_dependencies(meta_path))
    else:
        meta_path = find_package_metadata(args.package, root_path)
        if not meta_path:
            print(f"Error: Could not find METADATA for package '{args.package}' in '{root_path}'.", file=sys.stderr)
            sys.exit(1)
        pkgs_to_report.append(parse_metadata_dependencies(meta_path))
    report_output = generate_report(pkgs_to_report)
    print(report_output)
    if args.write:
        output_file = Path(args.write)
        try:
            output_file.write_text(report_output, encoding="utf-8")
            print(f"\n[✓] Diagnostic report safely written out to: {output_file.resolve()}", file=sys.stderr)
        except IOError as e:
            print(f"\n[✗] Failed to write report file: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
