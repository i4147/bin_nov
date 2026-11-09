#!/data/data/com.termux/files/usr/bin/env python
"""Rename SVG font files after their ``<font id="...">`` attribute.
Reads each ``.svg`` file, extracts the font id, sanitises the characters that cannot appear in a file name and renames the file to ``<font-id>.svg``, refusing to overwrite an existing target.
Usage: rename_svg_fonts [FILE.svg | DIRECTORY] Examples: $ rename_svg_fonts fonts/ Runs as a script (``python rename_svg_fonts.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import re
import sys


def extract_font_id(svg_text):
    match = re.search(r'<font[^>]*\bid="([^"]+)"', svg_text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def rename_svg_font(path_obj: Path) -> None:
    if not path_obj.is_file():
        print(f"Skipping: Not a file - {path_obj.name}")
        return
    try:
        text = path_obj.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"Error reading {path_obj.name}: {e}")
        return
    font_id = extract_font_id(text)
    if not font_id:
        print(f'Skipping {path_obj.name}: Could not find <font id="..."> tag.')
        return
    sanitized_font_id = re.sub(r'[<>:"/\\|?*]', "_", font_id)
    if sanitized_font_id != font_id:
        print(f"Warning: Sanitized font ID for '{font_id}' in '{path_obj.name}' to '{sanitized_font_id}'.")
        font_id = sanitized_font_id
    new_name_obj = path_obj.with_name(font_id + ".svg")
    if new_name_obj == path_obj:
        print(f"No rename needed for {path_obj.name}: already correct name.")
        return
    try:
        path_obj.rename(new_name_obj)
        print(f"Renamed '{path_obj.name}' to '{new_name_obj.name}'")
    except FileExistsError:
        print(f"Error renaming '{path_obj.name}' to '{new_name_obj.name}': Target file already exists.")
    except Exception as e:
        print(f"Error renaming '{path_obj.name}': {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        input_path = Path(sys.argv[1])
        if input_path.is_dir():
            print(f"Processing directory: {input_path.resolve()}")
            for item in input_path.rglob("*"):
                rename_svg_font(item)
        elif input_path.is_file() and input_path.suffix.lower() == ".svg":
            rename_svg_font(input_path)
        else:
            print(f"Error: Invalid path provided. Must be an SVG file or a directory. Path: {input_path}")
            sys.exit(1)
    else:
        cwd = Path()
        print(f"No file specified. Processing current directory: {cwd.resolve()}")
        processed_count = 0
        for item in cwd.rglob("*.svg"):
            rename_svg_font(item)
            processed_count += 1
        if processed_count == 0:
            print("No SVG files found in the current directory.")
