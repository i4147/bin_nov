#!/data/data/com.termux/files/usr/bin/env python
"""Remove version pins/specifiers from a requirements.txt file, in place.
Usage: python strip_versions.py requirements.txt"""

from pathlib import Path
import re
import sys


# Package name (PEP 508 style) + optional extras, e.g. "lxml[html_clean]"
REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?")


def strip_version(line: str) -> str:
    """Return the requirement line with any version specifiers removed."""
    m = REQ_RE.match(line)
    if not m:
        return line

    requirement = m.group(1) + (m.group(2) or "")

    # Preserve an environment marker (e.g. `; python_version < "3.8"`), if any.
    rest = line[m.end() :]
    marker = ""
    if ";" in rest:
        marker = " ;" + rest.split(";", 1)[1].strip()

    return requirement + marker


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: {Path(sys.argv[0]).name} <requirements.txt>")

    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")

    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        # Keep blank lines and comments untouched
        if not stripped or stripped.startswith("#"):
            out_lines.append(line)
        else:
            out_lines.append(strip_version(line))

    path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"Rewrote {path} ({len(out_lines)} lines)")


if __name__ == "__main__":
    main()
