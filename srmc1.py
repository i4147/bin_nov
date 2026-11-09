#!/data/data/com.termux/files/usr/bin/env python
"""Strip selected Python comments, docstrings, and annotations in place."""


import argparse
import ast
from dataclasses import asdict, dataclass
import difflib
import json
import multiprocessing
from pathlib import Path
import re
import stat
import sys
import textwrap
from typing import ClassVar, Iterable, Iterator, Literal, Sequence

from dh import MAX_WORKERS, atomic_write
import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider
from loguru import logger


VERSION = "1.0.0"
UTF8_BOM = b"\xef\xbb\xbf"
CODING_PATTERN = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")
FULL_COMMENT_PATTERN = re.compile(r"^([ \t\f]*)#(.*)$")
COMMENT_DIRECTIVE_PATTERN = re.compile(r"^\s*#\s*(?:type|fmt)\s*:", re.IGNORECASE)
Status = Literal["modified", "unchanged", "would_modify", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class TransformOptions:
    remove_all_comments: bool
    remove_protected_comments: bool
    remove_docstrings: bool
    remove_module_docstring: bool
    remove_annotations: bool
    dry_run: bool
    check: bool
    include_diff: bool


@dataclass(frozen=True, slots=True)
class WorkItem:
    path: Path
    backup_path: Path | None
    options: TransformOptions


@dataclass(slots=True)
class FileResult:
    path: str
    status: Status
    original_bytes: int = 0
    output_bytes: int = 0
    bytes_saved: int = 0
    comments_removed: int = 0
    docstrings_removed: int = 0
    annotations_removed: int = 0
    message: str = ""
    diff: str = ""
    backup_path: str | None = None


@dataclass(frozen=True, slots=True)
class DecodedSource:
    text: str
    encoding: str
    bom: bytes


class UnsupportedEncodingError(Exception):
    pass


def decode_source(data: bytes) -> DecodedSource:
    if data.startswith(UTF8_BOM):
        try:
            return DecodedSource(data[len(UTF8_BOM) :].decode("utf-8"), "utf-8", UTF8_BOM)
        except UnicodeDecodeError:
            pass
    else:
        try:
            return DecodedSource(data.decode("utf-8"), "utf-8", b"")
        except UnicodeDecodeError:
            pass
    try:
        return DecodedSource(data.decode("cp1252"), "cp1252", b"")
    except UnicodeDecodeError as error:
        raise UnsupportedEncodingError(str(error)) from error


def encode_source(source: DecodedSource, text: str) -> bytes:
    return source.bom + text.encode(source.encoding)


def strip_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1:]
    return line, ""


def commented_code_lines(source: str) -> frozenset[int]:
    protected: set[int] = set()
    run: list[tuple[int, str]] = []

    def is_valid(candidate: str) -> bool:
        if not candidate.strip():
            return False
        try:
            ast.parse(textwrap.dedent(candidate), mode="exec", feature_version=(3, 12))
        except (SyntaxError, ValueError, TypeError):
            return False
        return True

    def finish_run() -> None:
        if not run:
            return
        candidate = "".join(line for _, line in run)
        if is_valid(candidate):
            protected.update(number for number, _ in run)
        else:
            for start in range(len(run)):
                candidate_lines: list[str] = []
                for end in range(start, len(run)):
                    candidate_lines.append(run[end][1])
                    if is_valid("".join(candidate_lines)):
                        protected.update(number for number, _ in run[start : end + 1])
        run.clear()

    for number, raw_line in enumerate(source.splitlines(keepends=True), start=1):
        line, ending = strip_line_ending(raw_line)
        match = FULL_COMMENT_PATTERN.match(line)
        if match is None:
            finish_run()
            continue
        payload = match.group(2)
        if payload.startswith((" ", "\t")):
            payload = payload[1:]
        run.append((number, match.group(1) + payload + ending))
    finish_run()
    return frozenset(protected)


def is_protected_comment(comment: str, line_number: int, remove_everything: bool, full_line: bool) -> bool:
    if remove_everything:
        return False
    if full_line and line_number == 1 and comment.startswith("#!"):
        return True
    if full_line and line_number <= 2 and CODING_PATTERN.match(comment):
        return True
    return COMMENT_DIRECTIVE_PATTERN.match(comment) is not None


def is_string_expression(value: cst.BaseExpression) -> bool:
    if isinstance(value, cst.SimpleString):
        try:
            return isinstance(ast.literal_eval(value.value), str)
        except (SyntaxError, ValueError):
            return False
    if isinstance(value, cst.ConcatenatedString):
        return is_string_expression(value.left) and is_string_expression(value.right)
    return False


