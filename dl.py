#!/data/data/com.termux/files/usr/bin/env python
"""
Unified downloader with wget, check and size subcommands.

Subcommands:
``wget`` downloads the URLs of a list
(or the one given) with urllib,requests or pycurl,
``check`` tests whether the links are alive and
``size`` prints the remote sizes without downloading;
``--download-small`` fetches the files under the size limit,
``--filter-ext`` restricts the extension and
``--update-file`` rewrites the list with only the failed URLs.

Usage:
  dl {wget,check,size} [options] [URL|URLS_FILE]

Positional arguments:
url input URL or file of URLs output sha256

Subcommands:
wget Single-URL download with progress
check Check remote size, optionally download
size Show remote size
batch Batch download URLs from a file
threaded Multi-chunk threaded downloader
chunked Resumable chunked downloader with SHA-256

Options:
  -o, --output Output file or directory
  --timeout
  --resume Resume partial file if present
  -q, --quiet
  -d, --download Download after checking
  --download-small Prompt+download files ≤ --max-size
  --download Directory for downloaded small files
  --max-size Threshold for --download-small (e.g. 1M, 512K) --urls URL list file --dir Destination directory ...
plus 7 more; run ``dl --help`` for the full list.

Examples:
  $ dl size urls.txt
  $ dl wget --download-small urls.txt
  Command-line script:
  run ``python dl.py`` or the ``dl`` command.
"""

import argparse
import contextlib
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request

from dh import MAX_WORKERS, fsz
from loguru import logger
import pycurl
import requests
from tqdm import tqdm

_HAS_PYCURL: bool = True
from loguru import logger

_DEFAULT_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def read_url_list(path: Path) -> list[str]:
    try:
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except FileNotFoundError:
        return []


_FILENAME_BAD = re.compile(r'[<>:"|?*]')


def safe_filename(name: str) -> str:
    name = urllib.parse.unquote(name)
    name = _FILENAME_BAD.sub("_", name)
    return name[:255].strip() or "downloaded_file"


def filename_from_url(url: str) -> str:
    q = urllib.parse.urlparse(url)
    name = Path(urllib.parse.unquote(q.path)).name
    name = name.split("?")[0].split("#")[0]
    return safe_filename(name) if name else "downloaded_file"


def filename_from_headers(url: str, headers: dict) -> str:
    cd = ""
    for k in ("Content-Disposition", "content-disposition"):
        if headers.get(k):
            cd = headers[k]
            break
    if cd:
        m = re.search(r'filename\*?=(?:UTF-8)?"?([^";]+)"?', cd, re.IGNORECASE)
        if m:
            return safe_filename(m.group(1))
    return filename_from_url(url)


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.parent / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def remote_size_urllib(url: str, timeout: float = 10.0, ua: str = _DEFAULT_UA) -> Optional[int]:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cl = r.headers.get("Content-Length")
            if cl:
                return int(cl)
    except urllib.error.HTTPError as e:
        if e.code not in (405, 403):
            pass
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": ua, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                return int(cr.rsplit("/", 1)[1])
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception:
        return None


def remote_size_requests(url: str, timeout: float = 15.0) -> Optional[int]:
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        return int(cl) if cl else None
    except Exception:
        return None


def remote_size_pycurl(url: str, timeout: float = 15.0) -> Optional[int]:
    if not _HAS_PYCURL:
        return None
    c = pycurl.Curl()
    try:
        c.setopt(c.URL, url)
        c.setopt(c.NOBODY, True)
        c.setopt(c.FOLLOWLOCATION, True)
        c.setopt(c.TIMEOUT, int(timeout))
        c.setopt(c.USERAGENT, _DEFAULT_UA)
        c.perform()
        n = c.getinfo(c.CONTENT_LENGTH_DOWNLOAD)
        return int(n) if n and n > 0 else None
    except Exception:
        return None
    finally:
        c.close()


