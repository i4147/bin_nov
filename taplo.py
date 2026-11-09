#!/data/data/com.termux/files/usr/bin/env python
"""A pure-Python TOML formatter.
Tokenizes the document, builds a small AST (tables, arrays of tables, key/value pairs), normalises the spacing, the indentation and the quote style of the keys and values and prints the formatted TOML, with the formatting rules held in ``FormatConfig``.
Usage: taplo [FILE] Examples: $ taplo pyproject.toml Runs as a script (``python taplo.py``) in the directory it should act on; the module can also be imported."""

from dataclasses import dataclass, field
from enum import Enum, auto
from io import StringIO
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from loguru import logger


logger.enable("__main__")


class TokenType(Enum):
    EOF = auto()
    NEWLINE = auto()
    COMMENT = auto()
    LBRACKET = auto()
    RBRACKET = auto()
    LBRACE = auto()
    RBRACE = auto()
    LSQUARE = auto()
    RSQUARE = auto()
    COMMA = auto()
    DOT = auto()
    EQUALS = auto()
    KEY = auto()
    STRING = auto()
    INTEGER = auto()
    FLOAT = auto()
    BOOLEAN = auto()
    DATETIME = auto()
    WHITESPACE = auto()


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str
    line: int
    col: int


@dataclass
class FormatConfig:
    indent_size: int = 2
    sort_keys: bool = True
    spaces_around_equals: bool = True
    spaces_in_braces: bool = True
    array_trailing_comma: bool = False
    column_align_equals: bool = False
    max_inline_table_width: int = 120


