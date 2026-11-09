#!/data/data/com.termux/files/usr/bin/env python
"""Remove the comments from source files with tree-sitter, keeping the licences.
Supports many languages through per-language grammars, preserves the copyright headers, the SPDX lines and the lint directives (also the CGo preamble in Go files), validates each result before writing and offers ``--backup``, ``--dry-run``, ``--verbose``, a pool method and a log file.
Usage: uncomment [options] [root] Positional arguments: root Options: -b, --backup -n, --dry-run -v, --verbose --pool-method --chunksize --max-size --skip-dir --log-file Examples: $ uncomment -n -v src/ Command-line script: run ``python uncomment.py`` or the ``uncomment`` command.
The module is importable as well."""

import argparse
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cache
import hashlib
import importlib
import importlib.util
import multiprocessing as mp
from multiprocessing.pool import Pool
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger
from tree_sitter import Language, Node, Parser


Status = Literal["stripped", "unchanged", "skipped", "error"]
PoolMethod = Literal["imap_unordered", "imap", "map", "starmap", "apply_async", "apply"]
Veto = Callable[[Node, bytes], bool]
POOL_METHODS: Final[tuple[PoolMethod, ...]] = (
    "imap_unordered",
    "imap",
    "map",
    "starmap",
    "apply_async",
    "apply",
)
COMMENT_TYPES: Final[frozenset[str]] = frozenset({"comment", "line_comment", "block_comment", "Comment"})
TEXT_TYPES: Final[frozenset[str]] = frozenset({"text", "CharData"})
_COMMON: Final[str] = (
    r"copyright|\(c\)\s*\d|\u00a9|spdx-|all rights reserved|permission is hereby granted"
    r"|licensed (?:under|to)|@license|@preserve|@generated|code generated|auto-?generated"
    r"|do not (?:remove|delete|edit|modify)|nosec|noqa|nolint|\bvim?:|-\*-|coding[:=]"
)
_C_FAMILY: Final[str] = r"\A/\*!|clang-format|clang-tidy|NOLINT|coverity|fallthrough|-Wno-|\bpragma\b"
_JS_FAMILY: Final[str] = (
    r"\A/\*!|\A///\s*<|@cc_on|eslint|@ts-|tslint|jshint|biome-ignore|prettier-ignore"
    r"|istanbul|c8 ignore|v8 ignore|webpack(?:Chunk|Mode|Prefetch|Preload)"
    r"|sourceMappingURL|sourceURL|@jsx|@flow|@vite-ignore|[@#]__(?:PURE|NO_SIDE_EFFECTS)__"
)
_SHELL_FAMILY: Final[str] = r"\A#!|shellcheck|shfmt"


class SkipFile(Exception): ...


class ValidationError(Exception): ...


def _is_cgo_preamble(node: Node, src: bytes) -> bool:
    sibling = node.next_sibling
    while sibling is not None and sibling.type in COMMENT_TYPES:
        sibling = sibling.next_sibling
    return (
        sibling is not None
        and sibling.type == "import_declaration"
        and b'"C"' in src[sibling.start_byte : sibling.end_byte]
    )


@dataclass(frozen=True, slots=True)
class LangSpec:
    module: str
    factory: str
    suffixes: frozenset[str]
    keep: re.Pattern[str]
    veto: Veto | None = None


def _spec(
    module: str,
    factory: str,
    suffixes: str,
    extra: str = "",
    veto: Veto | None = None,
) -> LangSpec:
    pattern = "|".join(part for part in (_COMMON, extra) if part)
    return LangSpec(
        module=module,
        factory=factory,
        suffixes=frozenset(suffixes.split()),
        keep=re.compile(pattern, re.IGNORECASE | re.DOTALL),
        veto=veto,
    )