def is_docstring_statement(statement: cst.BaseStatement) -> bool:
    if not isinstance(statement, cst.SimpleStatementLine):
        return False
    if len(statement.body) != 1:
        return False
    only_statement = statement.body[0]
    return isinstance(only_statement, cst.Expr) and is_string_expression(only_statement.value)


def is_docstring_small_statement(statement: cst.BaseSmallStatement) -> bool:
    return isinstance(statement, cst.Expr) and is_string_expression(statement.value)


def line_with_pass(statement: cst.SimpleStatementLine) -> cst.SimpleStatementLine:
    return statement.with_changes(body=(cst.Pass(),))


def prepend_leading_lines(removed: cst.SimpleStatementLine, replacement: cst.BaseStatement) -> cst.BaseStatement:
    leading_lines = removed.leading_lines
    if not leading_lines:
        return replacement
    current_lines = getattr(replacement, "leading_lines", ())
    return replacement.with_changes(leading_lines=leading_lines + current_lines)


def remove_docstring_from_suite(suite: cst.BaseSuite) -> tuple[cst.BaseSuite, bool]:
    if isinstance(suite, cst.IndentedBlock):
        body = tuple(suite.body)
        if not body or not is_docstring_statement(body[0]):
            return suite, False
        first = body[0]
        if len(body) == 1:
            return suite.with_changes(body=(line_with_pass(first),)), True
        next_statement = prepend_leading_lines(first, body[1])
        return suite.with_changes(body=(next_statement,) + body[2:]), True
    if isinstance(suite, cst.SimpleStatementSuite):
        body = tuple(suite.body)
        if not body or not is_docstring_small_statement(body[0]):
            return suite, False
        if len(body) == 1:
            return suite.with_changes(body=(cst.Pass(),)), True
        return suite.with_changes(body=body[1:]), True
    return suite, False


def remove_module_docstring(module: cst.Module) -> tuple[cst.Module, bool]:
    body = tuple(module.body)
    if not body or not is_docstring_statement(body[0]):
        return module, False
    first = body[0]
    if len(body) == 1:
        return module.with_changes(body=(line_with_pass(first),)), True
    next_statement = prepend_leading_lines(first, body[1])
    return module.with_changes(body=(next_statement,) + body[2:]), True


class StripTransformer(cst.CSTTransformer):
    METADATA_DEPENDENCIES: ClassVar[tuple[object, ...]] = (PositionProvider,)

    def __init__(self, options: TransformOptions, protected_code_lines: frozenset[int]) -> None:
        self.options = options
        self.protected_code_lines = protected_code_lines
        self.comments_removed = 0
        self.docstrings_removed = 0
        self.annotations_removed = 0

    def leave_TrailingWhitespace(
        self,
        original_node: cst.TrailingWhitespace,
        updated_node: cst.TrailingWhitespace,
    ) -> cst.TrailingWhitespace:
        comment = original_node.comment
        if comment is None:
            return updated_node
        position = self.get_metadata(PositionProvider, original_node)
        if is_protected_comment(
            comment.value,
            position.start.line,
            self.options.remove_protected_comments,
            False,
        ):
            return updated_node
        self.comments_removed += 1
        return updated_node.with_changes(whitespace=cst.SimpleWhitespace(""), comment=None)

    def leave_EmptyLine(
        self, original_node: cst.EmptyLine, updated_node: cst.EmptyLine
    ) -> cst.EmptyLine | cst.RemovalSentinel:
        if not self.options.remove_all_comments:
            return updated_node
        comment = original_node.comment
        if comment is None:
            return updated_node
        position = self.get_metadata(PositionProvider, original_node)
        line_number = position.start.line
        if is_protected_comment(comment.value, line_number, self.options.remove_protected_comments, True):
            return updated_node
        if not self.options.remove_module_docstring and line_number in self.protected_code_lines:
            return updated_node
        self.comments_removed += 1
        return cst.RemoveFromParent()

    def leave_Param(self, original_node: cst.Param, updated_node: cst.Param) -> cst.Param:
        if self.options.remove_annotations and original_node.annotation is not None:
            self.annotations_removed += 1
            return updated_node.with_changes(annotation=None)
        return updated_node

    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.SimpleStatementLine:
        if not self.options.remove_annotations:
            return updated_node
        new_body: list[cst.BaseSmallStatement] = []
        changed = False
        for statement in updated_node.body:
            if not isinstance(statement, cst.AnnAssign):
                new_body.append(statement)
                continue
            changed = True
            self.annotations_removed += 1
            if statement.value is None:
                continue
            if isinstance(statement.equal, cst.AssignEqual):
                target = cst.AssignTarget(
                    target=statement.target,
                    whitespace_before_equal=statement.equal.whitespace_before,
                    whitespace_after_equal=statement.equal.whitespace_after,
                )
            else:
                target = cst.AssignTarget(target=statement.target)
            new_body.append(
                cst.Assign(
                    targets=(target,),
                    value=statement.value,
                    semicolon=statement.semicolon,
                )
            )
        if not changed:
            return updated_node
        if not new_body:
            return updated_node.with_changes(body=(cst.Pass(),))
        return updated_node.with_changes(body=tuple(new_body))

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
        result = updated_node
        if self.options.remove_annotations and original_node.returns is not None:
            self.annotations_removed += 1
            result = result.with_changes(returns=None)
        if self.options.remove_docstrings:
            body, removed = remove_docstring_from_suite(result.body)
            if removed:
                self.docstrings_removed += 1
                result = result.with_changes(body=body)
        return result

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        if not self.options.remove_docstrings:
            return updated_node
        body, removed = remove_docstring_from_suite(updated_node.body)
        if not removed:
            return updated_node
        self.docstrings_removed += 1
        return updated_node.with_changes(body=body)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        if not self.options.remove_module_docstring:
            return updated_node
        module, removed = remove_module_docstring(updated_node)
        if removed:
            self.docstrings_removed += 1
        return module