class Tokenizer:
    def __init__(self, source: str) -> None:
        self.source: str = source
        self.pos: int = 0
        self.line: int = 1
        self.col: int = 1
        self.tokens: list[Token] = []

    def current_char(self) -> str | None:
        return self.source[self.pos] if self.pos < len(self.source) else None

    def peek_char(self, offset: int = 1) -> str | None:
        p = self.pos + offset
        return self.source[p] if p < len(self.source) else None

    def advance(self) -> None:
        if self.pos < len(self.source):
            if self.source[self.pos] == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
            self.pos += 1

    def skip_whitespace_inline(self) -> None:
        while self.current_char() in (" ", "\t"):
            self.advance()

    def read_string(self, quote: str) -> str:
        start_pos = self.pos
        self.advance()
        result: list[str] = []
        is_multiline = self.current_char() == quote and self.peek_char() == quote
        if is_multiline:
            self.advance()
            self.advance()
            while self.pos < len(self.source):
                if self.current_char() == quote and self.peek_char() == quote and self.peek_char(2) == quote:
                    self.advance()
                    self.advance()
                    self.advance()
                    break
                if self.current_char() == "\\":
                    self.advance()
                    if self.current_char() in ("n", "t", "r", "\\", '"', "'"):
                        result.append(self.current_char() or "")
                        self.advance()
                    elif self.current_char() == "\n" and quote == '"':
                        self.advance()
                        while self.current_char() in (" ", "\t", "\n"):
                            self.advance()
                else:
                    result.append(self.current_char() or "")
                    self.advance()
        else:
            while self.pos < len(self.source) and self.current_char() != quote:
                if self.current_char() == "\\":
                    self.advance()
                    if self.current_char() in (
                        "n",
                        "t",
                        "r",
                        "b",
                        "f",
                        "\\",
                        '"',
                        "'",
                        "u",
                        "U",
                    ):
                        result.append(self.current_char() or "")
                        self.advance()
                else:
                    result.append(self.current_char() or "")
                    self.advance()
            if self.current_char() == quote:
                self.advance()
        return quote + "".join(result) + quote if is_multiline else quote + "".join(result) + quote

    def read_number(self) -> tuple[TokenType, str]:
        start = self.pos
        has_dot = False
        has_e = False
        while self.current_char() and self.current_char() in "0123456789._eE+-":
            if self.current_char() == ".":
                if has_dot or has_e:
                    break
                has_dot = True
            elif self.current_char() in "eE":
                if has_e:
                    break
                has_e = True
                self.advance()
                if self.current_char() in "+-":
                    self.advance()
                continue
            self.advance()
        num_str = self.source[start : self.pos]
        return (TokenType.FLOAT if has_dot or has_e else TokenType.INTEGER, num_str)

    def read_key(self) -> str:
        start = self.pos
        while self.current_char() and (self.current_char().isalnum() or self.current_char() in "_-"):
            self.advance()
        return self.source[start : self.pos]

    def tokenize(self) -> list[Token]:
        while self.pos < len(self.source):
            line, col = self.line, self.col
            ch = self.current_char()
            if ch == "#":
                start = self.pos
                while self.current_char() and self.current_char() != "\n":
                    self.advance()
                self.tokens.append(Token(TokenType.COMMENT, self.source[start : self.pos], line, col))
            elif ch == "\n":
                self.tokens.append(Token(TokenType.NEWLINE, "\n", line, col))
                self.advance()
            elif ch in (" ", "\t"):
                start = self.pos
                while self.current_char() in (" ", "\t"):
                    self.advance()
                self.tokens.append(Token(TokenType.WHITESPACE, self.source[start : self.pos], line, col))
            elif ch == "[":
                if self.peek_char() == "[":
                    self.advance()
                    self.advance()
                    self.tokens.append(Token(TokenType.LBRACKET, "[[", line, col))
                else:
                    self.advance()
                    self.tokens.append(Token(TokenType.LSQUARE, "[", line, col))
            elif ch == "]":
                if self.peek_char() == "]":
                    self.advance()
                    self.advance()
                    self.tokens.append(Token(TokenType.RBRACKET, "]]", line, col))
                else:
                    self.advance()
                    self.tokens.append(Token(TokenType.RSQUARE, "]", line, col))
            elif ch == "{":
                self.advance()
                self.tokens.append(Token(TokenType.LBRACE, "{", line, col))
            elif ch == "}":
                self.advance()
                self.tokens.append(Token(TokenType.RBRACE, "}", line, col))
            elif ch == "=":
                self.advance()
                self.tokens.append(Token(TokenType.EQUALS, "=", line, col))
            elif ch == ",":
                self.advance()
                self.tokens.append(Token(TokenType.COMMA, ",", line, col))
            elif ch == ".":
                self.advance()
                self.tokens.append(Token(TokenType.DOT, ".", line, col))
            elif ch in ('"', "'"):
                val = self.read_string(ch)
                self.tokens.append(Token(TokenType.STRING, val, line, col))
            elif ch == "-" or ch.isdigit():
                if ch == "-" and self.peek_char() and not self.peek_char().isdigit():
                    key = self.read_key()
                    self.tokens.append(Token(TokenType.KEY, key, line, col))
                else:
                    tt, val = self.read_number()
                    self.tokens.append(Token(tt, val, line, col))
            elif ch.isalpha() or ch == "_":
                start = self.pos
                word = self.read_key()
                if word in ("true", "false"):
                    self.tokens.append(Token(TokenType.BOOLEAN, word, line, col))
                elif self._is_datetime(word):
                    self.tokens.append(Token(TokenType.DATETIME, word, line, col))
                else:
                    self.tokens.append(Token(TokenType.KEY, word, line, col))
            else:
                self.advance()
        self.tokens.append(Token(TokenType.EOF, "", self.line, self.col))
        return self.tokens

    def _is_datetime(self, s: str) -> bool:
        iso_pattern = r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$"
        return bool(re.match(iso_pattern, s))


