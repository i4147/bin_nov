#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

import tree_sitter_xml
from tree_sitter import Language, Parser


def build_parser() -> Parser:
    lang = Language(tree_sitter_xml.language_xml())
    parser = Parser(lang)
    return parser


def format_svg(source: bytes, parser: Parser) -> str:
    tree = parser.parse(source)
    root = tree.root_node
    lines: list[str] = []
    _walk(root, 0, lines)
    return "\n".join(lines)


def _walk(node, depth: int, lines: list[str]) -> None:
    if node.type == "element":
        tag = node.child_by_field_name("name").text.decode()
        attrs = node.child_by_field_name("attributes")
        attr_str = ""
        if attrs:
            attr_parts = []
            for child in attrs.named_children:
                if child.type == "attribute":
                    name = child.child_by_field_name("name").text.decode()
                    value = child.child_by_field_name("value").text.decode()
                    attr_parts.append(f"{name}={value}")
            if attr_parts:
                attr_str = " " + " ".join(attr_parts)
        indent = "  " * depth
        start_tag = f"<{tag}{attr_str}>"
        children = [c for c in node.named_children if c.type in ("element", "text")]
        if not children:
            lines.append(f"{indent}{start_tag}</{tag}>")
            return
        lines.append(f"{indent}{start_tag}")
        for child in children:
            if child.type == "element":
                _walk(child, depth + 1, lines)
            elif child.type == "text":
                text = child.text.decode().strip()
                if text:
                    lines.append(f"{'  ' * (depth + 1)}{text}")
        lines.append(f"{indent}</{tag}>")
    elif node.type == "prolog":
        lines.append(f"{'  ' * depth}{node.text.decode()}")
    elif node.type == "comment":
        lines.append(f"{'  ' * depth}{node.text.decode()}")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python format_svg.py <input.svg>")
        sys.exit(1)
    file_path = Path(sys.argv[1])
    parser = build_parser()
    source = file_path.read_bytes()
    formatted = format_svg(source, parser)
    file_path.write_text(formatted, encoding="utf-8")


if __name__ == "__main__":
    main()
