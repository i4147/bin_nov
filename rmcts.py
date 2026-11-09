#!/data/data/com.termux/files/usr/bin/env python
"""Remove comments and docstrings from Python files in place using tree-sitter."""

import argparse
import ast
from collections.abc import Iterator
from multiprocessing import Pool
from pathlib import Path
from typing import TYPE_CHECKING

from dh import MAX_WORKERS, gsz, rrs
from loguru import logger
from tree_sitter import Language, Node, Parser
import tree_sitter_python as tsp


if TYPE_CHECKING:
    from multiprocessing.pool import AsyncResult
PY_EXTS: set[str] = {".py"}
_PARSER: Parser | None = None


def get_parser() -> Parser:
    global _PARSER
    if _PARSER is None:
        language: Language = Language(tsp.language())
        _PARSER = Parser(language)
    return _PARSER


def line_start(content: bytes, offset: int) -> int:
    return content.rfind(b"\n", 0, offset) + 1


def line_end(content: bytes, offset: int) -> int:
    newline: int = content.find(b"\n", offset)
    return len(content) if newline == -1 else newline + 1


def node_line_start(content: bytes, node: Node) -> int:
    return line_start(content, node.start_byte)


def node_line_end(content: bytes, node: Node) -> int:
    return line_end(content, node.end_byte)


def is_keep_comment(comment: bytes, start_byte: int) -> bool:
    if start_byte == 0 and comment.startswith(b"#!"):
        return True
    stripped: bytes = comment.lstrip()
    if not stripped.startswith(b"#"):
        return False
    text: bytes = stripped[1:].lstrip().lower()
    if text.startswith((b"type:", b"fmt:")):
        return True
    return bool(b"coding" in text and b":" in text)


def first_named_child(node: Node) -> Node | None:
    for child in node.children:
        if child.is_named:
            return child
    return None


def is_string_expression(node: Node) -> bool:
    if node.type in {"string", "concatenated_string"}:
        return True
    if node.type != "expression_statement":
        return False
    child: Node | None = first_named_child(node)
    return child is not None and child.type in {"string", "concatenated_string"}


def first_real_statement(container: Node) -> Node | None:
    for child in container.children:
        if child.is_named and child.type != "comment":
            return child
    return None


def is_docstring(node: Node) -> bool:
    if not is_string_expression(node):
        return False
    parent: Node | None = node.parent
    if parent is None:
        return False
    if parent.type == "module":
        return first_real_statement(parent) == node
    if parent.type != "block":
        return False
    owner: Node | None = parent.parent
    if owner is None:
        return False
    if owner.type not in {"function_definition", "class_definition"}:
        return False
    return first_real_statement(parent) == node


def indent_for_block(block: Node, content: bytes) -> bytes:
    for child in block.children:
        if not child.is_named:
            continue
        start: int = line_start(content, child.start_byte)
        indent: bytes = content[start : child.start_byte]
        if not indent.strip():
            return indent
    owner: Node | None = block.parent
    if owner is not None:
        start = line_start(content, owner.start_byte)
        parent_indent: bytes = content[start : owner.start_byte]
        if not parent_indent.strip():
            return parent_indent + b"    "
    return b"    "


def block_named_children(block: Node) -> Iterator[Node]:
    for child in block.children:
        if child.is_named:
            yield child


def collect_actions(root: Node, content: bytes) -> list[tuple[int, int, bytes]]:
    removals: dict[int, tuple[int, int, bytes]] = {}
    blocks: list[Node] = []
    stack: list[Node] = [root]
    while stack:
        node: Node = stack.pop()
        if node.type == "comment":
            raw_comment: bytes = content[node.start_byte : node.end_byte]
            if not is_keep_comment(raw_comment, node.start_byte):
                start: int = node.start_byte
                end: int = node.end_byte
                before: bytes = content[line_start(content, start) : start]
                after: bytes = content[end : line_end(content, end)]
                if not before.strip() and not after.strip():
                    start = line_start(content, start)
                    end = line_end(content, end)
                removals[node.id] = (start, end, b"")
        elif is_docstring(node):
            start = node_line_start(content, node)
            end = node_line_end(content, node)
            removals[node.id] = (start, end, b"")
        if node.type == "block":
            blocks.append(node)
        children: list[Node] = node.children
        for child in reversed(children):
            stack.append(child)
    for block in blocks:
        first: Node | None = None
        last: Node | None = None
        all_removed: bool = True
        for child in block_named_children(block):
            action: tuple[int, int, bytes] | None = removals.get(child.id)
            if action is None:
                all_removed = False
                break
            if first is None:
                first = child
            last = child
        if not all_removed or first is None or last is None:
            continue
        first_action: tuple[int, int, bytes] = removals[first.id]
        last_action: tuple[int, int, bytes] = removals[last.id]
        replacement_start: int = line_start(content, first_action[0])
        replacement_end: int = last_action[1]
        indent: bytes = indent_for_block(block, content)
        removals[first.id] = (
            replacement_start,
            replacement_end,
            indent + b"pass\n",
        )
        for child in block_named_children(block):
            if child.id != first.id:
                removals.pop(child.id, None)
    actions: list[tuple[int, int, bytes]] = list(removals.values())
    actions.sort(key=lambda action: (action[0], action[1]))
    filtered: list[tuple[int, int, bytes]] = []
    previous_end: int = -1
    for action in actions:
        start, end, replacement = action
        if start < previous_end:
            continue
        filtered.append((start, end, replacement))
        previous_end = end
    return filtered


