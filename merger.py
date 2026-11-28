#!/data/data/com.termux/files/usr/bin/env python
"""Merge files into a single bundle, and split a bundle back apart.

Concatenates the given files with boundary markers into one output; --unmerge splits an existing bundle back into the original files under <stem>_unmerged/.
"""

from __future__ import annotations
import argparse
from pathlib import Path
import subprocess

from dh import atomic_write, get_nobinary, get_random_filename, should_skip


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


def display_path(path: Path, cwd: Path) -> str:
    try:
        return path.relative_to(cwd).as_posix()
    except ValueError:
        return path.as_posix()


def build_merged_content(files_content: list[tuple[Path, str]], cwd: Path) -> str:
    merged_content: str = ""

    for path, content in files_content:
        merged_content += f"# File: {display_path(path, cwd)}\n"
        merged_content += content

        if not content.endswith("\n"):
            merged_content += "\n"

    return merged_content


def merged_content_byte_size(files_content: list[tuple[Path, str]], cwd: Path) -> int:
    total: int = 0

    for path, content in files_content:
        total += len(f"# File: {display_path(path, cwd)}\n".encode("utf-8"))
        total += len(content.encode("utf-8"))

        if not content.endswith("\n"):
            total += 1

    return total


def copy_to_clipboard(content: str) -> None:
    if not content:
        return

    size: int = len(content.encode("utf-8"))

    if size > MAX_CLIPBOARD_BYTES:
        print(f"ℹ️  Merged content is {size:,} bytes (> 1 MB), skipping clipboard.")
        return

    runcmd(["termux-clipboard-set", str(content)])
    print("✅ merged content copied to clipboard.")


def write_merged_file(
    output_file: Path,
    files_content: list[tuple[Path, str]],
    cwd: Path,
) -> None:
    try:
        total_size: int = 0
        file_count: int = 0

        with output_file.open("w", encoding="utf-8") as output:
            for path, content in files_content:
                header: str = f"# File: {display_path(path, cwd)}\n"
                output.write(header)
                output.write(content)

                if not content.endswith("\n"):
                    output.write("\n")

                total_size += len(header.encode("utf-8")) + len(content.encode("utf-8"))

                if not content.endswith("\n"):
                    total_size += 1

                file_count += 1

        print(f"✅ Merged {file_count} files ({total_size:,} bytes) into: {output_file}")

    except OSError as error:
        print(f"❌ Error writing output file {output_file}: {error}")

        if output_file.exists():
            output_file.unlink()


def collect_files(inputs: list[Path], cwd: Path) -> list[Path]:
    """Collect text files from explicit inputs, or recursively from cwd."""
    if not inputs:
        return sorted(path for path in get_nobinary(cwd) if path.is_file() and not should_skip(path))

    collected: set[Path] = set()

    for item in inputs:
        resolved_item: Path = item.expanduser().resolve()

        if resolved_item.is_dir():
            collected.update(path for path in get_nobinary(resolved_item) if path.is_file() and not should_skip(path))
        elif resolved_item.is_file():
            if not should_skip(resolved_item):
                collected.add(resolved_item)
        else:
            print(f"❌ Input not found: {item}")

    return sorted(collected)


def merge_files_by_type(
    files: list[Path],
    cwd: Path,
    ext_filter: list[str] | None = None,
    group_by_ext: bool = False,
    write: bool = False,
    output_dir: Path | None = None,
) -> list[Path]:
    if ext_filter:
        normalized_extensions: list[str] = [extension.lower().lstrip(".") for extension in ext_filter]
        files = [path for path in files if get_file_extension(path) in normalized_extensions]

    valid_files: list[tuple[Path, str]] = []

    for path in files:
        if should_skip(path):
            continue

        content: str | None = read_file(path)

        if content is not None and content.strip():
            valid_files.append((path, content))

    if not valid_files:
        print("ℹ️  No files to merge.")
        return []

    if not group_by_ext:
        output_files: list[Path] = []
        total_size: int = merged_content_byte_size(valid_files, cwd)

        if write:
            base_dir: Path = output_dir if output_dir is not None else cwd
            base_dir.mkdir(parents=True, exist_ok=True)
            output_file: Path = base_dir / f"{get_random_filename()}.txt"

            extensions: set[str] = {get_file_extension(path) for path, _ in valid_files}

            if len(extensions) == 1 and ext_filter is None:
                extension: str = next(iter(extensions))

                if extension:
                    output_file = base_dir / f"{get_random_filename()}.{extension}"

            write_merged_file(output_file, valid_files, cwd)
            output_files.append(output_file)

        if total_size <= MAX_CLIPBOARD_BYTES:
            copy_to_clipboard(build_merged_content(valid_files, cwd))
        else:
            print(f"ℹ️  Merged content is {total_size:,} bytes (> 1 MB), skipping clipboard.")

        return output_files

    ext_groups: dict[str, list[tuple[Path, str]]] = {}

    for path, content in valid_files:
        extension: str = get_file_extension(path)

        if extension not in ext_groups:
            ext_groups[extension] = []

        ext_groups[extension].append((path, content))

    output_files: list[Path] = []
    combined_parts: list[str] = []
    combined_size: int = 0
    clipboard_skipped: bool = False

    base_output_dir: Path = output_dir if output_dir is not None else cwd / "merged"

    if write:
        base_output_dir.mkdir(parents=True, exist_ok=True)

    for extension, group_files in ext_groups.items():
        group_content: str = build_merged_content(group_files, cwd)

        if not clipboard_skipped:
            group_bytes: int = len(group_content.encode("utf-8"))

            if combined_size + group_bytes <= MAX_CLIPBOARD_BYTES:
                combined_parts.append(group_content)
                combined_size += group_bytes
            else:
                clipboard_skipped = True
                print("ℹ️  Combined merged content exceeds 1 MB, skipping clipboard.")

        if write:
            output_file: Path = (
                base_output_dir / f"{get_random_filename()}.{extension}"
                if extension
                else base_output_dir / f"{get_random_filename()}.txt"
            )
            write_merged_file(output_file, group_files, cwd)
            output_files.append(output_file)

    if not clipboard_skipped and combined_parts:
        copy_to_clipboard("".join(combined_parts))

    return output_files


