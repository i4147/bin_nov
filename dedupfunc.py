#!/data/data/com.termux/files/usr/bin/env python
"""Find and optionally remove the duplicate functions of a Python file.
Compares the normalized function bodies with ast, prints each duplicate group and with ``-r`` asks which definition to keep before deleting the others (``--backup`` keeps a copy).
Usage: dedupfunc FILE.py [-r] [--backup] Positional arguments: file Python file to analyze Options: -r, --remove Remove duplicate functions with user confirmation --backup Create a backup before removing (implies -r) Examples: $ dedupfunc module.py Command-line script: run ``python dedupfunc.py`` or the ``dedupfunc`` command.
The module is importable as well."""

import argparse
import ast
from collections import defaultdict
from pathlib import Path
import re
import sys


class FunctionInfo:
    def __init__(self, name: str, body: str, lineno: int, node: ast.FunctionDef) -> None:
        self.name = name
        self.body = self._normalize_body(body)
        self.original_body = body
        self.lineno = lineno
        self.node = node

    @staticmethod
    def _normalize_body(body: str) -> str:
        lines = []
        for line in body.split("\n"):
            line_without_comments = re.sub(r"(?<![\"\'])#.*$", "", line)
            lines.append(line_without_comments)
        body = "\n".join(lines)
        lines = [line.strip() for line in body.split("\n") if line.strip()]
        return "\n".join(lines)


class DuplicateFunctionFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.functions: list[FunctionInfo] = []
        self.source_lines: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        body_start = node.body[0].lineno - 1
        body_end = node.body[-1].end_lineno
        body_lines = self.source_lines[body_start:body_end]
        body_code = "\n".join(body_lines)
        func_info = FunctionInfo(name=node.name, body=body_code, lineno=node.lineno, node=node)
        self.functions.append(func_info)
        self.generic_visit(node)

    def analyze_file(self, path: str) -> dict[str, list[FunctionInfo]]:
        with open(path, encoding="utf-8") as f:
            content = f.read()
            self.source_lines = content.splitlines()
        try:
            tree = ast.parse(content)
            self.visit(tree)
        except SyntaxError as e:
            print(f"Syntax error in file: {e}")
            return {}
        groups = defaultdict(list)
        for func in self.functions:
            groups[func.body].append(func)
        return {body: funcs for body, funcs in groups.items() if len(funcs) > 1}


class DuplicateFunctionRemover:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.content = None
        self.lines = None

    def _get_function_lines(self, func_info: FunctionInfo) -> tuple[int, int]:
        node = func_info.node
        end_line = node.end_lineno if hasattr(node, "end_lineno") else node.lineno
        if node.decorator_list:
            start_line = node.decorator_list[0].lineno
        else:
            start_line = node.lineno
        return start_line, end_line

    def _extract_function_code(self, func_info: FunctionInfo) -> str:
        start_line, end_line = self._get_function_lines(func_info)
        return "\n".join(self.lines[start_line - 1 : end_line])

    def remove_duplicates(self, groups: dict[str, list[FunctionInfo]], keep_choice: dict[str, int]) -> bool:
        with open(self.path, encoding="utf-8") as f:
            self.content = f.read()
            self.lines = self.content.splitlines()
        lines_to_remove = set()
        functions_to_remove = []
        for body, funcs in groups.items():
            keep_index = keep_choice.get(body, 0)
            for i, func in enumerate(funcs):
                if i != keep_index:
                    start_line, end_line = self._get_function_lines(func)
                    for line_num in range(start_line - 1, end_line):
                        lines_to_remove.add(line_num)
                    functions_to_remove.append(func)
        new_lines = []
        for i, line in enumerate(self.lines):
            if i not in lines_to_remove:
                new_lines.append(line)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines))
            return True
        except Exception as e:
            print(f"Error writing to file: {e}")
            return False

    def backup_file(self) -> str:
        backup_path = self.path.with_suffix(self.path.suffix + ".backup")
        with (
            open(self.path, encoding="utf-8") as src,
            open(backup_path, "w", encoding="utf-8") as dst,
        ):
            dst.write(src.read())
        return str(backup_path)


def display_duplicates(groups: dict[str, list[FunctionInfo]]) -> bool:
    if not groups:
        print("No duplicate functions found!")
        return False
    print("\n" + "=" * 40)
    print("DUPLICATE FUNCTIONS FOUND")
    print("-" * 40)
    for idx, (body, funcs) in enumerate(groups.items(), 1):
        print(f"\nGroup {idx}:")
        print(f"  Body hash: {hash(body)}")
        print(f"  {len(funcs)} functions with identical body:")
        for i, func in enumerate(funcs, 1):
            print(f"    {i}. '{func.name}' (line {func.lineno})")
        body_preview = body[:150] + "..." if len(body) > 150 else body
        print(f"\n  Body preview:\n{body_preview}")
        print("-" * 40)
    return True


def get_user_choices(groups: dict[str, list[FunctionInfo]]) -> dict[str, int]:
    choices = {}
    for body, funcs in groups.items():
        print(f"\nGroup with {len(funcs)} duplicate functions:")
        for i, func in enumerate(funcs):
            print(f"  [{i}] Keep '{func.name}' (line {func.lineno})")
        while True:
            try:
                choice = input(f"Which function to keep? [0-{len(funcs) - 1}]: ")
                choice_num = int(choice)
                if 0 <= choice_num < len(funcs):
                    choices[body] = choice_num
                    break
                print(f"Please enter a number between 0 and {len(funcs) - 1}")
            except ValueError:
                print("Please enter a valid number")
    return choices


def main() -> None:
    parser = argparse.ArgumentParser(description="Find and optionally remove duplicate functions in Python files")
    parser.add_argument("file", help="Python file to analyze")
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Remove duplicate functions with user confirmation",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create a backup before removing (implies -r)",
    )
    args = parser.parse_args()
    path = args.file
    if not Path(path).exists():
        print(f"Error: File '{path}' not found")
        sys.exit(1)
    if not path.endswith(".py"):
        print(f"Warning: File '{path}' does not have .py extension")
        response = input("Continue anyway? (y/N): ")
        if response.lower() != "y":
            sys.exit(0)
    print(f"Analyzing {path}...")
    finder = DuplicateFunctionFinder()
    duplicates = finder.analyze_file(path)
    if not display_duplicates(duplicates):
        sys.exit(0)
    if args.remove or args.backup:
        remover = DuplicateFunctionRemover(path)
        if args.backup:
            backup_path = remover.backup_file()
            print(f"\nBackup created at: {backup_path}")
        print("\n" + "=" * 40)
        print("SELECT FUNCTIONS TO KEEP")
        print("-" * 40)
        choices = get_user_choices(duplicates)
        print("\n" + "=" * 40)
        confirm = input("Proceed with removing duplicate functions? (y/N): ")
        if confirm.lower() == "y":
            if remover.remove_duplicates(duplicates, choices):
                print("✓ Duplicate functions removed successfully!")
            else:
                print("✗ Failed to remove duplicates")
                sys.exit(1)
        else:
            print("Operation cancelled")
            sys.exit(0)
    else:
        print("\nUse -r or --remove to remove duplicates with user confirmation")


if __name__ == "__main__":
    raise SystemExit(main())
