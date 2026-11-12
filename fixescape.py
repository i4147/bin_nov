#!/data/data/com.termux/files/usr/bin/env python
"""Find and fix the invalid escape sequences of Python string literals.
Compiles each file with the warnings promoted to errors, locates the offending literals with tokenize, asks to make them raw or to double the backslash and with ``-a`` applies the fixes in a pool.
Usage: fixescape [-a] [path] Positional arguments: path Directory to scan (default: current directory) Options: -a, --apply Update inplace in -a mode to fix issues (convert to raw regex) Examples: $ fixescape -a src/ Command-line script: run ``python fixescape.py`` or the ``fixescape`` command.
The module is importable as well."""

import argparse
import ast
import contextlib
import io
import multiprocessing as mp
import pathlib
import re
import sys
import tokenize
import warnings
from typing import Any

RED: str = "\033[91m"
CYAN: str = "\033[96m"
RESET: str = "\033[0m"


def check_and_fix_file(args: list[Any]):
    filepath_str, fix = args
    filepath = pathlib.Path(filepath_str)
    try:
        with open(filepath, "r", encoding="utf-8", newline="") as f:
            source_text = f.read()
    except Exception as e:
        return (
            filepath_str,
            True,
            [f"{RED}Error reading {filepath_str}: {e}{RESET}"],
            None,
        )
    has_warning = False
    has_syntax_error = False
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always", SyntaxWarning)
        warnings.simplefilter("always", DeprecationWarning)
        try:
            compile(source_text, str(filepath), "exec")
        except SyntaxError:
            has_syntax_error = True
        except Exception:
            pass
        has_warning = any("invalid escape sequence" in str(warn.message) for warn in w)
    if not has_warning and not has_syntax_error:
        return filepath_str, False, [], None
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source_text).readline))
    except (tokenize.TokenError, IndentationError) as e:
        return filepath_str, True, [f"{RED}Cannot tokenize {filepath_str}: {e}{RESET}"], None
    issues = []
    replacements = []
    manual_review_lines = []
    for tok in tokens:
        if tok.type == tokenize.STRING:
            with warnings.catch_warnings(record=True) as w2:
                warnings.simplefilter("always", SyntaxWarning)
                warnings.simplefilter("always", DeprecationWarning)
                with contextlib.suppress(Exception):
                    compile(tok.string, "<string>", "eval")
                if any("invalid escape sequence" in str(warn.message) for warn in w2):
                    m = re.match(r'^([a-zA-Z_]*)(["\'].*)$', tok.string, re.DOTALL)
                    if m:
                        prefix = m.group(1)
                        rest = m.group(2)
                        if "r" not in prefix.lower():
                            prefix = prefix.replace("u", "").replace("U", "")
                            new_string = prefix + "r" + rest
                            issues.append((tok.start[0], tok.string, new_string))
                            try:
                                original_ast = ast.dump(ast.parse(tok.string, mode="eval"))
                                replacement_ast = ast.dump(ast.parse(new_string, mode="eval"))
                            except SyntaxError:
                                manual_review_lines.append(tok.start[0])
                                continue
                            if original_ast != replacement_ast:
                                manual_review_lines.append(tok.start[0])
                                continue
                            replacements.append((
                                tok.start[0],
                                tok.start[1],
                                tok.end[0],
                                tok.end[1],
                                new_string,
                            ))
    if not issues:
        return filepath_str, False, [], None
    lines = source_text.splitlines(keepends=True)
    output = []
    output.append(f"{CYAN}File: {filepath}{RESET}")
    issue_lines = sorted({line_num for line_num, _, _ in issues})
    for lineno in issue_lines:
        idx = lineno - 1
        if idx - 1 >= 0:
            previous_line = lines[idx - 1].rstrip("\r\n")
            output.append(f"  {idx}: {previous_line}")
        current_line = lines[idx].rstrip("\r\n")
        output.append(f"{RED}> {idx + 1}: {current_line}{RESET}")
        if idx + 1 < len(lines):
            next_line = lines[idx + 1].rstrip("\r\n")
            output.append(f"  {idx + 2}: {next_line}")
        output.append("-" * 40)
    for lineno in sorted(set(manual_review_lines)):
        output.append(f"Line {lineno} needs manual review; a raw string could change its meaning or be invalid.")
    new_content = None
    if fix and replacements:
        replacements.sort(key=lambda x: (x[0], x[1]), reverse=True)
        text_lines = source_text.splitlines(keepends=True)
        for r_start, c_start, r_end, c_end, new_string in replacements:
            r1 = r_start - 1
            r2 = r_end - 1
            if r1 == r2:
                line = text_lines[r1]
                text_lines[r1] = line[:c_start] + new_string + line[c_end:]
            else:
                first_line = text_lines[r1]
                last_line = text_lines[r2]
                text_lines[r1] = first_line[:c_start] + new_string + last_line[c_end:]
                for i in range(r2, r1, -1):
                    del text_lines[i]
        new_content = "".join(text_lines)
    return filepath_str, True, output, new_content


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recursively find and fix 'invalid escape sequence' SyntaxWarnings in Python code."
    )
    parser.add_argument(
        "-a",
        "--apply",
        action="store_true",
        help="Update inplace in -a mode to fix issues (convert to raw regex)",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory)",
    )
    args = parser.parse_args()
    base_path = pathlib.Path(args.path)
    if base_path.is_file():
        files = [base_path]
    else:
        files = [
            f
            for f in base_path.rglob("*.py")
            if not any(part.startswith(".") for part in f.parts[:-1])
            and "venv" not in f.parts
            and "__pycache__" not in f.parts
        ]
    if not files:
        print("No Python files found.")
        sys.exit(0)
    tasks = [(str(f), args.apply) for f in files]
    with mp.Pool(8) as pool:
        for filepath_str, has_issues, output_lines, new_content in pool.imap_unordered(check_and_fix_file, tasks):
            if has_issues:
                for line in output_lines:
                    print(line)
                if args.apply and new_content is not None:
                    try:
                        with open(filepath_str, "w", encoding="utf-8", newline="") as f:
                            f.write(new_content)
                        print(f"{CYAN}Fixed inplace: {filepath_str}{RESET}\n")
                    except Exception as e:
                        print(f"{RED}Failed to write {filepath_str}: {e}{RESET}\n")


if __name__ == "__main__":
    main()
