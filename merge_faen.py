#!/data/data/com.termux/files/usr/bin/env python
"""Merge Persian↔English translation pairs and keep the untranslated words aside.
Pairs the input files, loads the existing translations, merges the new entries and appends the words that could not be matched to the failed list, logging progress with loguru and saving as it goes.
Usage: merge_faen Examples: $ merge_faen Runs as a script (``python merge_faen.py``) in the directory it should act on; the module can also be imported.
"""

import json
import os
from pathlib import Path

from loguru import logger


def load_existing_translations(json_path: str) -> dict[str, str]:
    if Path(json_path).exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
                if isinstance(existing_data, dict):
                    print(f"Loaded {len(existing_data)} existing translations from {json_path}")
                    return existing_data
                else:
                    logger.warning(f"Existing {json_path} is not a valid dictionary format")
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load existing JSON file: {e}")
    return {}


def load_failed_words(failed_path: str) -> set[str]:
    if Path(failed_path).exists():
        try:
            with open(failed_path, "r", encoding="utf-8") as f:
                failed_words = {line.strip() for line in f if line.strip()}
                print(f"Loaded {len(failed_words)} existing failed words")
                return failed_words
        except OSError as e:
            logger.warning(f"Could not load failed words file: {e}")
    return set()


def process_file_pair(fa_path: Path, en_path: Path) -> tuple[dict[str, str], set[str], list[str]]:
    translations = {}
    failed = set()
    warnings = []
    try:
        with (
            open(fa_path, "r", encoding="utf-8") as f_fa,
            open(en_path, "r", encoding="utf-8") as f_en,
        ):
            fa_lines = [line.strip() for line in f_fa if line.strip()]
            en_lines = [line.strip() for line in f_en if line.strip()]
        if len(fa_lines) != len(en_lines):
            warning_msg = (
                f"Line count mismatch: {fa_path.name} ({len(fa_lines)} lines) "
                f"vs {en_path.name} ({len(en_lines)} lines). "
                f"Processing matching lines only."
            )
            warnings.append(warning_msg)
            logger.warning(warning_msg)
        for fa_word, en_word in zip(fa_lines, en_lines, strict=False):
            if not fa_word or not en_word:
                continue
            if fa_word == en_word:
                failed.add(fa_word)
            else:
                translations[fa_word] = en_word
        return translations, failed, warnings
    except OSError as e:
        logger.error(f"Error reading files {fa_path.name} or {en_path.name}: {e}")
        return {}, set(), [f"Error: {e}"]
    except Exception as e:
        logger.error(f"Unexpected error processing {fa_path.name}: {e}")
        return {}, set(), [f"Unexpected error: {e}"]


def merge_translations(src_dir: str = ".") -> None:
    output_json = "dic_fa_en.json"
    output_failed = "failed-fa.txt"
    translations = load_existing_translations(output_json)
    failed_words = load_failed_words(output_failed)
    processed_files = set()
    total_new_translations = 0
    if not Path(src_dir).is_dir():
        logger.error(f"Source directory '{src_dir}' does not exist")
        return
    try:
        all_files = sorted([f for f in os.listdir(src_dir) if f.endswith(".txt")])
    except OSError as e:
        logger.error(f"Cannot read directory {src_dir}: {e}")
        return
    fa_files = [f for f in all_files if not f.endswith("_en.txt")]
    if not fa_files:
        logger.warning(f"No FA files found in {src_dir}")
        return
    print(f"Found {len(fa_files)} FA files to process")
    for fa_file in fa_files:
        base_name = fa_file[:-4]
        en_file = f"{base_name}_en.txt"
        fa_path = Path(src_dir) / fa_file
        en_path = Path(src_dir) / en_file
        if not en_path.exists():
            logger.warning(f"Skipping {fa_file}: {en_file} not found")
            continue
        file_translations, file_failed, _warnings = process_file_pair(fa_path, en_path)
        new_translations_count = 0
        for fa_word, en_word in file_translations.items():
            if fa_word not in translations:
                translations[fa_word] = en_word
                new_translations_count += 1
                total_new_translations += 1
        failed_words.update(file_failed)
        for fa_word in file_failed:
            if fa_word in translations:
                failed_words.discard(fa_word)
        processed_files.add(fa_file)
        processed_files.add(en_file)
        print(
            f"Processed {fa_file}: "
            f"{len(file_translations)} translations "
            f"({new_translations_count} new), "
            f"{len(file_failed)} failed"
        )
    try:
        sorted_translations = dict(sorted(translations.items()))
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(sorted_translations, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(translations)} translations to {output_json}")
    except OSError as e:
        logger.error(f"Error saving translations to {output_json}: {e}")
    try:
        sorted_failed = sorted(failed_words)
        with open(output_failed, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted_failed))
        print(f"Saved {len(failed_words)} failed words to {output_failed}")
    except OSError as e:
        logger.error(f"Error saving failed words to {output_failed}: {e}")
    print("=" * 40)
    print("SUMMARY:")
    print(f"Files processed: {len(processed_files) // 2} pairs")
    print(f"New translations added: {total_new_translations}")
    print(f"Total translations in dictionary: {len(translations)}")
    print(f"Total failed words: {len(failed_words)}")
    print("=" * 40)


if __name__ == "__main__":
    import time

    start_time = time.time()
    try:
        merge_translations()
    except KeyboardInterrupt:
        print("Process interrupted by user")
    except Exception as e:
        logger.opt(exception=True).error(
            f"Unexpected error in main execution: {e}",
        )
    finally:
        elapsed_time = time.time() - start_time
        print(f"Execution completed in {elapsed_time:.2f} seconds")