def merge_files(args: argparse.Namespace) -> None:
    cwd: Path = Path.cwd()
    inputs: list[Path] = list(args.inputs)
    files: list[Path] = collect_files(inputs, cwd)

    if not files:
        print("ℹ️  No input files found.")
        return

    if args.extensions:
        print(f"🔍 Filtering for extensions: {', '.join(args.extensions)}")

    output_dir: Path | None = None

    if args.output:
        output_dir = args.output.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    output_files: list[Path] = merge_files_by_type(
        files,
        cwd,
        ext_filter=args.extensions,
        group_by_ext=args.group,
        write=args.write,
        output_dir=output_dir,
    )

    if args.group and output_files:
        effective_output_dir: Path = output_dir if output_dir is not None else cwd / "merged"
        print(f"📁 All merged files saved in: {effective_output_dir}")


def unmerge_file(merged_file: Path, output_dir: Path) -> list[Path]:
    if not merged_file.is_file():
        print(f"❌ File not found: {merged_file}")
        return []

    try:
        content: str = merged_file.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError) as error:
        print(f"❌ Cannot read {merged_file}: {error}")
        return []

    lines: list[str] = content.split("\n")
    marker: str = "# File: "
    current_path: Path | None = None
    buffer: list[str] = []
    written: list[Path] = []

    def flush() -> None:
        if current_path is None:
            return

        target: Path = output_dir / current_path

        try:
            target.parent.mkdir(parents=True, exist_ok=True)

            if buffer and buffer[-1] == "":
                buffer.pop()

            atomic_write(target, "\n".join(buffer), encoding="utf-8")
            written.append(target)

        except OSError as error:
            print(f"❌ Error writing {target}: {error}")

    for line in lines:
        if line.startswith(marker):
            flush()

            relative_path: str = line[len(marker) :].strip()
            current_path = Path(relative_path) if relative_path else None
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
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "Merge one or more text files or directories. "
            "With no input, recursively merge text files in the "
            "current directory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python merger.py                        # Merge text files recursively in .
  python merger.py file1.txt src/         # Merge a file and a directory
  python merger.py -w                     # Merge and write output to file(s)
  python merger.py -e py cpp              # Merge only .py and .cpp files
  python merger.py -w -c                  # Group files by extension and write
  python merger.py -c -e py cpp           # Group .py and .cpp files by extension
  python merger.py -u merged.txt          # Split a previously merged file
        """,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="One or more text files or directories to merge",
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
        help=('Group files by extension and output multiple files in the "merged" directory'),
    )
    parser.add_argument(
        "-w",
        "--write",
        action="store_true",
        help=("Write merged output to file(s) (default: only copy to clipboard)"),
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Directory in which merged output file(s) are written",
    )
    parser.add_argument(
        "-u",
        "--unmerge",
        action="store_true",
        help=("Split a previously merged file (path given as positional argument)"),
    )
    parser.add_argument(
        "file",
        nargs="?",
        help=("Path to the merged file to split (used with -u/--unmerge)"),
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()

    if args.unmerge:
        if not args.file:
            print("❌ Please provide the merged file path: python merger.py -u <file>")
            return 1

        merged_path: Path = Path(args.file).expanduser()
        output_dir: Path = merged_path.parent / f"{merged_path.stem}_unmerged"

        unmerge_file(merged_path, output_dir)
        return 0

    merge_files(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
