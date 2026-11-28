#!/data/data/com.termux/files/usr/bin/env python
"""Sort a fuzzy-hash similarity JSON report by score, in place.

Loads a {pairs: [...]} document (algorithm, file1, file2, similarity_score), sorts descending by score and writes it back to the same file.
"""

from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import sys

import loguru


@dataclass(slots=True)
class Pair:
    algorithm: str
    file1: str
    file2: str
    similarity_score: int


def load_pairs(path: Path) -> list[Pair]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Pair(**item) for item in data["pairs"]]


def sort_pairs(pairs: list[Pair], reverse: bool = False) -> list[Pair]:
    return sorted(pairs, key=lambda p: p.similarity_score, reverse=reverse)


def write_output(pairs: list[Pair], path: Path) -> None:
    output = {
        "errors": [],
        "files": len(pairs),
        "pairs": [
            {
                "algorithm": p.algorithm,
                "file1": p.file1,
                "file2": p.file2,
                "similarity_score": p.similarity_score,
            }
            for p in pairs
        ],
    }
    text = json.dumps(output, indent=2)
    path.write_text(text + "\n", encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        loguru.logger.error("Usage: python sort_json.py <input.json>")
        sys.exit(1)
    input_path = Path(sys.argv[1])
    if not input_path.exists():
        loguru.logger.error(f"File not found: {input_path}")
        sys.exit(1)
    pairs = load_pairs(input_path)
    sorted_pairs = sort_pairs(pairs, reverse=True)
    write_output(sorted_pairs, input_path)
    loguru.logger.info(f"Sorted {len(sorted_pairs)} pairs in-place")


if __name__ == "__main__":
    main()
