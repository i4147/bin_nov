#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments and docstrings from Python files in place, with backup/restore support."""


import argparse
import ast
import contextlib
import functools
import io
import multiprocessing as mp
from pathlib import Path
import sys
import tokenize as _tokenize

from atomicio import atomic_replace
from dh import DOC_TH1, DOC_TH2, SKIP_DIRS
import libcst as cst


POOL_PROCESSES = 8
CHUNK_SIZE = 4
PY_SUFFIXES = (".py", ".pyi")
PROTECTED_COMMENT_PREFIXES = (
    "#!",
    "#-*-",
    "# coding",
    "# fmt",
    "# type",
    "# noqa",
    "# pylint",
    "# ruff",
    "# isort",
    "# mypy",
    "# pyright",
    "# pragma",
)
GREEN = "\x1b[32m"
RESET = "\x1b[0m"
BACKUP_SUFFIX = ".pystripbak"


def has_no_strippable_content(source: str) -> bool:
    return "#" not in source and DOC_TH1 not in source and DOC_TH2 not in source


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    for suffix, threshold in (("M", 1024**3), ("k", 1024)):
        if num_bytes >= threshold:
            scaled = num_bytes / threshold
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{num_bytes} B"


def is_docstring_literal(expr: cst.BaseExpression) -> bool:
    if isinstance(expr, cst.SimpleString):
        return "b" not in expr.prefix.lower()
    if isinstance(expr, cst.ConcatenatedString):
        return is_docstring_literal(expr.left) and is_docstring_literal(expr.right)
    return False


def starts_with_docstring(stmt_line: cst.SimpleStatementLine) -> bool:
    if not stmt_line.body:
        return False
    first = stmt_line.body[0]
    return isinstance(first, cst.Expr) and is_docstring_literal(first.value)


def has_trailing_comment(node: cst.CSTNode) -> bool:
    trailing_ws = getattr(node, "trailing_whitespace", None)
    return trailing_ws is not None and getattr(trailing_ws, "comment", None) is not None


def strip_indented_block_docstring(
    block: cst.IndentedBlock,
) -> tuple[cst.IndentedBlock, bool]:
    if not block.body:
        return block, False
    first = block.body[0]
    if not isinstance(first, cst.SimpleStatementLine):
        return block, False
    if not starts_with_docstring(first):
        return block, False
    remaining_in_line = list(first.body[1:])
    rest_of_block = list(block.body[1:])
    if remaining_in_line:
        new_line = first.with_changes(body=remaining_in_line)
        return block.with_changes(body=[new_line, *rest_of_block]), True
    if not rest_of_block or has_trailing_comment(first):
        new_line = first.with_changes(body=[cst.Pass()])
        return block.with_changes(body=[new_line, *rest_of_block]), True
    if first.leading_lines:
        next_stmt = rest_of_block[0]
        existing_leading = list(getattr(next_stmt, "leading_lines", ()) or ())
        merged_leading = [*first.leading_lines, *existing_leading]
        with contextlib.suppress(AttributeError):
            rest_of_block[0] = next_stmt.with_changes(leading_lines=merged_leading)
    return block.with_changes(body=rest_of_block), True


def strip_simple_suite_docstring(
    suite: cst.SimpleStatementSuite,
) -> tuple[cst.SimpleStatementSuite, bool]:
    if not suite.body:
        return suite, False
    first = suite.body[0]
    if not (isinstance(first, cst.Expr) and is_docstring_literal(first.value)):
        return suite, False
    remaining = list(suite.body[1:])
    if not remaining:
        remaining = [cst.Pass()]
    return suite.with_changes(body=remaining), True


def strip_suite_docstring(suite: cst.BaseSuite) -> tuple[cst.BaseSuite, bool]:
    if isinstance(suite, cst.IndentedBlock):
        return strip_indented_block_docstring(suite)
    if isinstance(suite, cst.SimpleStatementSuite):
        return strip_simple_suite_docstring(suite)
    return suite, False