def apply_actions(content: bytes, actions: list[tuple[int, int, bytes]]) -> bytes:
    output: bytearray = bytearray()
    last_end: int = 0
    for start, end, replacement in actions:
        output.extend(content[last_end:start])
        output.extend(replacement)
        last_end = end
    output.extend(content[last_end:])
    return bytes(output)


def strip_comments_and_docstrings(content: bytes) -> tuple[bytes, int]:
    parser: Parser = get_parser()
    tree = parser.parse(content)
    actions: list[tuple[int, int, bytes]] = collect_actions(tree.root_node, content)
    if not actions:
        return content, 0
    new_content: bytes = apply_actions(content, actions)
    try:
        ast.parse(new_content)
    except SyntaxError as exc:
        msg = f"Generated invalid Python source: {exc}"
        raise ValueError(msg) from exc
    return new_content, len(actions)


def process_file(path: Path, base: Path) -> tuple[str, int, str]:
    try:
        content: bytes = path.read_bytes()
        new_content, removed_count = strip_comments_and_docstrings(content)
        if new_content != content:
            path.write_bytes(new_content)
        try:
            relative_path: str = str(path.relative_to(base))
        except ValueError:
            relative_path = str(path)
        return relative_path, removed_count, ""
    except Exception as exc:
        return str(path), 0, str(exc)


def iter_py_files(paths: list[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for path in paths:
        if path.is_file():
            if path.suffix.lower() not in PY_EXTS:
                continue
            resolved: Path = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path
            continue
        if not path.is_dir():
            continue
        try:
            for path in path.rglob("*.py"):
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield path
        except OSError as exc:
            logger.error(f"{path}: ERROR walking directory: {exc}")


def _walker(root_dir: Path) -> Iterator[Path]:
    for r, _, files in root_dir.walk():
        rp: Path = Path(r)
        for f in files:
            path: Path = rp / f
            if path.is_symlink() or ".git" in path.parts:
                continue
            if path.is_file() and path.suffix == ".py":
                yield path


def main() -> int:
    cwd: Path = Path.cwd()
    before: int = gsz(cwd)
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Remove Python comments and docstrings in-place."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories. Defaults to the current directory recursively.",
    )
    args: argparse.Namespace = parser.parse_args()
    inputs: list[Path] = args.paths or [Path()]
    base: Path = Path.cwd()
    file_iterator: Iterator[Path] = iter_py_files(inputs)
    total_files: int = 0
    changed_files: int = 0
    total_removed: int = 0
    errors: int = 0
    with Pool(processes=MAX_WORKERS) as pool:
        pending: dict[AsyncResult[tuple[str, int, str]], Path] = {}
        exhausted: bool = False

        def submit_until_full() -> bool:
            nonlocal exhausted
            while len(pending) < MAX_WORKERS:
                try:
                    path: Path = next(file_iterator)
                except StopIteration:
                    exhausted = True
                    break
                result: AsyncResult[tuple[str, int, str]] = pool.apply_async(process_file, (path, base))
                pending[result] = path
            return exhausted

        exhausted = submit_until_full()
        while pending:
            result = next(iter(pending))
            path = pending.pop(result)
            total_files += 1
            try:
                relative_path, count, error = result.get()
            except Exception as exc:
                errors += 1
                logger.error(f"{path}: ERROR: {exc}")
                if not exhausted:
                    exhausted = submit_until_full()
                continue
            if error:
                errors += 1
                logger.error(f"{relative_path}: ERROR: {error}")
            else:
                total_removed += count
                if count:
                    changed_files += 1
                    print(f"{relative_path}: {count} comment(s)/docstring(s) removed")
            if not exhausted:
                exhausted = submit_until_full()
    print(
        f"Summary: {changed_files}/{total_files} file(s) changed, "
        f"{total_removed} comment(s)/docstring(s) removed, "
        f"{errors} error(s)."
    )
    after: int = gsz(cwd)
    print("_" * 40)
    rrs(cwd, before, after)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
