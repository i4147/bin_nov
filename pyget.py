#!/data/data/com.termux/files/usr/bin/env python
"""A pip-style download manager with progress bars.
Downloads the URLs (or the list of ``-f``) with the stdlib, requests or pycurl backend, picks the chunk size from the size, resumes from the ``.part`` files, derives the name from the URL or the ``Content-Disposition`` and shows the ``pip``-style progress display.
Usage: pyget URL [...] [-f URLS.txt] [-b python|requests|pycurl] [-o NAME] Positional arguments: urls ...
URL(s) to download Options: -f, --file Read URLs from FILE (one per line, '#' comments allowed); may be given multiple times -b, --backend HTTP backend to use (default: python) -o, --output Output filename (single URL only); relative paths use the CWD --no-resume Ignore partial files and restart from scratch --timeout Socket timeout in seconds (default: 30) -q, --quiet Suppress the progress display -V, --version Examples: $ pyget https://example.com/big.iso -b requests Command-line script: run ``python pyget.py`` or the ``pyget`` command.
The module is importable as well."""

from abc import ABC, abstractmethod
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
from typing import Any, Iterator, Self
import urllib.error
import urllib.parse
import urllib.request

from dh import MAX_WORKERS


__version__ = "2.0.0"
CHUNK_SMALL: int = 64 * 1024
CHUNK_MEDIUM: int = 256 * 1024
CHUNK_LARGE: int = 1024 * 1024
BIG_FILE_THRESHOLD: int = 10 * 1024 * 1024
HUGE_FILE_THRESHOLD: int = 100 * 1024 * 1024
USER_AGENT: str = f"dl/{__version__} (pip-style download manager)"
FULL, HEAD, EMPTY = "━", "╸", " "
STOP = threading.Event()


def fmt_size(n: float | None) -> str:
    if n is None:
        return "?"
    if n > 1000 * 1000:
        return f"{n / 1e6:.1f} MB"
    if n > 10 * 1000:
        return f"{n / 1000:.0f} kB"
    if n > 1000:
        return f"{n / 1000:.1f} kB"
    return f"{n:.0f} bytes"


def fmt_pair(done: float, total: float) -> str:
    if total < 1000:
        return f"{done:.0f}/{total:.0f} bytes"
    if total < 1000 * 1000:
        return f"{done / 1e3:.1f}/{total / 1e3:.1f} kB"
    return f"{done / 1e6:.1f}/{total / 1e6:.1f} MB"


def fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:
        return "--:--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def fmt_bar(fraction: float | None, width: int, spin: int = 0) -> str:
    if width <= 0:
        return ""
    if fraction is None:
        block = max(1, width // 5)
        pos = spin % (width + block) - block
        return "".join(FULL if pos <= i < pos + block else EMPTY for i in range(width))
    frac = min(max(fraction, 0.0), 1.0)
    filled = int(frac * width)
    if filled >= width:
        return FULL * width
    return FULL * filled + HEAD + EMPTY * (width - filled - 1)


def shorten(url: str, n: int = 42) -> str:
    path = urllib.parse.urlparse(url).path
    name = Path(path).name or url
    return name if len(name) <= n else name[: n - 1] + "…"


def pick_chunk_size(total: int | None) -> int:
    if total is None:
        return CHUNK_MEDIUM
    if total >= HUGE_FILE_THRESHOLD:
        return CHUNK_LARGE
    if total >= BIG_FILE_THRESHOLD:
        return CHUNK_MEDIUM
    return CHUNK_SMALL


class Bar:
    def __init__(self, label: str, total: int | None = None) -> None:
        self.label: str = label
        self.total: int | None = total
        self.done: int = 0
        self.speed: float = 0.0
        self.spin: int = 0
        self.finished: bool = False
        self.failed: bool = False
        self.skipped: bool = False
        self.error: str | None = None
        self.start: float = time.monotonic()
        self._last_t: float = self.start
        self._last_d: int = 0

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self.start, 1e-6)

    def _tick(self) -> None:
        now = time.monotonic()
        dt = now - self._last_t
        if dt < 0.15:
            return
        inst = (self.done - self._last_d) / dt
        self.speed = inst if self.speed <= 0 else 0.6 * self.speed + 0.4 * inst
        self._last_t, self._last_d = now, self.done

    def _eta(self) -> float | None:
        if not self.total or self.speed <= 0:
            return None
        return max(self.total - self.done, 0) / self.speed

    def render(self, width: int) -> str:
        if self.skipped:
            return f"{self.label}  already downloaded ({fmt_size(self.done)})"
        if self.failed:
            return f"{self.label}: error: {self.error}"
        if self.finished:
            el = self.elapsed
            rate = self.done / el if el > 0 else 0.0
            return f"{self.label}  {fmt_size(self.done)} in {fmt_time(el)} ({fmt_size(rate)}/s)"
        self._tick()
        if self.total:
            frac: float | None = self.done / self.total
            parts = [
                fmt_pair(self.done, self.total),
                f"{fmt_size(self.speed)}/s",
                f"eta {fmt_time(self._eta())}",
            ]
        else:
            frac = None
            parts = [fmt_size(self.done), f"{fmt_size(self.speed)}/s"]
        tail = "  ".join(parts)
        room = width - len(tail) - 1
        prefix = ""
        if len(self.label) + 12 <= room:
            prefix = self.label + " "
            room -= len(prefix)
        if room < 5:
            return tail
        return prefix + fmt_bar(frac, room, self.spin) + " " + tail


class Progress:
    def __init__(
        self,
        stream: Any | None = None,
        enabled: bool | None = None,
        final: bool = True,
    ) -> None:
        self.stream = stream if stream is not None else sys.stdout
        if enabled is None:
            enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self.enabled = enabled
        self.final = final
        self.bars: list[Bar] = []
        self.lock = threading.RLock()
        self._drawn: int = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, bar: Bar) -> None:
        with self.lock:
            self.bars.append(bar)

    def __enter__(self) -> Self:
        if self.enabled:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            self.refresh()

    def refresh(self) -> None:
        with self.lock:
            for b in self.bars:
                b.spin += 1
            self._paint()

    def _width(self) -> int:
        return max(shutil.get_terminal_size((80, 24)).columns - 1, 24)

    def _paint(self) -> None:
        if not self.bars:
            return
        width = self._width()
        lines = [b.render(width) for b in self.bars]
        out: list[str] = []
        if self._drawn:
            out.append(f"\x1b[{self._drawn}A")
        for ln in lines:
            out.append("\x1b[2K" + ln + "\n")
        self.stream.write("".join(out))
        self.stream.flush()
        self._drawn = len(lines)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self.lock:
            if self.enabled and self._drawn:
                out = [f"\x1b[{self._drawn}A"]
                out.extend(["\x1b[2K\n"] * self._drawn)
                out.append(f"\x1b[{self._drawn}A")
                self.stream.write("".join(out))
                self._drawn = 0
            if self.final:
                width = self._width()
                for b in self.bars:
                    self.stream.write(b.render(width) + "\n")
            self.stream.flush()


@dataclass
class StreamInfo:
    status: int
    total_length: int | None
    content_disposition: str | None


class Backend(ABC):
    name: str = "?"

    @abstractmethod
    def open(
        self,
        url: str,
        offset: int,
        timeout: float,
    ) -> Iterator[tuple[StreamInfo, Iterator[bytes]]]:
        raise NotImplementedError


