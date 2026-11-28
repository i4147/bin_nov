#!/data/data/com.termux/files/usr/bin/env python
"""Rewrite direct file writes to dh.atomic_write calls.

libcst-based codemod that finds path.write_text/open(...,'w')-style writes and replaces them with atomic_write, adding the import; designed for large in-place rollouts with preview.
"""

from __future__ import annotations
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import difflib
from functools import partial
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import traceback
from typing import TYPE_CHECKING, Literal

from dh import SKIP_DIRS
import libcst as cst
from loguru import logger
from parallel_runner import (
    FileFilter,
    RunnerConfig,
    RunnerStats,
    TaskOutcome,
    build_base_parser,
    count_files,
    get_files,
    normalize_extensions,
    print_summary,
)
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text


if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Sequence


DEFAULT_WORKERS: int = os.cpu_count() or 1
DEFAULT_LOG_FILE: str = "atomic_write_refactor.log"
DEFAULT_IMPORT_FROM: str = "dh"

MethodMode = Literal["bytes", "text", "both"]
Status = Literal["changed", "would-change", "unchanged", "error"]

console: Console = Console()


@dataclass(frozen=True)
class RewritePayload:
    """Metadata returned alongside a TaskOutcome for codemod reporting."""

    status: Status
    rewrites: int = 0
    message: str = ""


class ReplaceWriteMethods(cst.CSTTransformer):
    """Rewrite `<expr>.write_bytes(...)` and/or `<expr>.write_text(...)` into `atomic_write(<expr>, ...)`."""

    def __init__(self, target_methods: frozenset[str]) -> None:
        super().__init__()
        self.target_methods: frozenset[str] = target_methods
        self.changed: bool = False
        self.rewritten_counts: dict[str, int] = dict.fromkeys(sorted(target_methods), 0)

    def leave_Call(
        self,
        original_node: cst.Call,
        updated_node: cst.Call,
    ) -> cst.Call:
        func: cst.BaseExpression = updated_node.func
        if not isinstance(func, cst.Attribute):
            return updated_node

        method_name: str = func.attr.value
        if method_name not in self.target_methods:
            return updated_node

        # Avoid rewriting `atomic_write(...)` calls that already use the helper name.
        if isinstance(func.value, cst.Name) and func.value.value == "atomic_write":
            return updated_node

        self.changed = True
        self.rewritten_counts[method_name] = (
            self.rewritten_counts.get(method_name, 0) + 1
        )
        return updated_node.with_changes(
            func=cst.Name("atomic_write"),
            args=[
                cst.Arg(value=func.value),
                *updated_node.args,
            ],
        )


def _is_docstring(statement: cst.BaseStatement) -> bool:
    return (
        isinstance(statement, cst.SimpleStatementLine)
        and len(statement.body) == 1
        and isinstance(statement.body[0], cst.Expr)
        and isinstance(statement.body[0].value, cst.SimpleString)
    )


def _is_future_import(statement: cst.BaseStatement) -> bool:
    return (
        isinstance(statement, cst.SimpleStatementLine)
        and len(statement.body) == 1
        and isinstance(statement.body[0], cst.ImportFrom)
        and isinstance(statement.body[0].module, cst.Name)
        and statement.body[0].module.value == "__future__"
    )


def _format_module_expr(module_expr: cst.BaseExpression | None) -> str:
    if module_expr is None:
        return ""
    return cst.Module([]).code_for_node(module_expr).strip()


def has_atomic_write_import(module: cst.Module, import_from: str) -> bool:
    """Return True if `atomic_write` is already imported from `import_from` (or top-level)."""
    target_mod: str = import_from.strip()
    for statement in module.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        for small in statement.body:
            if isinstance(small, cst.ImportFrom):
                mod_name: str = _format_module_expr(small.module)
                if target_mod and mod_name != target_mod:
                    continue
                if isinstance(small.names, cst.ImportStar):
                    return True
                for alias in small.names:
                    if (
                        isinstance(alias.name, cst.Name)
                        and alias.name.value == "atomic_write"
                    ):
                        return True
            elif isinstance(small, cst.Import):
                for alias in small.names:
                    as_name: str | None = (
                        alias.asname.name.value
                        if alias.asname and isinstance(alias.asname.name, cst.Name)
                        else None
                    )
                    imported_name: str = _format_module_expr(alias.name)
                    if imported_name == "atomic_write" or as_name == "atomic_write":
                        return True
    return False