LANGS: Final[dict[str, LangSpec]] = {
    "bash": _spec("tree_sitter_bash", "language", ".sh .bash", _SHELL_FAMILY),
    "zsh": _spec("tree_sitter_zsh", "language", ".zsh", _SHELL_FAMILY),
    "c": _spec("tree_sitter_c", "language", ".c .h", _C_FAMILY),
    "cpp": _spec("tree_sitter_cpp", "language", ".cc .cpp .cxx .hpp .hh .hxx", _C_FAMILY),
    "css": _spec(
        "tree_sitter_css",
        "language",
        ".css",
        r"\A/\*!|sourceMappingURL|@charset|prettier-ignore|stylelint",
    ),
    "go": _spec(
        "tree_sitter_go",
        "language",
        ".go",
        r"\A//go:|\A//\s*\+build|\A//line |\A//export |#cgo|lint:ignore|\A//\s*Code generated",
        veto=_is_cgo_preamble,
    ),
    "html": _spec(
        "tree_sitter_html",
        "language",
        ".html .htm",
        r"\A<!--\s*\[if|\[endif\]|\A<!--!|prettier-ignore" r"|\A<!--\s*#(?:include|exec|echo|config|set|if|else|endif)",
    ),
    "javascript": _spec("tree_sitter_javascript", "language", ".js .mjs .cjs .jsx", _JS_FAMILY),
    "typescript": _spec("tree_sitter_typescript", "language_typescript", ".ts .mts .cts", _JS_FAMILY),
    "tsx": _spec("tree_sitter_typescript", "language_tsx", ".tsx", _JS_FAMILY),
    "json": _spec("tree_sitter_json", "language", ".json .jsonc"),
    "lua": _spec(
        "tree_sitter_lua",
        "language",
        ".lua",
        r"\A---\s*@|luacheck|stylua|selene|luacov|@diagnostic",
    ),
    "php": _spec(
        "tree_sitter_php",
        "language_php",
        ".php .phtml",
        r"\A#!|phpcs|phpstan|psalm|php-cs-fixer|noinspection|@var|@codeCoverageIgnore",
    ),
    "ruby": _spec(
        "tree_sitter_ruby",
        "language",
        ".rb .rake .gemspec",
        r"\A#!|frozen_string_literal|rubocop|:nodoc:|:nocov:|typed:|sorbet|steep:"
        r"|shareable_constant_value|warn_indent",
    ),
    "rust": _spec(
        "tree_sitter_rust",
        "language",
        ".rs",
        r"rustfmt|clippy|SAFETY:|cbindgen",
    ),
    "toml": _spec("tree_sitter_toml", "language", ".toml", r"taplo|\A#:schema|:schema"),
    "xml": _spec(
        "tree_sitter_xml",
        "language_xml",
        ".xml .xsd .xsl .xslt .svg",
        r"\A<!--!|\A<!--\s*\[if|noinspection|xmllint",
    ),
    "vim": _spec("tree_sitter_vim", "language", ".vim"),
}
AVAILABLE: Final[dict[str, LangSpec]] = {
    name: spec for name, spec in LANGS.items() if importlib.util.find_spec(spec.module) is not None
}
SUFFIX_TO_LANG: Final[dict[str, str]] = {suffix: name for name, spec in AVAILABLE.items() for suffix in spec.suffixes}


class Task(NamedTuple):
    path: Path
    backup: bool
    max_size: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class Outcome:
    path: str
    status: Status
    before: int = 0
    after: int = 0
    detail: str = ""

    @property
    def saved(self) -> int:
        return self.before - self.after


@dataclass(slots=True)
class Summary:
    counts: Counter[str] = field(default_factory=Counter)
    freed: int = 0
    stripped_before: int = 0

    def record(self, outcome: Outcome) -> None:
        self.counts[outcome.status] += 1
        if outcome.status == "stripped":
            self.freed += outcome.saved
            self.stripped_before += outcome.before


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError


def iter_files(root: Path, skip_dirs: frozenset[str]) -> Iterator[Path]:
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in skip_dirs:
                        stack.append(entry)
                elif entry.is_file() and entry.suffix.lower() in SUFFIX_TO_LANG:
                    yield entry
        except OSError as exc:
            logger.error("{} | cannot list directory: {}", current, exc)


@cache
def _parser(name: str) -> Parser:
    spec = AVAILABLE[name]
    module = importlib.import_module(spec.module)
    return Parser(Language(getattr(module, spec.factory)()))


def walk(root: Node) -> Iterator[Node]:
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        yield node
        if node.type not in COMMENT_TYPES:
            stack.extend(reversed(node.children))


def _flush(digest: hashlib._Hash, pending: list[bytes]) -> None:
    if pending:
        digest.update(b"T\0" + b" ".join(b"".join(pending).split()) + b"\0")
        pending.clear()


def fingerprint(root: Node, src: bytes) -> bytes:
    digest = hashlib.blake2b(digest_size=16)
    pending: list[bytes] = []
    for node in walk(root):
        if node.type in COMMENT_TYPES or node.child_count:
            continue
        chunk = src[node.start_byte : node.end_byte]
        if node.type in TEXT_TYPES:
            pending.append(chunk)
            continue
        _flush(digest, pending)
        digest.update(node.type.encode() + b"\0" + chunk + b"\0")
    _flush(digest, pending)
    return digest.digest()


def _expand(src: bytes, start: int, end: int) -> tuple[int, int]:
    line_start = src.rfind(b"\n", 0, start) + 1
    newline = src.find(b"\n", end)
    line_end = len(src) if newline == -1 else newline
    head = src[line_start:start]
    tail = src[end:line_end]
    if tail.strip():
        return start, end
    if head.strip():
        return start - (len(head) - len(head.rstrip())), end
    return line_start, min(line_end + 1, len(src))


def removal_spans(root: Node, src: bytes, spec: LangSpec) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    floor = 0
    for node in walk(root):
        if node.type not in COMMENT_TYPES:
            continue
        start, end = node.start_byte, node.end_byte
        while end > start and src[end - 1] in b"\r\n":
            end -= 1
        text = src[start:end].decode("utf-8", "replace")
        if spec.keep.search(text) or (spec.veto is not None and spec.veto(node, src)):
            continue
        start, end = _expand(src, start, end)
        start = max(start, floor)
        if start >= end:
            continue
        spans.append((start, end))
        floor = end
    return spans


def strip_comments(data: bytes, lang: str) -> bytes:
    spec = AVAILABLE[lang]
    parser = _parser(lang)
    root = parser.parse(data).root_node
    if root.has_error:
        msg = "source has syntax errors"
        raise SkipFile(msg)
    spans = removal_spans(root, data, spec)
    if not spans:
        return data
    view = memoryview(data)
    parts: list[memoryview] = []
    cursor = 0
    for start, end in spans:
        parts.append(view[cursor:start])
        cursor = end
    parts.append(view[cursor:])
    result = b"".join(parts)
    new_root = parser.parse(result).root_node
    if new_root.has_error:
        msg = "stripped result has syntax errors"
        raise ValidationError(msg)
    if fingerprint(new_root, result) != fingerprint(root, data):
        msg = "stripped result changed the token stream"
        raise ValidationError(msg)
    return result


