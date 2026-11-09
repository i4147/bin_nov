#!/data/data/com.termux/files/usr/bin/env python
"""Translate detectable non-English Python comments and strings."""

import argparse
import ast
from dataclasses import dataclass
import json
import multiprocessing
from multiprocessing import Lock, Pool, Value
from pathlib import Path
import re
import time
import tokenize
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from atomicio import atomic_write
from dh import MAX_WORKERS
from loguru import logger


DEFAULT_CHUNK_SIZE: Final[int] = 2500
DEFAULT_DELAY: Final[float] = 1.5
DEFAULT_TIMEOUT: Final[float] = 60.0
MAX_RETRIES: Final[int] = 5
_NON_LATIN_RE: Final[re.Pattern[str]] = re.compile(
    r"[^\x00-\x7f]|[\u0400-\u04ff\u0600-\u06ff\u0900-\u097f" r"\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
)
_NON_ENGLISH_WORDS: Final[frozenset[str]] = frozenset({
    " el ",
    " la ",
    " los ",
    " las ",
    " una ",
    " uno ",
    " que ",
    " para ",
    " con ",
    " por ",
    " como ",
    " del ",
    " der ",
    " die ",
    " das ",
    " und ",
    " nicht ",
    " ist ",
    " ein ",
    " eine ",
    " des ",
    " les ",
    " une ",
    " dans ",
    " pour ",
    " avec ",
    " est ",
    " de ",
    " en ",
    " un ",
    " uma ",
    " não ",
    " não",
    " são ",
    " com ",
    " هذا ",
    " هذه ",
    " من ",
    " إلى ",
    " و ",
})
_WORKER_BACKEND: str
_WORKER_LIBRE_URL: str
_WORKER_TIMEOUT: float
_WORKER_DELAY: float
_WORKER_NEXT_REQUEST: Value
_WORKER_RATE_LOCK: Lock


@dataclass(frozen=True)
class TextEdit:
    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class TranslationJob:
    text: str
    start: int
    end: int


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate detectable non-English Python comments and strings.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Python files or directories; with no arguments, recurse from the current directory.",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=("google", "libretranslate"),
        default="google",
        help="Translation backend. Default: google.",
    )
    parser.add_argument(
        "--libre-url",
        default="http://127.0.0.1:5000/translate",
        help="LibreTranslate endpoint.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Maximum extracted text chunk size. Default: 2500.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Minimum delay between translation requests globally. Default: 1.5 seconds.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="HTTP timeout in seconds. Default: 60.",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="Input/output encoding. Default: utf-8.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report changes without writing files.",
    )
    return parser.parse_args()


def discover_python_files(inputs: list[Path]) -> list[Path]:
    roots = inputs or [Path.cwd()]
    discovered: set[Path] = set()
    for root in roots:
        path = root.expanduser()
        if not path.exists():
            msg = f"Input path does not exist: {path}"
            raise FileNotFoundError(msg)
        if path.is_file():
            if path.suffix == ".py":
                discovered.add(path.resolve())
        elif path.is_dir():
            discovered.update(candidate.resolve() for candidate in path.rglob("*.py") if candidate.is_file())
        else:
            msg = f"Input path is neither a regular file nor directory: {path}"
            raise OSError(msg)
    return sorted(discovered)


def contains_probable_non_english(text: str) -> bool:
    if not text.strip():
        return False
    if _NON_LATIN_RE.search(text):
        return True
    normalized = f" {text.casefold()} "
    return any(marker in normalized for marker in _NON_ENGLISH_WORDS)


def split_text(text: str, limit: int) -> list[tuple[int, int, str]]:
    if limit <= 0:
        msg = "chunk size must be greater than zero"
        raise ValueError(msg)
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = max(text.rfind(" ", start, end), text.rfind("\n", start, end))
            if boundary > start:
                end = boundary
        piece = text[start:end]
        if piece:
            chunks.append((start, end, piece))
        start = end
    return chunks


def position_to_offset(line_offsets: list[int], row: int, column: int) -> int:
    return line_offsets[row - 1] + column


def token_payload_bounds(token_text: str, token_start: int) -> tuple[int, int] | None:
    match = re.match(r"(?is)^([rubf]*)(\"\"\"|'''|\"|')", token_text)
    if match is None:
        return None
    prefix_end = match.end(1)
    quote = match.group(2)
    payload_start = prefix_end + len(quote)
    if token_text.endswith(quote):
        payload_end = len(token_text) - len(quote)
    else:
        payload_end = len(token_text)
    if payload_end < payload_start:
        return None
    return token_start + payload_start, token_start + payload_end