def add_import(module: cst.Module, import_from: str) -> cst.Module:
    """Insert `from <import_from> import atomic_write` after docstring and __future__ imports."""
    body: list[cst.BaseStatement] = list(module.body)
    index: int = 0

    if body and _is_docstring(body[0]):
        index = 1

    while index < len(body) and _is_future_import(body[index]):
        index += 1

    mod_target: str = import_from.strip() or DEFAULT_IMPORT_FROM
    statement_text: str = f"from {mod_target} import atomic_write\n"
    new_statement: cst.BaseStatement = cst.parse_statement(statement_text)
    body.insert(index, new_statement)
    return module.with_changes(body=body)


@dataclass(frozen=True)
class Options:
    mode: MethodMode = "both"
    import_from: str = DEFAULT_IMPORT_FROM
    dry_run: bool = False
    check: bool = False
    diff: bool = False
    backup: bool = False
    workers: int = DEFAULT_WORKERS
    excluded_dirs: frozenset[str] = SKIP_DIRS
    log_file: Path | None = Path(DEFAULT_LOG_FILE)

    def target_methods(self) -> frozenset[str]:
        if self.mode == "bytes":
            return frozenset({"write_bytes"})
        if self.mode == "text":
            return frozenset({"write_text"})
        return frozenset({"write_bytes", "write_text"})


