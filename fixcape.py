#!/data/data/com.termux/files/usr/bin/env python
"""Make Python string literals escape-safe so they stop warning.
Scans string tokens for invalid escapes and rewrites the literal either as a raw string or with the backslashes doubled, then writes the module back when something changed.
Files are collected recursively from the current directory.
Usage: fixcape Examples: $ fixcape Runs as a script (``python fixcape.py``) in the directory it should act on; the module can also be imported."""

import ast
import io
from pathlib import Path
import re
import sys
import tokenize
from typing import Iterator


def get_python_files(root_dir: Path) -> Iterator[Path]:
    for path in root_dir.rglob("*.py"):
        if path.is_file():
            yield path


def fix_source_code(source_bytes: bytes) -> tuple[bytes, bool]:
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(source_bytes).readline))
    except tokenize.TokenError:
        return source_bytes, False
    out_tokens = []
    changed = False
    string_pattern = re.compile(r'^([rRuUbBfF]*)([\'"]{1,3})(.*)(\2)$', re.DOTALL)
    valid_escape_chars = set("nrtbfva\\'\"xuUN01234567\n\r")
    for tok in tokens:
        if tok.type == tokenize.STRING:
            m = string_pattern.match(tok.string)
            if m:
                prefix, quote, content, end_quote = m.groups()
                if "r" in prefix.lower():
                    out_tokens.append(tok)
                    continue
                has_valid_escape = False
                has_invalid_escape = False
                for escape_match in re.finditer(r"\\.", content):
                    char = escape_match.group()[1]
                    if char in valid_escape_chars:
                        has_valid_escape = True
                    else:
                        has_invalid_escape = True
                if has_invalid_escape:
                    changed = True
                    if not has_valid_escape and "f" not in prefix.lower():
                        new_prefix = prefix + ("R" if prefix.isupper() else "r")
                        new_string = new_prefix + quote + content + end_quote
                    else:

                        def replacer(match: re.Match[str]):
                            char = match.group(1)
                            if char in valid_escape_chars:
                                return match.group(0)
                            return "\\\\" + char

                        new_content = re.sub(r"\\(.)", replacer, content)
                        new_string = prefix + quote + new_content + end_quote
                    out_tokens.append(tokenize.TokenInfo(tok.type, new_string, tok.start, tok.end, tok.line))
                    continue
        out_tokens.append(tok)
    if changed:
        new_source = tokenize.untokenize(out_tokens)
        return new_source, True
    return source_bytes, False


def main() -> None:
    root = Path.cwd()
    print(f"Scanning for Python files in: {root}")
    changed_files = []
    error_files = []
    for py_file in get_python_files(root):
        try:
            original_bytes = py_file.read_bytes()
            new_bytes, changed = fix_source_code(original_bytes)
            if changed:
                try:
                    ast.parse(new_bytes)
                except SyntaxError as e:
                    rel_path = py_file.relative_to(root)
                    print(f"⚠️  Skipping {rel_path} - Syntax error introduced during fix: {e}")
                    error_files.append(rel_path)
                    continue
                py_file.write_bytes(new_bytes)
                rel_path = py_file.relative_to(root)
                print(f"✅ Fixed: {rel_path}")
                changed_files.append(rel_path)
        except Exception as e:
            rel_path = py_file.relative_to(root)
            print(f"❌ Error processing {rel_path}: {e}")
            error_files.append(rel_path)
    print("\n" + "=" * 40)
    print("Execution Summary")
    print("=" * 40)
    print(f"Successfully fixed: {len(changed_files)} files.")
    if error_files:
        print(f"Encountered errors: {len(error_files)} files.")
        sys.exit(1)


if __name__ == "__main__":
    main()
