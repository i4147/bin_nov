#!/data/data/com.termux/files/usr/bin/env python
"""Rewrite a JSON object with its keys sorted by length.
Loads the file, sorts the items by key length (or by the length of the value list with ``-v``), re-serialises with two-space indentation and writes it back after a round-trip validation.
Usage: sjby [-v] FILE.json Examples: $ sjby words.json Runs as a script (``python sjby.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import sys
from typing import Any, Callable


def main() -> None:
    args = sys.argv[1:]
    verbose = False
    if args and args[0] == "-v":
        verbose = True
        args = args[1:]
    if not args:
        sys.exit(1)
    path: Path = Path(args[0])
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    key_func: Callable[[tuple[str, Any]], int]
    if verbose:
        key_func = lambda item: len(item[1]) if isinstance(item[1], list) else 0
    else:
        key_func = lambda item: len(item[0])
    sorted_data: dict[str, Any] = dict(sorted(data.items(), key=key_func))
    serialized: str = json.dumps(sorted_data, ensure_ascii=False, indent=2)
    json.loads(serialized)
    path.write_text(serialized, encoding="utf-8")


if __name__ == "__main__":
    main()
