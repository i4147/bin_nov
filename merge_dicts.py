#!/data/data/com.termux/files/usr/bin/env python
"""
merge_dicts.py

Convert or merge word/translation JSON files.

Modes:
  * one input file, no -o  -> rewrite that file *in place* in the list format:
        {"word": ["translation1", "translation2", ...]}
  * one input file, with -o -> convert into the given (non-existing) file
  * two+ files              -> merge into the given -o file
                               (default: merged.json, must not exist)

Input files may use either shape:
    {"word": "translation"}                 # single string
    {"word": ["translation1", ...]}         # list of strings

Rules (all modes):
  * All translations for a word are combined.
  * Duplicates are removed.
  * "Similar" translations (differing only by case, whitespace, or
    Arabic/Persian character variants) are treated as duplicates and
    collapsed to the first-seen form.
  * Output is sorted by key for reproducibility.

Overwrite policy:
  * Any file named via -o (or the default "merged.json") must NOT already
    exist. The script refuses with an error instead of silently clobbering.
  * The only exception is single-file mode *without* -o, which is by
    definition an in-place rewrite of the input file.

Usage:
    python merge_dicts.py dictionary.json                     # convert in place
    python merge_dicts.py dictionary.json -o new.json         # convert to new file
    python merge_dicts.py a.json b.json -o merged.json        # merge to new file
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


# --- normalization -------------------------------------------------------

# Arabic/Persian look-alike characters -> canonical Persian form
_CHAR_MAP = str.maketrans({
    "ي": "ی",  # Arabic yeh      -> Persian yeh
    "ى": "ی",  # alef maksura    -> Persian yeh
    "ك": "ک",  # Arabic kaf      -> Persian kaf
    "\u200c": " ",  # ZWNJ -> space (for comparison only)
    "\u200f": "",  # RLM
    "\u200e": "",  # LRM
    "\u064b": "",  # Arabic diacritics (fathatan)
    "\u064c": "",
    "\u064d": "",
    "\u064e": "",
    "\u064f": "",
    "\u0650": "",
    "\u0651": "",
    "\u0652": "",
    "\u0670": "",
})

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Return a comparison key for a translation."""
    text = text.translate(_CHAR_MAP)
    text = _WS_RE.sub(" ", text).strip()
    return text.casefold()


# --- loading -------------------------------------------------------------


def load_translations(path: Path) -> dict[str, list[str]]:
    """Load a JSON file and normalize every value to a list of strings."""
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")

    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            out[key] = [value]
        elif isinstance(value, list):
            out[key] = [v for v in value if isinstance(v, str)]
        else:
            raise ValueError(f"{path}: value for key {key!r} must be a string or list of strings")
    return out


# --- merging / deduplication --------------------------------------------


def merge(*dicts: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Merge any number of {word: [translations]} mappings.

    For each word, concatenate all candidate translations and dedupe
    by their normalized form (keeping the first occurrence).
    """
    merged_raw: dict[str, list[str]] = {}
    for d in dicts:
        for word, translations in d.items():
            merged_raw.setdefault(word, []).extend(translations)

    merged: dict[str, list[str]] = {}
    for word, translations in merged_raw.items():
        seen: set[str] = set()
        unique: list[str] = []
        for t in translations:
            key = normalize(t)
            if key and key not in seen:
                seen.add(key)
                unique.append(t)
        merged[word] = unique

    # sort by key for stable, reviewable output
    return dict(sorted(merged.items(), key=lambda kv: kv[0].casefold()))


# --- output guards -------------------------------------------------------


def check_target_available(target: Path) -> None:
    """Refuse to proceed if `target` already exists on disk."""
    if target.exists():
        raise SystemExit(
            f"error: refusing to overwrite existing file: {target}\n"
            f"       remove it first, or pick a different name with -o"
        )


# --- writing -------------------------------------------------------------


def write_json_atomic(path: Path, data: dict, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    """Write JSON to `path` via a temp file + rename, so a crash can't
    corrupt the target."""
    path = path.resolve()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# --- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert (one file) or merge (two+ files) "
        "word/translation JSON files. Refuses to overwrite "
        "an existing -o target."
    )
    parser.add_argument("files", nargs="+", type=Path, help="one file (converted) or two+ files (merged)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output file. In single-file mode defaults to "
        "in-place rewrite of the input; in merge mode "
        "defaults to 'merged.json'. Must not already exist.",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    parser.add_argument("--ensure-ascii", action="store_true", help="escape non-ASCII characters in output")
    args = parser.parse_args(argv)

    write_kwargs = dict(indent=args.indent, ensure_ascii=args.ensure_ascii)

    # ---- single-file mode ---------------------------------------------
    if len(args.files) == 1:
        src = args.files[0]
        if not src.is_file():
            parser.error(f"input file not found: {src}")

        if args.output is None:
            # in-place conversion: overwriting src is the whole point
            data = merge(load_translations(src))
            write_json_atomic(src, data, **write_kwargs)
            target_desc = f"{src} (in place)"
        else:
            # explicit output: must not clobber anything
            check_target_available(args.output)
            data = merge(load_translations(src))
            write_json_atomic(args.output, data, **write_kwargs)
            target_desc = str(args.output)

        total_pairs = sum(len(v) for v in data.values())
        print(f"Converted {src} -> {target_desc}: {len(data)} words ({total_pairs} translations)", file=sys.stderr)
        return 0

    # ---- merge mode ----------------------------------------------------
    output = args.output or Path("merged.json")
    check_target_available(output)

    # extra safety: refuse if the target collides with any input
    output_resolved = output.resolve()
    for p in args.files:
        if p.is_file() and p.resolve() == output_resolved:
            raise SystemExit(f"error: output file {output} is also an input file")

    loaded = [load_translations(p) for p in args.files]
    result = merge(*loaded)
    write_json_atomic(output, result, **write_kwargs)

    total_pairs = sum(len(v) for v in result.values())
    print(
        f"Wrote {len(result)} words ({total_pairs} translations) from {len(args.files)} files to {output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
