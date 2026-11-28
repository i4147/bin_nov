#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations
import argparse
from collections import defaultdict
from pathlib import Path
import sys


def find_site_packages() -> Path:
    for path in sys.path:
        if path and "".join(Path(path).parts[-2:]) == "site-packages":
            return Path(path)
    return Path.cwd()


def human_readable_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024.0
        if size < 1024:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} PiB"


def scan_and_clean(site_packages_dir: Path, remove: bool = False) -> None:
    print(f"Scanning directory: {site_packages_dir}\n")

    inventory: dict[Path, dict[str, set[Path]]] = defaultdict(lambda: defaultdict(set))

    for path in site_packages_dir.rglob("*"):
        if not path.is_file():
            continue

        suffix = path.suffix.lower()
        if suffix in (".c", ".cpp", ".pyx", ".so", ".pyd"):
            stem = path.stem
            if ".cpython" in stem:
                stem = stem.split(".cpython")[0]
            inventory[path.parent][stem].add(path)

    total_c_files: int = 0
    removed_c_files: int = 0
    total_freed_bytes: int = 0

    for parent_dir, stems in inventory.items():
        package_printed = False

        for stem, files in stems.items():
            suffixes = {f.suffix.lower() for f in files}
            has_compiled = any(s in (".so", ".pyd") for s in suffixes)
            has_source = any(s in (".c", ".cpp") for s in suffixes)

            if has_compiled and has_source:
                source_files = [f for f in files if f.suffix.lower() in (".c", ".cpp")]

                if not package_printed:
                    try:
                        display_path = parent_dir.relative_to(site_packages_dir)
                    except ValueError:
                        display_path = parent_dir
                    print(f"📦 Package Subdirectory: {display_path}")
                    package_printed = True

                for src_file in source_files:
                    total_c_files += 1
                    status = "[FOUND]"
                    file_size: int = 0
                    try:
                        file_size = src_file.stat().st_size
                    except OSError:
                        file_size = 0

                    if remove:
                        try:
                            src_file.unlink()
                            status = "[REMOVED]"
                            removed_c_files += 1
                            total_freed_bytes += file_size
                        except Exception as e:
                            status = f"[ERROR: Couldn't delete - {e}]"

                    size_info = f", {human_readable_size(file_size)}" if file_size else ""
                    print(f"  {status} {src_file.name} (Compiled binary exists{size_info})")

    print("\n" + "=" * 40)
    print("Scan complete.")
    print(f"Total redundant source files found: {total_c_files}")
    if remove:
        print(f"Successfully removed: {removed_c_files}")
        print(f"Space freed: {human_readable_size(total_freed_bytes)} ({total_freed_bytes} bytes)")
    else:
        print("Run with the '-r' flag to delete these redundant files.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect and optionally remove redundant .c/.cpp files inside site-packages where compiled extensions exist."
    )
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Remove the redundant source files instead of just reporting them",
    )
    parser.add_argument(
        "-d",
        "--directory",
        type=str,
        default=None,
        help="Specify a custom site-packages directory path (defaults to active environment)",
    )

    args = parser.parse_args()

    if args.directory:
        target_dir = Path(args.directory)
    else:
        target_dir = find_site_packages()

    if not target_dir.exists() or not target_dir.is_dir():
        print(
            f"Error: Target directory '{target_dir}' does not exist or is not a directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    scan_and_clean(target_dir, remove=args.remove)


if __name__ == "__main__":
    main()
