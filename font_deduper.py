#!/data/data/com.termux/files/usr/bin/env python
"""Find fonts that share the same PostScript name and keep only one copy.
Groups the ``.ttf``/``.woff``/``.woff2`` files below the current directory by the PostScript name read from their ``name`` table with fontTools and reports (or with ``DRY_RUN = False`` deletes) the duplicates.
Usage: font_deduper Examples: $ font_deduper Runs as a script (``python font_deduper.py``) in the directory it should act on; the module can also be imported."""

from __future__ import annotations

import os
from pathlib import Path

from fontTools.ttLib import TTFont


DRY_RUN: bool = False
FONT_EXTENSIONS: tuple[str, str, str] = (".ttf", ".woff2", ".woff")


def get_font_ps_name(file_path: Path) -> str | None:
    try:
        font = TTFont(file_path, fontNumber=0, lazy=True)
        name_table = font["name"]
        ps_name = name_table.getDebugName(6)
        if not ps_name:
            ps_name = name_table.getDebugName(4) or name_table.getDebugName(1)
        font.close()
        return ps_name.strip() if ps_name else None
    except Exception:
        return None


def main() -> None:
    seen_fonts = {}
    duplicates = []
    print("Scanning directories recursively...")
    font_count = 0
    for root, dirs, files in os.walk("."):
        for file_name in files:
            if file_name.lower().endswith(FONT_EXTENSIONS):
                font_count += 1
                file_path = Path(root).joinpath(file_name)
                ps_name = get_font_ps_name(file_path)
                if not ps_name:
                    print(f"⚠️ Skipped (could not read metadata): {file_path}")
                    continue
                file_size = Path(file_path).stat().st_size
                if ps_name in seen_fonts:
                    existing_path, existing_size = seen_fonts[ps_name]
                    if file_size > existing_size:
                        duplicates.append(existing_path)
                        seen_fonts[ps_name] = (file_path, file_size)
                        print(
                            f"🏷️ Duplicate '{ps_name}': Keeping {file_path} ({file_size} bytes) over {existing_path} ({existing_size} bytes)"
                        )
                    else:
                        duplicates.append(file_path)
                        print(
                            f"🏷️ Duplicate '{ps_name}': Keeping {existing_path} ({existing_size} bytes) over {file_path} ({file_size} bytes)"
                        )
                else:
                    seen_fonts[ps_name] = (file_path, file_size)
    print(f"\n--- Scan Complete: Processed {font_count} fonts. Found {len(duplicates)} duplicates ---")
    if DRY_RUN:
        print("[DRY-RUN] No files were deleted. Files marked for removal:")
        for dup in duplicates:
            print(f"  - {dup}")
        print(
            "\n👉 To permanently delete these files, open the script and change 'DRY_RUN = True' to 'DRY_RUN = False'."
        )
    else:
        print("Deleting duplicate fonts...")
        for dup in duplicates:
            try:
                os.remove(dup)
                print(f"❌ Deleted: {dup}")
            except Exception as e:
                print(f"📁 Error deleting {dup}: {e}")


if __name__ == "__main__":
    main()
