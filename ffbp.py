#!/data/data/com.termux/files/usr/bin/env python
"""Small file utilities in one CLI.
Subcommands: ``clean-bak`` deletes the ``.bak`` files recursively, ``extinfo`` counts the files and total size of an extension, ``find-scripts`` finds the Python scripts without an extension and ``prefix``/``search`` list the entries of the current directory by prefix or substring.
Usage: ffbp {clean-bak,extinfo,find-scripts,prefix,search} [options] Positional arguments: extension File extension to scan for (e.g., 'py' or '.py') prefix Prefix string to match substring Substring to match in filenames Subcommands: clean-bak Recursively delete .bak files extinfo Get file count and total size for an extension find-scripts Find extensionless Python scripts recursively prefix List items in current directory starting with prefix search Search items in current directory containing substring Options: -s, --show-symlinks Include symlinks in the output Examples: $ ffbp extinfo py $ ffbp clean-bak Command-line script: run ``python ffbp.py`` or the ``ffbp`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import sys


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def is_extensionless_python(file_path: Path) -> bool:
    if file_path.suffix or not file_path.is_file():
        return False
    if any(part.startswith(".") or part == "__pycache__" for part in file_path.parts):
        return False
    try:
        with file_path.open("rb") as f:
            header = f.read(1024)
            if b"\0" in header:
                return False
            if header.startswith(b"#!") and b"python" in header.lower():
                return True
    except Exception:
        pass
    return False


def clean_bak_files(cwd: Path) -> None:
    for p in cwd.rglob("*.bak"):
        if p.is_file():
            print(p.relative_to(cwd))
            p.unlink()


def get_extension_info(cwd: Path, extension: str) -> None:
    ext = extension.lstrip(".")
    files = list(cwd.rglob(f"*.{ext}"))
    if not files:
        print(f"No .{ext} files found in current directory")
        return
    total_size = sum(f.stat().st_size for f in files)
    print(f"Total number of .{ext} files: {len(files)}")
    print(f"Total size of .{ext} files: {format_size(total_size)}")


def find_extensionless_scripts(cwd: Path) -> None:
    scripts = [p for p in cwd.rglob("*") if is_extensionless_python(p)]
    if scripts:
        print("Found Python scripts without extension (relative paths):")
        for script in scripts:
            print(script.relative_to(cwd))
    else:
        print("No Python scripts without extension found in the current directory or its subdirectories.")


def list_by_prefix(cwd: Path, prefix: str) -> None:
    for p in cwd.iterdir():
        if p.name.startswith(prefix) and not p.is_symlink():
            print(p.name)


def search_by_substring(cwd: Path, substring: str, show_symlinks: bool) -> None:
    matched = []
    for p in cwd.glob("*"):
        if substring in p.name:
            if not show_symlinks and p.is_symlink():
                continue
            matched.append(p)
    for p in sorted(matched):
        if p.is_symlink():
            print(f"- {p.name} -> {p.resolve()}")
        else:
            print(f"- {p.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified File Utilities CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("clean-bak", help="Recursively delete .bak files")
    p_ext = subparsers.add_parser("extinfo", help="Get file count and total size for an extension")
    p_ext.add_argument("extension", help="File extension to scan for (e.g., 'py' or '.py')")
    subparsers.add_parser("find-scripts", help="Find extensionless Python scripts recursively")
    p_prefix = subparsers.add_parser("prefix", help="List items in current directory starting with prefix")
    p_prefix.add_argument("prefix", help="Prefix string to match")
    p_search = subparsers.add_parser("search", help="Search items in current directory containing substring")
    p_search.add_argument("substring", nargs="?", default="", help="Substring to match in filenames")
    p_search.add_argument("-s", "--show-symlinks", action="store_true", help="Include symlinks in the output")
    args = parser.parse_args()
    cwd = Path.cwd()
    if args.command == "clean-bak":
        clean_bak_files(cwd)
    elif args.command == "extinfo":
        get_extension_info(cwd, args.extension)
    elif args.command == "find-scripts":
        find_extensionless_scripts(cwd)
    elif args.command == "prefix":
        list_by_prefix(cwd, args.prefix)
    elif args.command == "search":
        search_by_substring(cwd, args.substring, args.show_symlinks)


if __name__ == "__main__":
    sys.exit(main())
