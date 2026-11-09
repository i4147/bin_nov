#!/data/data/com.termux/files/usr/bin/env python
"""Merge the text files of a directory into one document.
Concatenates the files of the given extensions (``-e``) into ``merged.txt`` or copies the result to the clipboard, ``-c`` writes one merged file per extension into ``merged/`` and ``-u`` splits a merged file back into its parts.
Usage: merger [-e py js] [-c] [-w] | merger -u MERGED.txt Positional arguments: file Path to the merged file to split (used with -u) Options: -e, --extensions File extensions to merge (e.g., py cpp js) -c, --group Group files by extension, output multiple files in "merged" directory -w, --write Write merged output to file(s) (default: only copy to clipboard) -u, --unmerge Split a previously merged file (path given as positional argument) Examples: $ merger -e py -w $ merger -u merged.txt Command-line script: run ``python merger.py`` or the ``merger`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable, Sequence

from dh import get_nobinary, get_random_filename, should_skip


MAX_CLIPBOARD_BYTES: int = 1024 * 1024


def runcmd(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        print(f"❌ Command not found: {cmd[0]}")
        return 127


def read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return None


def get_file_extension(path: Path) -> str:
    return path.suffix.lstrip(".").lower()


def build_merged_content(files_content: list[tuple[Path, str]], cwd: Path) -> str:
    merged_content = ""
    for path, content in files_content:
        relative_path = path.relative_to(cwd)
        merged_content += f"# File: {relative_path}\n"
        merged_content += content
        if not content.endswith("\n"):
            merged_content += "\n"
    return merged_content


def merged_content_byte_size(files_content: list[tuple[Path, str]], cwd: Path) -> int:
    total = 0
    for path, content in files_content:
        total += len(f"# File: {path.relative_to(cwd)}\n".encode("utf-8"))
        total += len(content.encode("utf-8"))
        if not content.endswith("\n"):
            total += 1
    return total


def copy_to_clipboard(content: str) -> None:
    if not content:
        return
    size = len(content.encode("utf-8"))
    if size > MAX_CLIPBOARD_BYTES:
        print(f"ℹ️  Merged content is {size:,} bytes (> 1 MB), skipping clipboard.")
        return
    runcmd(["termux-clipboard-set", content])
    print("✅ merged content copied to clipboard.")


def write_merged_file(output_file: Path, files_content: list[tuple[Path, str]], cwd: Path) -> None:
    try:
        total_size = 0
        file_count = 0
        with output_file.open("w", encoding="utf-8") as fo:
            for path, content in files_content:
                relative_path = path.relative_to(cwd)
                header = f"# File: {relative_path}\n"
                fo.write(header)
                fo.write(content)
                if not content.endswith("\n"):
                    fo.write("\n")
                total_size += len(header.encode("utf-8")) + len(content.encode("utf-8"))
                if not content.endswith("\n"):
                    total_size += 1
                file_count += 1
        print(f"✅ Merged {file_count} files ({total_size:,} bytes) into: {output_file}")
    except OSError as e:
        print(f"❌ Error writing output file {output_file}: {e}")
        if output_file.exists():
            output_file.unlink()


def merge_files_by_type(
    files: list[Path],
    cwd: Path,
    ext_filter: list[str] | None = None,
    group_by_ext: bool = False,
    write: bool = False,
) -> list[Path]:
    if ext_filter:
        ext_filter = [e.lower() for e in ext_filter]
        files = [f for f in files if get_file_extension(f).lower() in ext_filter]
    valid_files: list[tuple[Path, str]] = []
    for path in files:
        if should_skip(path):
            continue
        content = read_file(path)
        if content is not None and content.strip():
            valid_files.append((path, content))
    if not valid_files:
        print("ℹ️  No files to merge.")
        return []
    if not group_by_ext:
        output_files: list[Path] = []
        total_size = merged_content_byte_size(valid_files, cwd)
        if write:
            output_file = cwd / f"{get_random_filename()}.txt"
            extensions = {get_file_extension(f) for f, _ in valid_files}
            if len(extensions) == 1 and ext_filter is None:
                ext = next(iter(extensions))
                if ext:
                    output_file = cwd / f"{get_random_filename()}.{ext}"
            write_merged_file(output_file, valid_files, cwd)
            output_files.append(output_file)
        if total_size <= MAX_CLIPBOARD_BYTES:
            copy_to_clipboard(build_merged_content(valid_files, cwd))
        else:
            print(f"ℹ️  Merged content is {total_size:,} bytes (> 1 MB), skipping clipboard.")
        return output_files
    ext_groups: dict[str, list[tuple[Path, str]]] = {}
    for path, content in valid_files:
        ext = get_file_extension(path)
        if ext not in ext_groups:
            ext_groups[ext] = []
        ext_groups[ext].append((path, content))
    output_files = []
    combined_parts: list[str] = []
    combined_size = 0
    clipboard_skipped = False
    output_dir = cwd / "merged"
    if write:
        output_dir.mkdir(exist_ok=True)
    for ext, group_files in ext_groups.items():
        group_content = build_merged_content(group_files, cwd)
        if not clipboard_skipped:
            group_bytes = len(group_content.encode("utf-8"))
            if combined_size + group_bytes <= MAX_CLIPBOARD_BYTES:
                combined_parts.append(group_content)
                combined_size += group_bytes
            else:
                clipboard_skipped = True
                print("ℹ️  Combined merged content exceeds 1 MB, skipping clipboard.")
        if write:
            output_file = (
                output_dir / f"{get_random_filename()}.{ext}" if ext else output_dir / f"{get_random_filename()}.txt"
            )
            write_merged_file(output_file, group_files, cwd)
            output_files.append(output_file)
    if not clipboard_skipped and combined_parts:
        copy_to_clipboard("".join(combined_parts))
    return output_files


def merge_files(args: argparse.Namespace) -> None:
    cwd = Path.cwd()
    files = list(get_nobinary(cwd))
    if args.extensions:
        print(f"🔍 Filtering for extensions: {', '.join(args.extensions)}")
    output_files = merge_files_by_type(
        files,
        cwd,
        ext_filter=args.extensions,
        group_by_ext=args.group,
        write=args.write,
    )
    if args.group and output_files:
        print(f"📁 All merged files saved in: {cwd / 'merged'}")


def unmerge_file(merged_file: Path, output_dir: Path) -> list[Path]:
    if not merged_file.is_file():
        print(f"❌ File not found: {merged_file}")
        return []
    try:
        content = merged_file.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError) as e:
        print(f"❌ Cannot read {merged_file}: {e}")
        return []
    lines = content.split("\n")
    marker = "# File: "
    current_path: Path | None = None
    buffer: list[str] = []
    written: list[Path] = []

    def flush() -> None:
        if current_path is None:
            return
        target = output_dir / current_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(buffer), encoding="utf-8")
            written.append(target)
        except OSError as e:
            print(f"❌ Error writing {target}: {e}")

    for line in lines:
        if line.startswith(marker):
            flush()
            rel = line[len(marker) :].strip()
            current_path = Path(rel) if rel else None
            buffer = []
        elif current_path is not None:
            buffer.append(line)
    flush()
    if not written:
        print("ℹ️  No files extracted (missing '# File:' markers).")
    else:
        print(f"✅ Extracted {len(written)} files into: {output_dir}")
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge text files in the current directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python merger.py                    # Merge all non-binary files (clipboard only)
  python merger.py -w                 # Merge and write output to file(s)
  python merger.py -e py cpp          # Merge only .py and .cpp files
  python merger.py -w -c              # Group files by extension and write to files
  python merger.py -c -e py cpp       # Group .py and .cpp files by extension
  python merger.py -u merged.txt      # Split a previously merged file
        """,
    )
    parser.add_argument(
        "-e",
        "--extensions",
        nargs="+",
        help="File extensions to merge (e.g., py cpp js)",
    )
    parser.add_argument(
        "-c",
        "--group",
        action="store_true",
        help='Group files by extension, output multiple files in "merged" directory',
    )
    parser.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="Write merged output to file(s) (default: only copy to clipboard)",
    )
    parser.add_argument(
        "-u",
        "--unmerge",
        action="store_true",
        help="Split a previously merged file (path given as positional argument)",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Path to the merged file to split (used with -u)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.unmerge:
        if not args.file:
            print("❌ Please provide the merged file path: python merger.py -u <file>")
            sys.exit(1)
        merged_path = Path(args.file)
        output_dir = merged_path.parent / f"{merged_path.stem}_unmerged"
        unmerge_file(merged_path, output_dir)
        return
    merge_files(args)


if __name__ == "__main__":
    main()
