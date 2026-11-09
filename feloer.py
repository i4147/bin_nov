#!/data/data/com.termux/files/usr/bin/env python
"""Write module docstrings for Python files with the felo superagent.
Sends a simplified view of each module to ``felo-sa.mjs``, cleans the answer into a Google-style docstring and inserts it (with ``--force`` even over an existing one).
Options cover the glob to walk, concurrency, delay between calls, a log file and a dry run; files that already fail noisily are skipped.
Usage: feloer [paths ...] [--dry-run] [--force] [--pattern '*.py'] [-j N] Positional arguments: paths ...
One or more files or directories.
If omitted, current directory is processed recursively.
Options: --dry-run -f, --force Force overwrite existing module docstrings.
--pattern Glob pattern used when expanding directories (default: *.py).
--delay -j, --concurrency Number of concurrent workers.
-v, --verbose Show prompts and raw felo output on stderr.
--log-file Examples: $ feloer src/ --dry-run $ feloer notes/ --pattern '*.py' -j 2 Command-line script: run ``python feloer.py`` or the ``feloer`` command.
The module is importable as well."""

import argparse
import ast
import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import fnmatch
from os import environ
from pathlib import Path
import re
import shutil
import sys
import time
from typing import TYPE_CHECKING
import warnings

from dh import DOC_TH1, DOC_TH2, SKIP_DIRS
from loguru import logger


FELO_CLI: Path = Path(environ.get("FELO_CLI", "/data/data/com.termux/files/home/bashbin/felo-sa.mjs"))
TIMEOUT: int = int(environ.get("FELO_TIMEOUT", "300"))
MAX_SIZE: int = 80 * 1024
MAX_ARG_BYTES: int = 120_000
DEFAULT_LOG: str = "felo-docstrings.log"
IGNORED_NAMES: frozenset[str] = frozenset({
    "__init__.py",
    "__main__.py",
    "setup.py",
    "main.py",
    "test.py",
    "conf.py",
    "const.p",
    "constants.py",
    "tests.py",
    "conftest.py",
})
PROMPT_HEAD: str = (
    "You are an expert software requirements writer. Analyze the Python code provided between the "
    "<python_code> tags and write a clear, high-level prompt or specification that can be given to an "
    "AI coding agent to build a similar script from scratch. The prompt must begin with a phrase like "
    '"Write a Python script that..." or "Generate a Python tool that...". Focus strictly on the '
    "high-level purpose, main inputs and outputs, and key behaviors. Do NOT describe the code "
    "line-by-line, and do not restrict low-level implementation details such as specific function "
    "names, loop types, internal variables, or imported libraries, allowing the AI complete creative "
    "freedom. Do not include code snippets. The code is provided inline in this message and is all you "
    "need: never ask for more input, never claim it is truncated or missing (if it looks partial, infer "
    "the purpose from what is visible), and do not search the web or use tools. Output only the "
    "generated prompt in English without explanations or preamble.\n\n<python_code>\n"
)
PROMPT_TAIL: str = "\n</python_code>\n"
ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
FENCE_RE: re.Pattern[str] = re.compile(r"^\s*```.*$", re.MULTILINE)
SHELL_RE: re.Pattern[str] = re.compile(r"^\s*(?:\$|>|>>>)\s.*$", re.MULTILINE)
PREAMBLE_RE: re.Pattern[str] = re.compile(
    r"^(?:sure[,!.]?|certainly[,!.]?|here(?:'s| is)[^\n]*|of course[,!.]?)\s*\n", re.IGNORECASE
)
LIVEDOC_RE: re.Pattern[str] = re.compile(r"^\s*LiveDoc\s*:.*$", re.MULTILINE | re.IGNORECASE)
RECEIVED_RE: re.Pattern[str] = re.compile(r"^\s*Received\s*:", re.MULTILINE | re.IGNORECASE)
AGENT_LINE_RE: re.Pattern[str] = re.compile(r"^\s*(?:SuperAgent|felo|feli)\b.*$", re.MULTILINE | re.IGNORECASE)
LEADING_BLANK_RE: re.Pattern[str] = re.compile(r"^\s*\n+")
SENTENCE_RE: re.Pattern[str] = re.compile(r"(?<=[.!?])\s+")
BLANK_RUN_RE: re.Pattern[str] = re.compile(r"\n{3,}")
CODING_RE: re.Pattern[str] = re.compile(r"^#.*coding[:=]")
STARTER_RE: re.Pattern[str] = re.compile(
    r"^(?:write|generate|create|build|develop|implement|make|design)\b", re.IGNORECASE
)
REFUSAL_MARKERS: tuple[str, ...] = (
    "truncated",
    "cut off",
    "paste",
    "No content in stream",
    "mid-line",
    "can't produce",
    "cannot produce",
    "complete file",
)