def strip_module_docstring(module: cst.Module) -> cst.Module:
    if not module.body:
        return module
    first = module.body[0]
    if not isinstance(first, cst.SimpleStatementLine):
        return module
    if not starts_with_docstring(first):
        return module
    remaining_in_line = list(first.body[1:])
    rest_of_module = list(module.body[1:])
    if remaining_in_line:
        new_line = first.with_changes(body=remaining_in_line)
        return module.with_changes(body=[new_line, *rest_of_module])
    if first.leading_lines and rest_of_module:
        next_stmt = rest_of_module[0]
        existing_leading = list(getattr(next_stmt, "leading_lines", ()) or ())
        merged_leading = [*first.leading_lines, *existing_leading]
        with contextlib.suppress(AttributeError):
            rest_of_module[0] = next_stmt.with_changes(leading_lines=merged_leading)
    elif first.leading_lines and not rest_of_module:
        header = list(module.header)
        header.extend(first.leading_lines)
        return module.with_changes(body=[], header=header)
    return module.with_changes(body=rest_of_module)


def _dehash(comment_text: str) -> str:
    text = comment_text[1:]
    text = text.removeprefix(" ")
    return text


def looks_like_commented_out_code(comment_lines: list[str]) -> bool:
    dehashed = "\n".join(_dehash(line) for line in comment_lines)
    if not dehashed.strip():
        return False
    try:
        ast.parse(dehashed)
    except (SyntaxError, ValueError):
        return False
    return True


def find_commented_out_code_lines(source: str) -> set[int]:
    try:
        tokens = list(_tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, _tokenize.TokenError, SyntaxError):
        return set()
    comment_tokens = [tok for tok in tokens if tok.type == _tokenize.COMMENT]
    if not comment_tokens:
        return set()
    protected_lines: set[int] = set()
    group_lines: list[int] = []
    group_texts: list[str] = []
    prev_line = None

    def flush_group() -> None:
        if group_texts and looks_like_commented_out_code(group_texts):
            protected_lines.update(group_lines)

    for tok in comment_tokens:
        line_no = tok.start[0]
        if prev_line is not None and line_no != prev_line + 1:
            flush_group()
            group_lines.clear()
            group_texts.clear()
        group_lines.append(line_no)
        group_texts.append(tok.string)
        prev_line = line_no
    flush_group()
    return protected_lines