def validate_python(source: str, path: Path) -> None:
    ast.parse(source, filename=str(path), mode="exec", feature_version=(3, 12))


def make_diff(path: Path, original: str, transformed: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            transformed.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def process_file(item: WorkItem) -> FileResult:
    path = item.path
    try:
        raw = path.read_bytes()
    except OSError as error:
        return FileResult(path=str(path), status="skipped", message=f"cannot read: {error}")
    try:
        decoded = decode_source(raw)
    except UnsupportedEncodingError as error:
        return FileResult(path=str(path), status="skipped", message=f"unsupported encoding: {error}")
    try:
        protected_lines = commented_code_lines(decoded.text) if item.options.remove_all_comments else frozenset()
        module = cst.parse_module(decoded.text)
        transformer = StripTransformer(item.options, protected_lines)
        transformed = MetadataWrapper(module).visit(transformer).code
        validate_python(transformed, path)
        output = encode_source(decoded, transformed)
        changed = output != raw
        result = FileResult(
            path=str(path),
            status="unchanged",
            original_bytes=len(raw),
            output_bytes=len(output),
            bytes_saved=len(raw) - len(output),
            comments_removed=transformer.comments_removed,
            docstrings_removed=transformer.docstrings_removed,
            annotations_removed=transformer.annotations_removed,
        )
        if not changed:
            return result
        if item.options.include_diff:
            result.diff = make_diff(path, decoded.text, transformed)
        if item.options.dry_run or item.options.check:
            result.status = "would_modify"
            return result
        mode = stat.S_IMODE(path.stat().st_mode)
        if item.backup_path is not None:
            atomic_write(item.backup_path, raw, mode)
            result.backup_path = str(item.backup_path)
        atomic_write(path, output, mode)
        result.status = "modified"
        return result
    except Exception as error:
        return FileResult(
            path=str(path),
            status="failed",
            original_bytes=len(raw),
            message=f"{type(error).__name__}: {error}",
        )


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def matches_patterns(path: Path, root: Path, includes: Sequence[str], excludes: Sequence[str]) -> bool:
    relative = path.relative_to(root)
    if excludes and any(relative.match(pattern) for pattern in excludes):
        return False
    if includes:
        return any(relative.match(pattern) for pattern in includes)
    return path.suffix.lower() == ".py" or not path.suffix


def scan_paths(
    paths: Sequence[Path],
    recursive: bool,
    includes: Sequence[str],
    excludes: Sequence[str],
    backup_directory: Path | None,
) -> tuple[list[tuple[Path, Path]], list[FileResult]]:
    discovered: dict[Path, Path] = {}
    scan_results: list[FileResult] = []
    for supplied in paths:
        try:
            path = supplied.expanduser().resolve()
            exists = path.exists()
        except (OSError, RuntimeError, ValueError) as error:
            scan_results.append(
                FileResult(
                    path=str(supplied),
                    status="skipped",
                    message=f"cannot access: {error}",
                )
            )
            continue
        if not exists:
            scan_results.append(FileResult(path=str(path), status="skipped", message="path does not exist"))
            continue
        try:
            is_file = path.is_file()
            is_directory = path.is_dir()
        except OSError as error:
            scan_results.append(FileResult(path=str(path), status="skipped", message=f"cannot inspect: {error}"))
            continue
        if is_file:
            root = path.parent
            candidates: Iterable[Path] = (path,)
        elif is_directory:
            root = path
            candidates = path.rglob("*") if recursive else path.iterdir()
        else:
            scan_results.append(
                FileResult(
                    path=str(path),
                    status="skipped",
                    message="not a regular file or directory",
                )
            )
            continue
        try:
            for candidate in candidates:
                try:
                    resolved = candidate.resolve()
                    if (
                        backup_directory is not None
                        and backup_directory != root
                        and is_within(backup_directory, root)
                        and is_within(resolved, backup_directory)
                    ):
                        continue
                    if not resolved.is_file():
                        continue
                    if not matches_patterns(resolved, root, includes, excludes):
                        continue
                except (OSError, RuntimeError, ValueError):
                    continue
                previous_root = discovered.get(resolved)
                if previous_root is None or len(root.parts) < len(previous_root.parts):
                    discovered[resolved] = root
        except OSError as error:
            scan_results.append(FileResult(path=str(path), status="skipped", message=f"cannot scan: {error}"))
    return sorted(discovered.items(), key=lambda pair: str(pair[0])), scan_results


def create_work_items(
    discovered: Sequence[tuple[Path, Path]],
    options: TransformOptions,
    backup: bool,
    backup_directory: Path | None,
) -> list[WorkItem]:
    roots = {root for _, root in discovered}
    multiple_roots = len(roots) > 1
    items: list[WorkItem] = []
    for path, root in discovered:
        backup_path: Path | None = None
        if backup_directory is not None:
            relative = path.relative_to(root)
            if multiple_roots:
                relative = Path(root.name) / relative
            backup_path = backup_directory / relative
            backup_path = backup_path.with_name(f"{backup_path.name}.bak")
        elif backup:
            backup_path = path.with_name(f"{path.name}.bak")
        items.append(WorkItem(path=path, backup_path=backup_path, options=options))
    return items


def run_pool(items: Sequence[WorkItem], workers: int, method: str) -> list[FileResult]:
    if not items:
        return []
    with multiprocessing.Pool(processes=workers) as pool:
        if method == "map":
            return list(pool.map(process_file, items))
        if method == "imap":
            iterator: Iterator[FileResult] = pool.imap(process_file, items)
        else:
            iterator = pool.imap_unordered(process_file, items)
        return list(iterator)


def result_data(result: FileResult, include_diff: bool) -> dict[str, object]:
    data = asdict(result)
    if not include_diff:
        data.pop("diff", None)
    return data


def summary_data(results: Sequence[FileResult]) -> dict[str, int]:
    return {
        "scanned": len(results),
        "modified": sum(result.status == "modified" for result in results),
        "would_modify": sum(result.status == "would_modify" for result in results),
        "unchanged": sum(result.status == "unchanged" for result in results),
        "skipped": sum(result.status == "skipped" for result in results),
        "failed": sum(result.status == "failed" for result in results),
        "bytes_saved": sum(result.bytes_saved for result in results if result.status in {"modified", "would_modify"}),
        "comments_removed": sum(result.comments_removed for result in results),
        "docstrings_removed": sum(result.docstrings_removed for result in results),
        "annotations_removed": sum(result.annotations_removed for result in results),
    }


def format_counts(result: FileResult, show_stats: bool) -> str:
    saved = f"{result.bytes_saved} bytes saved"
    if not show_stats:
        return saved
    return (
        f"{saved}; comments={result.comments_removed}; "
        f"docstrings={result.docstrings_removed}; annotations={result.annotations_removed}"
    )


def report_results(
    results: Sequence[FileResult],
    show_stats: bool,
    include_diff: bool,
    json_output: bool,
    quiet: bool,
) -> None:
    summary = summary_data(results)
    if json_output:
        payload = {
            "files": [result_data(result, include_diff) for result in results],
            "summary": summary,
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return
    if include_diff:
        for result in results:
            if result.diff:
                sys.stdout.write(result.diff)
                if not result.diff.endswith("\n"):
                    sys.stdout.write("\n")
    if quiet:
        return
    for result in results:
        if result.status in {"modified", "would_modify"}:
            action = "would modify" if result.status == "would_modify" else "modified"
            sys.stdout.write(f"{result.path}: {action}; {format_counts(result, show_stats)}\n")
        elif result.status == "unchanged":
            sys.stdout.write(f"{result.path}: unchanged; {format_counts(result, show_stats)}\n")
        else:
            sys.stdout.write(f"{result.path}: {result.status}; {result.message}\n")
    summary_line = (
        f"summary: scanned={summary['scanned']} modified={summary['modified']} "
        f"would_modify={summary['would_modify']} unchanged={summary['unchanged']} "
        f"skipped={summary['skipped']} failed={summary['failed']} "
        f"total_bytes_saved={summary['bytes_saved']}"
    )
    if show_stats:
        summary_line += (
            f" comments={summary['comments_removed']} "
            f"docstrings={summary['docstrings_removed']} "
            f"annotations={summary['annotations_removed']}"
        )
    sys.stdout.write(summary_line + "\n")


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        msg = "must be an integer"
        raise argparse.ArgumentTypeError(msg) from error
    if number < 1:
        msg = "must be at least 1"
        raise argparse.ArgumentTypeError(msg)
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pystrip",
        description="Strip selected Python comments, docstrings, and annotations in place.",
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[Path()])
    parser.add_argument("-c", "--comments", action="store_true", help="remove all removable comments")
    parser.add_argument(
        "-d",
        "--docstrings",
        action="store_true",
        help="remove function and class docstrings",
    )
    parser.add_argument("-a", "--all", action="store_true", help="remove all selected source metadata")
    parser.add_argument("-n", "--stats", action="store_true", help="show removal counts")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    parser.add_argument("--diff", action="store_true", help="emit unified diffs")
    backups = parser.add_mutually_exclusive_group()
    backups.add_argument("--backup", action="store_true", help="write a .bak file beside each change")
    backups.add_argument("--backup-dir", type=Path, help="write .bak files under this directory")
    parser.add_argument(
        "--pool-method",
        choices=("imap_unordered", "imap", "map"),
        default="imap_unordered",
        help="Pool dispatch method",
    )
    parser.add_argument("--include", action="append", default=[], metavar="GLOB", help="include glob")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="exclude glob")
    parser.add_argument("--check", action="store_true", help="exit 1 if changes would be made")
    parser.add_argument("--json", action="store_true", help="write a machine-readable report")
    parser.add_argument("--no-recursive", action="store_true", help="do not descend into directories")
    parser.add_argument("-q", "--quiet", action="count", default=0, help="suppress normal output")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase logging detail")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def configure_logging(quiet: int, verbose: int) -> None:
    if quiet:
        level = "WARNING"
    elif verbose:
        level = "DEBUG"
    else:
        level = "INFO"
    logger.remove()
    logger.add(sys.stderr, level=level, colorize=False, format="{level} | {message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    configure_logging(arguments.quiet, arguments.verbose)
    backup_directory = arguments.backup_dir.expanduser().resolve() if arguments.backup_dir is not None else None
    options = TransformOptions(
        remove_all_comments=arguments.comments or arguments.all,
        remove_protected_comments=arguments.all,
        remove_docstrings=arguments.docstrings or arguments.all,
        remove_module_docstring=arguments.all,
        remove_annotations=arguments.all,
        dry_run=arguments.dry_run,
        check=arguments.check,
        include_diff=arguments.diff,
    )
    discovered, scan_results = scan_paths(
        arguments.paths,
        not arguments.no_recursive,
        arguments.include,
        arguments.exclude,
        backup_directory,
    )
    work_items = create_work_items(
        discovered,
        options,
        arguments.backup,
        backup_directory,
    )
    try:
        processed = run_pool(work_items, MAX_WORKERS, arguments.pool_method)
    except Exception as error:
        logger.error(f"worker pool failed: {type(error).__name__}: {error}")
        processed = [
            FileResult(path=str(item.path), status="failed", message="worker pool failed") for item in work_items
        ]
    results = sorted(scan_results + processed, key=lambda result: result.path)
    for result in results:
        if result.status == "failed":
            logger.error(f"{result.path}: {result.message}")
        elif result.status == "skipped":
            logger.debug(f"{result.path}: {result.message}")
    report_results(results, arguments.stats, arguments.diff, arguments.json, bool(arguments.quiet))
    if any(result.status == "failed" for result in results):
        return 2
    if arguments.check and any(result.status == "would_modify" for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