def extract_jobs(
    source: str,
    chunk_size: int,
) -> list[TranslationJob]:
    lines = source.splitlines(keepends=True)
    line_offsets: list[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line)
    if not lines:
        line_offsets.append(0)
    jobs: list[TranslationJob] = []
    tokens = tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__)
    for token in tokens:
        if token.type == tokenize.COMMENT:
            absolute_start = position_to_offset(line_offsets, token.start[0], token.start[1])
            marker_end = absolute_start + 1
            text_start = marker_end
            text = source[text_start : position_to_offset(line_offsets, token.end[0], token.end[1])]
            if contains_probable_non_english(text):
                for relative_start, relative_end, piece in split_text(text, chunk_size):
                    jobs.append(
                        TranslationJob(
                            piece,
                            text_start + relative_start,
                            text_start + relative_end,
                        )
                    )
        elif token.type == tokenize.STRING:
            token_start = position_to_offset(line_offsets, token.start[0], token.start[1])
            token_end = position_to_offset(line_offsets, token.end[0], token.end[1])
            token_text = source[token_start:token_end]
            prefix_match = re.match(r"(?i)^([rubf]*)", token_text)
            prefix = prefix_match.group(1) if prefix_match else ""
            if "f" in prefix.casefold():
                logger.warning(
                    "Skipping f-string at line {} because translating it safely requires parsing embedded expressions.",
                    token.start[0],
                )
                continue
            if "b" in prefix.casefold():
                logger.warning(
                    "Skipping bytes literal at line {} because its content is not text.",
                    token.start[0],
                )
                continue
            bounds = token_payload_bounds(token_text, token_start)
            if bounds is None:
                continue
            payload_start, payload_end = bounds
            payload = source[payload_start:payload_end]
            if contains_probable_non_english(payload):
                for relative_start, relative_end, piece in split_text(payload, chunk_size):
                    jobs.append(
                        TranslationJob(
                            piece,
                            payload_start + relative_start,
                            payload_start + relative_end,
                        )
                    )
    return jobs


def acquire_rate_slot() -> None:
    while True:
        with _WORKER_RATE_LOCK:
            now = time.monotonic()
            wait_for = _WORKER_NEXT_REQUEST.value - now
            if wait_for <= 0:
                _WORKER_NEXT_REQUEST.value = now + _WORKER_DELAY
                return
        time.sleep(min(wait_for, 0.25))


def http_json_request(
    request: Request,
    timeout: float,
) -> object:
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def translate_google(text: str, timeout: float) -> str:
    query = urlencode({
        "client": "gtx",
        "sl": "auto",
        "tl": "en",
        "dt": "t",
        "q": text,
    })
    request = Request(
        f"https://translate.googleapis.com/translate_a/single?{query}",
        headers={"User-Agent": "python-translation-script/1.0"},
    )
    payload = http_json_request(request, timeout)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        msg = "Google returned an unexpected response"
        raise ValueError(msg)
    translated = "".join(item[0] for item in payload[0] if isinstance(item, list) and item and isinstance(item[0], str))
    if not translated:
        msg = "Google returned empty translation"
        raise ValueError(msg)
    return translated


def translate_libretranslate(text: str, url: str, timeout: float) -> str:
    body = json.dumps({"q": text, "source": "auto", "target": "en", "format": "text"}).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "python-translation-script/1.0",
        },
    )
    payload = http_json_request(request, timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("translatedText"), str):
        msg = "LibreTranslate returned an unexpected response"
        raise ValueError(msg)
    translated = payload["translatedText"]
    if not translated:
        msg = "LibreTranslate returned empty translation"
        raise ValueError(msg)
    return translated


def translate_with_retry(text: str) -> str:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            acquire_rate_slot()
            if _WORKER_BACKEND == "google":
                return translate_google(text, _WORKER_TIMEOUT)
            return translate_libretranslate(text, _WORKER_LIBRE_URL, _WORKER_TIMEOUT)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            last_error = error
            wait = min(60.0, 2.0**attempt * max(_WORKER_DELAY, 1.0))
            logger.warning(
                "Translation request failed on attempt {}/{}: {}. Retrying in {:.1f}s.",
                attempt + 1,
                MAX_RETRIES,
                error,
                wait,
            )
            time.sleep(wait)
    msg = f"Translation failed after {MAX_RETRIES} attempts: {last_error}"
    raise RuntimeError(msg)