def _download_urllib(
    url: str,
    dest: Path,
    timeout: float = 30.0,
    ua: str = _DEFAULT_UA,
    resume: bool = False,
    quiet: bool = False,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = remote_size_urllib(url, timeout, ua)
    start = dest.stat().st_size if resume and dest.exists() else 0
    if total is not None and start >= total:
        if not quiet:
            print(f"✅ Already complete: {dest} ({start} bytes)")
        return dest
    headers = {"User-Agent": ua}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    mode = "ab" if start else "wb"
    with (
        tqdm(
            total=total or 0,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="Downloading",
            leave=False,
            disable=quiet,
            initial=start,
        ) as bar,
        urllib.request.urlopen(req, timeout=timeout) as r,
        dest.open(mode) as f,
    ):
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            bar.update(len(chunk))
    if not quiet:
        print(f"\n✅ Saved to: {dest}")
    return dest


def _download_requests(
    url: str,
    dest: Path,
    timeout: float = 60.0,
    ua: str = _DEFAULT_UA,
    resume: bool = False,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    start = dest.stat().st_size if resume and dest.exists() else 0
    headers = {"User-Agent": ua}
    if start:
        headers["Range"] = f"bytes={start}-"
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        mode = "ab" if start else "wb"
        with dest.open(mode) as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    return dest


def _download_pycurl(
    url: str,
    dest: Path,
    timeout: float = 30.0,
    ua: str = _DEFAULT_UA,
    resume: bool = False,
) -> Path:
    if not _HAS_PYCURL:
        msg = "pycurl not available"
        raise RuntimeError(msg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if (resume and dest.exists()) else "wb"
    c = pycurl.Curl()
    try:
        with dest.open(mode) as f:
            c.setopt(c.URL, url)
            c.setopt(c.WRITEDATA, f)
            c.setopt(c.FOLLOWLOCATION, True)
            c.setopt(c.MAXREDIRS, 10)
            c.setopt(c.TIMEOUT, int(timeout))
            c.setopt(c.CONNECTTIMEOUT, 5)
            c.setopt(c.USERAGENT, ua)
            c.setopt(c.NOSIGNAL, 1)
            if resume and mode == "ab":
                c.setopt(c.RESUME_FROM, dest.stat().st_size)
            c.perform()
            code = c.getinfo(c.RESPONSE_CODE)
            if code >= 400:
                msg = f"HTTP {code}"
                raise RuntimeError(msg)
    finally:
        c.close()
    return dest


def cmd_wget(args: argparse.Namespace) -> int:
    url = args.url
    out: Optional[Path] = Path(args.output) if args.output else None
    if out and out.is_dir():
        out = out / filename_from_url(url)
    if out is None:
        out = Path(filename_from_url(url))
    out = unique_path(out)
    try:
        _download_urllib(url, out, timeout=args.timeout, resume=args.resume, quiet=args.quiet)
        return 0
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP error {e.code}: {e.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"❌ URL error: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"❌ Download failed: {e}", file=sys.stderr)
        return 1


def cmd_check(args: argparse.Namespace) -> int:
    url = args.url
    print(f"Checking remote link: {url}")
    size = None
    if _HAS_PYCURL:
        size = remote_size_pycurl(url, timeout=args.timeout)
    if size is None:
        size = remote_size_urllib(url, timeout=args.timeout)
    if size:
        print(f"Remote file size: {fsz(size)}")
    else:
        print("Remote file size: Unknown (server didn't provide Content-Length)")
    if not args.download:
        print("\nUse --download to download the file.")
        return 0
    if not _HAS_PYCURL:
        out = Path(args.output) if args.output else Path(filename_from_url(url))
        out = unique_path(out)
        _download_urllib(url, out, timeout=args.timeout)
        print(f"\nSaved to: {out}")
        return 0
    print("Starting download...\n")
    out = Path(args.output) if args.output else Path(filename_from_url(url))
    out = unique_path(out)
    downloaded = 0
    t0 = time.time()

    def _write(data: bytes) -> None:
        nonlocal downloaded
        with out.open("ab") as f:
            f.write(data)
        downloaded += len(data)
        elapsed = time.time() - t0
        speed = downloaded / elapsed if elapsed > 0 else 0.0
        if size:
            pct = downloaded / size * 100
            eta = (size - downloaded) / speed if speed > 0 else 0
            eta_s = time.strftime("%H:%M:%S", time.gmtime(eta))
            info = f"{fsz(downloaded)}/{fsz(size)}"
        else:
            pct, eta_s, info = 0.0, "Unknown", f"{fsz(downloaded)}/Unknown"
        sys.stdout.write(f"\r[{pct:5.1f}%] {info} | Speed: {fsz(speed)}/s | ETA: {eta_s}   ")
        sys.stdout.flush()

    c = pycurl.Curl()
    try:
        c.setopt(c.URL, url)
        c.setopt(c.FOLLOWLOCATION, True)
        c.setopt(c.USERAGENT, _DEFAULT_UA)
        c.setopt(c.TIMEOUT, int(args.timeout))
        c.setopt(c.WRITEFUNCTION, _write)
        c.perform()
        code = c.getinfo(c.HTTP_CODE)
        if code >= 400:
            print(f"\n\nError: HTTP {code}")
            return 1
    except pycurl.error as e:
        print(f"\n\nDownload error: {e}")
        return 1
    finally:
        c.close()
    print(f"\n\nDownload complete! Saved to: {out}")
    return 0


def _process_size_url(url: str, download_small: bool, dest_dir: Path, max_size: int, timeout: float) -> str:
    size = remote_size_urllib(url, timeout=timeout)
    if size is None:
        return f"{url}\tUnknown"
    label = fsz(size)
    print(f"URL: {url}  Size: {label}")
    if download_small and size <= max_size:
        try:
            ans = input(f"Download this file ({label})? [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans == "y":
            dest_dir.mkdir(parents=True, exist_ok=True)
            fname = filename_from_url(url)
            fpath = unique_path(dest_dir / fname)
            try:
                urllib.request.urlretrieve(url, fpath)
                print(f"Downloaded: {fpath}")
            except Exception as e:
                print(f"Failed to download {url}: {e}")
        else:
            print("Download skipped.")
    elif download_small:
        print(f"File too large (> {fsz(max_size)}); skipping download.")
    return f"{url}\t{label}"


def cmd_size(args: argparse.Namespace) -> int:
    inp = Path(args.input)
    dest_dir = Path(args.download).expanduser() if args.download else Path.home() / "Downloads"
    max_size = _parse_size(args.max_size)
    if inp.is_file():
        lines = inp.read_text(encoding="utf-8", errors="ignore").splitlines()
        out_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue
            url = stripped.split("\t")[0].split()[0]
            out_lines.append(_process_size_url(url, args.download_small, dest_dir, max_size, args.timeout))
        inp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"Updated file: {inp} ({len(out_lines)} lines)")
    else:
        print(_process_size_url(args.input, args.download_small, dest_dir, max_size, args.timeout))
    return 0


def _parse_size(s: str) -> int:
    s = s.strip().upper()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)(?:B)?$", s)
    if not m:
        return int(s)
    n = float(m.group(1))
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[m.group(2)]
    return int(n * mult)


_EXT_WHITELIST: list[str] = [
    r"\.ttf$",
    r"\.woff$",
    r"\.woff2$",
    r"\.eot$",
    r"\.otf$",
    r"\.min\.css$",
    r"\.min\.js$",
    r"\.css$",
    r"\.js$",
    r"\.pdf$",
    r"\.html?$",
    r"\.whl$",
    r"\.tar\.(gz|xz|zst|bz2|lzma|7z)$",
    r"\.zip$",
    r"\.tar$",
    r"\.gz$",
    r"\.7z$",
]
_EXT_RE = re.compile("|".join(_EXT_WHITELIST), re.IGNORECASE)


def _passes_ext_filter(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    tail = path.split("/")[-1].split("?")[0].split("#")[0]
    return bool(_EXT_RE.search(tail))


def _batch_worker(url: str, dest_dir: str, engine: str, resume: bool, timeout: float, ua: str) -> tuple[str, bool, str]:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    fpath = unique_path(dest / filename_from_url(url))
    if resume and fpath.exists():
        remote = remote_size_requests(url, timeout)
        if remote is not None and fpath.stat().st_size >= remote:
            return (url, True, f"Already complete ({fpath.stat().st_size} bytes)")
    try:
        if engine == "pycurl":
            _download_pycurl(url, fpath, timeout, ua, resume)
        elif engine == "requests":
            _download_requests(url, fpath, timeout, ua, resume)
        elif engine == "urllib":
            _download_urllib(url, fpath, timeout, ua, resume, quiet=True)
        elif _HAS_PYCURL:
            try:
                _download_pycurl(url, fpath, timeout, ua, resume)
            except Exception:
                _download_requests(url, fpath, timeout, ua, resume)
        else:
            _download_requests(url, fpath, timeout, ua, resume)
        return (url, True, str(fpath))
    except Exception as e:
        with contextlib.suppress(Exception):
            if fpath.exists() and fpath.stat().st_size == 0:
                fpath.unlink()
        return (url, False, str(e))


def _sig_ignore(_signum: int, _frame: Any) -> None:
    return None


def cmd_batch(args: argparse.Namespace) -> int:
    urls_path = Path(args.urls)
    urls = read_url_list(urls_path)
    if not urls:
        print(f"No URLs found in {urls_path}")
        return 0
    if args.filter_ext:
        kept = [u for u in urls if _passes_ext_filter(u)]
        skipped = len(urls) - len(kept)
        if skipped:
            logger.warning(f"Filtered out {skipped} URL(s) not matching whitelist.")
        urls = kept
        if not urls:
            logger.error("No URLs left after extension filter.")
            return 0
    dest = Path(args.dir)
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(urls)} URL(s) with engine={args.engine} workers={MAX_WORKERS} ...")
    succeeded: set[str] = set()
    failed: list[str] = []
    if MAX_WORKERS <= 1:
        for i, url in enumerate(urls, 1):
            u, ok, msg = _batch_worker(url, str(dest), args.engine, args.resume, args.timeout, args.user_agent)
            print(f"[{i}/{len(urls)}] {'OK ' if ok else 'ERR'} {u} -> {msg}")
            (succeeded.add(u) if ok else failed.append(u))
    else:
        original_sigint = signal.getsignal(signal.SIGINT)
        with mp.Pool(processes=MAX_WORKERS, initializer=_sig_ignore) as pool:
            results = [
                (
                    url,
                    pool.apply_async(
                        _batch_worker,
                        (
                            url,
                            str(dest),
                            args.engine,
                            args.resume,
                            args.timeout,
                            args.user_agent,
                        ),
                    ),
                )
                for url in urls
            ]
            try:
                for i, (url, ar) in enumerate(results, 1):
                    try:
                        u, ok, msg = ar.get(timeout=args.timeout + 30)
                    except mp.TimeoutError:
                        u, ok, msg = url, False, "worker timeout"
                    except Exception as e:
                        u, ok, msg = url, False, f"pool error: {e}"
                    print(f"[{i}/{len(urls)}] {'OK ' if ok else 'ERR'} {u} -> {msg}")
                    (succeeded.add(u) if ok else failed.append(u))
            finally:
                signal.signal(signal.SIGINT, original_sigint)
    if args.update_file:
        remaining = [u for u in urls if u not in succeeded]
        urls_path.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
        print(f"\nRemaining URLs saved back to: {urls_path}")
    print(f"\nDone. Success: {len(succeeded)}, Failed: {len(failed)}")
    print(f"Files saved in: {dest.resolve()}")
    return 0 if not failed else 1


def _threaded_worker(
    url: str,
    start: int,
    end: int,
    filename: str,
    headers: dict,
    idx: int,
    timeout: float,
    chunk_size: int,
) -> tuple[str, int]:
    h = dict(headers)
    h["Range"] = f"bytes={start}-{end}"
    part = f"{filename}.part{idx}"
    with requests.get(url, headers=h, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
    return part, start


def cmd_threaded(args: argparse.Namespace) -> int:
    url = args.url
    headers = {
        "User-Agent": args.user_agent,
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    try:
        head = requests.head(url, headers=headers, allow_redirects=True, timeout=args.timeout)
        head.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Error reaching URL: {e}")
        return 1
    total = int(head.headers.get("content-length", 0) or 0)
    accept_ranges = head.headers.get("accept-ranges", "bytes")
    out = args.output or (url.split("/")[-1].split("?")[0] or "downloaded_file")
    out = safe_filename(out)
    if total == 0:
        logger.warning("No Content-Length; falling back to single stream.")
    if accept_ranges != "bytes" and args.chunks > 1:
        logger.warning("Server doesn't support byte ranges; single stream.")
    use_chunks = args.chunks
    if total == 0 or accept_ranges != "bytes":
        use_chunks = 1
    print(f"Target: {out}")
    print(f"Size  : {fsz(total) if total else 'Unknown'}")
    print(f"Slices: {use_chunks}")
    if use_chunks == 1:
        with (
            requests.get(url, headers=headers, stream=True, timeout=args.timeout) as r,
            open(out, "wb") as f,
            tqdm(total=total or 0, unit="B", unit_scale=True, desc=out) as bar,
        ):
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
        logger.success(f"Download complete: {out}")
        return 0
    per = total // use_chunks
    parts: list[Optional[str]] = [None] * use_chunks
    with mp.Pool(processes=min(8, use_chunks)) as pool:
        with tqdm(total=total, unit="B", unit_scale=True, desc="Downloading") as bar:
            futures = []
            for i in range(use_chunks):
                s = i * per
                e = total - 1 if i == use_chunks - 1 else s + per - 1
                futures.append(
                    pool.apply_async(
                        _threaded_worker,
                        (url, s, e, out, headers, i, args.timeout, 65536),
                    )
                )
            for fut in futures:
                try:
                    part, _ = fut.get()
                    idx = int(part.split(".part")[-1])
                    parts[idx] = part
                    bar.update(Path(part).stat().st_size)
                except Exception as e:
                    logger.error(f"Worker failed: {e}")
                    for pf in parts:
                        if pf:
                            with contextlib.suppress(FileNotFoundError):
                                Path(pf).unlink()
                    return 1
    with open(out, "wb") as dst:
        for pf in parts:
            if pf is None:
                continue
            with open(pf, "rb") as src:
                dst.write(src.read())
            Path(pf).unlink()
    logger.success(f"Download complete and assembled: {out}")
    return 0


def _download_chunk(url: str, dest: Path, start: int, end: int, timeout: float, ua: str) -> None:
    headers = {"Range": f"bytes={start}-{end}", "User-Agent": ua}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with dest.open("r+b") as f:
            f.seek(start)
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)


def _chunk_worker(url: str, dest_str: str, idx: int, start: int, end: int, timeout: float, ua: str) -> tuple[int, bool]:
    try:
        _download_chunk(url, Path(dest_str), start, end, timeout, ua)
        return (idx, True)
    except Exception:
        return (idx, False)


def cmd_chunked(args: argparse.Namespace) -> int:
    try:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )
    except ImportError:
        logger.error("The 'chunked' subcommand needs 'rich'. Install: pip install rich")
        return 1
    url = args.url
    filename: Optional[str] = safe_filename(args.output) if args.output else None
    expected_hash = args.sha256
    try:
        r = requests.head(url, allow_redirects=True, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Cannot reach {url}: {e}")
        return 1
    size = int(r.headers.get("content-length", 0) or 0)
    if size <= 0:
        logger.error("Server did not provide Content-Length; cannot chunk.")
        return 1
    if not filename:
        filename = filename_from_headers(url, dict(r.headers))
    dest = Path(filename)
    state_file = dest.with_suffix(dest.suffix + ".progress")
    completed: set[int] = set()
    if state_file.exists() and args.resume:
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            completed = set(data.get("completed", []))
            logger.info(f"Resuming: {len(completed)} chunk(s) already done.")
        except Exception as e:
            logger.warning(f"Bad state file, ignoring: {e}")
    if not dest.exists():
        with dest.open("wb") as f:
            f.truncate(size)
    chunk_size = args.chunk_size
    ranges = [(i, min(i + chunk_size - 1, size - 1)) for i in range(0, size, chunk_size)]
    todo = [(i, s, e) for i, (s, e) in enumerate(ranges) if i not in completed]
    if not todo:
        logger.success("All chunks already present.")
    else:
        console = Console()
        with Progress(
            TextColumn("[bold blue]{task.fields[filename]}"),
            BarColumn(),
            "[progress.percentage]{task.percentage:>3.0f}%",
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(
                "download",
                filename=filename,
                total=size,
                completed=sum(ranges[i][1] - ranges[i][0] + 1 for i in completed),
            )
            with mp.Pool(processes=MAX_WORKERS) as pool:
                futures = []
                for idx, s, e in todo:
                    futures.append(
                        pool.apply_async(
                            _chunk_worker,
                            (url, str(dest), idx, s, e, args.timeout, args.user_agent),
                        )
                    )
                for fut in futures:
                    idx, ok = fut.get()
                    if ok:
                        completed.add(idx)
                        length = ranges[idx][1] - ranges[idx][0] + 1
                        progress.update(task, advance=length)
                    else:
                        logger.warning(f"Chunk {idx} failed; will resume later.")
        state_file.write_text(json.dumps({"completed": sorted(completed)}), encoding="utf-8")
    if len(completed) < len(ranges):
        logger.warning("Not all chunks completed. Re-run to resume.")
        return 1
    h = hashlib.sha256()
    with dest.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    digest = h.hexdigest()
    if expected_hash:
        if digest.lower() == expected_hash.lower():
            logger.success("Integrity verified: hashes match!")
        else:
            logger.error("Integrity check FAILED")
            logger.error(f"Expected: {expected_hash}")
            logger.error(f"Got:      {digest}")
            return 1
    else:
        logger.warning(f"SHA-256 checksum: {digest}")
    state_file.unlink(missing_ok=True)
    logger.success(f"Download complete: {dest}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dl.py",
        description="Unified downloader (merged from 10 scripts). See module docstring for mappings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)
    pw = sub.add_parser("wget", help="Single-URL download with progress (pywget.py).")
    pw.add_argument("url")
    pw.add_argument("-o", "--output", help="Output file or directory")
    pw.add_argument("--timeout", type=float, default=30.0)
    pw.add_argument("--resume", action="store_true", help="Resume partial file if present")
    pw.add_argument("-q", "--quiet", action="store_true")
    pw.set_defaults(func=cmd_wget)
    pc = sub.add_parser("check", help="Check remote size, optionally download (download_checker.py).")
    pc.add_argument("url")
    pc.add_argument("-d", "--download", action="store_true", help="Download after checking")
    pc.add_argument("-o", "--output", help="Output filename")
    pc.add_argument("--timeout", type=float, default=15.0)
    pc.set_defaults(func=cmd_check)
    ps = sub.add_parser("size", help="Show remote size (dsize.py / dsized.py).")
    ps.add_argument("input", help="URL or file of URLs")
    ps.add_argument(
        "--download-small",
        action="store_true",
        help="Prompt+download files ≤ --max-size",
    )
    ps.add_argument("--download", help="Directory for downloaded small files")
    ps.add_argument(
        "--max-size",
        default="1M",
        help="Threshold for --download-small (e.g. 1M, 512K)",
    )
    ps.add_argument("--timeout", type=float, default=10.0)
    ps.set_defaults(func=cmd_size)
    pb = sub.add_parser(
        "batch",
        help="Batch download URLs from a file (cget / pycurl_downloader / url_downloader / rget).",
    )
    pb.add_argument("--urls", default="urls.txt", help="URL list file")
    pb.add_argument("--dir", default="downloads", help="Destination directory")
    pb.add_argument("--engine", choices=["auto", "pycurl", "requests", "urllib"], default="auto")
    pb.add_argument("--resume", action="store_true")
    pb.add_argument(
        "--update-file",
        action="store_true",
        help="Rewrite input file keeping only failed URLs",
    )
    pb.add_argument(
        "--filter-ext",
        action="store_true",
        help="Only download URLs matching the safe extension whitelist (rget.py)",
    )
    pb.add_argument("--timeout", type=float, default=60.0)
    pb.add_argument("--user-agent", default=_DEFAULT_UA)
    pb.set_defaults(func=cmd_batch)
    pt = sub.add_parser("threaded", help="Multi-chunk threaded downloader (ghost_downloader.py).")
    pt.add_argument("url")
    pt.add_argument("-o", "--output")
    pt.add_argument("-c", "--chunks", type=int, default=8)
    pt.add_argument("--user-agent", default=_DEFAULT_UA)
    pt.add_argument("--timeout", type=float, default=30.0)
    pt.set_defaults(func=cmd_threaded)
    pk = sub.add_parser("chunked", help="Resumable chunked downloader with SHA-256 (gget.py).")
    pk.add_argument("url")
    pk.add_argument("output", nargs="?", default=None)
    pk.add_argument("sha256", nargs="?", default=None)
    pk.add_argument("--chunk-size", type=int, default=32768)
    pk.add_argument("--timeout", type=float, default=15.0)
    pk.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Resume from .progress state file (default)",
    )
    pk.add_argument("--no-resume", dest="resume", action="store_false")
    pk.add_argument("--user-agent", default=_DEFAULT_UA)
    pk.set_defaults(func=cmd_chunked)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
