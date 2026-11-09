#!/data/data/com.termux/files/usr/bin/env python
"""Categorize installed packages (editable/pure/non-pure) into a requirements.txt file."""

import importlib.metadata
import json
from pathlib import Path
import sysconfig


def get_site_packages_dirs() -> set[Path]:
    """Retrieves all standard site-packages paths for the current environment."""
    paths = set()
    for path_type in ("purelib", "platlib"):
        path_str = sysconfig.get_path(path_type)
        if path_str:
            paths.add(Path(path_str).resolve())
    return paths


def is_editable_install(dist: importlib.metadata.Distribution, dist_path: Path, site_dirs: set[Path]) -> bool:
    """Determines if a package is an editable installation."""
    # Check if the code path lives outside of standard site-packages directories
    if not any(dist_path.is_relative_to(s_dir) for s_dir in site_dirs):
        return True

    # Fallback: Check if PEP 660 direct_url.json explicitly marks it as an editable project
    try:
        direct_url_content = dist.read_text("direct_url.json")
        if direct_url_content:
            data = json.loads(direct_url_content)
            if data.get("dir_info", {}).get("editable", False):
                return True
    except Exception:
        pass

    return False


def is_pure_package(dist: importlib.metadata.Distribution) -> bool:
    """Determines if a package is pure Python or contains binary compilation wheels."""
    try:
        wheel_content = dist.read_text("WHEEL")
        if wheel_content:
            for line in wheel_content.splitlines():
                if line.lower().startswith("root-is-purelib:"):
                    return "true" in line.lower()
    except Exception:
        pass

    if dist.files:
        binary_extensions = {".so", ".pyd", ".dylib", ".dll"}
        for file_ref in dist.files:
            if Path(file_ref).suffix.lower() in binary_extensions:
                return False

    return True


def main():
    site_dirs = get_site_packages_dirs()

    # Storage bucket for each target category
    segments = {"editable": [], "pure": [], "not_pure": []}

    for dist in importlib.metadata.distributions():
        if not dist.locate_file(""):
            continue

        dist_path = Path(dist.locate_file("")).resolve()
        pkg_name = dist.metadata["Name"]
        pkg_version = dist.version
        pkg_format = f"{pkg_name}=={pkg_version}"

        # 1. Evaluate Segment: Editable Layouts
        if is_editable_install(dist, dist_path, site_dirs):
            # Attempt to output as an installable pip local path requirement if editable path is known
            segments["editable"].append(f"-e {dist_path.parent}  # {pkg_name}=={pkg_version}")

        # 2. Evaluate Segment: Pure Python Wheels
        elif is_pure_package(dist):
            segments["pure"].append(pkg_format)

        # 3. Evaluate Segment: Compiled / Platform C-Extensions
        else:
            segments["not_pure"].append(pkg_format)

    # Sort each individual segment alphabetically
    for key in segments:
        segments[key].sort(key=str.lower)

    # Build the structured requirements.txt string contents
    output_lines = []

    output_lines.append("# editable")
    output_lines.extend(segments["editable"] if segments["editable"] else ["# No packages found"])
    output_lines.append("")  # Blank spacer newline

    output_lines.append("# pure")
    output_lines.extend(segments["pure"] if segments["pure"] else ["# No packages found"])
    output_lines.append("")

    output_lines.append("# not pure")
    output_lines.extend(segments["not_pure"] if segments["not_pure"] else ["# No packages found"])

    # Write out block to file
    output_file = Path("requirements.txt")
    output_file.write_text("\n".join(output_lines) + "\n", encoding="utf-8")

    print(f"Successfully generated categorized file: {output_file.resolve()}")
    print(
        f"-> Editable: {len(segments['editable'])} | Pure: {len(segments['pure'])} | Not Pure: {len(segments['not_pure'])}"
    )


if __name__ == "__main__":
    main()