@dataclass(frozen=True, slots=True)
class Options:
    dry_run: bool
    force: bool
    delay: float
    concurrency: int
    verbose: bool


@dataclass(frozen=True, slots=True)
class FeloOutput:
    stdout: str
    stderr: str
    returncode: int
    seconds: float


def configure_logging(verbose: bool, log_file: Path) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> <level>{level: <8}</level> {message}",
    )
    logger.add(
        log_file,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {message}",
        encoding="utf-8",
        rotation="5 MB",
        retention=3,
        backtrace=True,
        diagnose=False,
    )


def log_block(label: str, title: str, text: str) -> None:
    logger.debug("[{}] {} ({} chars):\n{}", label, title, len(text), text)


def drop_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


class Simplifier(ast.NodeTransformer):
    def visit_Module(self, node: ast.Module) -> ast.AST:
        node.body = [
            stmt
            for stmt in drop_docstring(node.body)
            if not (isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__")
        ]
        return self.generic_visit(node)

    def simplify_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        node.body = drop_docstring(node.body) or [ast.Pass()]
        node.returns = None
        node.type_params = []
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
            if arg is not None:
                arg.annotation = None
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self.simplify_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self.simplify_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.body = drop_docstring(node.body) or [ast.Pass()]
        node.type_params = []
        return self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        if node.value is not None:
            return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)
        return ast.copy_location(ast.Pass(), node)


def simplify_source(code: str, label: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(code)
        tree = Simplifier().visit(tree)
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)
    except Exception as exc:
        logger.warning("[{}] could not simplify source, sending it as is: {}: {}", label, type(exc).__name__, exc)
        return code


def build_prompt(code: str, label: str) -> str:
    return PROMPT_HEAD + simplify_source(code, label) + PROMPT_TAIL


def clean_response(raw: str) -> tuple[str, str]:
    text = ANSI_RE.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
    match = RECEIVED_RE.search(text)
    if match is not None:
        tail = text[match.end() :]
        tail = tail.split("\n", 1)[1] if "\n" in tail else ""
        text = LEADING_BLANK_RE.sub("", tail)
    text = AGENT_LINE_RE.sub("", text)
    text = FENCE_RE.sub("", text)
    text = SHELL_RE.sub("", text)
    text = PREAMBLE_RE.sub("", text.lstrip())
    text = LIVEDOC_RE.sub("", text).strip()
    if not text:
        return "", "empty after cleaning"
    if "\n" not in text:
        text = "\n".join(part.strip() for part in SENTENCE_RE.split(text) if part.strip())
    text = BLANK_RUN_RE.sub("\n\n", text)
    if STARTER_RE.match(text) is None:
        head = text[:300].lower()
        marker = next((item for item in REFUSAL_MARKERS if item in head), None)
        if marker is not None:
            return "", f"refusal marker {marker!r} in response head"
    return text, ""


