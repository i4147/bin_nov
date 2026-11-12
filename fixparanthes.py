#!/data/data/com.termux/files/usr/bin/env python
"""Fix unbalanced or JSON-bracket parentheses inside the strings of a JSON file.
Walks every string value, converts ``[``/``{`` into ``(`` (and the closing counterparts) and pads missing parentheses, then validates the result with :func:`json.loads` and rewrites the file with two-space indentation.
Usage: fixparanthes FILE.json Examples: $ fixparanthes data.json Runs as a script (``python fixparanthes.py``) in the directory it should act on; the module can also be imported.
"""

import json
from pathlib import Path
import re
import sys

STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def fix_content(content: str) -> str:
    out = []
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if c == "\\" and i + 1 < n:
            nxt = content[i + 1]
            if nxt in "[{":
                out.append("(")
                i += 2
                continue
            if nxt in "]}":
                out.append(")")
                i += 2
                continue
            if nxt in "()":
                out.append(nxt)
                i += 2
                continue
            out.append(c)
            out.append(nxt)
            i += 2
            continue
        if c in "[{":
            out.append("(")
        elif c in "]}":
            out.append(")")
        else:
            out.append(c)
        i += 1
    s = "".join(out)
    opens = s.count("(")
    closes = s.count(")")
    if opens > closes:
        s += ")" * (opens - closes)
    elif closes > opens:
        s = "(" * (closes - opens) + s
    return s


def fix_literal(match: re.Match) -> str:
    raw = match.group(0)
    return '"' + fix_content(raw[1:-1]) + '"'


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} <json-file>")
    path = Path(sys.argv[1])
    if not path.is_file():
        sys.exit(f"not a file: {path}")
    text = path.read_text(encoding="utf-8")
    fixed_text = STRING_RE.sub(fix_literal, text)
    try:
        data = json.loads(fixed_text)
    except json.JSONDecodeError as e:
        sys.exit(f"still invalid JSON after fix: {e}\n---\n{fixed_text}")
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"fixed and updated: {path}")


if __name__ == "__main__":
    main()