@dataclass
class AST:
    root: dict[str, Any] = field(default_factory=dict)
    comments: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens: list[Token] = tokens
        self.pos: int = 0

    def current(self) -> Token:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else self.tokens[-1]

    def peek(self, offset: int = 1) -> Token:
        p = self.pos + offset
        return self.tokens[p] if p < len(self.tokens) else self.tokens[-1]

    def advance(self) -> Token:
        token = self.current()
        self.pos += 1
        return token

    def skip_whitespace_and_comments(self) -> None:
        while self.current().type in (
            TokenType.WHITESPACE,
            TokenType.COMMENT,
            TokenType.NEWLINE,
        ):
            self.advance()

    def expect(self, tt: TokenType) -> Token:
        if self.current().type != tt:
            msg = f"Expected {tt}, got {self.current().type} at line {self.current().line}"
            raise SyntaxError(msg)
        return self.advance()

    def parse_key(self) -> list[str]:
        keys: list[str] = []
        while True:
            self.skip_whitespace_and_comments()
            if self.current().type == TokenType.STRING:
                keys.append(self.advance().value.strip("\"'"))
            elif self.current().type == TokenType.KEY:
                keys.append(self.advance().value)
            else:
                msg = f"Expected key at line {self.current().line}"
                raise SyntaxError(msg)
            self.skip_whitespace_and_comments()
            if self.current().type == TokenType.DOT:
                self.advance()
            else:
                break
        return keys

    def parse_value(self) -> Any:
        self.skip_whitespace_and_comments()
        token = self.current()
        if token.type == TokenType.STRING:
            return self.advance().value
        elif token.type == TokenType.INTEGER:
            return int(self.advance().value)
        elif token.type == TokenType.FLOAT:
            return float(self.advance().value)
        elif token.type == TokenType.BOOLEAN:
            return self.advance().value == "true"
        elif token.type == TokenType.DATETIME:
            return self.advance().value
        elif token.type == TokenType.LSQUARE:
            return self.parse_array()
        elif token.type == TokenType.LBRACE:
            return self.parse_inline_table()
        else:
            msg = f"Unexpected token {token.type} at line {token.line}"
            raise SyntaxError(msg)

    def parse_array(self) -> list[Any]:
        self.expect(TokenType.LSQUARE)
        result: list[Any] = []
        while True:
            self.skip_whitespace_and_comments()
            if self.current().type == TokenType.RSQUARE:
                self.advance()
                break
            result.append(self.parse_value())
            self.skip_whitespace_and_comments()
            if self.current().type == TokenType.COMMA:
                self.advance()
            elif self.current().type != TokenType.RSQUARE:
                msg = f"Expected ',' or ']' at line {self.current().line}"
                raise SyntaxError(msg)
        return result

    def parse_inline_table(self) -> dict[str, Any]:
        self.expect(TokenType.LBRACE)
        result: dict[str, Any] = {}
        while True:
            self.skip_whitespace_and_comments()
            if self.current().type == TokenType.RBRACE:
                self.advance()
                break
            keys = self.parse_key()
            self.skip_whitespace_and_comments()
            self.expect(TokenType.EQUALS)
            val = self.parse_value()
            current = result
            for k in keys[:-1]:
                current = current.setdefault(k, {})
            current[keys[-1]] = val
            self.skip_whitespace_and_comments()
            if self.current().type == TokenType.COMMA:
                self.advance()
            elif self.current().type != TokenType.RBRACE:
                msg = f"Expected ',' or '}}' at line {self.current().line}"
                raise SyntaxError(msg)
        return result

    def parse_document(self) -> AST:
        ast = AST()
        while self.current().type != TokenType.EOF:
            self.skip_whitespace_and_comments()
            if self.current().type == TokenType.LSQUARE:
                is_array_table = self.peek().type == TokenType.LBRACKET
                if is_array_table:
                    self.advance()
                self.advance()
                keys = self.parse_key()
                self.skip_whitespace_and_comments()
                if is_array_table:
                    self.expect(TokenType.RBRACKET)
                self.expect(TokenType.RSQUARE)
                current = ast.root
                for k in keys[:-1]:
                    current = current.setdefault(k, {})
                if is_array_table:
                    if keys[-1] not in current:
                        current[keys[-1]] = []
                    table = {}
                    current[keys[-1]].append(table)
                    current = table
                else:
                    current = current.setdefault(keys[-1], {})
                ast.order.append(".".join(keys))
                while self.current().type != TokenType.EOF and self.current().type != TokenType.LSQUARE:
                    self.skip_whitespace_and_comments()
                    if self.current().type == TokenType.KEY or self.current().type == TokenType.STRING:
                        keys = self.parse_key()
                        self.skip_whitespace_and_comments()
                        self.expect(TokenType.EQUALS)
                        val = self.parse_value()
                        nested = current
                        for k in keys[:-1]:
                            nested = nested.setdefault(k, {})
                        nested[keys[-1]] = val
            elif self.current().type == TokenType.KEY or self.current().type == TokenType.STRING:
                keys = self.parse_key()
                self.skip_whitespace_and_comments()
                self.expect(TokenType.EQUALS)
                val = self.parse_value()
                current = ast.root
                for k in keys[:-1]:
                    current = current.setdefault(k, {})
                current[keys[-1]] = val
            else:
                self.advance()
        return ast


