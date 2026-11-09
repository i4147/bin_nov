#!/data/data/com.termux/files/usr/bin/env python
"""Format the TOML files of a tree with tree-sitter.
Parses and re-emits each ``.toml`` file with the configured indentation (``--indent``) and line width (``--column-width``), validates the result before writing and processes the files in parallel.
Usage: fmt_toml [--indent N] [--column-width N] [paths ...] Positional arguments: paths ...
Files or directories to format (default: current directory) Options: --indent Indentation size (default: 2) --column-width Column width for line wrapping (default: 80) Examples: $ fmt_toml pyproject.toml Command-line script: run ``python fmt_toml.py`` or the ``fmt_toml`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
from io import StringIO
from multiprocessing import Pool, cpu_count
from pathlib import Path
import sys
from typing import Any, Optional

from dh import MAX_WORKERS
from tree_sitter import Language, Parser


@dataclass
class FormatConfig:
    indent_size: int = 2
    column_width: int = 80
    align_values: bool = True


TOML_LANGUAGE: Language = None


def init_tree_sitter() -> None:
    global TOML_LANGUAGE
    try:
        TOML_LANGUAGE = Language("build/my-languages.so", "toml")
    except:
        try:
            import tree_sitter_toml

            TOML_LANGUAGE = tree_sitter_toml.language()
        except ImportError:
            msg = "tree-sitter-toml not found. Install with: pip install tree-sitter-toml"
            raise ImportError(msg)


class TomlVisitor:
    def __init__(self, source: bytes, tree: Any, config: FormatConfig) -> None:
        self.source: bytes = source
        self.tree: Any = tree
        self.config: FormatConfig = config
        self.output: StringIO = StringIO()
        self.indent_level: int = 0
        self.last_line: int = 0
        self.in_table: bool = False

    def visit(self, node: Any) -> str:
        self._visit_node(node)
        result: str = self.output.getvalue()
        return self._finalize_output(result)

    def _visit_node(self, node: Any) -> None:
        if node.type == "document":
            self._visit_document(node)
        elif node.type == "table":
            self._visit_table(node)
        elif node.type == "array_of_tables":
            self._visit_array_of_tables(node)
        elif node.type == "pair":
            self._visit_pair(node)
        elif node.type == "array":
            self._visit_array(node)
        elif node.type == "inline_table":
            self._visit_inline_table(node)
        elif node.type == "comment":
            self._visit_comment(node)
        elif node.child_count > 0:
            for child in node.children:
                self._visit_node(child)

    def _visit_document(self, node: Any) -> None:
        prev_line: int = 0
        for child in node.children:
            if child.type in ("table", "array_of_tables", "pair", "comment"):
                curr_line: int = child.start_point[0]
                if curr_line > prev_line and self.output.getvalue():
                    if not self.output.getvalue().endswith("\n\n"):
                        self.output.write("\n")
                self._visit_node(child)
                prev_line = child.end_point[0]

    def _visit_table(self, node: Any) -> None:
        indent: str = " " * (self.indent_level * self.config.indent_size)
        header_text: str = self.source[node.start_byte : node.end_byte].decode("utf-8")
        self.output.write(f"{indent}{header_text}\n")

    def _visit_array_of_tables(self, node: Any) -> None:
        indent: str = " " * (self.indent_level * self.config.indent_size)
        header_text: str = self.source[node.start_byte : node.end_byte].decode("utf-8")
        self.output.write(f"{indent}{header_text}\n")

    def _visit_pair(self, node: Any) -> None:
        indent: str = " " * (self.indent_level * self.config.indent_size)
        key_node: Any = None
        value_node: Any = None
        for child in node.children:
            if child.type == "key":
                key_node = child
            elif child.type in (
                "string",
                "integer",
                "float",
                "boolean",
                "array",
                "inline_table",
                "datetime",
                "date",
                "time",
            ):
                value_node = child
        if key_node and value_node:
            key_text: str = self.source[key_node.start_byte : key_node.end_byte].decode("utf-8")
            value_text: str = self.source[value_node.start_byte : value_node.end_byte].decode("utf-8")
            formatted_value: str = self._format_value(value_node)
            self.output.write(f"{indent}{key_text} = {formatted_value}")
            for child in node.children:
                if child.type == "comment":
                    comment_text: str = self.source[child.start_byte : child.end_byte].decode("utf-8")
                    self.output.write(f"  {comment_text}")
            self.output.write("\n")

    def _format_value(self, node: Any) -> str:
        if node.type == "array":
            return self._format_array(node)
        elif node.type == "inline_table":
            return self._format_inline_table(node)
        else:
            return self.source[node.start_byte : node.end_byte].decode("utf-8")

    def _format_array(self, node: Any) -> str:
        elements: list[str] = []
        for child in node.children:
            if child.type not in ("[", "]", ",", "comment"):
                elem_text: str = self.source[child.start_byte : child.end_byte].decode("utf-8").strip()
                if elem_text:
                    elements.append(elem_text)
        if not elements:
            return "[]"
        total_width: int = sum(len(e) for e in elements) + len(elements) * 2
        if total_width > self.config.column_width:
            formatted: str = "[\n"
            for elem in elements:
                indent: str = " " * ((self.indent_level + 1) * self.config.indent_size)
                formatted += f"{indent}{elem},\n"
            formatted += " " * (self.indent_level * self.config.indent_size) + "]"
            return formatted
        else:
            return "[ " + ", ".join(elements) + " ]"

    def _format_inline_table(self, node: Any) -> str:
        pairs: list[str] = []
        for child in node.children:
            if child.type == "pair":
                pair_text: str = self.source[child.start_byte : child.end_byte].decode("utf-8").strip()
                pairs.append(pair_text)
        if not pairs:
            return "{}"
        return "{ " + ", ".join(pairs) + " }"

    def _visit_comment(self, node: Any) -> None:
        indent: str = " " * (self.indent_level * self.config.indent_size)
        comment_text: str = self.source[node.start_byte : node.end_byte].decode("utf-8")
        self.output.write(f"{indent}{comment_text}\n")

    def _finalize_output(self, output: str) -> str:
        lines: list[str] = output.split("\n")
        cleaned: list[str] = [line.rstrip() for line in lines]
        result: str = "\n".join(cleaned)
        if result and not result.endswith("\n"):
            result += "\n"
        return result


class TomlFormatter:
    def __init__(self, config: FormatConfig = None) -> None:
        self.config: FormatConfig = config or FormatConfig()
        self.parser: Parser = Parser()
        self.parser.set_language(TOML_LANGUAGE)

    def format(self, content: str) -> str:
        source_bytes: bytes = content.encode("utf-8")
        tree: Any = self.parser.parse(source_bytes)
        visitor: TomlVisitor = TomlVisitor(source_bytes, tree, self.config)
        return visitor.visit(tree.root_node)


def validate_toml(content: str) -> tuple[bool, Optional[str]]:
    try:
        parser: Parser = Parser()
        parser.set_language(TOML_LANGUAGE)
        source_bytes: bytes = content.encode("utf-8")
        tree: Any = parser.parse(source_bytes)
        has_error: bool = any(child.type == "ERROR" for child in tree.root_node.children if hasattr(child, "type"))
        if has_error:
            return False, "Parse error in TOML content"
        return True, None
    except Exception as e:
        return False, str(e)


def format_file(file_path: Path, config: FormatConfig) -> tuple[Path, bool, str]:
    try:
        content: str = file_path.read_text(encoding="utf-8")
        formatter: TomlFormatter = TomlFormatter(config)
        formatted: str = formatter.format(content)
        valid: bool
        error: Optional[str]
        valid, error = validate_toml(formatted)
        if not valid:
            return file_path, False, f"Validation failed: {error}"
        file_path.write_text(formatted, encoding="utf-8")
        return file_path, True, "Formatted successfully"
    except Exception as e:
        return file_path, False, f"Error: {e!s}"


def find_toml_files(start_path: Path) -> list[Path]:
    return list(start_path.rglob("*.toml"))


def main() -> int:
    parser_obj: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Format TOML files recursively using tree-sitter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Format all .toml files in current directory
  %(prog)s path/to/file.toml           # Format specific file
  %(prog)s path/to/directory           # Format all .toml files in directory
  %(prog)s --indent 4 path/to/file.toml  # Use 4-space indentation
        """,
    )
    parser_obj.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to format (default: current directory)",
    )
    parser_obj.add_argument("--indent", type=int, default=2, help="Indentation size (default: 2)")
    parser_obj.add_argument(
        "--column-width",
        type=int,
        default=80,
        help="Column width for line wrapping (default: 80)",
    )
    args: argparse.Namespace = parser_obj.parse_args()
    init_tree_sitter()
    if not args.paths:
        paths: list[Path] = [Path.cwd()]
    else:
        paths = [Path(p) for p in args.paths]
    toml_files: list[Path] = []
    for path in paths:
        if path.is_file() and path.suffix == ".toml":
            toml_files.append(path)
        elif path.is_dir():
            toml_files.extend(find_toml_files(path))
    if not toml_files:
        print("No .toml files found")
        return 0
    config: FormatConfig = FormatConfig(indent_size=args.indent, column_width=args.column_width)
    num_workers: int = MAX_WORKERS or cpu_count()
    with Pool(num_workers) as pool_obj:
        results: Any = pool_obj.imap_unordered(lambda f: format_file(f, config), toml_files)
        success_count: int = 0
        failed_files: list[Path] = []
        for file_path, success, message in results:
            if success:
                print(f"✓ {file_path}")
                success_count += 1
            else:
                print(f"✗ {file_path}: {message}")
                failed_files.append(file_path)
    total: int = len(toml_files)
    print(f"\n{'=' * 60}")
    print(f"Formatted: {success_count}/{total} files")
    if failed_files:
        print(f"\nFailed files:")
        for f in failed_files:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
