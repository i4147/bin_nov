#!/data/data/com.termux/files/usr/bin/env python
"""Merge several dictionaries into one JSON file, detecting the translation direction.
Discovers the dictionary inputs, decides for each pair which side is English (a plain ASCII heuristic), normalises the values and writes a single merged mapping with unique file names when the target already exists.
Usage: mergedic [inputs ...] Examples: $ mergedic dic/*.json Runs as a script (``python mergedic.py``) in the directory it should act on; the module can also be imported."""

from collections import defaultdict
import json
from pathlib import Path
import re
import sys

from dh import get_random_filename
from loguru import logger


ENGLISH_RE = re.compile(r"^[A-Za-z][A-Za-z\s\-'.]*$")


def looks_english(text: str) -> bool:
    return bool(ENGLISH_RE.match(text))


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        msg = f"{path} does not contain a JSON object (dict)."
        raise ValueError(msg)
    return data


def detect_direction(data: dict, path: Path) -> str:
    if not data:
        msg = f"{path} is empty."
        raise ValueError(msg)
    en_count = sum(1 for k in data if looks_english(k))
    total = len(data)
    ratio = en_count / total
    if ratio >= 0.9:
        return "en"
    if ratio <= 0.1:
        return "fa"
    msg = f"{path} has mixed key languages ({en_count}/{total} English-looking keys). Refusing to merge ambiguous file."
    raise ValueError(msg)


def unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    i = 1
    while True:
        candidate = base.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def discover_inputs() -> list[Path]:
    return sorted(Path.cwd().glob("*.json"))


def collect_inputs(argv: list[str]) -> list[Path]:
    if not argv:
        paths = discover_inputs()
        if not paths:
            logger.error("No .json files found in the current directory.")
            sys.exit(1)
        print(f"No CLI args provided — merging {len(paths)} .json file(s) from cwd.")
        return paths
    paths = []
    for arg in argv:
        path = Path(arg)
        if not path.is_file():
            logger.error(f"File not found: {path}")
            sys.exit(1)
        paths.append(path)
    return paths


def normalize_value(value) -> list:
    if isinstance(value, list):
        return list(value)
    return [value]


def main():
    input_paths = collect_inputs(sys.argv[1:])
    merged: dict[str, list] = defaultdict(list)
    origin: dict[str, Path] = {}
    direction: str | None = None
    direction_source: Path | None = None
    skipped: list[tuple[Path, str]] = []
    for path in input_paths:
        data = load_json(path)
        try:
            file_dir = detect_direction(data, path)
        except ValueError as e:
            logger.error(str(e))
            skipped.append((path, str(e)))
            continue
        if direction is None:
            direction = file_dir
            direction_source = path
            print(f"Detected direction '{direction}' from {path.name}.")
        elif file_dir != direction:
            msg = (
                f"{path.name} looks like a '{file_dir}' dict but we already "
                f"committed to '{direction}' (from {direction_source.name}). Skipping."
            )
            logger.error(msg)
            skipped.append((path, msg))
            continue
        for key, value in data.items():
            for translation in normalize_value(value):
                if translation not in merged[key]:
                    merged[key].append(translation)
            if key not in origin:
                origin[key] = path
    if not merged:
        logger.error("Nothing to merge — no valid input files.")
        sys.exit(1)
    merged = dict(merged)
    output_filename = get_random_filename() + ".json"
    output_path = unique_path(Path(output_filename))
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    merged_files = len(input_paths) - len(skipped)
    logger.success(
        f"Merged {merged_files} file(s) (direction: {direction}) → {output_path} ({len(merged)} unique keys)"
    )
    multi = sum(1 for v in merged.values() if len(v) > 1)
    if multi:
        print(f"{multi} key(s) have multiple translations.")
    for path in input_paths:
        try:
            path.unlink()
            logger.debug(f"Deleted input file: {path}")
        except OSError as e:
            logger.error(f"Failed to delete {path}: {e}")
    logger.success(f"Removed {len(input_paths)} input file(s).")


if __name__ == "__main__":
    main()
