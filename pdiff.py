#!/data/data/com.termux/files/usr/bin/env python
"""Compare two Python files at the AST, bytecode or disassembly level.
Modes select the comparison: ``ast`` dumps both syntax trees, ``dis`` shows the disassembly and the default diffs the compiled bytecode opcodes, so files that differ only in formatting or comments compare equal.
Usage: pdiff FILE1.py FILE2.py [--mode ast|dis|bytecode] Positional arguments: files Files to compare Examples: $ pdiff a.py b.py Command-line script: run ``python pdiff.py`` or the ``pdiff`` command.
The module is importable as well."""

import ast
import contextlib
import difflib
import opcode
import pprint
import sys
import types
from typing import Any, TextIO


def diff_bytecode_of_files(filename_a: Any, filename_b: Any):
    with open_with_encoding(filename_a) as file_a:
        source_a = file_a.read()
    with open_with_encoding(filename_b) as file_b:
        source_b = file_b.read()
    return diff_bytecode(source_a, source_b, filename_a, filename_b)


def diff_bytecode(source_a: Any, source_b: Any, filename_a="", filename_b="") -> str:
    if dump_ast(source_a, filename_a) == dump_ast(source_b, filename_b):
        return ""
    bytecode_a = disassemble(source_a, filename_a)
    bytecode_b = disassemble(source_b, filename_b)
    return "".join(
        difflib.unified_diff(
            pprint.pformat(bytecode_a).splitlines(True),
            pprint.pformat(bytecode_b).splitlines(True),
            filename_a,
            filename_b,
        )
    )


def disassemble(source: str, filename=None):
    return tree(compile(source, filename or "<string>", "exec", dont_inherit=True))


def dump_ast(source: str, filename=None) -> None:
    return ast.dump(ast.parse(source, filename or "<string>", "exec"))


def tree(code: Any) -> dict[str, list[Any]]:
    dictionary = {"co_consts": [], "co_code": []}
    for name in dir(code):
        if name.startswith("co_") and name not in [
            "co_code",
            "co_consts",
            "co_lnotab",
            "co_filename",
            "co_firstlineno",
        ]:
            dictionary[name] = getattr(code, name)
    for index, _object in enumerate(code.co_consts):
        if isinstance(_object, types.CodeType):
            _object = tree(_object)
        if index < 2 and isinstance(_object, str):
            _object = " ".join([line.strip() for line in _object.splitlines()])
        dictionary["co_consts"].append(_object)
    for op in code.co_code:
        with contextlib.suppress(TypeError):
            op = ord(op)
        dictionary["co_code"].append(opcode.opname[op])
    return dictionary


def open_with_encoding(filename: str, encoding=None, mode="r") -> TextIO:
    if not encoding:
        encoding = detect_encoding(filename)
    import io

    return io.open(filename, mode=mode, encoding=encoding, newline="")


def detect_encoding(filename: str):
    try:
        with open(filename, "rb") as input_file:
            import tokenize

            encoding = tokenize.detect_encoding(input_file.readline)[0]
        with open_with_encoding(filename, encoding) as test_file:
            test_file.read()
        return encoding
    except (LookupError, SyntaxError, UnicodeDecodeError):
        return "latin-1"


def parse_args(argv: list[Any]):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, prog="pydiff")
    parser.add_argument("files", nargs=2, help="files to compare")
    args = parser.parse_args(argv[1:])
    return args


def main(argv: list[Any], standard_out: TextIO, standard_error: TextIO) -> None:
    args = parse_args(argv)
    try:
        diff = diff_bytecode_of_files(args.files[0], args.files[1])
        if diff:
            standard_out.write(diff + "\n")
    except SyntaxError as exception:
        standard_error.write("{0}:{1} invalid syntax\n".format(exception.filename, exception.lineno))
        standard_error.write(str(exception.text))
        standard_error.write((exception.offset * " ")[:-1] + "^\n")


if __name__ == "__main__":
    main(sys.argv, sys.stdout, sys.stderr)
