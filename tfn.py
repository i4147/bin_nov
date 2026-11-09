#!/data/data/com.termux/files/usr/bin/env python
"""Rename font files based on their internal metadata."""


import argparse
from pathlib import Path

from dh import FONT_EXTENSIONS, unique_path
from fontTools.ttLib import TTFont


STYLE_MAPPING = {
    "normal": "Regular",
    "regular": "Regular",
    "bold": "Bold",
    "italic": "Italic",
    "bold italic": "BoldItalic",
    "bolditalic": "BoldItalic",
    "semibold": "SemiBold",
    "light": "Light",
    "thin": "Thin",
    "black": "Black",
    "medium": "Medium",
    "ultra light": "UltraLight",
    "extra bold": "ExtraBold",
    "condensed": "Condensed",
    "extended": "Extended",
    "narrow": "Narrow",
}


def get_font_name_and_style(font_path):
    font_path.suffix.lower()
    try:
        font = TTFont(font_path)
        name_table = font.get("name")
        if not name_table:
            return (None, None)
        family_name = subfamily_name = None
        for record in name_table.names:
            name_str = record.string.decode("utf-16-be", errors="ignore").strip()
            if not name_str:
                continue
            if record.nameID == 1:
                family_name = name_str
            elif record.nameID == 2:
                subfamily_name = name_str
        font.close()
        style = "Regular"
        if subfamily_name:
            subfamily_lower = subfamily_name.lower().strip()
            for key, value in STYLE_MAPPING.items():
                if key in subfamily_lower:
                    style = value
                    break
            if style == "Regular" and subfamily_name.lower() != "regular":
                style = subfamily_name
        return (family_name, style)
    except Exception as e:
        print(f"  Warning: Could not read {font_path.name}: {e}")
        return (None, None)


def sanitize_filename(name) -> str:
    if not name:
        return "Unknown"
    sanitized = "".join(c if c.isalnum() or c in ("-", "_", " ") else "_" for c in name)
    sanitized = sanitized.replace(" ", "_").strip("_")
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    return sanitized


def rename_font_file(font_path: Path, apply: bool = False) -> tuple[str | None, str | None]:
    family_name, style = get_font_name_and_style(font_path)
    if not family_name:
        print(f"  Skipping {font_path.name}: Could not extract font family name")
        return (None, None)
    family_name = sanitize_filename(family_name)
    style = sanitize_filename(style)
    ext = font_path.suffix
    new_name = f"{family_name}-{style}{ext}"
    new_path = font_path.parent / new_name
    if font_path == new_path:
        print(f"  {font_path.name} -> already has correct name")
        return (None, None)
    if new_path.exists():
        new_path = unique_path(new_path)
        new_name = new_path.name
    if apply:
        try:
            font_path.rename(new_path)
            return (font_path.name, new_name)
        except Exception as e:
            print(f"  Error renaming {font_path.name}: {e}")
            return (None, None)
    else:
        return (font_path.name, new_name)


def process_directory(directory: Path, recursive: bool = True, apply: bool = False) -> int:
    directory = Path(directory)
    renamed_count = 0
    for item in directory.iterdir():
        if item.is_file() and item.suffix.lower() in FONT_EXTENSIONS:
            original_name, new_name = rename_font_file(item, apply)
            if original_name and new_name:
                if apply:
                    print(f"  {original_name} -> {new_name}")
                else:
                    print(f"  [DRY-RUN] {original_name} -> {new_name}")
                renamed_count += 1
        elif item.is_dir() and recursive:
            renamed_count += process_directory(item, recursive, apply)
    return renamed_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename font files based on their internal metadata",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s -a
  %(prog)s --apply
        """,
    )
    parser.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Apply the renames (default is dry-run)",
    )
    parser.add_argument("--no-recursive", action="store_true", help="Don't process subdirectories")
    args = parser.parse_args()
    cwd = Path.cwd()
    if args.apply:
        print("Applying renames...")
    else:
        print("Dry-run mode (no changes will be made). Use -a to apply.\n")
    renamed_count = process_directory(cwd, recursive=not args.no_recursive, apply=args.apply)
    if args.apply:
        print(f"\n{renamed_count} font file(s) renamed.")
    else:
        print(f"\n{renamed_count} font file(s) would be renamed.")
        print("Use -a or --apply to apply these changes.")


if __name__ == "__main__":
    raise SystemExit(main())