class PurePythonBackend(Backend):
    name = "python"

    @contextmanager
    def open(self, url: str, offset: int, timeout: float) -> Iterator[tuple[StreamInfo, Iterator[bytes]]]:
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            status = getattr(resp, "status", 200)
            length_hdr = resp.headers.get("Content-Length")
            total = int(length_hdr) if length_hdr is not None else None
            info = StreamInfo(
                status=status,
                total_length=total,
                content_disposition=resp.headers.get("Content-Disposition"),
            )
            csize = pick_chunk_size(total)

            def gen() -> Iterator[bytes]:
                while True:
                    if STOP.is_set():
                        return
                    data = resp.read(csize)
                    if not data:
                        return
                    yield data

            yield info, gen()
        finally:
            with suppress(Exception):
                resp.close()


class RequestsBackend(Backend):
    name = "requests"

    def __init__(self) -> None:
        import requests

        self._requests = requests

    @contextmanager
    def open(self, url: str, offset: int, timeout: float) -> Iterator[tuple[StreamInfo, Iterator[bytes]]]:
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        r = self._requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
        )
        try:
            r.raise_for_status()
            length_hdr = r.headers.get("Content-Length")
            total = int(length_hdr) if length_hdr is not None else None
            info = StreamInfo(
                status=r.status_code,
                total_length=total,
                content_disposition=r.headers.get("Content-Disposition"),
            )
            csize = pick_chunk_size(total)

            def gen() -> Iterator[bytes]:
                for chunk in r.iter_content(chunk_size=csize):
                    if STOP.is_set():
                        return
                    if chunk:
                        yield chunk

            yield info, gen()
        finally:
            r.close()


