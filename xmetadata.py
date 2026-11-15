#!/data/data/com.termux/files/usr/bin/env python
"""Extract fenced code snippets from site-package METADATA files."""

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


def parse_metadata_fields(content: str) -> dict[str, str]:
    fields = {"Name": "unknown_package", "Version": "0.0.0"}
    for line in content.splitlines():
        if not line.strip():
            break
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            if key in fields:
                fields[key] = val.strip()
    return fields


def extract_metadata_code_blocks(target_dir: Path = Path()) -> None:
    output_dir = Path.cwd() / "extracted_code"
    output_dir.mkdir(parents=True, exist_ok=True)
    block_pattern = re.compile(r"^```(\w*)\n(.*?)^```", re.DOTALL | re.MULTILINE)
    metadata_files = list(target_dir.rglob("METADATA"))
    if not metadata_files:
        print("No METADATA files found in the specified target directory path.")
        return
    extracted_count = 0
    for meta_path in metadata_files:
        if output_dir in meta_path.resolve().parents:
            continue
        try:
            content = meta_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError) as err:
            print(f"Skipping {meta_path} due to read restriction: {err}")
            continue
        pkg_info = parse_metadata_fields(content)
        pkg_name = pkg_info["Name"]
        pkg_version = pkg_info["Version"]
        matches = block_pattern.findall(content)
        if not matches:
            continue
        base_filename = f"pkg_{pkg_name}_v{pkg_version}"
        for index, (lang, code_body) in enumerate(matches, start=1):
            clean_code = code_body.strip()
            if not clean_code:
                continue
            lang_clean = lang.lower().strip()
            ext = EXTENSION_MAP.get(lang_clean, ".txt")
            block_name = f"{base_filename}_block_{index}"
            destination = get_unique_path(output_dir, block_name, ext)
            try:
                destination.write_text(clean_code + "\n", encoding="utf-8")
                print(f"Extracted: [{pkg_name}=={pkg_version}] -> {destination.name}")
                extracted_count += 1
            except IOError as err:
                print(f"Failed writing block snippet to {destination.name}: {err}")
    print(
        f"\nProcessing finished. Extracted {extracted_count} total snippets into '{output_dir.relative_to(Path.cwd())}'"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract fenced code snippets directly out of site-package METADATA files."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root path folder of site-packages to traverse downward.",
    )
    args = parser.parse_args()
    extract_metadata_code_blocks(Path(args.directory))
