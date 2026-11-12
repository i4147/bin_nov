#!/data/data/com.termux/files/usr/bin/env python
"""Regenerate ``SUMMARY.md`` from the Markdown files of a book tree.
Collects every ``.md`` file below the current directory, turns the paths into chapter titles and rewrites ``SUMMARY.md`` with the sorted link list below the existing header.
Usage: update_summary Examples: $ update_summary Runs as a script (``python update_summary.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path


def find_md_files():
    cwd = Path.cwd()
    md_files = []
    for path in cwd.rglob("*.md"):
        rel_path = path.relative_to(cwd)
        md_files.append(rel_path)
    return md_files


def update_summary() -> None:
    md_files = find_md_files()
    md_files.sort()
    summarymd = Path("SUMMARY.md")
    lines = summarymd.read_text(encoding="utf-8").splitlines()
    header = []
    for line in lines:
        if line.strip() and not line.strip().startswith("- ["):
            header.append(line)
        else:
            break
    new_entries = []
    for md_file in md_files:
        title = md_file.stem.replace("_", " ").replace("/", " ").title()
        entry = f"- [{title}](./{md_file})\n"
        new_entries.append(entry)
    with summarymd.open("w", encoding="utf-8") as f:
        f.writelines(header)
        f.write("\n")
        f.writelines(new_entries)
    print(f"Updated SUMMARY.md with {len(new_entries)} chapters.")


if __name__ == "__main__":
    update_summary()
