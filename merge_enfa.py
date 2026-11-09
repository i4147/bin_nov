#!/data/data/com.termux/files/usr/bin/env python
"""Merge English→Persian translation files into one dictionary.
Reads the JSON translation files it finds, merges the mappings (first value wins) and writes the combined dictionary back to disk.
Usage: merge_enfa Examples: $ merge_enfa Runs as a script (``python merge_enfa.py``) in the directory it should act on; the module can also be imported."""

import json
from pathlib import Path


def merge_translation_files(base_dir=".", output_file="dic_en_fa.json", failed_file="failed-en.txt"):
    base_path = Path(base_dir)
    existing_dictionary = {}
    if (base_path / output_file).exists():
        try:
            with open(base_path / output_file, "r", encoding="utf-8") as f:
                existing_dictionary = json.load(f)
            print(f"📖 Loaded existing dictionary with {len(existing_dictionary)} entries")
        except json.JSONDecodeError:
            print(f"⚠️  Warning: Could not parse existing {output_file}, starting fresh")
        except Exception as e:
            print(f"⚠️  Warning: Error reading {output_file}: {e}, starting fresh")
    existing_failed = set()
    if (base_path / failed_file).exists():
        try:
            with open(base_path / failed_file, "r", encoding="utf-8") as f:
                existing_failed = {line.strip() for line in f if line.strip()}
            print(f"📖 Loaded existing failed entries: {len(existing_failed)}")
        except Exception as e:
            print(f"⚠️  Warning: Error reading {failed_file}: {e}, starting fresh")
    all_files = [f for f in base_path.iterdir() if f.is_file()]
    en_files = []
    fa_files = {}
    for file in all_files:
        if file.name.endswith("_fa"):
            base_name = file.name[:-3]
            if file.suffix:
                base_name = file.name.replace("_fa", "", 1)
            fa_files[base_name] = file
        else:
            potential_fa = None
            if file.suffix:
                stem = file.stem
                fa_name = f"{stem}_fa{file.suffix}"
                potential_fa = base_path / fa_name
            else:
                potential_fa = base_path / f"{file.name}_fa"
            if potential_fa.exists():
                en_files.append(file)
    dictionary = existing_dictionary.copy()
    failed_entries = existing_failed.copy()
    new_entries_count = 0
    updated_entries_count = 0
    for en_file in sorted(en_files, key=lambda x: x.name):
        if en_file.suffix:
            fa_file = base_path / f"{en_file.stem}_fa{en_file.suffix}"
        else:
            fa_file = base_path / f"{en_file.name}_fa"
        if not fa_file.exists():
            print(f"⚠️  Warning: Missing {fa_file.name} for {en_file.name}")
            continue
        with (
            open(en_file, "r", encoding="utf-8") as f_en,
            open(fa_file, "r", encoding="utf-8") as f_fa,
        ):
            en_lines = [line.strip() for line in f_en if line.strip()]
            fa_lines = [line.strip() for line in f_fa if line.strip()]
            if len(en_lines) != len(fa_lines):
                print(
                    f"⚠️  Mismatch: {en_file.name} has {len(en_lines)} lines, {fa_file.name} has {len(fa_lines)} lines"
                )
            for en_word, fa_word in zip(en_lines, fa_lines, strict=False):
                if en_word.lower() == fa_word.lower():
                    if en_word not in failed_entries:
                        failed_entries.add(en_word)
                    if en_word in dictionary:
                        del dictionary[en_word]
                else:
                    if en_word not in dictionary:
                        new_entries_count += 1
                    elif dictionary[en_word] != fa_word:
                        updated_entries_count += 1
                    dictionary[en_word] = fa_word
                    if en_word in failed_entries:
                        failed_entries.remove(en_word)
    with open(base_path / output_file, "w", encoding="utf-8") as f:
        json.dump(dictionary, f, ensure_ascii=False, indent=2)
    with open(base_path / failed_file, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(failed_entries)))
    print(f"✅ Total dictionary entries: {len(dictionary)}")
    print(f"   - Existing entries preserved: {len(existing_dictionary)}")
    print(f"   - New entries added: {new_entries_count}")
    print(f"   - Updated entries: {updated_entries_count}")
    print(f"⚠️  Failed/untranslated entries: {len(failed_entries)}")
    print(f"✅ Dictionary saved → {output_file}")
    print(f"⚠️  Failed entries saved → {failed_file}")
    return dictionary, failed_entries


if __name__ == "__main__":
    merge_translation_files()
