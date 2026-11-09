#!/data/data/com.termux/files/usr/bin/env python
"""Find the Python files with identical normalized ASTs.
Normalizes each file (comments, formatting and names of the local definitions), hashes the result with xxhash, groups the matches and caches them in ``.cache.json``; ``-r`` keeps the first file of each group and deletes the others, ``--rich`` adds a progress bar.
Usage: dufpy [-r] [--root DIR] [--rich] Options: -r, --remove Remove duplicate files, keeping the first path in each group --rich Show a progress bar while processing files --root Directory to scan (default: current directory) Examples: $ dufpy --root src --rich Command-line script: run ``python dufpy.py`` or the ``dufpy`` command.
The module is importable as well."""

import argparse
import ast
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import tokenize
from typing import Any, Callable, Iterable, TypeAlias

from dh import MAX_WORKERS, cprint, get_pyfiles
from xxhash import xxh64_hexdigest


DUPLICATES_CACHE_FILENAME: str = ".cache.json"

ProcessResult: TypeAlias = tuple[str, str | None, str | None]
ProcessingFailure: TypeAlias = tuple[str, str]


@dataclass(slots=True)
class DuplicateGroup:
    digest: str
    paths: list[Path]


class SourceNormalizer(ast.NodeTransformer):
    @staticmethod
    def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            return body[1:]
        return body

    @staticmethod
    def _remove_type_parameters(node: ast.AST) -> None:
        if hasattr(node, "type_params"):
            node.type_params = []

    def visit_Module(self, node: ast.Module) -> ast.Module:
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        node.type_ignores.clear()
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.returns = None
        node.type_comment = None
        self._remove_type_parameters(node)
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        node.returns = None
        node.type_comment = None
        self._remove_type_parameters(node)
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self._remove_type_parameters(node)
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        node.type_comment = None
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.Assign:
        node.type_comment = None
        self.generic_visit(node)
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if node.value is None:
            return None
        target = self.visit(node.target)
        value = self.visit(node.value)
        if target is None or value is None:
            return None
        return ast.Assign(targets=[target], value=value, type_comment=None)

    def visit_For(self, node: ast.For) -> ast.For:
        node.type_comment = None
        self.generic_visit(node)
        return node

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.AsyncFor:
        node.type_comment = None
        self.generic_visit(node)
        return node

    def visit_With(self, node: ast.With) -> ast.With:
        node.type_comment = None
        self.generic_visit(node)
        return node

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AsyncWith:
        node.type_comment = None
        self.generic_visit(node)
        return node

    def visit_TypeAlias(self, node: ast.AST) -> None:
        return None


def normalize_file(path: Path) -> str:
    with tokenize.open(path) as source_file:
        source = source_file.read()
    tree = ast.parse(source, filename=str(path), mode="exec", type_comments=False)
    normalized_tree = SourceNormalizer().visit(tree)
    if not isinstance(normalized_tree, ast.Module):
        msg = f"Expected a module AST for {path}"
        raise TypeError(msg)
    return ast.dump(normalized_tree, annotate_fields=True, include_attributes=False)


def process_file(path_text: str) -> ProcessResult:
    try:
        normalized = normalize_file(Path(path_text))
        digest = xxh64_hexdigest(normalized.encode("utf-8"))
        return path_text, digest, None
    except Exception as error:
        return path_text, None, f"{type(error).__name__}: {error}"


def _run_jobs(
    paths: list[Path],
    workers: int,
    on_complete: Callable[[], None] | None = None,
) -> list[ProcessResult]:
    if workers == 1:
        results: list[ProcessResult] = []
        for path in paths:
            results.append(process_file(str(path)))
            if on_complete is not None:
                on_complete()
        return results
    results = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process_file, str(path)) for path in paths]
        for future in as_completed(futures):
            results.append(future.result())
            if on_complete is not None:
                on_complete()
    return results


def process_files(
    paths: list[Path],
    workers: int | None,
    show_progress: bool,
) -> list[ProcessResult]:
    total = len(paths)
    if total == 0:
        return []
    available_workers = os.cpu_count() or 1
    worker_count = min(total, workers if workers is not None else available_workers)
    worker_count = max(1, worker_count)
    if not show_progress:
        return _run_jobs(paths, worker_count)
    try:
        from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
    except ImportError:
        completed = 0

        def update_basic_progress() -> None:
            nonlocal completed
            completed += 1
            print(
                f"\rProcessing Python files: {completed}/{total}",
                end="",
                file=sys.stderr,
                flush=True,
            )

        try:
            return _run_jobs(paths, worker_count, update_basic_progress)
        finally:
            print(file=sys.stderr)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task("Processing Python files", total=total)
        return _run_jobs(
            paths,
            worker_count,
            lambda: progress.update(task_id, advance=1),
        )