def validate_source(source: str, path: Path) -> str | None:
    """Return an error message if the source is not valid Python, else None."""
    try:
        compile(source, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        return f"SyntaxError at line {exc.lineno}: {exc.msg}"
    except ValueError as exc:
        return f"invalid source: {exc}"

    try:
        cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return f"LibCST could not re-parse the result: {exc}"

    return None


def make_diff(old: str, new: str, rel_path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


def write_atomically(path: Path, data: bytes, backup: bool) -> None:
    """Write bytes via a temp file and os.replace, optionally keeping FILE.py.bak."""
    mode: int = stat.S_IMODE(path.stat().st_mode)

    if backup:
        shutil.copy2(path, path.with_name(path.name + ".bak"))

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp: Path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

        if tmp.read_bytes() != data:
            msg = "verification of temporary file failed"
            raise OSError(msg)

        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def refactor_file(path: Path, root: Path, opts: Options) -> TaskOutcome[RewritePayload]:
    """Run in a worker process; never raises, returning failures as TaskOutcome objects."""
    rel: str = str(path.relative_to(root.resolve()))
    try:
        original_bytes: bytes = path.read_bytes()
        source: str = original_bytes.decode("utf-8")
        module: cst.Module = cst.parse_module(source)

        transformer: ReplaceWriteMethods = ReplaceWriteMethods(opts.target_methods())
        updated: cst.Module = module.visit(transformer)

        if not transformer.changed:
            return TaskOutcome(
                rel_path=rel,
                changed=False,
                bytes_delta=0,
                payload=RewritePayload(status="unchanged", rewrites=0),
            )

        if not has_atomic_write_import(updated, opts.import_from):
            updated = add_import(updated, opts.import_from)

        new_source: str = updated.code
        if new_source == source:
            return TaskOutcome(
                rel_path=rel,
                changed=False,
                bytes_delta=0,
                payload=RewritePayload(status="unchanged", rewrites=0),
            )

        error: str | None = validate_source(new_source, path)
        if error is not None:
            msg = f"refusing to write, result is invalid: {error}"
            return TaskOutcome(
                rel_path=rel,
                error=msg,
                payload=RewritePayload(status="error", message=msg),
            )

        new_bytes: bytes = new_source.encode("utf-8")
        bytes_delta: int = len(new_bytes) - len(original_bytes)
        total_rewrites: int = sum(transformer.rewritten_counts.values())
        diff: str = make_diff(source, new_source, rel) if opts.diff else ""
        preview_only: bool = opts.dry_run or opts.check

        if preview_only:
            return TaskOutcome(
                rel_path=rel,
                changed=True,
                bytes_delta=bytes_delta,
                diff=diff,
                payload=RewritePayload(status="would-change", rewrites=total_rewrites),
            )

        write_atomically(path, new_bytes, backup=opts.backup)
        return TaskOutcome(
            rel_path=rel,
            changed=True,
            bytes_delta=bytes_delta,
            diff=diff,
            payload=RewritePayload(status="changed", rewrites=total_rewrites),
        )

    except OSError as exc:
        msg = f"file operation failed, file left unchanged: {exc}"
        return TaskOutcome(
            rel_path=rel,
            error=msg,
            payload=RewritePayload(status="error", message=msg),
        )
    except Exception:
        tb_msg: str = f"unexpected error:\n{traceback.format_exc()}"
        return TaskOutcome(
            rel_path=rel,
            error=tb_msg,
            payload=RewritePayload(status="error", message=tb_msg),
        )


def render_diff(diff: str, out: Console) -> None:
    for line in diff.splitlines():
        style: str
        if line.startswith(("---", "+++")):
            style = "bold"
        elif line.startswith("@@"):
            style = "cyan"
        elif line.startswith("+"):
            style = "green"
        elif line.startswith("-"):
            style = "red"
        else:
            style = ""
        out.print(Text(line, style=style), soft_wrap=True, highlight=False)


STATUS_LABEL: dict[Status, str] = {
    "changed": "[green]changed[/green]",
    "would-change": "[yellow]would change[/yellow]",
    "unchanged": "[dim]unchanged[/dim]",
    "error": "[bold red]error[/bold red]",
}


def handle_outcome(
    outcome: TaskOutcome[RewritePayload],
    out: Console,
    status_counts: dict[Status, int],
    stats: RunnerStats,
) -> int:
    """Run in the main process: log via loguru, update counters, and print Rich lines."""
    payload: RewritePayload = outcome.payload or RewritePayload(
        status="error"
        if outcome.error
        else ("changed" if outcome.changed else "unchanged"),
        message=outcome.error or "",
    )
    status: Status = payload.status
    status_counts[status] = status_counts.get(status, 0) + 1
    stats.processed += 1

    if status == "error" or outcome.error is not None:
        stats.error_count += 1
        err_text: str = outcome.error or payload.message or "unknown error"
        logger.error(f"{outcome.rel_path}: {err_text}")
        first_line: str = err_text.splitlines()[0] if err_text else ""
        out.print(
            f"{STATUS_LABEL['error']} {escape(outcome.rel_path)}: {escape(first_line)}"
        )
        return 0

    if outcome.diff:
        render_diff(outcome.diff, out)

    if status == "unchanged":
        return 0

    stats.changed_count += 1
    stats.total_delta += outcome.bytes_delta
    detail: str = f" ({payload.rewrites})" if payload.rewrites else ""
    logger.info(f"{status}{detail}: {outcome.rel_path}")
    out.print(f"{STATUS_LABEL[status]} {escape(outcome.rel_path)}{escape(detail)}")
    return payload.rewrites


def run_with_progress(
    runner_cfg: RunnerConfig,
    file_filter: FileFilter,
    handler: Callable[[Path], TaskOutcome[RewritePayload]],
    opts: Options,
) -> tuple[int, RunnerStats, dict[Status, int], int]:
    """Walk twice using parallel_runner helpers while driving a Rich Progress bar."""
    root: Path = runner_cfg.root.resolve()
    worker_count: int = max(1, runner_cfg.workers)

    # Pass 1: count matching files before creating the progress bar.
    total: int = count_files(root, file_filter)
    stats: RunnerStats = RunnerStats(total=total)
    status_counts: dict[Status, int] = {
        "changed": 0,
        "would-change": 0,
        "unchanged": 0,
        "error": 0,
    }
    total_rewrites: int = 0

    if total == 0:
        console.print("[yellow]no Python files found[/yellow]")
        return 1, stats, status_counts, total_rewrites

    # Pass 2: stream files from get_files() into a process pool and update Rich Progress.
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task: TaskID = progress.add_task(
            f"Processing ({opts.mode}, {worker_count} workers)",
            total=total,
        )
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures: dict[Future[TaskOutcome[RewritePayload]], Path] = {
                pool.submit(handler, path): path
                for path in get_files(root, file_filter)
            }
            for future in as_completed(futures):
                path: Path = futures[future]
                rel: str = str(path.relative_to(root))
                try:
                    outcome: TaskOutcome[RewritePayload] = future.result()
                except Exception:
                    tb: str = f"worker process crashed:\n{traceback.format_exc()}"
                    outcome = TaskOutcome(
                        rel_path=rel,
                        error=tb,
                        payload=RewritePayload(status="error", message=tb),
                    )
                total_rewrites += handle_outcome(
                    outcome, progress.console, status_counts, stats
                )
                progress.advance(task)

    exit_code: int = print_summary(console, stats, runner_cfg)
    run_label: str = "dry run" if (opts.dry_run or opts.check) else "done"
    console.print(
        f"[bold]{run_label}:[/bold] "
        f"{status_counts['changed']} changed, "
        f"{status_counts['would-change']} would change, "
        f"{status_counts['unchanged']} unchanged, "
        f"{status_counts['error']} errors "
        f"({total_rewrites} call(s) rewritten)"
    )
    if status_counts["error"] and opts.log_file is not None:
        console.print(f"[red]Details written to {escape(str(opts.log_file))}[/red]")

    return exit_code, stats, status_counts, total_rewrites


def _configure_logging(log_file: Path | None) -> None:
    logger.remove()
    if log_file is not None:
        logger.add(
            str(log_file),
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
            backtrace=True,
            diagnose=False,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = build_base_parser(
        "Replace .write_bytes(...) and/or .write_text(...) calls with atomic_write(path, ...)."
    )
    parser.add_argument(
        "--mode",
        choices=("bytes", "text", "both"),
        default="both",
        help="which write method(s) to rewrite: bytes, text, or both (default: both)",
    )
    parser.add_argument(
        "--bytes-only",
        action="store_true",
        help="shorthand for --mode bytes",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="shorthand for --mode text",
    )
    parser.add_argument(
        "--import-from",
        default=DEFAULT_IMPORT_FROM,
        help=f"module exporting atomic_write (default: {DEFAULT_IMPORT_FROM})",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="save the original as FILE.py.bak before overwriting",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path(DEFAULT_LOG_FILE),
        metavar="PATH",
        help=f"log file path (default: {DEFAULT_LOG_FILE})",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="disable writing a log file",
    )
    return parser


def _resolve_mode(args: argparse.Namespace) -> MethodMode:
    if args.bytes_only and args.text_only:
        msg = "cannot combine --bytes-only and --text-only"
        raise SystemExit(msg)
    if args.bytes_only:
        return "bytes"
    if args.text_only:
        return "text"
    return args.mode


def main(argv: Sequence[str] | None = None) -> int:
    parser: argparse.ArgumentParser = _build_parser()
    args: argparse.Namespace = parser.parse_args(argv)
    mode: MethodMode = _resolve_mode(args)

    log_path: Path | None = None if args.no_log else args.log_file
    _configure_logging(log_path)

    extra_exts: frozenset[str] = normalize_extensions(args.ext)
    extensions: frozenset[str] = frozenset({".py"}) | extra_exts
    extra_dirs: frozenset[str] = frozenset(
        d.strip() for d in args.exclude_dir if d.strip()
    )
    excluded_dirs: frozenset[str] = SKIP_DIRS | extra_dirs

    file_filter: FileFilter = FileFilter(
        extensions=extensions,
        allow_extensionless=bool(args.extensionless),
        match_any_when_empty=False,
        excluded_dirs=excluded_dirs,
        skip_hidden_files=True,
        skip_symlinks=True,
    )

    runner_cfg: RunnerConfig = RunnerConfig(
        root=args.root,
        workers=max(1, int(args.workers)),
        dry_run=bool(args.dry_run),
        check=bool(args.check),
        show_diff=not bool(args.no_diff),
        unchanged=bool(args.unchanged),
        item_label="Python files",
    )

    opts: Options = Options(
        mode=mode,
        import_from=args.import_from,
        dry_run=runner_cfg.dry_run,
        check=runner_cfg.check,
        diff=runner_cfg.show_diff is not False
        and bool(args.dry_run or args.check or args.diff),
        backup=bool(args.backup),
        workers=runner_cfg.workers,
        excluded_dirs=excluded_dirs,
        log_file=log_path,
    )

    handler: Callable[[Path], TaskOutcome[RewritePayload]] = partial(
        refactor_file,
        root=runner_cfg.root,
        opts=opts,
    )

    exit_code, _, _, _ = run_with_progress(
        runner_cfg=runner_cfg,
        file_filter=file_filter,
        handler=handler,
        opts=opts,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
