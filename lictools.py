#!/data/data/com.termux/files/usr/bin/env python
"""License manager CLI.
Subcommands: ``collect`` concatenates the discovered license files into one file, ``clear`` empties them and ``strip`` removes the license text patterns (read from ``-p``) from every non-binary file, with ``-g`` separating the patterns by blank lines.
Usage: lictools {collect,clear,strip} [-d DIR] [options] Subcommands: collect Concatenate license files into one file clear Empty the contents of discovered license files strip Strip text patterns from non-binary files Options: -d, --dir Target directory (default: current) -o, --output Output file path -e, --exclude Directories to exclude --exts Extensions to target -p, --patterns File containing license patterns -g, --gap Number of newlines separating patterns Examples: $ lictools collect -o LICENSES.txt $ lictools strip -p patterns.txt Command-line script: run ``python lictools.py`` or the ``lictools`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re
import sys
from typing import Generator


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def get_nobinary(directory: Path) -> list[Path]:
    files = []
    for p in directory.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            with p.open("rb") as f:
                if b"\0" in f.read(1024):
                    continue
            files.append(p)
        except Exception:
            pass
    return files


def cmd_collect(directory: Path, output_file: Path, exclude_dirs: set[str]) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    def find_licenses(d: Path) -> Generator[Path, None, None]:
        try:
            for p in d.iterdir():
                if p.is_dir() and p.name not in exclude_dirs:
                    yield from find_licenses(p)
                elif p.is_file() and p.resolve() != output_file.resolve():
                    if "license" in p.name.lower():
                        yield p
        except PermissionError:
            pass

    license_files = list(find_licenses(directory))
    print(f"Found {len(license_files)} files")
    with output_file.open("w", encoding="utf-8") as out:
        for i, file_path in enumerate(license_files, 1):
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                print(f"Skipping unreadable file: {file_path}")
                continue
            out.write(content)
            if i != len(license_files):
                out.write("\n\n\n")
            print(f"Added: {file_path}")
    print(f"\nFinished: {output_file} created.")


def cmd_clear(directory: Path, valid_exts: list[str]) -> None:
    found = []
    for p in directory.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        if p.name.lower().startswith("license") and (p.suffix.lower() in valid_exts or not p.suffix):
            print(p.name, p.suffix)
            found.append(p)
    print(f"Found {len(found)} license files")
    for p in found:
        p.write_text("", encoding="utf-8")


def cmd_strip(directory: Path, patterns_file: Path, gap: int) -> None:
    if not patterns_file.exists():
        print(f"Error: License file not found: {patterns_file}")
        return
    try:
        content = patterns_file.read_text(encoding="utf-8", errors="ignore")
        split_regex = r"\n(?:\s*\n){" + str(gap) + r",}"
        raw_patterns = re.split(split_regex, content)
        patterns = [p.strip() for p in raw_patterns if p.strip()]
    except Exception as e:
        print(f"Error loading patterns from {patterns_file}: {e}")
        return
    if not patterns:
        print("No patterns found. Exiting.")
        return
    print()
    files = get_nobinary(directory)
    if not files:
        print("No files to process.")
        return
    for file_path in files:
        old_size = file_path.stat().st_size
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        new_text = text
        for pat in patterns:
            escaped_pat = re.escape(pat).replace("\n", r"\s*\n\s*")
            new_text = re.sub(escaped_pat, "", new_text, flags=re.IGNORECASE | re.MULTILINE)
        if len(new_text) != len(text):
            file_path.write_text(new_text, encoding="utf-8")
            diff = old_size - file_path.stat().st_size
            print(f"{file_path.name} updated | {format_size(diff)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified License Manager CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    p_collect = subparsers.add_parser("collect", help="Concatenate license files into one file")
    p_collect.add_argument("-d", "--dir", type=Path, default=Path(), help="Target directory (default: current)")
    p_collect.add_argument("-o", "--output", type=Path, default=Path("/sdcard/all2.txt"), help="Output file path")
    p_collect.add_argument("-e", "--exclude", nargs="+", default=[".git"], help="Directories to exclude")
    p_clear = subparsers.add_parser("clear", help="Empty the contents of discovered license files")
    p_clear.add_argument("-d", "--dir", type=Path, default=Path(), help="Target directory (default: current)")
    p_clear.add_argument("--exts", nargs="+", default=[".md", ".txt", ".rst"], help="Extensions to target")
    p_strip = subparsers.add_parser("strip", help="Strip text patterns from non-binary files")
    p_strip.add_argument("-d", "--dir", type=Path, default=Path(), help="Target directory (default: current)")
    p_strip.add_argument(
        "-p", "--patterns", type=Path, default=Path("/sdcard/lic"), help="File containing license patterns"
    )
    p_strip.add_argument("-g", "--gap", type=int, default=3, help="Number of newlines separating patterns")
    args = parser.parse_args()
    if args.command == "collect":
        cmd_collect(args.dir, args.output, set(args.exclude))
    elif args.command == "clear":
        cmd_clear(args.dir, args.exts)
    elif args.command == "strip":
        cmd_strip(args.dir, args.patterns, args.gap)


if __name__ == "__main__":
    sys.exit(main())
