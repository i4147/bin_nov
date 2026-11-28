#!/data/data/com.termux/files/usr/bin/env python
"""
Fix WHEEL files in .whl archives so pip accepts them.

For a wheel to be accepted by pip, the tag encoded in its filename (the last three --separated components) must appear as one of the Tag: lines inside its .dist-info/WHEEL file, and that tag must be one of:

cp314-cp314-android_24_armeabi_v7a cp3{9,10,11,12,13,14}-abi3-android_24_armeabi_v7a

Wheels that already satisfy this are left untouched. Otherwise the wheel is fixed so both agree:

* If the filename already carries an acceptable tag (e.g. an abi3 one), the WHEEL file is rewritten to match it, so the tag is preserved. * Otherwise the wheel is renamed to the target tag and the WHEEL file is replaced with TARGET_CONTENT.

Usage:
    python fix_wheel.py # fix wheels in place python fix_wheel.py --check # only report what would change

"""

from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import sys
import tempfile
import zipfile


TARGET_TAG: str = "cp314-cp314-android_24_armeabi_v7a"

ACCEPTABLE_TAG_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^cp314-cp314-android_24_armeabi_v7a$"),
    re.compile(r"^cp3(?:9|1[0-4])-abi3-android_24_armeabi_v7a$"),
]


def make_target_content(tag: str) -> str:
    return f"Wheel-Version: 1.0\nGenerator: setuptools (84.0.0)\nRoot-Is-Purelib: false\nTag: {tag}\n"


TARGET_CONTENT = make_target_content(TARGET_TAG)


def parse_tags(text: str) -> list[str]:
    tags: list[str] = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "Tag":
            tags.append(value.strip())
    return tags


def is_acceptable_tag(tag: str) -> bool:
    return any(p.match(tag) for p in ACCEPTABLE_TAG_PATTERNS)


def extract_tag_from_filename(filename: str) -> str:
    if not filename.endswith(".whl"):
        msg = f"not a .whl file: {filename}"
        raise ValueError(msg)
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        msg = f"malformed wheel filename: {filename}"
        raise ValueError(msg)
    return "-".join(parts[-3:])


def replace_tag_in_filename(filename: str, new_tag: str) -> str:
    if not filename.endswith(".whl"):
        msg = f"not a .whl file: {filename}"
        raise ValueError(msg)
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        msg = f"malformed wheel filename: {filename}"
        raise ValueError(msg)
    return "-".join(parts[:-3] + [new_tag]) + ".whl"


def find_wheel_members(zf: zipfile.ZipFile) -> list[str]:
    return [name for name in zf.namelist() if name.count("/") == 1 and name.endswith(".dist-info/WHEEL")]


def rewrite_wheel(src_path: Path, wheel_member: str, dst_path: Path, content: str) -> None:
    """Write a copy of ``src_path`` with ``wheel_member`` replaced by ``content``,
    then move it to ``dst_path`` (removing ``src_path`` if different)."""
    fd, tmp_name = tempfile.mkstemp(dir=str(src_path.parent), suffix=".whl.tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with (
            zipfile.ZipFile(src_path, "r") as src,
            zipfile.ZipFile(tmp_path, "w") as dst,
        ):
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == wheel_member:
                    data = content.encode("utf-8")
                dst.writestr(info, data, compress_type=info.compress_type)
        os.chmod(tmp_path, src_path.stat().st_mode)
        os.replace(tmp_path, dst_path)
        if dst_path != src_path:
            src_path.unlink()
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fix tags in .whl filenames and their WHEEL metadata.")
    parser.add_argument(
        "--check",
        "--dry-run",
        dest="check",
        action="store_true",
        help="Only report wheels that would change (no modifications).",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(),
        help="Directory to scan for .whl files (default: current directory).",
    )
    args = parser.parse_args(argv)

    wheels = sorted(p for p in args.directory.glob("*.whl") if p.is_file())
    if not wheels:
        print(f"No .whl files found in {args.directory.resolve()}", file=sys.stderr)
        return 0

    changed = 0
    for whl in wheels:
        try:
            filename_tag = extract_tag_from_filename(whl.name)
        except ValueError as exc:
            print(f"SKIP  {whl.name}: {exc}", file=sys.stderr)
            continue

        try:
            with zipfile.ZipFile(whl) as zf:
                members = find_wheel_members(zf)
                if not members:
                    print(f"SKIP  {whl.name}: no .dist-info/WHEEL entry", file=sys.stderr)
                    continue
                if len(members) > 1:
                    print(
                        f"WARN  {whl.name}: multiple WHEEL entries {members}, using {members[0]!r}",
                        file=sys.stderr,
                    )
                wheel_member = members[0]
                current = zf.read(wheel_member).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"ERROR {whl.name}: {exc}", file=sys.stderr)
            continue

        wheel_tags = parse_tags(current)

        # Already consistent and using an acceptable tag → leave untouched.
        if is_acceptable_tag(filename_tag) and filename_tag in wheel_tags:
            continue

        # Decide how to fix:
        #   - filename already acceptable → keep it, fix WHEEL content only
        #   - otherwise                    → rename + replace WHEEL content
        if is_acceptable_tag(filename_tag):
            target_tag = filename_tag
            new_name = whl.name
        else:
            target_tag = TARGET_TAG
            new_name = replace_tag_in_filename(whl.name, TARGET_TAG)
        new_path = whl.parent / new_name

        if new_path != whl and new_path.exists():
            print(
                f"ERROR {whl.name}: target filename {new_name} already exists",
                file=sys.stderr,
            )
            continue

        changed += 1
        rename = new_path != whl

        if args.check:
            if rename:
                print(f"WOULD UPDATE {whl.name} -> {new_name}")
            else:
                print(f"WOULD UPDATE {whl.name}  (content only, tag {target_tag})")
            continue

        try:
            rewrite_wheel(whl, wheel_member, new_path, make_target_content(target_tag))
        except OSError as exc:
            print(f"ERROR updating {whl.name}: {exc}", file=sys.stderr)
            changed -= 1
            continue

        if rename:
            print(f"UPDATED {whl.name} -> {new_name}")
        else:
            print(f"UPDATED {whl.name}  (content only, tag {target_tag})")

    if args.check:
        if changed:
            print(f"{changed} wheel(s) would be updated.", file=sys.stderr)
            return 1
        print("All wheels are already correct.", file=sys.stderr)
    else:
        print(f"{changed} wheel(s) updated.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
