#!/data/data/com.termux/files/usr/bin/env python
"""Recursively find the 64-bit ELF binaries of a directory.
Parses the ELF headers with pyelftools, prints each 64-bit binary with its type, machine, entry point and section summary and with ``-r`` deletes the matches; ``-L`` follows the symlinks.
Usage: elf64 DIRECTORY [-r] [-L] Positional arguments: directory Directory to scan Options: -r, --remove Delete each 64-bit ELF binary that is found -L, --follow-symlinks Follow symlinks during traversal Examples: $ elf64 ./lib Command-line script: run ``python elf64.py`` or the ``elf64`` command.
The module is importable as well."""

import argparse
import multiprocessing as mp
from pathlib import Path
import sys
from typing import Any, Iterator

from dh import MAX_WORKERS, is_binary
from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

ELF_TYPES: dict[str, str] = {
    "ET_NONE": "no file type",
    "ET_REL": "relocatable object",
    "ET_EXEC": "executable",
    "ET_DYN": "shared object / PIE",
    "ET_CORE": "core dump",
}
ELF_MACHINES: dict[str, str] = {
    "EM_386": "x86",
    "EM_X86_64": "x86-64",
    "EM_ARM": "ARM",
    "EM_AARCH64": "AArch64",
    "EM_RISCV": "RISC-V",
    "EM_MIPS": "MIPS",
    "EM_PPC": "PowerPC",
    "EM_PPC64": "PowerPC64",
    "EM_S390": "S/390",
    "EM_IA_64": "IA-64",
}


def analyze_file(path: Path) -> dict | None:
    try:
        if not is_binary(str(path)):
            return None
    except (OSError, PermissionError):
        return None
    try:
        with path.open("rb") as f:
            elf = ELFFile(f)
            if elf.elfclass != 64:
                return None
            info = {
                "path": str(path),
                "size": path.stat().st_size,
                "type": ELF_TYPES.get(elf.header["e_type"], elf.header["e_type"]),
                "machine": ELF_MACHINES.get(elf.header["e_machine"], elf.header["e_machine"]),
                "endianness": "little" if elf.little_endian else "big",
                "entry": f"0x{elf.header['e_entry']:x}",
            }
            return info
    except (ELFError, OSError, PermissionError, KeyError):
        return None


def iter_files(root: Path, follow_symlinks: bool = False) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.is_symlink() and not follow_symlinks:
            continue
        yield path


def scan(root: Path, follow_symlinks: bool = False) -> Iterator[dict]:
    paths = iter_files(root, follow_symlinks=follow_symlinks)
    with mp.Pool(processes=MAX_WORKERS) as pool:
        for result in pool.imap_unordered(analyze_file, paths, chunksize=1):
            if result is not None:
                yield result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recursively find 64-bit ELF binaries.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        type=Path,
        help="directory to scan",
    )
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="delete each 64-bit ELF binary that is found",
    )
    parser.add_argument(
        "-L",
        "--follow-symlinks",
        action="store_true",
        help="follow symlinks during traversal",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root: Path = args.directory.resolve()
    if not root.is_dir():
        return 1
    count = 0
    try:
        for info in scan(root, follow_symlinks=args.follow_symlinks):
            count += 1
            path = Path(info["path"])
            try:
                display = path.relative_to(root)
            except ValueError:
                display = path
            print(f"[{count}] {display}")
            print(f"    Path:       {path}")
            print(f"    Size:       {info['size']:,} bytes")
            print(f"    Type:       {info['type']}")
            print(f"    Machine:    {info['machine']}")
            print(f"    Endianness: {info['endianness']}")
            print(f"    Entry:      {info['entry']}")
            if args.remove:
                try:
                    path.unlink()
                    print("    -> Removed")
                except OSError as e:
                    print(f"    -> Failed to remove: {e}", file=sys.stderr)
            print()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    if count:
        print(f"Found {count} 64-bit ELF binary/binaries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
