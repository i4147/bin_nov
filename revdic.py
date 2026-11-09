#!/data/data/com.termux/files/usr/bin/env python
"""Reverse the keys and values of a JSON dictionary in place.
Loads the mapping, swaps keys with values, and writes the result back to the same file with two-space indentation and sorted keys.
Usage: revdic FILE.json Examples: $ revdic words.json Runs as a script (``python revdic.py``) in the directory it should act on; the module can also be imported."""

import json
import sys

from dh import reverse_dict


if __name__ == "__main__":
    fn = sys.argv[1]
    with open(fn, encoding="utf-8") as f:
        data = json.load(f)
    revdict = reverse_dict(data)
    with open(fn, "w", encoding="utf-8") as fo:
        json.dump(revdict, fo, ensure_ascii=False, indent=2, sort_keys=True)
    print("done")
