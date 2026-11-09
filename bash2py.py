#!/data/data/com.termux/files/usr/bin/env python
"""Extract ``python`` here-documents from shell scripts into standalone Python files.
Finds ``python <<EOF`` style blocks, re-indents the captured code and writes it next to the caller as ``<script>.py`` (``<script>_1.py``, ``_2`` ...
when a script contains several blocks), formatting the result along the way.
Usage: bash2py SCRIPT.sh Examples: $ bash2py install.sh Runs as a script (``python bash2py.py``) in the directory it should act on; the module can also be imported."""

import io
from pathlib import Path
import re
import sys
import tokenize


HEREDOC_START_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<cmd>(?:python3?|py)\b[^;\n]*?)"
    r"<<(?P<dash>-)?[ \t]*"
    r'(?P<quote>["\']?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)[ \t]*$',
    re.MULTILINE,
)


def extract_python_heredocs(bash_text: str) -> list[str]:
    blocks: list[str] = []
    lines = bash_text.splitlines(keepends=True)
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = HEREDOC_START_RE.match(line.rstrip("\n"))
        if not m:
            i += 1
            continue
        delim = m.group("delim")
        dash = m.group("dash") is not None
        body_lines: list[str] = []
        j = i + 1
        terminator_found = False
        while j < n:
            raw = lines[j]
            stripped = raw.rstrip("\n")
            candidate = stripped.strip() if dash else stripped
            if candidate == delim:
                terminator_found = True
                break
            body_lines.append(raw)
            j += 1
        if terminator_found:
            blocks.append("".join(body_lines))
            i = j + 1
        else:
            blocks.append("".join(lines[i + 1 :]))
            i = n
    return blocks


def format_python_code(code: str) -> str:
    if not code.endswith("\n"):
        code += "\n"
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except tokenize.TokenError:
        return code
    result: list[str] = []
    indent_level = 0
    at_line_start = True
    paren_depth = 0
    previous = None

    def append(text: str) -> None:
        nonlocal at_line_start
        if at_line_start:
            result.append("    " * indent_level)
            at_line_start = False
        result.append(text)

    def add_space() -> None:
        if result and not result[-1].endswith((" ", "\n")):
            result.append(" ")

    def newline() -> None:
        nonlocal at_line_start
        while result and result[-1] == " ":
            result.pop()
        if result and not result[-1].endswith("\n"):
            result.append("\n")
        at_line_start = True

    for token in tokens:
        token_type = token.type
        text = token.string
        if token_type in {tokenize.ENCODING, tokenize.ENDMARKER, tokenize.NL}:
            continue
        if token_type == tokenize.NEWLINE:
            newline()
            previous = token
            continue
        if token_type == tokenize.INDENT:
            indent_level += 1
            continue
        if token_type == tokenize.DEDENT:
            indent_level = max(0, indent_level - 1)
            continue
        if token_type == tokenize.COMMENT:
            if not at_line_start:
                add_space()
            append(text)
            newline()
            previous = token
            continue
        if text in {"(", "[", "{"}:
            append(text)
            paren_depth += 1
            previous = token
            continue
        if text in {")", "]", "}"}:
            append(text)
            paren_depth = max(0, paren_depth - 1)
            previous = token
            continue
        if text == ",":
            append(",")
            add_space()
            previous = token
            continue
        if text == ";":
            newline()
            previous = token
            continue
        if text == ":" and paren_depth == 0:
            append(":")
            newline()
            indent_level += 1
            previous = token
            continue
        if (
            token_type == tokenize.NAME
            and previous is not None
            and paren_depth == 0
            and previous.string in {")", "]", "}", '"', "'"}
            and text
            in {
                "import",
                "from",
                "for",
                "while",
                "if",
                "elif",
                "else",
                "try",
                "except",
                "finally",
                "with",
                "def",
                "class",
                "return",
                "raise",
                "print",
            }
        ):
            newline()
        if (
            previous is not None
            and previous.type in {tokenize.NAME, tokenize.NUMBER, tokenize.STRING}
            and token_type in {tokenize.NAME, tokenize.NUMBER, tokenize.STRING}
        ):
            add_space()
        if text in {
            "=",
            "+=",
            "-=",
            "*=",
            "/=",
            "//=",
            "%=",
            "==",
            "!=",
            "<",
            ">",
            "<=",
            ">=",
            "+",
            "-",
            "*",
            "/",
            "//",
            "%",
            "|",
            "&",
            "^",
        }:
            add_space()
            append(text)
            add_space()
        else:
            append(text)
        previous = token
    return "".join(result).rstrip() + "\n"


def get_output_path(input_path: Path, idx: int, total: int) -> Path:
    stem = input_path.stem or "extracted"
    if total == 1:
        output = Path.cwd() / f"{stem}.py"
        number = 2
        while output.exists():
            output = Path.cwd() / f"{stem}_{number}.py"
            number += 1
        return output
    else:
        output = Path.cwd() / f"{stem}_{idx}.py"
        number = 2
        while output.exists():
            output = Path.cwd() / f"{stem}_{idx}_{number}.py"
            number += 1
        return output


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python script.py <bash_script_path>", file=sys.stderr)
        sys.exit(1)
    input_path = Path(sys.argv[1]).expanduser()
    if not input_path.is_file():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)
    try:
        bash_text = input_path.read_text(encoding="utf-8")
        blocks = extract_python_heredocs(bash_text)
        if not blocks:
            print("No python heredocs found in the given script.", file=sys.stderr)
            sys.exit(1)
        written: list[Path] = []
        total = len(blocks)
        for idx, code in enumerate(blocks, start=1):
            formatted_code = format_python_code(code)
            out_path = get_output_path(input_path, idx, total)
            out_path.write_text(formatted_code, encoding="utf-8")
            written.append(out_path)
        for p in written:
            print(f"Wrote: {p}")
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
