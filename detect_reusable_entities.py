#!/data/data/com.termux/files/usr/bin/env python
"""
Find reusable Python entities with LibCST and write a reviewable report.

The script never edits scanned source files. An optional --output-module writes a draft module containing candidate definitions; review it before importing it.

"""

from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import concurrent.futures
import csv
from dataclasses import asdict, dataclass
import fnmatch
import io
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence

from dh import atomic_write
import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider, ScopeProvider
from loguru import logger


@dataclass
class Definition:
    kind: str
    name: str
    path: str
    line: int
    source: str
    decorators: list[str]
    is_private: bool
    nested: bool = False
    references: int = 0
    reference_files: list[str] | None = None
    warnings: list[str] | None = None


@dataclass
class FileResult:
    path: str
    definitions: list[Definition]
    references: dict[str, int]
    reference_files: dict[str, list[str]]
    error: str | None = None


def assigned_names(target: cst.CSTNode) -> Iterable[str]:
    """Yield simple names from assignment targets."""
    if isinstance(target, cst.Name):
        yield target.value
    elif isinstance(target, (cst.Tuple, cst.List)):
        for element in target.elements:
            if element is not None:
                yield from assigned_names(element.value)
    elif isinstance(target, cst.StarredElement):
        yield from assigned_names(target.value)


def constant_names(statement: cst.CSTNode, all_assignments: bool) -> list[str]:
    """Get eligible names assigned by an assignment statement."""
    targets: list[cst.CSTNode] = []
    if isinstance(statement, cst.Assign):
        targets.extend(item.target for item in statement.targets)
    elif isinstance(statement, cst.AnnAssign):
        targets.append(statement.target)
    else:
        return []

    names = {
        name
        for target in targets
        for name in assigned_names(target)
        if all_assignments or name.isupper()
    }
    return sorted(names)