def to_docstring(text: str) -> str:
    quote = DOC_TH1[:1]
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace(DOC_TH1, DOC_TH1.replace(quote, "\\" + quote))
    if quote and escaped.endswith(quote):
        escaped = escaped[:-1] + "\\" + quote
    return DOC_TH1 + escaped + DOC_TH1 + "\n\n"


def split_header(source: str) -> tuple[str, str]:
    lines = source.split("\n")
    count = 0
    if lines and lines[0].startswith("#!"):
        count = 1
    if count < len(lines) and CODING_RE.match(lines[count]):
        count += 1
    if count == 0:
        return "", source
    return "\n".join(lines[:count]) + "\n", "\n".join(lines[count:])


def has_docstring_prefix(body: str) -> bool:
    return body.lstrip().startswith((DOC_TH1, DOC_TH2))


def remove_docstring_prefix(body: str) -> str:
    stripped = body.lstrip()
    delimiter = DOC_TH1 if stripped.startswith(DOC_TH1) else DOC_TH2 if stripped.startswith(DOC_TH2) else ""
    if not delimiter:
        return body
    end = stripped.find(delimiter, len(delimiter))
    if end == -1:
        return body
    return stripped[end + len(delimiter) :].lstrip("\r\n")


async def run_felo(prompt: str, label: str) -> FeloOutput:
    size = len(prompt.encode("utf-8"))
    if size > MAX_ARG_BYTES:
        msg = f"prompt too large for a single argv entry: {size} bytes"
        raise ValueError(msg)
    command = [
        "node",
        str(FELO_CLI),
        "--query",
        prompt,
        "--accept-language",
        "en",
        "--timeout",
        str(TIMEOUT),
        "--verbose",
    ]
    log_block(label, f"prompt sent to felo, {size} bytes", prompt)
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT + 10)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    result = FeloOutput(
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        returncode=proc.returncode if proc.returncode is not None else -1,
        seconds=time.perf_counter() - started,
    )
    logger.debug("[{}] felo exit code {} after {:.1f}s", label, result.returncode, result.seconds)
    log_block(label, "felo raw stdout", result.stdout)
    log_block(label, "felo raw stderr", result.stderr)
    if result.returncode != 0:
        msg = f"felo exited {result.returncode}: {result.stderr.strip()[:300]}"
        raise RuntimeError(msg)
    return result


async def process_one(
    path: Path, opts: Options, gate: asyncio.Semaphore, stats: Counter[str], abort: asyncio.Event
) -> None:
    label = path.name
    async with gate:
        if abort.is_set():
            stats["errors"] += 1
            logger.warning("[{}] skipped, run aborted", label)
            return
        if path.name in IGNORED_NAMES:
            stats["skipped_ignored"] += 1
            logger.info("[{}] skipped: ignored filename", label)
            return
        size = path.stat().st_size
        if size > MAX_SIZE:
            stats["skipped_size"] += 1
            logger.info("[{}] skipped: {} bytes exceeds limit", label, size)
            return
        source = path.read_text(encoding="utf-8")
        if not source.strip():
            stats["skipped_empty"] += 1
            logger.info("[{}] skipped: empty file", label)
            return
        header, body = split_header(source)
        if has_docstring_prefix(body):
            if not opts.force:
                stats["skipped_docstring"] += 1
                logger.info("[{}] skipped: docstring exists", label)
                return
            body = remove_docstring_prefix(body)
        logger.info("[{}] querying felo", label)
        try:
            response = await run_felo(build_prompt(source, label), label)
            logger.info(response)
        except FileNotFoundError:
            logger.critical("[{}] node executable not found in PATH, aborting run", label)
            abort.set()
            stats["errors"] += 1
            return
        except TimeoutError:
            logger.error("[{}] felo timed out after {}s", label, TIMEOUT)
            stats["errors"] += 1
            return
        except (RuntimeError, ValueError) as exc:
            logger.error("[{}] {}", label, exc)
            stats["errors"] += 1
            return
        cleaned, reason = clean_response(response.stdout)
        if not cleaned:
            logger.error("[{}] response rejected: {}", label, reason)
            stats["errors"] += 1
            return
        log_block(label, "cleaned response", cleaned)
        logger.info(cleaned)
        result = header + to_docstring(cleaned) + body
        if not opts.dry_run:
            path.write_text(result, encoding="utf-8")
        stats["success"] += 1
        logger.info("[{}] done{}", label, " (dry run)" if opts.dry_run else "")


