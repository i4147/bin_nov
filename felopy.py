#!/data/data/com.termux/files/usr/bin/env python
"""Run the ``felo superagent`` query and save the returned code as a Python file.
Calls the external ``felo`` command with the query and the timeout, extracts the fenced Python blocks of the output and writes them to ``~/tmp/apps/felo_<timestamp>.py``, printing the path.
Usage: felopy [--timeout SECONDS] QUERY ...
Positional arguments: query ...
Query string to pass to felo superagent Options: --timeout Timeout in seconds for the felo command (default: 300) Examples: $ felopy write a csv deduplicator Requires the ``felo`` CLI on ``PATH``.
"""

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys

SAVE_DIR = Path("~/tmp/apps").expanduser()


def extract_code_blocks(text: str) -> list[str]:
    pattern = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
    blocks = pattern.findall(text)
    if not blocks:
        pattern = re.compile(r"```\s*\n(.*?)```", re.DOTALL)
        blocks = pattern.findall(text)
    return [b.strip() for b in blocks]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run felo superagent and save returned code as a Python file.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout in seconds for the felo command (default: 300)",
    )
    parser.add_argument(
        "query",
        nargs="+",
        help="Query string to pass to felo superagent",
    )
    args = parser.parse_args()
    query = " ".join(args.query)
    cmd = [
        "felo",
        "superagent",
        "--timeout",
        str(args.timeout),
        "--query",
        query,
    ]
    print(f"[wrapper] Running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + "\n" + result.stderr
    blocks = extract_code_blocks(output)
    if not blocks:
        print("[wrapper] No code blocks found in felo output.", file=sys.stderr)
        print("---- felo output ----", file=sys.stderr)
        print(output, file=sys.stderr)
        print("---------------------", file=sys.stderr)
        sys.exit(1)
    code = "\n\n".join(blocks)
    os.makedirs(SAVE_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"felo_{timestamp}.py"
    filepath = Path(SAVE_DIR).joinpath(filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    print(filepath)


if __name__ == "__main__":
    main()