class DefinitionCollector(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (ScopeProvider,)

    def __init__(
        self,
        relative_path: str,
        source_module: cst.Module,
        include_nested: bool,
        include_private: bool,
        all_assignments: bool,
    ) -> None:
        self.relative_path = relative_path
        self.source_module = source_module
        self.include_nested = include_nested
        self.include_private = include_private
        self.all_assignments = all_assignments
        self.definitions: list[Definition] = []
        self.references: Counter[str] = Counter()
        self.reference_files: dict[str, set[str]] = defaultdict(set)
        self._scope_depth = 0

    def _add(
        self,
        kind: str,
        name: str,
        node: cst.CSTNode,
        decorators: list[str] | None = None,
    ) -> None:
        private = name.startswith("_")
        if private and not self.include_private:
            return

        position = self.get_metadata(
            # PositionProvider is imported below to keep metadata explicit.
            PositionProvider,
            node,
        )
        self.definitions.append(
            Definition(
                kind=kind,
                name=name,
                path=self.relative_path,
                line=position.start.line,
                source=self.source_module.code_for_node(node),
                decorators=decorators or [],
                is_private=private,
                nested=self._scope_depth > 0,
            )
        )

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool | None:
        if node.name.value != "main":
            self._add(
                "functions",
                node.name.value,
                node,
                [
                    self.source_module.code_for_node(d.decorator)
                    for d in node.decorators
                ],
            )
        self._scope_depth += 1
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self._scope_depth -= 1

    def visit_ClassDef(self, node: cst.ClassDef) -> bool | None:
        self._add(
            "classes",
            node.name.value,
            node,
            [self.source_module.code_for_node(d.decorator) for d in node.decorators],
        )
        self._scope_depth += 1
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self._scope_depth -= 1

    def visit_Module(self, node: cst.Module) -> None:
        # Constants are only considered at module scope.
        for statement in node.body:
            for name in constant_names(statement, self.all_assignments):
                self._add("constants", name, statement)

    def leave_Module(self, original_node: cst.Module) -> None:
        scope = self.get_metadata(ScopeProvider, original_node)
        for access in scope.accesses:
            self.references[access.node.value] += 1
            self.reference_files[access.node.value].add(self.relative_path)


# Imported after the class definition so the visitor's metadata dependencies
# remain easy to see in one place.


def analyze_file(task: tuple[str, str, bool, bool, bool]) -> FileResult:
    file_path, relative_path, include_nested, include_private, all_assignments = task
    try:
        source = Path(file_path).read_text(encoding="utf-8")
        module = cst.parse_module(source)
        wrapper = MetadataWrapper(module)
        collector = DefinitionCollector(
            relative_path,
            wrapper.module,
            include_nested,
            include_private,
            all_assignments,
        )
        wrapper.visit(collector)
        return FileResult(
            path=relative_path,
            definitions=collector.definitions,
            references=dict(collector.references),
            reference_files={
                name: sorted(paths) for name, paths in collector.reference_files.items()
            },
        )
    except Exception as exc:
        logger.exception("Failed to analyze {}: {}", relative_path, exc)
        return FileResult(relative_path, [], {}, {}, str(exc))


def matches_exclusion(path: Path, root: Path, patterns: Sequence[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    return any(
        fnmatch.fnmatch(relative, pattern)
        or any(fnmatch.fnmatch(part, pattern) for part in path.relative_to(root).parts)
        for pattern in patterns
    )


def find_files(
    root: Path, exclusions: Sequence[str], follow_symlinks: bool
) -> list[Path]:
    found: list[Path] = []
    for current, dirs, filenames in os.walk(root, followlinks=follow_symlinks):
        current_path = Path(current)
        dirs[:] = [
            name
            for name in dirs
            if not matches_exclusion(current_path / name, root, exclusions)
        ]
        for filename in filenames:
            path = current_path / filename
            if filename.endswith(".py") and not matches_exclusion(
                path, root, exclusions
            ):
                found.append(path)
    return sorted(found)


def get_candidates(
    results: Sequence[FileResult],
    min_uses: int,
    min_files: int,
    min_uses_per_file: int,
    cross_file_only: bool,
) -> tuple[list[Definition], list[Definition]]:
    definitions = [item for result in results for item in result.definitions]
    by_name: dict[str, list[Definition]] = defaultdict(list)
    for definition in definitions:
        by_name[definition.name].append(definition)

    references: Counter[str] = Counter()
    ref_files: dict[str, Counter[str]] = defaultdict(Counter)
    for result in results:
        for name, count in result.references.items():
            references[name] += count
            ref_files[name][result.path] += count

    candidates: list[Definition] = []
    duplicates: list[Definition] = []
    for definition in definitions:
        if len(by_name[definition.name]) > 1:
            duplicates.append(definition)
            continue

        files_used = {
            path
            for path, count in ref_files[definition.name].items()
            if count >= min_uses_per_file
            and (not cross_file_only or path != definition.path)
        }
        use_count = sum(
            count
            for path, count in ref_files[definition.name].items()
            if not cross_file_only or path != definition.path
        )
        if use_count < min_uses or len(files_used) < min_files:
            continue

        definition.references = use_count
        definition.reference_files = sorted(files_used)
        definition.warnings = []
        if definition.decorators:
            definition.warnings.append("has decorators; review dependencies")
        if definition.nested:
            definition.warnings.append("nested definition; moving may change behavior")
        candidates.append(definition)

    return candidates, duplicates


def sort_candidates(items: list[Definition], sort_by: str) -> list[Definition]:
    if sort_by == "uses":
        return sorted(items, key=lambda x: (-x.references, x.name.lower(), x.path))
    if sort_by == "files":
        return sorted(
            items,
            key=lambda x: (-len(x.reference_files or []), x.name.lower(), x.path),
        )
    return sorted(items, key=lambda x: (x.name.lower(), x.path))


def text_report(
    candidates: Sequence[Definition],
    duplicates: Sequence[Definition],
    summaries: bool,
) -> str:
    sections = (
        ("constants", "1. CONSTANTS"),
        ("classes", "2. CLASSES"),
        ("functions", "3. FUNCTIONS"),
    )
    output = [
        "# Reusable entity candidates",
        "# Review before moving; source files are not modified.",
        "",
    ]
    for kind, title in sections:
        output.extend((title, "=" * len(title), ""))
        group = [item for item in candidates if item.kind == kind]
        if not group:
            output.extend(("(none)", ""))
            continue
        for item in group:
            output.append(
                f"# {item.name}: {item.references} reference(s); defined at {item.path}:{item.line}"
            )
            output.append(f"# Referenced in: {', '.join(item.reference_files or [])}")
            for warning in item.warnings or []:
                output.append(f"# Review: {warning}")
            output.extend(item.source.splitlines())
            output.append("")

    if duplicates:
        output.extend(("DUPLICATE DEFINITIONS", "=====================", ""))
        for item in duplicates:
            output.append(f"# {item.name}: {item.path}:{item.line} ({item.kind})")

    if summaries:
        output.extend(("", "SUMMARY", "======="))
        output.append(f"Candidates: {len(candidates)}")
        output.append(f"Duplicate-name definitions: {len(duplicates)}")
        for kind, title in sections:
            output.append(
                f"{title.split('. ', 1)[1].title()}: {sum(item.kind == kind for item in candidates)}"
            )
    return "\n".join(output).rstrip() + "\n"


def json_report(
    candidates: Sequence[Definition],
    duplicates: Sequence[Definition],
    summaries: bool,
) -> str:
    payload: dict[str, Any] = {
        "candidates": [asdict(item) for item in candidates],
        "duplicate_definitions": [asdict(item) for item in duplicates],
    }
    if summaries:
        payload["summary"] = {
            "candidate_count": len(candidates),
            "duplicate_definition_count": len(duplicates),
            "by_kind": {
                kind: sum(item.kind == kind for item in candidates)
                for kind in ("constants", "classes", "functions")
            },
        }
    return json.dumps(payload, indent=2) + "\n"


def csv_report(candidates: Sequence[Definition]) -> str:
    buffer = io.StringIO()
    fields = (
        "kind",
        "name",
        "path",
        "line",
        "references",
        "reference_files",
        "nested",
        "decorators",
        "warnings",
        "source",
    )
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for item in candidates:
        row = asdict(item)
        row["reference_files"] = ";".join(item.reference_files or [])
        row["decorators"] = ";".join(item.decorators)
        row["warnings"] = ";".join(item.warnings or [])
        writer.writerow({field: row[field] for field in fields})
    return buffer.getvalue()


def draft_module(candidates: Sequence[Definition]) -> str:
    """Create a draft module; imports and dependency fixes are intentionally omitted."""
    sections = (
        ("constants", "# Constants"),
        ("classes", "# Classes"),
        ("functions", "# Functions"),
    )
    output = [
        '"""Draft shared module generated for review.',
        "Resolve dependencies and circular imports before using.",
        '"""',
        "",
    ]
    for kind, title in sections:
        group = [item for item in candidates if item.kind == kind]
        if group:
            output.extend((title, ""))
            for item in group:
                output.extend(
                    (
                        f"# Original: {item.path}:{item.line}",
                        item.source,
                        "",
                    )
                )
    return "\n".join(output).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find reusable constants, classes, and functions with LibCST."
    )
    parser.add_argument("directory", nargs="?", default=".", help="Scan root.")
    parser.add_argument("-o", "--output", default="utilz.txt", help="Report path.")
    parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="Report format.",
    )
    parser.add_argument(
        "--output-module",
        metavar="PATH",
        help="Also write a draft Python module; source files are never modified.",
    )
    parser.add_argument("--min-uses", type=int, default=2)
    parser.add_argument("--min-files", type=int, default=1)
    parser.add_argument("--min-uses-per-file", type=int, default=1)
    parser.add_argument(
        "--cross-file-only",
        action="store_true",
        help="Count references only from files other than the definition file.",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="Include names beginning with an underscore.",
    )
    parser.add_argument(
        "--include-nested",
        action="store_true",
        help="Include nested classes and functions.",
    )
    parser.add_argument(
        "--all-assignments",
        action="store_true",
        help="Treat lowercase module-level assignments as constants too.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob pattern to exclude; may be repeated.",
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinked directories while scanning.",
    )
    parser.add_argument(
        "--sort",
        choices=("name", "uses", "files"),
        default="name",
        help="Candidate ordering.",
    )
    parser.add_argument("--summary", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with status 1 if candidates are found.",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="Worker process count; set to 1 for sequential processing.",
    )
    parser.add_argument("--log-file", help="Optional Loguru log file path.")
    parser.add_argument("--quiet", action="store_true", help="Suppress console logs.")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="WARNING",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="Do not automatically exclude common virtualenv/cache directories.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger.remove()
    if not args.quiet:
        logger.add(sys.stderr, level=args.log_level)
    if args.log_file:
        logger.add(args.log_file, level=args.log_level, rotation="10 MB")

    if min(args.min_uses, args.min_files, args.min_uses_per_file) < 1:
        logger.error("Usage thresholds must be at least 1")
        return 2
    if args.jobs is not None and args.jobs < 1:
        logger.error("--jobs must be at least 1")
        return 2

    root = Path(args.directory).resolve()
    if not root.is_dir():
        logger.error("Not a directory: {}", root)
        return 2

    defaults = (
        [] if args.no_default_excludes else [".git", ".venv", "venv", "__pycache__"]
    )
    exclusions = defaults + args.exclude
    files = find_files(root, exclusions, args.follow_symlinks)

    tasks = [
        (
            str(path),
            path.relative_to(root).as_posix(),
            args.include_nested,
            args.include_private,
            args.all_assignments,
        )
        for path in files
    ]

    try:
        if args.jobs == 1:
            results = [analyze_file(task) for task in tasks]
        else:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=args.jobs
            ) as executor:
                results = list(executor.map(analyze_file, tasks))
    except Exception:
        logger.exception("Parallel analysis failed")
        return 1

    for result in results:
        if result.error:
            logger.error(
                "Skipping {} after analysis error: {}", result.path, result.error
            )

    candidates, duplicates = get_candidates(
        results,
        args.min_uses,
        args.min_files,
        args.min_uses_per_file,
        args.cross_file_only,
    )
    candidates = sort_candidates(candidates, args.sort)

    if args.format == "json":
        report = json_report(candidates, duplicates, args.summary)
    elif args.format == "csv":
        report = csv_report(candidates)
    else:
        report = text_report(candidates, duplicates, args.summary)

    try:
        atomic_write(Path(args.output).resolve(), report, encoding="utf-8")
        if args.output_module:
            atomic_write(
                Path(args.output_module).resolve(),
                draft_module(candidates),
                encoding="utf-8",
            )
    except OSError:
        logger.exception("Could not write output file")
        return 1

    logger.info(
        "Scanned {} file(s); found {} candidate(s) and {} duplicate definition(s)",
        len(files),
        len(candidates),
        len(duplicates),
    )
    if not args.quiet:
        print(
            f"Scanned {len(files)} Python file(s); found {len(candidates)} candidate(s)."
        )
        print(f"Report written to {Path(args.output).resolve()}.")

    if args.check and candidates:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
