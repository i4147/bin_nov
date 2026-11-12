#!/data/data/com.termux/files/usr/bin/env python
"""Obfuscate a shell script into three-character variables evaluated at the end.
Splits the script into three-character chunks, assigns each chunk to a generated variable name and finishes with a single ``eval`` that concatenates them, so the original text is no longer readable in the file.
Usage: obf SCRIPT.sh Examples: $ obf install.sh > obfuscated.sh Runs as a script (``python obf.py``) in the directory it should act on; the module can also be imported.
"""

import string
import sys
from typing import Iterator


def varnames() -> Iterator[str]:
    letters = string.ascii_letters
    for c in letters:
        yield c
    for c in letters:
        yield c + "z"
    for c in letters:
        for d in letters:
            yield c + d + "z"


def obfuscate(src: str) -> str:
    names = varnames()
    out = []
    eval_parts = []
    CHUNK = 3
    for i in range(0, len(src), CHUNK):
        piece = src[i : i + CHUNK]
        name = next(names)
        escaped = piece.replace("'", "'\\''")
        out.append(f"{name}='{escaped}';")
        eval_parts.append(f"${name}")
    out.append('eval "' + "".join(eval_parts) + '"')
    return "\n".join(out)


if __name__ == "__main__":
    src = open(sys.argv[1]).read()
    print(obfuscate(src))
