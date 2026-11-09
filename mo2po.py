#!/data/data/com.termux/files/usr/bin/env python
"""Convert the ``.mo`` translation catalogues back to ``.po``.
Runs ``msgunfmt`` on each ``.mo`` file (or uses a pure-Python fallback with ``--fallback``), writes the ``.po`` next to it and deletes the original unless ``-k`` is given; ``-r`` walks the directories.
Usage: mo2po PATH [-k] [-r] [--fallback] Positional arguments: path Path to .mo file or directory containing .mo files Options: -k, --keep Keep original .mo files (don't remove) -r, --recursive Process directories recursively -v, --verbose Show detailed progress messages --fallback Use pure Python fallback method (doesn't require gettext utilities) Examples: $ mo2po locale/ -r -k Command-line script: run ``python mo2po.py`` or the ``mo2po`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import subprocess
import sys


def check_msgunfmt() -> bool:
    try:
        subprocess.run(
            ["msgunfmt", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def mo_to_po(mo_path: Path, remove_orig: bool = True, verbose: bool = False) -> bool:
    mo_path = Path(mo_path)
    if not mo_path.exists():
        print(f"Error: File not found: {mo_path}")
        return False
    if mo_path.suffix != ".mo":
        print(f"Warning: {mo_path} does not have .mo extension, skipping...")
        return False
    po_path = mo_path.with_suffix(".po")
    try:
        with open(po_path, "w", encoding="utf-8") as po_file:
            subprocess.run(
                ["msgunfmt", str(mo_path)],
                stdout=po_file,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        if po_path.stat().st_size == 0:
            msg = "Generated .po file is empty"
            raise Exception(msg)
        if verbose:
            print(f"Converted: {mo_path} -> {po_path}")
        if remove_orig:
            mo_path.unlink()
            if verbose:
                print(f"Removed original: {mo_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error converting {mo_path}: {e.stderr}")
        if po_path.exists():
            po_path.unlink()
        return False
    except Exception as e:
        print(f"Error converting {mo_path}: {e}")
        if po_path.exists():
            po_path.unlink()
        return False


def mo_to_po_python_only(mo_path: Path, remove_orig: bool = True, verbose: bool = False) -> bool:
    import struct

    mo_path = Path(mo_path)
    po_path = mo_path.with_suffix(".po")
    try:
        with open(mo_path, "rb") as mo_file:
            magic = mo_file.read(4)
            if magic == b"\xde\x12\x04\x95":
                endian = ">"
            elif magic == b"\x95\x04\x12\xde":
                endian = "<"
            else:
                msg = "Invalid .mo file magic number"
                raise ValueError(msg)
            struct.unpack(endian + "I", mo_file.read(4))[0]
            num_strings = struct.unpack(endian + "I", mo_file.read(4))[0]
            orig_table_offset = struct.unpack(endian + "I", mo_file.read(4))[0]
            trans_table_offset = struct.unpack(endian + "I", mo_file.read(4))[0]
            mo_file.seek(orig_table_offset)
            orig_strings = []
            for _ in range(num_strings):
                length = struct.unpack(endian + "I", mo_file.read(4))[0]
                offset = struct.unpack(endian + "I", mo_file.read(4))[0]
                pos = mo_file.tell()
                mo_file.seek(offset)
                string = mo_file.read(length).decode("utf-8")
                mo_file.seek(pos)
                orig_strings.append(string)
            mo_file.seek(trans_table_offset)
            trans_strings = []
            for _ in range(num_strings):
                length = struct.unpack(endian + "I", mo_file.read(4))[0]
                offset = struct.unpack(endian + "I", mo_file.read(4))[0]
                pos = mo_file.tell()
                mo_file.seek(offset)
                string = mo_file.read(length).decode("utf-8")
                mo_file.seek(pos)
                trans_strings.append(string)
        with open(po_path, "w", encoding="utf-8") as po_file:
            po_file.write("# Converted from .mo file\n")
            po_file.write('msgid ""\n')
            po_file.write('msgstr ""\n')
            po_file.write(r'"MIME-Version: 1.0\n"\n')
            po_file.write(r'"Content-Type: text/plain; charset=UTF-8\n"\n')
            po_file.write(r'"Content-Transfer-Encoding: 8bit\n"\n\n')
            for orig, trans in zip(orig_strings, trans_strings, strict=False):
                po_file.write(f'msgid "{orig}"\n')
                po_file.write(f'msgstr "{trans}"\n\n')
        if verbose:
            print(f"Converted (Python-only): {mo_path} -> {po_path}")
        if remove_orig:
            mo_path.unlink()
            if verbose:
                print(f"Removed original: {mo_path}")
        return True
    except Exception as e:
        print(f"Error in Python-only conversion {mo_path}: {e}")
        if po_path.exists():
            po_path.unlink()
        return False


def process_directory(
    directory: Path,
    recursive: bool = False,
    remove_orig: bool = True,
    verbose: bool = False,
    fallback: bool = False,
) -> None:
    directory = Path(directory)
    if not directory.exists():
        print(f"Error: Directory not found: {directory}")
        return
    if recursive:
        mo_files = list(directory.rglob("*.mo"))
    else:
        mo_files = list(directory.glob("*.mo"))
    if not mo_files:
        print(f"No .mo files found in {directory}")
        return
    converter = mo_to_po if not fallback else mo_to_po_python_only
    success_count = 0
    fail_count = 0
    for mo_file in mo_files:
        if converter(mo_file, remove_orig, verbose):
            success_count += 1
        else:
            fail_count += 1
    print(f"\nSummary: {success_count} converted, {fail_count} failed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert .mo files to .po files in-place",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s file.mo
  %(prog)s -k file.mo
  %(prog)s /path/to/locale/
  %(prog)s -r /usr/share/locale/
  %(prog)s -v -r ./locale/
  %(prog)s --fallback file.mo
        """,
    )
    parser.add_argument("path", help="Path to .mo file or directory containing .mo files")
    parser.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep original .mo files (don't remove)",
    )
    parser.add_argument("-r", "--recursive", action="store_true", help="Process directories recursively")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed progress messages")
    parser.add_argument(
        "--fallback",
        action="store_true",
        help="Use pure Python fallback method (doesn't require gettext utilities)",
    )
    args = parser.parse_args()
    path = Path(args.path)
    remove_orig = not args.keep
    if not args.fallback and not check_msgunfmt():
        print("Warning: 'msgunfmt' not found. Install gettext utilities or use --fallback")
        print("Install with: sudo apt install gettext (Debian/Ubuntu) or sudo dnf install gettext (Fedora/RHEL)")
        sys.exit(1)
    if path.is_file():
        converter = mo_to_po if not args.fallback else mo_to_po_python_only
        success = converter(path, remove_orig, args.verbose)
        sys.exit(0 if success else 1)
    elif path.is_dir():
        process_directory(path, args.recursive, remove_orig, args.verbose, args.fallback)
        sys.exit(0)
    else:
        print(f"Error: Path does not exist: {path}")
        sys.exit(1)


if __name__ == "__main__":
    raise SystemExit(main())
