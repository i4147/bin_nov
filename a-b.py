#!/data/data/com.termux/files/usr/bin/env python
"""Remove lines from the first file that also occur in the second file.
Pass the two file paths as command-line arguments; the first file is replaced."""

from pathlib import Path
import sys


def main() -> None:
    """Replace the first argument file with lines absent from the second."""
    file_a = Path(sys.argv[1])
    file_b = Path(sys.argv[2])
    b_lines = {line.rstrip("\n") for line in file_b.read_text(encoding="utf-8").splitlines()}
    a_lines = file_a.read_text(encoding="utf-8").splitlines(keepends=True)
    kept_lines = [line for line in a_lines if line.rstrip("\n") not in b_lines]
    tmp_path = file_a.with_suffix(file_a.suffix + ".tmp")
    tmp_path.write_text("".join(kept_lines), encoding="utf-8")
    tmp_path.replace(file_a)


if __name__ == "__main__":
    raise SystemExit(main())