def commit(path: Path, payload: bytes, backup: bool, ref: os.stat_result) -> None:
    current = path.stat()
    if (current.st_mtime_ns, current.st_size) != (ref.st_mtime_ns, ref.st_size):
        msg = "file modified concurrently"
        raise SkipFile(msg)
    if backup:
        shutil.copy2(path, path.with_name(f"{path.name}.bak"))
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def process_file(path: Path, backup: bool, max_size: int, dry_run: bool) -> Outcome:
    name = str(path)
    try:
        if path.is_symlink():
            msg = "symlink"
            raise SkipFile(msg)
        lang = SUFFIX_TO_LANG[path.suffix.lower()]
        ref = path.stat()
        if not 0 < ref.st_size <= max_size:
            msg = f"size {ref.st_size} outside accepted range"
            raise SkipFile(msg)
        data = path.read_bytes()
        if b"\0" in data:
            msg = "binary content"
            raise SkipFile(msg)
        result = strip_comments(data, lang)
        if result == data:
            return Outcome(name, "unchanged", len(data), len(data))
        if not dry_run:
            commit(path, result, backup, ref)
        return Outcome(name, "stripped", len(data), len(result))
    except SkipFile as exc:
        return Outcome(name, "skipped", detail=str(exc))
    except Exception as exc:
        return Outcome(name, "error", detail=f"{type(exc).__name__}: {exc}")


def _worker(task: Task) -> Outcome:
    return process_file(*task)


def run_pool(pool: Pool, method: PoolMethod, tasks: Iterable[Task], chunksize: int) -> Iterator[Outcome]:
    match method:
        case "imap_unordered":
            yield from pool.imap_unordered(_worker, tasks, chunksize)
        case "imap":
            yield from pool.imap(_worker, tasks, chunksize)
        case "map":
            yield from pool.map(_worker, tasks, chunksize)
        case "starmap":
            yield from pool.starmap(process_file, tasks, chunksize)
        case "apply_async":
            pending = [pool.apply_async(process_file, task) for task in tasks]
            for result in pending:
                yield result.get()
        case "apply":
            for task in tasks:
                yield pool.apply(process_file, task)
        case _:
            msg = f"unknown pool method: {method}"
            raise ValueError(msg)


def report(outcome: Outcome) -> None:
    match outcome:
        case Outcome(status="stripped"):
            logger.info(
                "{} | -{} B ({} -> {} B)",
                outcome.path,
                outcome.saved,
                outcome.before,
                outcome.after,
            )
        case Outcome(status="skipped"):
            logger.debug("{} | skipped: {}", outcome.path, outcome.detail)
        case Outcome(status="error"):
            logger.error("{} | {}", outcome.path, outcome.detail)
        case _:
            logger.debug("{} | unchanged", outcome.path)


def configure_logging(verbose: bool, log_file: Path | None) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="{time:HH:mm:ss} | {level: <8} | {message}",
    )
    if log_file is not None:
        logger.add(log_file, level="DEBUG", rotation="10 MB", encoding="utf-8")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="strip_comments")
    parser.add_argument("root", nargs="?", type=Path, default=Path())
    parser.add_argument("-b", "--backup", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--pool-method", choices=POOL_METHODS, default="imap_unordered")
    parser.add_argument("--chunksize", type=int, default=8)
    parser.add_argument("--max-size", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--skip-dir", action="append", default=[])
    parser.add_argument("--log-file", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    root: Path = args.root.resolve()
    if not root.is_dir():
        logger.error("{} is not a directory", root)
        return 2
    skip_dirs = SKIP_DIRS | frozenset(args.skip_dir)
    logger.info("languages: {}", ", ".join(sorted(AVAILABLE)) or "none")
    tasks = (Task(path, args.backup, args.max_size, args.dry_run) for path in iter_files(root, skip_dirs))
    summary = Summary()
    with mp.Pool(processes=max(1, MAX_WORKERS)) as pool:
        for outcome in run_pool(pool, args.pool_method, tasks, args.chunksize):
            report(outcome)
            summary.record(outcome)
    logger.info(
        "stripped={} unchanged={} skipped={} errors={}",
        summary.counts["stripped"],
        summary.counts["unchanged"],
        summary.counts["skipped"],
        summary.counts["error"],
    )
    logger.info(
        "{}freed {} of {} in stripped files",
        "[dry-run] would have " if args.dry_run else "",
        human(summary.freed),
        human(summary.stripped_before),
    )
    return int(summary.counts["error"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