class PycurlBackend(Backend):
    name = "pycurl"

    def __init__(self) -> None:
        import pycurl

        self._pc = pycurl

    @contextmanager
    def open(self, url: str, offset: int, timeout: float) -> Iterator[tuple[StreamInfo, Iterator[bytes]]]:
        pc = self._pc
        q: "queue.Queue[bytes | None]" = queue.Queue()
        headers_ready = threading.Event()
        stop_flag = threading.Event()
        state: dict[str, Any] = {
            "status": None,
            "headers": {},
            "error": None,
        }

        def write_cb(data: bytes) -> int:
            if stop_flag.is_set():
                return 0
            q.put(bytes(data))
            return len(data)

        def header_cb(data: bytes) -> int:
            line = data.decode("latin-1").rstrip("\r\n")
            if line.startswith("HTTP/"):
                state["headers"] = {}
                state["status"] = None
                with suppress(IndexError, ValueError):
                    state["status"] = int(line.split()[1])
            elif line == "":
                st = state["status"]
                if st is not None and not (300 <= st < 400):
                    headers_ready.set()
            elif ":" in line:
                k, v = line.split(":", 1)
                state["headers"][k.strip().lower()] = v.strip()
            return len(data)

        def run() -> None:
            try:
                curl = pc.Curl()
                try:
                    curl.setopt(pc.URL, url)
                    curl.setopt(pc.WRITEFUNCTION, write_cb)
                    curl.setopt(pc.HEADERFUNCTION, header_cb)
                    curl.setopt(pc.TIMEOUT, max(int(timeout), 1))
                    curl.setopt(pc.CONNECTTIMEOUT, max(int(timeout), 1))
                    curl.setopt(pc.FOLLOWLOCATION, True)
                    curl.setopt(pc.USERAGENT, USER_AGENT)
                    curl.setopt(pc.ACCEPT_ENCODING, "")
                    if offset:
                        curl.setopt(pc.HTTPHEADER, [f"Range: bytes={offset}-"])
                    curl.perform()
                finally:
                    curl.close()
            except Exception as exc:
                state["error"] = exc
            finally:
                headers_ready.set()
                q.put(None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        headers_ready.wait(timeout=timeout + 5)
        if state["error"] is not None and state["status"] is None:
            stop_flag.set()
            raise state["error"]
        if state["status"] is None:
            stop_flag.set()
            msg = "no response received"
            raise RuntimeError(msg)
        hdrs = state["headers"]
        length_hdr = hdrs.get("content-length")
        total = int(length_hdr) if length_hdr is not None else None
        info = StreamInfo(
            status=state["status"],
            total_length=total,
            content_disposition=hdrs.get("content-disposition"),
        )

        def gen() -> Iterator[bytes]:
            while True:
                item = q.get()
                if item is None:
                    if state["error"] is not None and not stop_flag.is_set():
                        raise state["error"]
                    return
                yield item

        try:
            yield info, gen()
        finally:
            stop_flag.set()


def get_backend(name: str) -> Backend:
    if name == "python":
        return PurePythonBackend()
    if name == "requests":
        try:
            return RequestsBackend()
        except ImportError as exc:
            msg = "backend 'requests' requires the requests package: pip install requests"
            raise SystemExit(msg) from exc
    if name == "pycurl":
        try:
            return PycurlBackend()
        except ImportError as exc:
            msg = "backend 'pycurl' requires the pycurl package: pip install pycurl"
            raise SystemExit(msg) from exc
    msg = f"unknown backend: {name}"
    raise SystemExit(msg)


_CD_STAR = re.compile(r"filename\*\s*=\s*[^']*''([^;]+)", re.IGNORECASE)
_CD_QUOTED = re.compile(r'filename\s*=\s*"([^"]*)"', re.IGNORECASE)
_CD_BARE = re.compile(r"filename\s*=\s*([^;]+)", re.IGNORECASE)


def _sanitize(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = name.strip().strip('"').strip("'").replace("\x00", "")
    return "" if name in ("", ".", "..") else name


def parse_content_disposition(cd: str | None) -> str | None:
    if not cd:
        return None
    for pattern in (_CD_STAR, _CD_QUOTED, _CD_BARE):
        m = pattern.search(cd)
        if m:
            name = _sanitize(urllib.parse.unquote(m.group(1)))
            if name:
                return name
    return None


def guess_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = _sanitize(urllib.parse.unquote(Path(path).name))
    return name or "index.html"


def filename_from_info(info: StreamInfo, url: str) -> str:
    return parse_content_disposition(info.content_disposition) or guess_from_url(url)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def part_path(outdir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return outdir / f".dl-{digest}.part"


def download_one(
    progress: Progress,
    backend: Backend,
    url: str,
    dest_hint: Path | None,
    outdir: Path,
    resume: bool,
    timeout: float,
) -> tuple[Path, bool]:
    bar = Bar(shorten(url))
    progress.add(bar)
    try:
        part = part_path(outdir, url)
        offset = part.stat().st_size if (resume and part.exists()) else 0
        with backend.open(url, offset, timeout) as response:
            info, chunks = response
            if not (200 <= info.status < 300):
                msg = f"HTTP {info.status}"
                raise RuntimeError(msg)
            if offset > 0 and info.status != 206:
                offset = 0
                with suppress(FileNotFoundError):
                    part.unlink()
            if dest_hint is not None:
                final = dest_hint if dest_hint.is_absolute() else outdir / dest_hint
            else:
                final = outdir / filename_from_info(info, url)
            if final.exists() and final.stat().st_size > 0:
                bar.done = final.stat().st_size
                bar.total = bar.done
                bar.skipped = True
                if part.exists() and offset > 0:
                    with suppress(FileNotFoundError):
                        part.unlink()
                return final, True
            bar.label = final.name
            total = info.total_length
            if total is not None:
                total += offset
            bar.total = total
            bar.done = offset
            mode = "r+b" if offset > 0 else "wb"
            with part.open(mode) as fh:
                if offset > 0:
                    fh.seek(offset)
                for chunk in chunks:
                    if STOP.is_set():
                        raise KeyboardInterrupt
                    if not chunk:
                        continue
                    fh.write(chunk)
                    bar.done += len(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            if total is not None and bar.done < total:
                msg = f"truncated download ({bar.done}/{total} bytes)"
                raise IOError(msg)
        part.replace(final)
        bar.finished = True
        return final, False
    except BaseException as exc:
        bar.failed = True
        bar.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        raise


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dl",
        description="pip-style download manager",
        epilog=(
            "examples:\n"
            "  dl https://example.com/a.iso\n"
            "  dl -b requests -j 4 url1 url2 url3\n"
            "  dl -f urls.txt -b pycurl\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("urls", nargs="*", metavar="URL", help="URL(s) to download")
    p.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        metavar="FILE",
        help="read URLs from FILE (one per line, '#' comments allowed); may be given multiple times",
    )
    p.add_argument(
        "-b",
        "--backend",
        default="python",
        choices=["python", "requests", "pycurl"],
        help="HTTP backend to use (default: python)",
    )
    p.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="output filename (single URL only); relative paths use the CWD",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore partial files and restart from scratch",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECS",
        help="socket timeout in seconds (default: 30)",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress the progress display",
    )
    p.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"dl {__version__}",
    )
    return p


def collect_urls(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    urls: list[str] = list(args.urls)
    for path_str in args.file:
        fpath = Path(path_str)
        if not fpath.is_file():
            parser.error(f"file not found: {path_str}")
        for line in fpath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    urls = collect_urls(args, parser)
    if not urls:
        parser.error("no URLs provided (give them positionally or with -f)")
    if args.output and len(urls) > 1:
        parser.error("-o/--output can only be used with a single URL")
    if MAX_WORKERS < 1:
        parser.error("-j/--jobs must be >= 1")
    outdir = Path.cwd()
    dest_hint: Path | None = None
    if args.output:
        dest_hint = Path(args.output)
        if dest_hint.is_absolute():
            if not dest_hint.parent.is_dir():
                parser.error(f"output directory does not exist: {dest_hint.parent}")
        elif dest_hint.parent != Path() and not (outdir / dest_hint.parent).is_dir():
            parser.error(f"output directory does not exist: {dest_hint.parent}")
    backend = get_backend(args.backend)
    resume = not args.no_resume
    stream = sys.stdout
    progress = Progress(
        stream,
        enabled=(not args.quiet) and bool(getattr(stream, "isatty", lambda: False)()),
        final=not args.quiet,
    )
    results: list[tuple[str, Path | None, bool, BaseException | None]] = []
    interrupted = False
    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(urls)))
    try:
        with progress:
            futures = {
                pool.submit(
                    download_one,
                    progress,
                    backend,
                    url,
                    dest_hint if i == 0 else None,
                    outdir,
                    resume,
                    args.timeout,
                ): url
                for i, url in enumerate(urls)
            }
            try:
                for fut in as_completed(futures):
                    url = futures[fut]
                    try:
                        path, skipped = fut.result()
                        results.append((url, path, skipped, None))
                    except BaseException as exc:
                        results.append((url, None, False, exc))
            except KeyboardInterrupt:
                interrupted = True
                STOP.set()
                for f in futures:
                    f.cancel()
    finally:
        STOP.set()
        pool.shutdown(wait=True, cancel_futures=True)
    elapsed = time.monotonic() - t0
    if not args.quiet:
        downloaded = skipped = failed = 0
        total_bytes = 0
        for url, path, was_skipped, exc in results:
            if exc is not None:
                failed += 1
                print(f"dl: {url}: {exc}", file=sys.stderr)
                continue
            if was_skipped:
                skipped += 1
                continue
            downloaded += 1
            if path is not None:
                with suppress(OSError):
                    total_bytes += path.stat().st_size
        if interrupted:
            print("interrupted", file=sys.stderr)
        parts: list[str] = []
        if downloaded:
            parts.append(f"downloaded {downloaded}")
        if skipped:
            parts.append(f"skipped {skipped}")
        if failed:
            parts.append(f"failed {failed}")
        if parts:
            print(f"{', '.join(parts)} ({fmt_size(total_bytes)} new) in {fmt_time(elapsed)}")
    if interrupted:
        return 130
    if any(exc is not None for _, _, _, exc in results):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