class CommentDocstringStripper(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (cst.metadata.PositionProvider,)

    def __init__(
        self,
        *,
        remove_all,
        remove_all_comments,
        remove_docstrings,
        remove_type_annotations,
        protected_code_lines,
    ):
        super().__init__()
        self.remove_all = remove_all
        self.remove_all_comments = remove_all_comments or remove_all
        self.remove_docstrings = remove_docstrings or remove_all
        self.remove_type_annotations = remove_type_annotations
        self.protected_code_lines = protected_code_lines

    @staticmethod
    def _is_protected_comment(comment_text: str) -> bool:
        return comment_text.startswith(PROTECTED_COMMENT_PREFIXES)

    def _is_on_protected_line(self, node: cst.CSTNode) -> bool:
        pos = self.get_metadata(cst.metadata.PositionProvider, node, None)
        if pos is None:
            return False
        return pos.start.line in self.protected_code_lines

    def _should_remove_comment(self, comment_node: cst.Comment) -> bool:
        if self._is_on_protected_line(comment_node):
            return False
        if self.remove_all:
            return True
        return not self._is_protected_comment(comment_node.value)

    def leave_TrailingWhitespace(self, original_node, updated_node):
        del original_node
        if updated_node.comment is None:
            return updated_node
        if not self._should_remove_comment(updated_node.comment):
            return updated_node
        return updated_node.with_changes(whitespace=cst.SimpleWhitespace(""), comment=None)

    def leave_EmptyLine(self, original_node, updated_node):
        del original_node
        if not self.remove_all_comments:
            return updated_node
        if updated_node.comment is None:
            return updated_node
        if not self._should_remove_comment(updated_node.comment):
            return updated_node
        return updated_node.with_changes(comment=None)

    def leave_FunctionDef(self, original_node, updated_node):
        del original_node
        if self.remove_docstrings:
            new_body, changed = strip_suite_docstring(updated_node.body)
            if changed:
                updated_node = updated_node.with_changes(body=new_body)
        if self.remove_type_annotations:
            updated_node = updated_node.with_changes(returns=None, type_comment=None)
        return updated_node

    def leave_ClassDef(self, original_node, updated_node):
        del original_node
        if not self.remove_docstrings:
            return updated_node
        new_body, changed = strip_suite_docstring(updated_node.body)
        if changed:
            return updated_node.with_changes(body=new_body)
        return updated_node

    def leave_Param(self, original_node, updated_node):
        del original_node
        if not self.remove_type_annotations:
            return updated_node
        if updated_node.annotation is None and updated_node.type_comment is None:
            return updated_node
        return updated_node.with_changes(annotation=None, type_comment=None)

    def leave_AnnAssign(self, original_node, updated_node):
        del original_node
        if not self.remove_type_annotations:
            return updated_node
        if updated_node.value is None:
            return cst.Pass()
        return cst.Assign(
            targets=[cst.AssignTarget(target=updated_node.target)],
            value=updated_node.value,
        )

    def leave_Assign(self, original_node, updated_node):
        del original_node
        if not self.remove_type_annotations:
            return updated_node
        type_comment = getattr(updated_node, "type_comment", None)
        if type_comment is None:
            return updated_node
        return updated_node.with_changes(type_comment=None)

    def leave_For(self, original_node, updated_node):
        del original_node
        if not self.remove_type_annotations:
            return updated_node
        type_comment = getattr(updated_node, "type_comment", None)
        if type_comment is None:
            return updated_node
        return updated_node.with_changes(type_comment=None)

    def leave_With(self, original_node, updated_node):
        del original_node
        if not self.remove_type_annotations:
            return updated_node
        type_comment = getattr(updated_node, "type_comment", None)
        if type_comment is None:
            return updated_node
        return updated_node.with_changes(type_comment=None)


def run_transform(module: cst.Module, transformer: CommentDocstringStripper) -> cst.Module:
    wrapper = cst.metadata.MetadataWrapper(module)
    return wrapper.visit(transformer)


def string_token_lines(source: str) -> set[int]:
    occupied = set()
    try:
        tokens = _tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type != _tokenize.STRING:
                continue
            start_line = tok.start[0]
            end_line = tok.end[0]
            occupied.update(range(start_line, end_line + 1))
    except (IndentationError, _tokenize.TokenError):
        return set()
    return occupied


def collapse_blank_lines(source: str) -> str:
    lines = source.splitlines(keepends=True)
    string_lines = string_token_lines(source)
    result = []
    prev_was_blank = False
    for line_no, line in enumerate(lines, start=1):
        is_blank = not line.strip()
        if is_blank and line_no not in string_lines:
            if prev_was_blank:
                continue
            newline = "\n"
            if line.endswith("\r\n"):
                newline = "\r\n"
            elif line.endswith("\r"):
                newline = "\r"
            result.append(newline)
            prev_was_blank = True
            continue
        result.append(line)
        prev_was_blank = False
    return "".join(result)


def backup_path_for(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def write_backup(path: Path, original_bytes: bytes) -> None:
    atomic_replace(backup_path_for(path), original_bytes)


def restore_from_backup(path: Path) -> tuple[Path, bool, str | None]:
    backup = backup_path_for(path)
    if not backup.exists():
        return path, False, None
    try:
        original_bytes = backup.read_bytes()
    except OSError as exc:
        return path, False, f"backup read error: {exc}"
    try:
        atomic_replace(path, original_bytes)
    except OSError as exc:
        return path, False, f"restore write error: {exc}"
    with contextlib.suppress(OSError):
        backup.unlink()
    return path, True, None


def process_file(
    path: Path,
    *,
    remove_all,
    remove_all_comments,
    remove_docstrings,
    remove_type_annotations,
    make_backup,
):
    try:
        original_bytes = path.read_bytes()
    except OSError as exc:
        return path, 0, False, f"read error: {exc}"
    try:
        encoding, _ = _tokenize.detect_encoding(io.BytesIO(original_bytes).readline)
        source = original_bytes.decode(encoding)
    except (SyntaxError, UnicodeDecodeError) as exc:
        return path, 0, False, f"encoding error: {exc}"
    if has_no_strippable_content(source):
        return path, 0, False, None
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return path, 0, False, f"LibCST parse error: {exc}"
    except Exception as exc:
        return path, 0, False, f"parse error: {type(exc).__name__}: {exc}"
    protected_code_lines = find_commented_out_code_lines(source)
    transformer = CommentDocstringStripper(
        remove_all=remove_all,
        remove_all_comments=remove_all_comments,
        remove_docstrings=remove_docstrings,
        remove_type_annotations=remove_type_annotations,
        protected_code_lines=protected_code_lines,
    )
    try:
        new_module = run_transform(module, transformer)
        if remove_all:
            new_module = strip_module_docstring(new_module)
    except Exception as exc:
        return path, 0, False, f"transform error: {type(exc).__name__}: {exc}"
    new_source = new_module.code
    new_source = collapse_blank_lines(new_source)
    new_bytes = new_source.encode(encoding)
    changed = new_bytes != original_bytes
    if not changed:
        return path, 0, False, None
    try:
        ast.parse(new_source, filename=str(path))
    except SyntaxError as exc:
        return path, 0, False, f"post-transform validation failed: {exc}"
    if make_backup:
        try:
            write_backup(path, original_bytes)
        except OSError as exc:
            return path, 0, False, f"backup write error: {exc}"
    try:
        atomic_replace(path, new_bytes)
    except (OSError, UnicodeEncodeError) as exc:
        return path, 0, False, f"write error: {exc}"
    bytes_reduced = len(original_bytes) - len(new_bytes)
    return path, bytes_reduced, True, None


def iter_python_files(paths):
    seen = set()

    def on_walk_error(exc):
        print(f"warning: {exc}", file=sys.stderr)

    for given_path in paths:
        try:
            if given_path.is_file():
                if given_path.suffix in PY_SUFFIXES:
                    resolved = given_path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield given_path
            elif given_path.is_dir():
                for dirpath, dirnames, filenames in given_path.walk(on_error=on_walk_error):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for filename in filenames:
                        if not filename.endswith(PY_SUFFIXES):
                            continue
                        candidate = dirpath / filename
                        try:
                            resolved = candidate.resolve()
                        except OSError:
                            continue
                        if resolved in seen:
                            continue
                        seen.add(resolved)
                        yield candidate
            else:
                print(
                    f"warning: skipping non-existent path: {given_path}",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(f"warning: cannot access {given_path}: {exc}", file=sys.stderr)


def iter_backup_files(paths):
    seen = set()

    def on_walk_error(exc):
        print(f"warning: {exc}", file=sys.stderr)

    for given_path in paths:
        try:
            if given_path.is_file():
                if given_path.name.endswith(BACKUP_SUFFIX):
                    original = given_path.with_name(given_path.name[: -len(BACKUP_SUFFIX)])
                    resolved = original.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield original
            elif given_path.is_dir():
                for dirpath, dirnames, filenames in given_path.walk(on_error=on_walk_error):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for filename in filenames:
                        if not filename.endswith(BACKUP_SUFFIX):
                            continue
                        original = dirpath / filename[: -len(BACKUP_SUFFIX)]
                        try:
                            resolved = original.resolve()
                        except OSError:
                            continue
                        if resolved in seen:
                            continue
                        seen.add(resolved)
                        yield original
            else:
                print(
                    f"warning: skipping non-existent path: {given_path}",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(f"warning: cannot access {given_path}: {exc}", file=sys.stderr)


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="strip_comments",
        description=(
            "Safely remove comments, docstrings, type annotations, and repeated "
            "blank lines from Python files using LibCST."
        ),
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help=(
            "Strip everything: all comments (including protected ones), all "
            "docstrings (including the module docstring), and repeated blank "
            "lines. Combine with -t to also strip type annotations. Does NOT "
            "override commented-out-code protection."
        ),
    )
    parser.add_argument(
        "-c",
        "--remove-all-comments",
        action="store_true",
        help=(
            "Remove standalone comments in addition to inline comments. "
            "Protected comments are preserved (unlike --all)."
        ),
    )
    parser.add_argument(
        "-d",
        "--remove-docstrings",
        action="store_true",
        help="Remove function and class docstrings. The module docstring is preserved (unlike --all).",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="remove_type_annotations",
        action="store_true",
        help=(
            "Remove function parameter annotations, return annotations, "
            "variable annotations, and supported type comments."
        ),
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help=(
            "Write a sidecar backup file before stripping each file. Without "
            "this flag (the default), no backup is written and --reverse has "
            "nothing to restore from."
        ),
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help=(
            "Restore files from their sidecar backup files instead of "
            "stripping. Only works for files that were stripped previously "
            "with backups enabled (the default)."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        metavar="PATH",
        help="Python files or directories to process. Defaults to the current directory.",
    )
    return parser


def run_reverse(paths):
    targets = list(iter_backup_files(paths))
    if not targets:
        print("No backup files found to restore.", file=sys.stderr)
        return 1
    restored_count = 0
    error_count = 0
    for path in targets:
        _, restored, error = restore_from_backup(path)
        if error is not None:
            error_count += 1
            print(f"{path.name}: {error}", file=sys.stderr)
        elif restored:
            restored_count += 1
            print(f"{path.name}  restored")
    summary = f"\nRestored {restored_count} file(s), {error_count} error(s)."
    print(summary, file=sys.stderr if error_count else sys.stdout)
    return 2 if error_count else 0


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    paths = args.paths or [Path.cwd()]
    if args.reverse:
        return run_reverse(paths)
    remove_all = args.all
    remove_all_comments = args.all or args.remove_all_comments
    remove_docstrings = args.all or args.remove_docstrings
    process_fn = functools.partial(
        process_file,
        remove_all=remove_all,
        remove_all_comments=remove_all_comments,
        remove_docstrings=remove_docstrings,
        remove_type_annotations=args.remove_type_annotations,
        make_backup=args.backup,
    )
    total_count = 0
    changed_count = 0
    bytes_reduced_total = 0
    error_count = 0
    with mp.Pool(processes=POOL_PROCESSES) as pool:
        results = pool.imap_unordered(process_fn, iter_python_files(paths), chunksize=CHUNK_SIZE)
        for path, bytes_reduced, changed, error in results:
            total_count += 1
            if error is not None:
                error_count += 1
                print(f"{path.name}: {error}", file=sys.stderr)
                continue
            if not changed:
                continue
            changed_count += 1
            bytes_reduced_total += bytes_reduced
            print(f"{path.name}   {GREEN}{format_size(bytes_reduced)}{RESET}")
    if total_count == 0:
        print("No Python files found.", file=sys.stderr)
        return 1
    summary = (
        f"\nProcessed {total_count} file(s): {changed_count} changed, "
        f"{GREEN}{format_size(bytes_reduced_total)}{RESET} reduced, {error_count} error(s)."
    )
    print(summary, file=sys.stderr if error_count else sys.stdout)
    return 2 if error_count else 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
