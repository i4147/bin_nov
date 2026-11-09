#!/data/data/com.termux/files/usr/bin/env python
"""Collect the import statements of every Python file into per-folder ``imports.py`` files.
Parses the tree below the current directory with tree-sitter, takes the import (and comment) statements of each module and writes them grouped by folder to ``output/<folder>/imports.py``.
Hidden folders and ``site-packages`` are skipped.
Usage: python ex_comments.py Examples: $ python ex_comments.py Run as a script from the tree to scan."""

from collections import defaultdict
from pathlib import Path

from tree_sitter import Language, Parser, Tree
import tree_sitter_python as tsp

OUT_DIR = Path("output")
VALID = {"comment", "expression_statements"}


def extract_file(src: bytes, tree: Tree) -> list[str]:
    root = tree.root_node
    return [src[node.start_byte : node.end_byte].decode() for node in root.children if node.type in VALID]


def main() -> None:
    parser = Parser()
    parser.language = Language(tsp.language())
    OUT_DIR.mkdir(exist_ok=True)

    folder_imports = defaultdict(list)
    for py in Path().rglob("*.py"):
        if any(part.startswith(".") for part in py.parts) or "site-packages" in py.parts:
            continue
        if OUT_DIR in py.parents:
            continue
        src = py.read_bytes()
        tree = parser.parse(src)
        imports = extract_file(src, tree)
        if imports:
            folder_path = py.parent
            relative_folder = folder_path.relative_to(".")
            folder_imports[relative_folder].append("\n".join(imports))
    for folder, imports_list in folder_imports.items():
        if not imports_list:
            continue
        out_file = OUT_DIR / folder / "imports.py"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        content = "\n\n".join(imports_list)
        out_file.write_text(content)
    print(f"""
✨ Done! Processed {len(folder_imports)} folder(s)""")


if __name__ == "__main__":
    main()