async def guarded(
    path: Path, opts: Options, gate: asyncio.Semaphore, stats: Counter[str], abort: asyncio.Event
) -> None:
    try:
        await process_one(path, opts, gate, stats, abort)
    except Exception as exc:
        stats["errors"] += 1
        logger.opt(exception=exc).error("[{}] unexpected failure: {}: {}", path.name, type(exc).__name__, exc)


def collect_files(targets: Sequence[str], pattern: str) -> list[Path]:
    self_path = Path(__file__).resolve()
    candidates: list[Path] = []
    for target in targets:
        root = Path(target)
        if root.is_file():
            candidates.append(root)
        elif root.is_dir():
            for current, directories, files in root.walk():
                directories[:] = [name for name in directories if name not in SKIP_DIRS]
                candidates.extend(current / name for name in files if fnmatch.fnmatch(name, pattern))
        else:
            logger.warning("Path not found: {}", target)
    unique = {path.resolve() for path in candidates}
    unique.discard(self_path)
    return sorted(unique)


async def run_all(paths: Sequence[Path], opts: Options) -> Counter[str]:
    gate = asyncio.Semaphore(opts.concurrency)
    abort = asyncio.Event()
    stats: Counter[str] = Counter()
    tasks: list[asyncio.Task[None]] = []
    for path in paths:
        tasks.append(asyncio.create_task(guarded(path, opts, gate, stats, abort)))
        if opts.delay > 0:
            await asyncio.sleep(opts.delay)
    await asyncio.gather(*tasks)
    return stats


def print_summary(stats: Counter[str]) -> None:
    print("\n--- Execution Summary ---")
    print(f"Successfully processed:     {stats['success']}")
    print(f"Skipped (>50KB limit):      {stats['skipped_size']}")
    print(f"Skipped (docstring exists): {stats['skipped_docstring']}")
    print(f"Skipped (ignored filename): {stats['skipped_ignored']}")
    print(f"Skipped (empty file):       {stats['skipped_empty']}")
    print(f"Errors / Empty responses:   {stats['errors']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "paths",
        nargs="*",
        help="One or more files or directories. If omitted, current directory is processed recursively.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-f", "--force", action="store_true", help="Force overwrite existing module docstrings.")
    parser.add_argument(
        "--pattern", default="*.py", help="Glob pattern used when expanding directories (default: *.py)."
    )
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("-j", "--concurrency", type=int, default=4, help="Number of concurrent workers.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show prompts and raw felo output on stderr.")
    parser.add_argument(
        "--log-file", type=Path, default=Path(DEFAULT_LOG), help=f"Debug log file (default: {DEFAULT_LOG})."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    configure_logging(args.verbose, args.log_file)
    if not FELO_CLI.is_file():
        logger.critical("felo CLI not found: {}", FELO_CLI)
        return 2
    if shutil.which("node") is None:
        logger.critical("node executable not found in PATH")
        return 2
    paths = collect_files(args.paths or ["."], args.pattern)
    if not paths:
        print(f"No matching files found (pattern={args.pattern!r}).")
        return 0
    opts = Options(
        dry_run=args.dry_run,
        force=args.force,
        delay=args.delay,
        concurrency=args.concurrency,
        verbose=args.verbose,
    )
    logger.info("{} file(s) queued, log file: {}", len(paths), args.log_file)
    stats = asyncio.run(run_all(paths, opts))
    print_summary(stats)
    logger.info("summary: {}", dict(stats))
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