def identify_duplicates(
    results: list[ProcessResult],
) -> tuple[list[DuplicateGroup], list[ProcessingFailure]]:
    candidates: dict[str, list[Path]] = defaultdict(list)
    failures: list[ProcessingFailure] = []
    for path_text, digest, error in results:
        if error is not None:
            failures.append((path_text, error))
        elif digest is not None:
            candidates[digest].append(Path(path_text))
    duplicate_groups: list[DuplicateGroup] = []
    for digest, paths in sorted(candidates.items()):
        if len(paths) < 2:
            continue
        exact_matches: dict[str, list[Path]] = defaultdict(list)
        for path in sorted(paths, key=str):
            try:
                exact_matches[normalize_file(path)].append(path)
            except Exception as error:
                failures.append((str(path), f"{type(error).__name__}: {error}"))
        for matched_paths in exact_matches.values():
            if len(matched_paths) > 1:
                duplicate_groups.append(DuplicateGroup(digest, sorted(matched_paths, key=str)))
    duplicate_groups.sort(key=lambda group: str(group.paths[0]))
    failures.sort(key=lambda failure: failure[0])
    return duplicate_groups, failures


def _cache_path(root: Path) -> Path:
    return root / DUPLICATES_CACHE_FILENAME


def save_duplicates_cache(root: Path, groups: list[DuplicateGroup]) -> None:
    if not groups:
        return
    payload = {
        "root": str(root),
        "groups": [[str(path) for path in group.paths] for group in groups],
    }
    _cache_path(root).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_duplicates_cache(root: Path) -> list[DuplicateGroup] | None:
    path = _cache_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("root") != str(root):
        return None
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        return None
    groups: list[DuplicateGroup] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, list):
            return None
        if not all(isinstance(entry, str) for entry in raw_group):
            return None
        paths = [Path(entry) for entry in raw_group]
        if len(paths) < 2:
            continue
        groups.append(DuplicateGroup("", paths))
    return groups


def clear_duplicates_cache(root: Path) -> None:
    try:
        _cache_path(root).unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        print(f"Failed to remove cache file {_cache_path(root)}: {error}", file=sys.stderr)


def verify_cached_groups(groups: list[DuplicateGroup]) -> list[DuplicateGroup]:
    verified: list[DuplicateGroup] = []
    for group in groups:
        by_digest: dict[str, list[Path]] = defaultdict(list)
        for path in group.paths:
            try:
                digest = xxh64_hexdigest(normalize_file(path).encode("utf-8"))
            except Exception:
                continue
            by_digest[digest].append(path)
        for digest, paths in by_digest.items():
            if len(paths) > 1:
                verified.append(DuplicateGroup(digest, sorted(paths, key=str)))
    verified.sort(key=lambda group: str(group.paths[0]))
    return verified


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        msg = "must be a positive integer"
        raise argparse.ArgumentTypeError(msg) from error
    if number < 1:
        msg = "must be a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return number


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find Python files with identical normalized ASTs.")
    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="remove duplicate files, keeping the first path in each group",
    )
    parser.add_argument(
        "--rich",
        action="store_true",
        help="show a progress bar while processing files",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="directory to scan (default: current directory)",
    )
    return parser.parse_args(argv)


def _print_failures(failures: list[ProcessingFailure]) -> None:
    for path, error in failures:
        print(f"Skipped {path}: {error}", file=sys.stderr)


def _report_duplicates(groups: list[DuplicateGroup], remove: bool) -> None:
    if not groups:
        cprint("No duplicate files found.", "green")
        return
    for index, group in enumerate(groups, start=1):
        cprint(f"Duplicate group {index}")
        for path in group.paths:
            cprint(f"  - {path.name}", "yellow")
    if not remove:
        cprint("Re-run with -r to remove duplicates.", "cyan")
        return
    deleted = 0
    for group in groups:
        keeper = group.paths[0]
        for duplicate in group.paths[1:]:
            try:
                duplicate.unlink()
                print(f"{duplicate} removed; kept {keeper}")
                deleted += 1
            except OSError as error:
                print(f"Failed to remove {duplicate}: {error}", file=sys.stderr)
    if deleted:
        cprint(f"{deleted} duplicate files removed.", "cyan")
    else:
        cprint("No duplicate files were removed.", "green")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    if args.remove:
        cached = load_duplicates_cache(root)
        if cached is not None:
            verified = verify_cached_groups(cached)
            if verified:
                _report_duplicates(verified, True)
            else:
                cprint("No duplicate files found.", "green")
            clear_duplicates_cache(root)
            return 0

    files = sorted({Path(path) for path in get_pyfiles(root)}, key=str)
    if not files:
        cprint("No Python files found.", "green")
        return 0

    results = process_files(files, MAX_WORKERS, args.rich)
    duplicate_groups, failures = identify_duplicates(results)
    if failures:
        _print_failures(failures)

    if args.remove:
        _report_duplicates(duplicate_groups, True)
    else:
        try:
            save_duplicates_cache(root, duplicate_groups)
        except OSError as error:
            print(f"Failed to write cache file: {error}", file=sys.stderr)
        _report_duplicates(duplicate_groups, False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