def worker_initializer(
    backend: str,
    libre_url: str,
    timeout: float,
    delay: float,
    next_request: Value,
    rate_lock: Lock,
) -> None:
    global _WORKER_BACKEND
    global _WORKER_LIBRE_URL
    global _WORKER_TIMEOUT
    global _WORKER_DELAY
    global _WORKER_NEXT_REQUEST
    global _WORKER_RATE_LOCK
    _WORKER_BACKEND = backend
    _WORKER_LIBRE_URL = libre_url
    _WORKER_TIMEOUT = timeout
    _WORKER_DELAY = delay
    _WORKER_NEXT_REQUEST = next_request
    _WORKER_RATE_LOCK = rate_lock


def translate_job(job: TranslationJob) -> tuple[TranslationJob, str]:
    return job, translate_with_retry(job.text)


def apply_translations(
    source: str,
    results: list[tuple[TranslationJob, str]],
) -> str:
    edits = [TextEdit(job.start, job.end, translated) for job, translated in results]
    edits.sort(key=lambda edit: edit.start, reverse=True)
    previous_start = len(source) + 1
    output = source
    for edit in edits:
        if edit.end > previous_start or edit.start < 0 or edit.end < edit.start:
            msg = "Overlapping or invalid translation edit detected"
            raise ValueError(msg)
        output = output[: edit.start] + edit.replacement + output[edit.end :]
        previous_start = edit.start
    return output


def validate_python(source: str, path: Path) -> None:
    try:
        ast.parse(source, filename=str(path))
        compile(source, str(path), "exec")
    except SyntaxError as error:
        msg = f"Translated source is invalid for {path}: {error}"
        raise SyntaxError(msg) from error


def process_file(
    path: Path,
    pool: Pool,
    encoding: str,
    dry_run: bool,
    chunk_size: int,
) -> bool:
    logger.info("Reading {}", path)
    source = path.read_text(encoding=encoding)
    jobs = extract_jobs(source, chunk_size)
    if not jobs:
        logger.info("No probable non-English text found in {}", path)
        return False
    logger.info("Translating {} chunks in {}", len(jobs), path)
    async_results = [pool.apply_async(translate_job, (job,)) for job in jobs]
    results = [result.get() for result in async_results]
    translated_source = apply_translations(source, results)
    validate_python(translated_source, path)
    if translated_source == source:
        logger.info("No changes required for {}", path)
        return False
    if dry_run:
        logger.info("Validated changes for {} (dry run; not written)", path)
    else:
        atomic_write(path, translated_source, encoding)
        logger.success("Updated {}", path)
    return True


def main() -> int:
    args = parse_arguments()
    if args.chunk_size <= 0:
        msg = "--chunk-size must be greater than zero"
        raise ValueError(msg)
    if args.delay < 0:
        msg = "--delay cannot be negative"
        raise ValueError(msg)
    if args.timeout <= 0:
        msg = "--timeout must be greater than zero"
        raise ValueError(msg)
    files = discover_python_files(args.paths)
    logger.info("Discovered {} Python file(s)", len(files))
    if not files:
        logger.warning("No Python files found")
        return 0
    manager = multiprocessing.Manager()
    next_request = manager.Value("d", 0.0)
    rate_lock = manager.Lock()
    changed = 0
    failed = 0
    try:
        with Pool(
            processes=MAX_WORKERS,
            initializer=worker_initializer,
            initargs=(
                args.backend,
                args.libre_url,
                args.timeout,
                args.delay,
                next_request,
                rate_lock,
            ),
        ) as pool:
            for path in files:
                try:
                    if process_file(
                        path,
                        pool,
                        args.encoding,
                        args.dry_run,
                        args.chunk_size,
                    ):
                        changed += 1
                except Exception:
                    failed += 1
                    logger.exception("Failed to process {}", path)
    finally:
        manager.shutdown()
    logger.info(
        "Finished: {} file(s) changed, {} file(s) failed",
        changed,
        failed,
    )
    if failed:
        logger.error("One or more files failed; no failure was silently ignored")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        raise SystemExit(130)
    except Exception:
        logger.exception("Fatal error")
        raise SystemExit(1)
