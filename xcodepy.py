#!/data/data/com.termux/files/usr/bin/env python
"""Recursively extract fenced markdown code blocks."""

import argparse
from pathlib import Path
import re


EXTENSION_MAP: dict[str, str] = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "bash": ".sh",
    "sh": ".sh",
    "shell": ".sh",
    "zsh": ".zsh",
    "html": ".html",
    "css": ".css",
    "json": ".json",
    "c": ".c",
    "cpp": ".cpp",
    "rust": ".rs",
    "rs": ".rs",
    "go": ".go",
    "yaml": ".yaml",
    "yml": ".yaml",
    "sql": ".sql",
}


def get_unique_path(base_dir: Path, name: str, extension: str) -> Path:
    counter = 1
    safe_name = re.sub(r"[^\w\-_]", "_", name)
    target = base_dir / f"{safe_name}{extension}"
    while target.exists():
        target = base_dir / f"{safe_name}_{counter}{extension}"
        counter += 1
    return target


def extract_code_blocks(target_dir: Path = Path()) -> None:
    output_dir = Path.cwd()
    block_pattern = re.compile(r"^```(\w*)\n(.*?)^```", re.DOTALL | re.MULTILINE)
    md_files = list(target_dir.rglob("*.md"))
    if not md_files:
        print("No Markdown files found in this workspace tree.")
        return
    extracted_count = 0
    for md_path in md_files:
        if md_path.resolve().parent == output_dir and md_path.name.startswith("extracted_"):
            continue
        try:
            content = md_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError) as err:
            print(f"Skipping file {md_path.name} due to read restriction: {err}")
            continue
        matches = block_pattern.findall(content)
        if not matches:
            continue
        base_name = f"extracted_{md_path.stem}"
        for index, (lang, code_body) in enumerate(matches, start=1):
            clean_code = code_body.strip()
            if not clean_code:
                continue
            lang_clean = lang.lower().strip()
            ext = EXTENSION_MAP.get(lang_clean, ".txt")
            block_name = f"{base_name}_block_{index}"
            destination = get_unique_path(output_dir, block_name, ext)
            try:
                destination.write_text(clean_code + "\n", encoding="utf-8")
                print(f"Saved: {md_path.name} -> {destination.name}")
                extracted_count += 1
            except IOError as err:
                print(f"Failed writing script context out to {destination.name}: {err}")
    print(f"\nProcessing finished. Extracted {extracted_count} total code blocks.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recursively extract fenced markdown code blocks.")
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Target folder path root to search down from.",
    )
    args = parser.parse_args()
    extract_code_blocks(Path(args.directory))