class Formatter:
    def __init__(self, config: FormatConfig = FormatConfig()) -> None:
        self.config = config
        self.output = StringIO()
        self.indent_level = 0

    def format(self, ast: AST) -> str:
        self._format_table(ast.root, [], is_root=True)
        return self.output.getvalue().rstrip() + "\n"

    def _indent(self) -> str:
        return " " * (self.indent_level * self.config.indent_size)

    def _format_table(self, table: dict[str, Any], path: list[str], is_root: bool = False) -> None:
        if not is_root and table:
            if path:
                self.output.write("\n[" + ".".join(path) + "]\n")
            self.indent_level += 1
        keys = sorted(table.keys()) if self.config.sort_keys else list(table.keys())
        for key in keys:
            val = table[key]
            if isinstance(val, dict) and not self._is_inline_table(val):
                continue
            if isinstance(val, list) and val and isinstance(val[0], dict):
                continue
            indent = self._indent() if not is_root else ""
            eq_spacing = " = " if self.config.spaces_around_equals else "="
            if isinstance(val, dict):
                formatted_val = self._format_inline_table(val)
                self.output.write(f"{indent}{key}{eq_spacing}{formatted_val}\n")
            elif isinstance(val, list):
                formatted_val = self._format_array(val)
                self.output.write(f"{indent}{key}{eq_spacing}{formatted_val}\n")
            elif isinstance(val, bool):
                bool_str = "true" if val else "false"
                self.output.write(f"{indent}{key}{eq_spacing}{bool_str}\n")
            elif isinstance(val, str):
                self.output.write(f"{indent}{key}{eq_spacing}{val}\n")
            elif isinstance(val, (int, float)):
                self.output.write(f"{indent}{key}{eq_spacing}{val}\n")
        if not is_root:
            self.indent_level -= 1
        for key in keys:
            val = table[key]
            if isinstance(val, dict) and not self._is_inline_table(val):
                self._format_table(val, path + [key])
            elif isinstance(val, list) and val and isinstance(val[0], dict):
                for i, item in enumerate(val):
                    self._format_array_table(item, path + [key])

    def _format_array_table(self, table: dict[str, Any], path: list[str]) -> None:
        self.output.write("\n[[" + ".".join(path) + "]]\n")
        self.indent_level += 1
        keys = sorted(table.keys()) if self.config.sort_keys else list(table.keys())
        for key in keys:
            val = table[key]
            indent = self._indent()
            eq_spacing = " = " if self.config.spaces_around_equals else "="
            if isinstance(val, dict) and not self._is_inline_table(val):
                continue
            elif isinstance(val, list) and val and isinstance(val[0], dict):
                continue
            if isinstance(val, dict):
                formatted_val = self._format_inline_table(val)
                self.output.write(f"{indent}{key}{eq_spacing}{formatted_val}\n")
            elif isinstance(val, list):
                formatted_val = self._format_array(val)
                self.output.write(f"{indent}{key}{eq_spacing}{formatted_val}\n")
            elif isinstance(val, bool):
                bool_str = "true" if val else "false"
                self.output.write(f"{indent}{key}{eq_spacing}{bool_str}\n")
            elif isinstance(val, str):
                self.output.write(f"{indent}{key}{eq_spacing}{val}\n")
            elif isinstance(val, (int, float)):
                self.output.write(f"{indent}{key}{eq_spacing}{val}\n")
        self.indent_level -= 1
        for key in keys:
            val = table[key]
            if isinstance(val, dict) and not self._is_inline_table(val):
                self._format_table(val, path + [key])

    def _is_inline_table(self, val: dict[str, Any]) -> bool:
        return all(not isinstance(v, (dict, list)) for v in val.values())

    def _format_inline_table(self, table: dict[str, Any]) -> str:
        if not table:
            return "{}" if self.config.spaces_in_braces else "{}"
        pairs = []
        keys = sorted(table.keys()) if self.config.sort_keys else list(table.keys())
        for key in keys:
            val = table[key]
            eq = " = " if self.config.spaces_around_equals else "="
            formatted_val = self._format_value(val)
            pairs.append(f"{key}{eq}{formatted_val}")
        content = ", ".join(pairs)
        if self.config.spaces_in_braces:
            return "{ " + content + " }"
        else:
            return "{" + content + "}"

    def _format_array(self, arr: list[Any]) -> str:
        if not arr:
            return "[]"
        all_simple = all(not isinstance(x, (dict, list)) for x in arr)
        if all_simple:
            formatted = [self._format_value(x) for x in arr]
            content = ", ".join(formatted)
            if len(content) <= self.config.max_inline_table_width:
                return "[" + content + "]"
        result = ["["]
        for i, item in enumerate(arr):
            formatted = self._format_value(item)
            if self.config.array_trailing_comma or i < len(arr) - 1:
                result.append(f"  {formatted},")
            else:
                result.append(f"  {formatted}")
        result.append("]")
        return "\n".join(result)

    def _format_value(self, val: Any) -> str:
        if isinstance(val, bool):
            return "true" if val else "false"
        elif isinstance(val, str):
            return val
        elif isinstance(val, (int, float)):
            return str(val)
        elif isinstance(val, dict):
            return self._format_inline_table(val)
        elif isinstance(val, list):
            return self._format_array(val)
        else:
            return str(val)


