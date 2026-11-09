#!/data/data/com.termux/files/usr/bin/env python
"""Normalise and reflow text files.
Subcommands: ``clean`` removes the zero-width and exotic space characters, ``normalize-lines`` trims and normalises each line, ``norm-white`` collapses the whitespace and ``reformat-text``/``reformatext`` rewraps the paragraphs to the configured maximum line length, splitting the long sentences at punctuation.
Usage: texttools {clean,normalize-lines,norm-white,reformat-text,reformatext} FILE Positional arguments: filename Subcommands: clean normalize-lines norm-white reformat-text reformatext Examples: $ texttools reformat-text notes.txt Command-line script: run ``python texttools.py`` or the ``texttools`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re
import sys
import tempfile
from typing import Final, Sequence
import unicodedata


MAX_LEN: Final[int] = 120
BREAK_PUNCTS: Final[list[str]] = [",", ";", ":", "?"]
WS_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\u00A0\u2000-\u200F\u2028\u2029\u202F\u205F\u3000\uFEFF]")
ZW_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\u200B-\u200D\uFEFF]")
SENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^.!]+[.!]", re.MULTILINE | re.DOTALL)
PARA_SPLIT: Final[re.Pattern[str]] = re.compile(r"\n\s*\n", re.MULTILINE)
SENT_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\?|!)\s+")


def _clean_file(filename: Path) -> None:
    try:
        text = filename.read_text(encoding="utf-8")
        cleaned = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\n\r\t")
        filename.write_text(cleaned, encoding="utf-8")
        print(f"Successfully cleaned: {filename}")
    except FileNotFoundError:
        print(f"Error: The file '{filename}' was not found.")
    except Exception as e:
        print(f"An error occurred: {e}")


def cmd_clean(args: argparse.Namespace) -> int:
    _clean_file(Path(args.filename))
    return 0


def _normalize_line(line: str) -> str:
    text = unicodedata.normalize("NFKC", line)
    text = "".join(ch for ch in text if ch.isprintable())
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("P"))
    text = text.casefold()
    text = "_".join(text.split())
    return text


def _normalize_file_in_place(path: Path) -> None:
    with (
        path.open("r", encoding="utf-8", newline="") as source,
        tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary,
    ):
        temporary_path = Path(temporary.name)
        for line in source:
            content = line.rstrip("\n")
            temporary.write(_normalize_line(content) + "\n")
    temporary_path.replace(path)


def cmd_normalize_lines(args: argparse.Namespace) -> int:
    input_path = Path(args.filename)
    if not input_path.is_file():
        print(f"Error: not a file: {input_path}", file=sys.stderr)
        return 1
    try:
        _normalize_file_in_place(input_path)
    except UnicodeDecodeError:
        print(f"Error: {input_path} is not valid UTF-8.", file=sys.stderr)
        return 1
    return 0


def _normalize_white_space(input_path: Path) -> None:
    text = input_path.read_text(encoding="utf-8", errors="ignore")
    cleaned = WS_PATTERN.sub(" ", text)
    cleaned = ZW_PATTERN.sub("", cleaned)
    input_path.write_text(cleaned, encoding="utf-8")


def cmd_norm_white(args: argparse.Namespace) -> int:
    path = Path(args.filename)
    if not path.is_file():
        print(f"Error: file '{path}' does not exist.", file=sys.stderr)
        return 1
    _normalize_white_space(path)
    return 0


def _split_sentences(text: str) -> list[str]:
    sentences = SENT_PATTERN.findall(text)
    return [s.strip() for s in sentences if s.strip()]


def _break_long_sentence(sentence: str, max_len: int = MAX_LEN) -> list[str]:
    parts: list[str] = []
    while len(sentence) > max_len:
        break_pos = -1
        window = sentence[:max_len]
        for p in BREAK_PUNCTS:
            pos = window.rfind(p)
            break_pos = max(break_pos, pos)
        if break_pos < 0:
            break_pos = max_len
        parts.append(sentence[: break_pos + 1].strip())
        sentence = sentence[break_pos + 1 :].strip()
    if sentence:
        parts.append(sentence.strip())
    return parts


def _restructure_paragraph(paragraph: str) -> str:
    sentences = _split_sentences(paragraph)
    lines: list[str] = []
    for s in sentences:
        lines.extend(_break_long_sentence(s, MAX_LEN))
    return "\n".join(lines)


def _restructure_file(path: Path) -> None:
    backup = path.with_suffix(path.suffix + ".bak")
    text = path.read_text(encoding="utf-8", errors="ignore")
    backup.write_text(text, encoding="utf-8")
    paragraphs = PARA_SPLIT.split(text.strip())
    new_paragraphs = [_restructure_paragraph(p) for p in paragraphs]
    new_text = "\n\n".join(new_paragraphs) + "\n"
    path.write_text(new_text, encoding="utf-8")


def cmd_reformat_text(args: argparse.Namespace) -> int:
    file_arg = Path(args.filename)
    if not file_arg.exists():
        print(f"Error: file '{file_arg}' does not exist.")
        return 1
    _restructure_file(file_arg)
    return 0


def _restructure_text_file(path: Path) -> None:
    if not path.is_file():
        print(f"Error: File not found at {path}")
        return
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error reading file {path}: {e}")
        return
    bak_path = path.with_suffix(path.suffix + ".bak")
    try:
        bak_path.write_text(content, encoding="utf-8")
        print(f"Backup created at: {bak_path}")
    except Exception as e:
        print(f"Error creating backup file {bak_path}: {e}")
        return
    restructured_lines: list[str] = []
    paragraphs = content.split("\n\n")
    for paragraph in paragraphs:
        if not paragraph.strip():
            restructured_lines.append("")
            continue
        sentences = SENT_SPLIT.split(paragraph)
        for sentence in sentences:
            if not sentence.strip():
                continue
            processed_sentence_parts: list[str] = []
            current_line_length = 0
            current_line_words: list[str] = []
            words = sentence.split()
            for word in words:
                potential_line_length = current_line_length + len(word) + (1 if current_line_words else 0)
                if potential_line_length > MAX_LEN and current_line_length > 0:
                    break_point = -1
                    for i, w in enumerate(current_line_words):
                        if w.endswith(","):
                            break_point = i
                    if break_point != -1:
                        processed_sentence_parts.append(" ".join(current_line_words[: break_point + 1]))
                        current_line_words = current_line_words[break_point + 1 :]
                        current_line_length = len(" ".join(current_line_words))
                    else:
                        processed_sentence_parts.append(" ".join(current_line_words))
                        current_line_words = [word]
                        current_line_length = len(word)
                else:
                    current_line_words.append(word)
                    current_line_length = potential_line_length
            if current_line_words:
                processed_sentence_parts.append(" ".join(current_line_words))
            restructured_lines.extend(processed_sentence_parts)
    try:
        path.write_text("\n".join(restructured_lines), encoding="utf-8")
        print(f"File successfully restructured: {path}")
    except Exception as e:
        print(f"Error writing to file {path}: {e}")


def cmd_reformatext(args: argparse.Namespace) -> int:
    path = Path(args.filename)
    _restructure_text_file(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="texttools")
    sub = parser.add_subparsers(dest="command", required=True)
    p_clean = sub.add_parser("clean")
    p_clean.add_argument("filename")
    p_clean.set_defaults(func=cmd_clean)
    p_norm = sub.add_parser("normalize-lines")
    p_norm.add_argument("filename")
    p_norm.set_defaults(func=cmd_normalize_lines)
    p_ws = sub.add_parser("norm-white")
    p_ws.add_argument("filename")
    p_ws.set_defaults(func=cmd_norm_white)
    p_ref = sub.add_parser("reformat-text")
    p_ref.add_argument("filename")
    p_ref.set_defaults(func=cmd_reformat_text)
    p_rex = sub.add_parser("reformatext")
    p_rex.add_argument("filename")
    p_rex.set_defaults(func=cmd_reformatext)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
