#!/data/data/com.termux/files/usr/bin/env python
"""Remove the ``if [ $(...) -ne 0 ]; then exit 1; fi`` root checks from shell scripts.
Scans the executable or ``#!``-shell files below the current directory and deletes the matching guard blocks (typically the ``must run as root`` checks) with a regular expression, rewriting only the files that changed.
Usage: noroot Examples: $ noroot Runs as a script (``python noroot.py``) in the directory it should act on; the module can also be imported.
"""

from pathlib import Path
import re
import sys

from dh import get_nobinary

IF_BLOCK_REGEX = re.compile(
    r"^if\s+\[\s*\$\((\S+)\)\s*\{\-ne\s+0\s*\}\]\s*;\s*then\s*\n((?:.|\n)*?)^\s*exit\s+1\s*$(.*?)^\s*fi",
    re.MULTILINE | re.IGNORECASE,
)


def remove_conditional_exit_blocks(path: Path) -> None:
    try:
        original_content = path.read_text(encoding="utf-8")
        modified_content = original_content
        while True:
            match = IF_BLOCK_REGEX.search(modified_content)
            if not match:
                break
            modified_content = modified_content[: match.start()] + modified_content[match.end() :]
        if original_content != modified_content:
            path.write_text(modified_content, encoding="utf-8")
            print(f"Cleaned: {path}")
    except Exception as e:
        print(f"Error processing {path}: {e}", file=sys.stderr)


def main() -> None:
    cwd = Path.cwd()
    files_to_process = get_nobinary(cwd)
    for item_path in files_to_process:
        if item_path.is_file():
            try:
                content = item_path.read_text(encoding="utf-8", errors="ignore")
                is_likely_bash = False
                if content.startswith(("#!/bin/bash", "#!/usr/bin/env bash")) or oct(item_path.stat().st_mode)[
                    -3:
                ] not in (
                    "000",
                    "001",
                    "010",
                    "011",
                    "002",
                    "012",
                    "100",
                    "110",
                    "111",
                    "101",
                ):
                    is_likely_bash = True
                if is_likely_bash:
                    remove_conditional_exit_blocks(item_path)
            except Exception as e:
                print(f"Could not read or process {item_path}: {e}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
