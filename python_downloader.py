#!/data/data/com.termux/files/usr/bin/env python
"""A pip-style download manager for the current directory.
Probes each URL for the size and the name, resumes from the ``.part`` files, splits the big files over four range requests, shows the ``pip`` progress bar and supports the stdlib, requests or pycurl backends with ``-F`` to re-download the existing files.
Usage: python_downloader URL [...] [-f URLS.txt] [-b BACKEND] [-F] Positional arguments: urls ...
URL(s) to download Options: -f, --file Read URLs from FILE (one per line, # comments) -o, --output Output file (single URL only) -b, --backend HTTP backend to use (default: pure / stdlib) --no-resume Ignore partial .part files and restart from scratch -F, --force Re-download even if the target already exists --timeout Socket timeout in seconds (default: 30) -q, --quiet Suppress output -V, --version Examples: $ python_downloader -f urls.txt -b requests Command-line script: run ``python python_downloader.py`` or the ``python_downloader`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
from typing import Any, Iterable, Iterator, Optional, Self, Sequence
import urllib.parse
import urllib.request

from dh import MAX_WORKERS


__version__: str = "2.0.0"
CHUNK: int = 64 * 1024
BIG_FILE: int = 50 * 1024 * 1024
CHUNK_PARTS: int = 4
UA: str = f"dl/{__version__} (pip-style download manager)"
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
    name = Path(urllib.parse.urlparse(url).path).name or url
    return name if len(name) <= n else name[: n - 1] + "…"


class Bar:
    def __init__(self, label: str, total: int | None = None) -> None:
        self.label = label
        self.total = total
        self.done = 0
        self.speed = 0.0
        self.spin = 0
        self.finished = False
        self.failed = False
        self.error: str | None = None
        self.start = time.monotonic()
        self._last_t = self.start
        self._last_d = 0
        self._lock = threading.Lock()

    def add_progress(self, n: int) -> None:
        with self._lock:
            self.done += n

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
        if self.failed:
            return f"{self.label}: error: {self.error}"
        if self.finished:
            el = self.elapsed
            rate = self.done / el if el > 0 else 0.0
            return f"{self.label}  {fmt_size(self.done)} in {fmt_time(el)} ({fmt_size(rate)}/s)"
        self._tick()
        if self.total:
            frac: float | None = self.done / self.total
            head = fmt_pair(self.done, self.total)
            parts = [head, f"{fmt_size(self.speed)}/s", f"eta {fmt_time(self._eta())}"]
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
    def __init__(self, stream=None, enabled: bool | None = None, final: bool = True) -> None:
        self.stream = stream if stream is not None else sys.stdout
        if enabled is None:
            enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self.enabled = enabled
        self.final = final
        self.bars: list[Bar] = []
        self.lock = threading.RLock()
        self._drawn = 0
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

    def __exit__(self, *exc: Any) -> bool:
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
        out = []
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


class StreamResponse:
    status: int = 0
    headers: dict = {}

    def chunks(self) -> Iterator[bytes]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class Backend:
    name = "base"

    def open(
        self,
        url: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
        timeout: float = 30.0,
    ) -> StreamResponse:
        raise NotImplementedError


class _UrllibStream(StreamResponse):
    def __init__(self, resp: Any) -> None:
        self._resp = resp
        self.status = getattr(resp, "status", 200) or 200
        self.headers = {k.lower(): v for k, v in resp.headers.items()}

    def chunks(self):
        while True:
            data = self._resp.read(CHUNK)
            if not data:
                return
            yield data

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._resp.close()


class PureBackend(Backend):
    name = "pure"

    def open(self, url: str, start=None, end=None, timeout=30.0) -> _UrllibStream:
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if start is not None or end is not None:
            rng = f"bytes={start or 0}-"
            if end is not None:
                rng += str(end)
            headers["Range"] = rng
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        return _UrllibStream(resp)


class _RequestsStream(StreamResponse):
    def __init__(self, r: int) -> None:
        self._r = r
        self.status = r.status_code
        self.headers = {k.lower(): v for k, v in r.headers.items()}

    def chunks(self):
        for data in self._r.iter_content(CHUNK):
            if data:
                yield data

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._r.close()


class RequestsBackend(Backend):
    name = "requests"

    def __init__(self) -> None:
        import requests

        self._requests = requests

    def open(self, url: str, start=None, end=None, timeout=30.0) -> _RequestsStream:
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if start is not None or end is not None:
            rng = f"bytes={start or 0}-"
            if end is not None:
                rng += str(end)
            headers["Range"] = rng
        r = self._requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True)
        return _RequestsStream(r)


class _PycurlStream(StreamResponse):
    def __init__(self, pycurl_mod: Any, url: str, start: int, end: int, timeout: float) -> None:
        self._pycurl = pycurl_mod
        self.status: int | None = None
        self.headers: dict = {}
        self._queue: queue.Queue = queue.Queue(maxsize=16)
        self._error: BaseException | None = None
        self._cancel = threading.Event()
        self._header_done = threading.Event()
        self._thread_done = threading.Event()
        self._header_buf = bytearray()
        self._thread = threading.Thread(target=self._run, args=(url, start, end, timeout), daemon=True)
        self._thread.start()
        if not self._header_done.wait(timeout=timeout):
            self._cancel.set()
            msg = "pycurl: no response headers"
            raise TimeoutError(msg)
        if self.status is None:
            msg = "pycurl: no HTTP status"
            raise IOError(msg)

    def _write_cb(self, data: bytes) -> int:
        if self._cancel.is_set() or STOP.is_set():
            return 0
        self._queue.put(data)
        return len(data)

    def _header_cb(self, line: bytes) -> int:
        if line.startswith(b"HTTP/"):
            self._header_buf.clear()
            self._header_buf.extend(line)
        elif line in (b"\r\n", b"\n"):
            self._parse_headers()
            if self.status and (self.status < 300 or self.status >= 400):
                self._header_done.set()
        else:
            self._header_buf.extend(line)
        return len(line)

    def _parse_headers(self) -> None:
        text = bytes(self._header_buf).decode("iso-8859-1", "replace")
        lines = text.splitlines()
        if not lines:
            return
        m = re.match(r"HTTP/\S+\s+(\d+)", lines[0])
        if m:
            self.status = int(m.group(1))
        self.headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                self.headers[k.strip().lower()] = v.strip()

    def _run(self, url: str, start: int, end: int, timeout: float) -> None:
        c = self._pycurl.Curl()
        try:
            c.setopt(c.URL, url)
            c.setopt(c.WRITEFUNCTION, self._write_cb)
            c.setopt(c.HEADERFUNCTION, self._header_cb)
            c.setopt(c.FOLLOWLOCATION, True)
            c.setopt(c.CONNECTTIMEOUT, int(min(timeout, 30)))
            c.setopt(c.LOW_SPEED_LIMIT, 1)
            c.setopt(c.LOW_SPEED_TIME, int(timeout))
            c.setopt(c.USERAGENT, UA)
            c.setopt(c.HTTPHEADER, ["Accept-Encoding: identity"])
            if start is not None or end is not None:
                rng = f"{start or 0}-"
                if end is not None:
                    rng += str(end)
                c.setopt(c.RANGE, rng)
            c.perform()
        except BaseException as e:
            self._error = e
        finally:
            self._header_done.set()
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(None)
                except Exception:
                    pass
            self._thread_done.set()
            with contextlib.suppress(Exception):
                c.close()

    def chunks(self) -> Iterator[bytes]:
        while True:
            item = self._queue.get()
            if item is None:
                if self._error:
                    raise self._error
                return
            yield item

    def close(self) -> None:
        if self._thread_done.is_set() and self._queue.empty():
            return
        self._cancel.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._thread_done.is_set():
                    break
                continue
            if item is None:
                break
        self._thread_done.wait(timeout=1.0)


class PycurlBackend(Backend):
    name = "pycurl"

    def __init__(self) -> None:
        import pycurl

        self._pycurl = pycurl

    def open(self, url: str, start=None, end=None, timeout=30.0) -> _PycurlStream:
        return _PycurlStream(self._pycurl, url, start, end, timeout)


def make_backend(name: str) -> Backend:
    if name == "pure":
        return PureBackend()
    if name == "requests":
        return RequestsBackend()
    if name == "pycurl":
        return PycurlBackend()
    msg = f"unknown backend: {name}"
    raise ValueError(msg)


_CD_STAR = re.compile(r"filename\*\s*=\s*[^']*''([^;]+)", re.IGNORECASE)
_CD_QUOTED = re.compile(r'filename\s*=\s*"([^"]*)"', re.IGNORECASE)
_CD_BARE = re.compile(r"filename\s*=\s*([^;]+)", re.IGNORECASE)


def _sanitize(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip().strip('"')
    return "" if name in ("", ".", "..") else name


def guess_filename(url: str, headers: dict) -> str:
    cd = headers.get("content-disposition") if headers else None
    if cd:
        for pattern in (_CD_STAR, _CD_QUOTED, _CD_BARE):
            m = pattern.search(cd)
            if m:
                name = _sanitize(urllib.parse.unquote(m.group(1)))
                if name:
                    return name
    path = urllib.parse.urlparse(url).path
    return _sanitize(urllib.parse.unquote(Path(path).name)) or "index.html"


class Probe:
    __slots__ = ("filename", "headers", "size", "supports_range")

    def __init__(self, size: int, filename: str, supports_range: Any, headers: Any) -> None:
        self.size = size
        self.filename = filename
        self.supports_range = supports_range
        self.headers = headers


def probe(backend: Backend, url: str, timeout: float) -> Probe:
    stream = backend.open(url, start=0, end=0, timeout=timeout)
    try:
        size: int | None = None
        supports_range = False
        if stream.status == 206:
            supports_range = True
            cr = stream.headers.get("content-range", "")
            m = re.search(r"/(\d+)\s*$", cr)
            if m and m.group(1) != "*":
                size = int(m.group(1))
        elif stream.status == 200:
            cl = stream.headers.get("content-length")
            if cl and cl.isdigit():
                size = int(cl)
            supports_range = stream.headers.get("accept-ranges", "").lower() == "bytes"
        return Probe(
            size,
            guess_filename(url, stream.headers),
            supports_range,
            dict(stream.headers),
        )
    finally:
        stream.close()


def _simple_download(backend: Backend, url: str, part: Path, bar: Bar, timeout: float, offset: int = 0) -> None:
    stream = backend.open(url, start=offset or None, timeout=timeout)
    try:
        if offset and stream.status != 206:
            offset = 0
        cl = stream.headers.get("content-length")
        total = int(cl) + offset if (cl and cl.isdigit()) else None
        bar.total = total
        with bar._lock:
            bar.done = offset
        mode = "ab" if offset else "wb"
        with open(part, mode) as fh:
            for chunk in stream.chunks():
                if STOP.is_set():
                    raise KeyboardInterrupt
                fh.write(chunk)
                bar.add_progress(len(chunk))
        if total is not None and bar.done < total:
            msg = f"truncated download ({bar.done}/{total} bytes)"
            raise IOError(msg)
    finally:
        stream.close()


def _chunked_download(
    backend: Backend,
    url: str,
    part: Path,
    bar: Bar,
    timeout: float,
    size: int,
    nchunks: int = CHUNK_PARTS,
) -> None:
    chunk_size = size // nchunks
    ranges = []
    for i in range(nchunks):
        start = i * chunk_size
        end = start + chunk_size - 1 if i < nchunks - 1 else size - 1
        ranges.append((i, start, end))

    def fetch(idx: int, start: int, end: int) -> Path:
        cp = Path(str(part) + f".{idx}")
        expected = end - start + 1
        have = cp.stat().st_size if cp.exists() else 0
        if have >= expected:
            bar.add_progress(expected)
            return cp
        if have:
            bar.add_progress(have)
        stream = backend.open(url, start=start + have, end=end, timeout=timeout)
        try:
            if stream.status not in (200, 206):
                msg = f"chunk {idx}: unexpected status {stream.status}"
                raise IOError(msg)
            mode = "ab" if have else "wb"
            with open(cp, mode) as fh:
                for chunk in stream.chunks():
                    if STOP.is_set():
                        raise KeyboardInterrupt
                    fh.write(chunk)
                    bar.add_progress(len(chunk))
        finally:
            stream.close()
        got = cp.stat().st_size
        if got != expected:
            msg = f"chunk {idx}: size mismatch ({got}/{expected})"
            raise IOError(msg)
        return cp

    with ThreadPoolExecutor(max_workers=nchunks) as pool:
        parts = list(pool.map(lambda r: fetch(*r), ranges))
    with open(part, "wb") as out:
        for cp in parts:
            with open(cp, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
            cp.unlink()


def download_one(
    backend: Backend,
    url: str,
    dest: Optional[Path],
    bar: Bar,
    timeout: float = 30.0,
    resume: bool = True,
    skip_existing: bool = True,
) -> tuple[Path, bool]:
    try:
        try:
            p = probe(backend, url, timeout)
        except Exception:
            p = Probe(None, guess_filename(url, {}), False, {})
        final = dest if dest is not None else (Path.cwd() / p.filename)
        bar.label = final.name
        if skip_existing and final.exists() and final.stat().st_size > 0:
            with bar._lock:
                bar.done = final.stat().st_size
                bar.total = bar.done
            bar.finished = True
            return final, True
        part = Path(str(final) + ".part")
        if p.size and p.supports_range and p.size > BIG_FILE:
            _chunked_download(backend, url, part, bar, timeout, p.size)
        else:
            offset = 0
            if resume and part.exists():
                offset = part.stat().st_size
                if p.size and offset >= p.size:
                    part.replace(final)
                    with bar._lock:
                        bar.done = p.size
                    bar.total = p.size
                    bar.finished = True
                    return final, False
            _simple_download(backend, url, part, bar, timeout, offset)
        part.replace(final)
        bar.finished = True
        return final, False
    except BaseException as exc:
        bar.failed = True
        bar.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        raise


def read_urls_from_file(path: Path) -> list[str]:
    urls: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dl",
        description="pip-style download manager (current directory only)",
        epilog="example: dl -b requests -f urls.txt -j 8",
    )
    p.add_argument("urls", nargs="*", metavar="URL", help="URL(s) to download")
    p.add_argument(
        "-f",
        "--file",
        metavar="FILE",
        help="read URLs from FILE (one per line, # comments)",
    )
    p.add_argument("-o", "--output", metavar="PATH", help="output file (single URL only)")
    p.add_argument(
        "-b",
        "--backend",
        choices=["pure", "requests", "pycurl"],
        default="pure",
        help="HTTP backend to use (default: pure / stdlib)",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="ignore partial .part files and restart from scratch",
    )
    p.add_argument(
        "-F",
        "--force",
        action="store_true",
        help="re-download even if the target already exists",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECS",
        help="socket timeout in seconds (default: 30)",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="suppress output")
    p.add_argument("-V", "--version", action="version", version=f"dl {__version__}")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    urls = list(args.urls)
    if args.file:
        try:
            urls.extend(read_urls_from_file(Path(args.file)))
        except OSError as exc:
            print(f"dl: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
    if not urls:
        parser.error("no URLs provided (pass URLs or use -f FILE)")
    if args.output and len(urls) > 1:
        parser.error("-o/--output can only be used with a single URL")
    if MAX_WORKERS < 1:
        parser.error("-j/--jobs must be >= 1")
    try:
        backend = make_backend(args.backend)
    except ImportError as exc:
        print(f"dl: backend '{args.backend}' unavailable: {exc}", file=sys.stderr)
        print("      install the package or pick a different -b", file=sys.stderr)
        return 2
    fixed_dest: Optional[Path] = Path(args.output) if args.output else None
    stream = sys.stdout
    progress = Progress(
        stream,
        enabled=(not args.quiet) and stream.isatty(),
        final=not args.quiet,
    )
    bars: list[Bar] = [Bar(shorten(u)) for u in urls]
    for b in bars:
        progress.add(b)
    downloaded: list[Path] = []
    skipped: list[Path] = []
    failed: list[tuple[str, BaseException]] = []
    interrupted = False
    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(urls)))
    try:
        with progress:
            futures = []
            for i, url in enumerate(urls):
                dest = fixed_dest if i == 0 else None
                fut = pool.submit(
                    download_one,
                    backend,
                    url,
                    dest,
                    bars[i],
                    args.timeout,
                    not args.no_resume,
                    not args.force,
                )
                futures.append((fut, url))
            try:
                for fut, url in futures:
                    try:
                        path, was_skipped = fut.result()
                        (skipped if was_skipped else downloaded).append(path)
                    except KeyboardInterrupt:
                        raise
                    except BaseException as exc:
                        failed.append((url, exc))
            except KeyboardInterrupt:
                interrupted = True
                STOP.set()
                for f, _ in futures:
                    f.cancel()
    finally:
        STOP.set()
        pool.shutdown(wait=True, cancel_futures=True)
    elapsed = time.monotonic() - t0
    if not args.quiet:
        if interrupted:
            print("interrupted", file=sys.stderr)
        for url, exc in failed:
            print(f"dl: {url}: {exc}", file=sys.stderr)
        summary = []
        if downloaded:
            summary.append(f"{len(downloaded)} downloaded")
        if skipped:
            summary.append(f"{len(skipped)} skipped")
        if failed:
            summary.append(f"{len(failed)} failed")
        if summary:
            print(f"{', '.join(summary)} in {fmt_time(elapsed)}")
    return 130 if interrupted else (1 if failed else 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