class TOMLFormatter:
    def __init__(self, config: FormatConfig = FormatConfig()) -> None:
        self.config = config

    def format_string(self, source: str) -> str:
        tokens = Tokenizer(source).tokenize()
        ast = Parser(tokens).parse_document()
        formatter = Formatter(self.config)
        return formatter.format(ast)

    def format_file(self, path: Path) -> str:
        source = path.read_text(encoding="utf-8")
        return self.format_string(source)

    def format_file_inplace(self, path: Path) -> None:
        formatted = self.format_file(path)
        path.write_text(formatted, encoding="utf-8")

    def stream_format(self, input_path: Path, output_path: Path, chunk_size: int = 8192) -> None:
        source = input_path.read_text(encoding="utf-8")
        formatted = self.format_string(source)
        output_path.write_text(formatted, encoding="utf-8")


def main() -> None:
    test_toml = """# Legacy config
[database]
host="localhost"
port=5432
connection={timeout=30,retries=3}
servers=["alpha","beta","gamma"]
[database.connection]
ssl=true
"""
    config = FormatConfig(
        indent_size=2,
        sort_keys=True,
        spaces_around_equals=True,
        spaces_in_braces=True,
        array_trailing_comma=False,
    )
    formatter = TOMLFormatter(config)
    result = formatter.format_string(test_toml)
    logger.info("Formatted TOML:")
    print(result)


if __name__ == "__main__":
    main()
