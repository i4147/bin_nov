#!/data/data/com.termux/files/usr/bin/env python
"""Restore indentation to unindented Python files."""

import argparse
from pathlib import Path
import sys


def restore_indentation(source_code: str, indent_size: int = 4) -> str:
    lines = source_code.splitlines()
    output_lines = []

    block_stack = [{"type": "module", "indent": 0}]

    branch_keywords = ("elif ", "else:", "except", "finally:")

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            output_lines.append("")
            continue

        is_branch = line.startswith(branch_keywords)

        if is_branch and len(block_stack) > 1:
            target_indent = block_stack[-2]["indent"]
        else:
            target_indent = block_stack[-1]["indent"]

        if not is_branch and len(block_stack) > 1 and block_stack[-1].get("expect_indent", False):
            target_indent = block_stack[-1]["indent"] + indent_size
            block_stack[-1]["expect_indent"] = False

        output_lines.append(" " * target_indent + line)

        if line.endswith(":"):
            if is_branch:
                block_stack[-1] = {"type": "branch", "indent": target_indent, "expect_indent": True}
            else:
                block_type = line.split()[0] if line else "block"
                block_stack.append({"type": block_type, "indent": target_indent, "expect_indent": True})
        elif line.startswith(("def ", "class ", "import ", "from ", "BACKENDS =", "DEFAULT_")) and len(block_stack) > 1:
            while len(block_stack) > 1:
                block_stack.pop()
            target_indent = block_stack[-1]["indent"]
            output_lines[-1] = " " * target_indent + line

    return "\n".join(output_lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Restore indentation to unindented Python files.")
    parser.add_argument("input", type=Path, help="Input Python file with lost indentation")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output file (default: print to stdout)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: File '{args.input}' not found.", file=sys.stderr)
        sys.exit(1)

    code = args.input.read_text(encoding="utf-8")
    reformatted = restore_indentation(code)

    if args.output:
        args.output.write_text(reformatted, encoding="utf-8")
        print(f"Successfully wrote re-indented code to {args.output}")
    else:
        sys.stdout.write(reformatted)


if __name__ == "__main__":
    main()
