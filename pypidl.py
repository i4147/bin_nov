#!/data/data/com.termux/files/usr/bin/env python
"""PyPI package downloader with several strategies and mirrors.
Subcommands: ``basic`` downloads the preferred ``py3-none-any`` wheel, ``download`` streams a specific version, ``wheels`` fetches the wheels in parallel scored by the Python version, ``mirror`` tries the PyPI, Tsinghua and Runflare mirrors with the hashes verified and ``scrape`` parses the ``/simple`` index.
Usage: pypidl {basic,download,wheels,mirror,scrape} [options] PACKAGE Positional arguments: package Package name packages ...
Package names Subcommands: basic Simple API download, prefers py3-none-any wheel (pd.py) download Version-aware streaming download (pdown.py) wheels Parallel wheel downloads scored by Python version (pdown2.py) mirror Multi-mirror + hash-verified downloads (pip_get.py) scrape HTML /simple index scraper (pipget.py) Options: -o, --output Output directory (default: current directory) -v, --version Specific version (default: latest) --python Target Python version (default: 3.12) --output Output directory (default: wheels) -f, --file Read package specifiers from a file (one per line) --backend Download backend (default: pycurl) -d, --dir Download directory (default: current directory) -p, --pypi Use official PyPI simple index -c, --china Use Tsinghua mirror -m, --mirror Choose a named mirror (default: runflare) Examples: $ pypidl wheels numpy --python 3.12 -o wheels/ Command-line script: run ``python pypidl.py`` or the ``pypidl`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable, Optional, Sequence
import urllib.error
import urllib.request

from dh import MAX_WORKERS


try:
    import requests
except ImportError:
    requests = None
try:
    import pycurl
except ImportError:
    pycurl = None
try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    _rich_console: Any = Console()
except ImportError:
    _rich_console = None
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
JSON_MIRRORS: tuple[str, ...] = (
    "https://pypi.org/pypi",
    "https://pypi.tuna.tsinghua.edu.cn/pypi",
    "https://mirror-pypi.runflare.com/pypi",
)
SIMPLE_MIRRORS: dict[str, str] = {
    "runflare": "https://mirror-pypi.runflare.com/simple",
    "pypi": "https://pypi.org/simple",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
}
MIRROR_RETRIES: int = 3
HTTP_TIMEOUT: int = 30
CHUNKED_THRESHOLD: int = 5 * 1024 * 1024
SCRAPE_RETRIES: int = 3
SCRAPE_BACKOFF: int = 2
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _require(mod: Any, pkg_name: str, mode: str) -> None:
    if mod is None:
        sys.exit(
            f"[{mode}] this subcommand requires the '{pkg_name}' package; install it with:  pip install {pkg_name}"
        )


def human_size(n: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    v = float(n)
    for u in units:
        if v < 1024.0 or u == units[-1]:
            return f"{int(v)} B" if u == "B" else f"{v:.1f} {u}"
        v /= 1024.0
    return f"{v:.1f} TB"


def _clean_spec(spec: str) -> str:
    return spec.split("==", maxsplit=1)[0].split(">=", maxsplit=1)[0].split("<=", maxsplit=1)[0].strip()


def fetch_json_multi_mirror(pkg_name: str) -> dict:
    for mirror in JSON_MIRRORS:
        url = f"{mirror}/{pkg_name}/json"
        for _ in range(MIRROR_RETRIES):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PyPIDownloader/1.0"})
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
    msg = f"Failed to fetch metadata for '{pkg_name}' from all mirrors."
    raise RuntimeError(msg)


def fetch_json_pypi(pkg_name: str) -> dict:
    r = requests.get(f"https://pypi.org/pypi/{pkg_name}/json")
    if r.status_code != 200:
        msg = f"Failed to fetch package info for {pkg_name}"
        raise ValueError(msg)
    return r.json()


def fetch_json_urlopen(pkg_name: str, timeout: int = 10) -> Optional[dict]:
    url = f"https://pypi.org/pypi/{pkg_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        print(f"  ❌ Error fetching {pkg_name}: {exc}")
        return None


def pick_py3_any_wheel_or_sdist(files: list[dict]) -> Optional[dict]:
    for f in files:
        if f.get("packagetype") == "bdist_wheel":
            if f.get("python_version") == "py3" and "any" in f.get("filename", ""):
                return f
    sdists = [f for f in files if f.get("packagetype") == "sdist"]
    if sdists:
        return sdists[0]
    return files[0] if files else None


def pick_wheel_then_sdist(files: list[dict]) -> Optional[dict]:
    for f in files:
        if f.get("packagetype") == "bdist_wheel":
            return f
    for f in files:
        if f.get("packagetype") == "sdist":
            return f
    return files[0] if files else None


def pick_mirror_file(files: list[dict], version: str) -> Optional[dict]:
    candidates: list[dict] = []
    for f in files:
        fname = f["filename"].lower()
        if any(k in fname for k in ("darwin", "win32", "win_amd64", "win_")):
            continue
        if fname.endswith(".whl") and "none-any" not in fname:
            continue
        candidates.append(f)
    if not candidates:
        msg = f"No suitable source or neutral wheel release files found for version {version}."
        raise RuntimeError(msg)
    sdists = [f for f in candidates if f["filename"].endswith(".tar.gz")]
    if sdists:
        return sdists[0]
    whls = [f for f in candidates if f["filename"].endswith(".whl")]
    if whls:
        return whls[0]
    return candidates[0]


def score_wheel(f_info: dict, python_version: str) -> int:
    fname = f_info["filename"].lower()
    pv = f_info.get("python_version", "").lower()
    score = 0
    compact = python_version.replace(".", "")
    if f"cp{compact}" in fname:
        score += 100
    if pv == f"=={python_version}":
        score += 100
    if pv.startswith(f">={python_version}"):
        score += 90
    if "py3" in fname and "none" in fname:
        score += 80
    if "abi3" in fname:
        score += 70
    if pv.startswith(">=3."):
        score += 60
    return score


def pick_scored_wheel(meta: dict, python_version: str) -> Optional[tuple[str, int]]:
    releases = meta.get("releases", {})
    if not releases:
        return None
    latest = max(releases.keys())
    files = releases[latest]
    scored: list[tuple[int, dict]] = []
    for f in files:
        if f.get("packagetype") != "bdist_wheel":
            continue
        s = score_wheel(f, python_version)
        if s > 0:
            scored.append((s, f))
    if not scored:
        return None
    _, best = max(scored, key=lambda x: x[0])
    return best["url"], best["size"]


def download_requests_simple(url: str, target: Path) -> None:
    r = requests.get(url)
    r.raise_for_status()
    target.write_bytes(r.content)


def download_requests_stream(url: str, target: Path) -> None:
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        written = 0
        with target.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
                if total > 0:
                    pct = written / total * 40
                    print(
                        f"\rProgress: {pct:.1f}% ({written}/{total} bytes)",
                        end="",
                    )
    print()


def download_urlopen_progress(url: str, target: Path, size: int, chunk: int = 8192) -> tuple[bool, str]:
    print(f"  📥 Downloading {target.name} ({size / 1024 / 1024:.2f} MB)...")
    try:
        with urllib.request.urlopen(url) as r:
            total = int(r.headers.get("content-length", size))
            written = 0
            with open(target, "wb") as fh:
                while True:
                    block = r.read(chunk)
                    if not block:
                        break
                    fh.write(block)
                    written += len(block)
                    pct = written / total * 40 if total else 0
                    print(
                        f"    ⬇ {written / 1024 / 1024:.2f} MB/{total / 1024 / 1024:.2f} MB ({pct:.1f}%)",
                        end="\r",
                    )
        print(f"    ✅ Downloaded {target.name} ({written / 1024 / 1024:.2f} MB)")
        return True, ""
    except Exception as exc:
        return False, f"Failed: {exc!s}"


def verify_hash(path: Path, digests: dict) -> bool:
    if "sha256" in digests:
        algo, expected = "sha256", digests["sha256"]
    elif "md5" in digests:
        algo, expected = "md5", digests["md5"]
    else:
        print("[yellow]No known hash provided in metadata. Skipping hash verification.[/yellow]")
        return True
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        while True:
            block = fh.read(1 << 20)
            if not block:
                break
            h.update(block)
    actual = h.hexdigest().lower()
    if actual == expected.lower():
        print(f"[bold green]✓ Integrity check passed ({algo.upper()})[/bold green]")
        return True
    print(f"[bold red]✗ Hash verification failed! Expected: {expected}, Got: {actual}[/bold red]")
    return False


def cmd_basic(args: argparse.Namespace) -> int:
    _require(requests, "requests", "basic")
    meta = fetch_json_pypi(args.package)
    releases = meta.get("releases", {})
    if not releases:
        msg = f"No releases for {args.package}"
        raise ValueError(msg)
    latest = max(releases.keys())
    print(f"latest version : {latest}")
    files = releases[latest]
    chosen = pick_py3_any_wheel_or_sdist(files)
    if chosen is None:
        msg = f"No suitable file for {args.package} {latest}"
        raise ValueError(msg)
    url = chosen["url"]
    name = chosen["filename"]
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {name}...")
    download_requests_simple(url, out_dir / name)
    print(f"Downloaded {name}")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    _require(requests, "requests", "download")
    print(f"Fetching {args.package} (version: {args.version or 'latest'})...")
    meta = fetch_json_pypi(args.package)
    releases = meta.get("releases", {})
    if not releases:
        msg = f"No releases found for {args.package}"
        raise ValueError(msg)
    if args.version:
        if args.version not in releases:
            msg = f"Version {args.version} not found for {args.package}"
            raise ValueError(msg)
        files = releases[args.version]
    else:
        latest = meta.get("info", {}).get("version")
        files = releases.get(latest, [])
    if not files:
        msg = "No downloadable files found"
        raise ValueError(msg)
    chosen = pick_wheel_then_sdist(files)
    assert chosen is not None
    url, filename = chosen["url"], chosen["filename"]
    print(f"Found: {filename}")
    print(f"URL: {url}")
    out_dir = Path(args.output or ".").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / filename
    print(f"Downloading {filename}...")
    download_requests_stream(url, target)
    print(f"✓ Downloaded to: {target}")
    return 0


def _download_wheel_worker(pkg: str, out_dir: Path, python_version: str) -> tuple[str, bool, str]:
    print(f"🔍 Fetching info for: {pkg}")
    meta = fetch_json_urlopen(pkg)
    if meta is None:
        return pkg, False, "Failed to fetch package info from PyPI"
    picked = pick_scored_wheel(meta, python_version)
    if picked is None:
        return pkg, False, f"No compatible wheel found for Python {python_version}"
    url, size = picked
    filename = url.split("/")[-1]
    target = out_dir / filename
    print(f"  📊 Package: {pkg}")
    print(f"  🔗 URL: {url}")
    print(f"  💾 Size: {size / 1024 / 1024:.2f} MB")
    ok, err = download_urlopen_progress(url, target, size)
    return pkg, ok, err


def cmd_wheels(args: argparse.Namespace) -> int:
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"📁 Saving wheels to: {out_dir}\n")
    downloaded = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_download_wheel_worker, pkg, out_dir, args.python): pkg for pkg in args.packages}
        for fut in as_completed(futures):
            pkg, success, err = fut.result()
            if success:
                downloaded += 1
            else:
                print(f"  ⚠️  {pkg}: {err}")
    print(f"\n✅ Downloaded {downloaded}/{len(args.packages)} packages successfully.")
    return 0


def _progress_bar() -> Any:
    return Progress(
        TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>3.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TextColumn("[cyan]Elapsed:[/cyan]"),
        TimeElapsedColumn(),
        "•",
        TextColumn("[cyan]ETA:[/cyan]"),
        TimeRemainingColumn(),
        console=_rich_console,
    )


def _mirror_download_pycurl(url: str, target: Path, total: int) -> None:
    size = target.stat().st_size if target.exists() else 0
    mode = "ab" if size > 0 else "wb"
    with _progress_bar() as progress:
        task = progress.add_task("download", filename=target.name, total=total, completed=size)

        def write_cb(data: bytes) -> int:
            n = len(data)
            progress.update(task, advance=n)
            return fh.write(data)

        for attempt in range(1, MIRROR_RETRIES + 1):
            try:
                with open(target, mode) as fh:
                    c = pycurl.Curl()
                    c.setopt(c.URL, url)
                    c.setopt(c.WRITEFUNCTION, write_cb)
                    c.setopt(c.TIMEOUT, HTTP_TIMEOUT)
                    c.setopt(c.CONNECTTIMEOUT, HTTP_TIMEOUT)
                    c.setopt(c.FOLLOWLOCATION, True)
                    if size > 0:
                        c.setopt(c.RESUME_FROM, size)
                    c.perform()
                    c.close()
                return
            except pycurl.Error:
                size = target.stat().st_size if target.exists() else 0
                mode = "ab"
                if attempt == MIRROR_RETRIES:
                    raise


def _mirror_download_requests(url: str, target: Path, total: int) -> None:
    size = target.stat().st_size if target.exists() else 0
    mode = "ab" if size > 0 else "wb"
    headers: dict[str, str] = {}
    if size > 0:
        headers["Range"] = f"bytes={size}-"
    chunked = total > CHUNKED_THRESHOLD
    with _progress_bar() as progress:
        task = progress.add_task("download", filename=target.name, total=total, completed=size)
        for attempt in range(1, MIRROR_RETRIES + 1):
            try:
                r = requests.get(url, headers=headers, stream=True, timeout=HTTP_TIMEOUT)
                if r.status_code == 200 and size > 0:
                    mode = "wb"
                    size = 0
                    progress.update(task, completed=0)
                r.raise_for_status()
                csize = 1 << 20 if chunked else 1 << 16
                with open(target, mode) as fh:
                    for block in r.iter_content(chunk_size=csize):
                        if block:
                            fh.write(block)
                            progress.update(task, advance=len(block))
                return
            except requests.RequestException:
                size = target.stat().st_size if target.exists() else 0
                mode = "ab"
                if size > 0:
                    headers["Range"] = f"bytes={size}-"
                if attempt == MIRROR_RETRIES:
                    raise


def _mirror_download_aria2c(url: str, target: Path) -> None:
    subprocess.run(
        [
            "aria2c",
            "--continue=true",
            f"--max-tries={MIRROR_RETRIES}",
            f"--timeout={HTTP_TIMEOUT}",
            f"--dir={target.parent.resolve()}",
            f"--out={target.name}",
            url,
        ],
        check=True,
    )


def _mirror_download(url: str, target: Path, total: int, backend: str) -> None:
    chunked = total > CHUNKED_THRESHOLD
    mb = total / 1024 / 1024
    print(f"File size: {mb:.2f} MB | Chunked mode: {chunked}")
    if target.exists() and target.stat().st_size == total:
        print("Local file matches full size. Skipping download...")
        return
    if backend == "pycurl":
        _mirror_download_pycurl(url, target, total)
    elif backend == "requests":
        _mirror_download_requests(url, target, total)
    elif backend == "aria2c":
        _mirror_download_aria2c(url, target)
    else:
        msg = f"Unsupported backend engine: {backend}"
        raise ValueError(msg)


def _read_specs_from_file(path: Path) -> list[str]:
    if not path.exists():
        msg = f"Package list file not found: {path}"
        raise FileNotFoundError(msg)
    if not path.is_file():
        msg = f"Path is not a regular file: {path}"
        raise ValueError(msg)
    specs: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "#" in line:
                line = line.split("#", 1)[0].strip()
            if not line:
                continue
            specs.extend(line.split())
    return specs


def _mirror_process_spec(spec: str, backend: str, output_dir: Path) -> None:
    print(f"\nProcessing Package Specifier: {spec}")
    if "==" in spec:
        name, version = spec.split("==", 1)
    else:
        name, version = spec, None
    meta = fetch_json_multi_mirror(name)
    releases = meta.get("releases", {})
    resolved = version or meta.get("info", {}).get("version")
    if not resolved or resolved not in releases:
        msg = f"Version '{resolved}' not found in package metadata."
        raise ValueError(msg)
    file_info = pick_mirror_file(releases[resolved], resolved)
    url = file_info["url"]
    filename = file_info["filename"]
    size = file_info.get("size", 0)
    digests = file_info.get("digests", {})
    target = output_dir / filename
    print(f"Selected file : {filename}")
    print(f"Target URL    : {url}")
    _mirror_download(url, target, size, backend)
    if not verify_hash(target, digests):
        print("Deleting corrupted/incomplete file...")
        target.unlink(missing_ok=True)


def cmd_mirror(args: argparse.Namespace) -> int:
    if args.backend == "pycurl":
        _require(pycurl, "pycurl", "mirror")
        _require(_rich_console, "rich", "mirror")
    elif args.backend == "requests":
        _require(requests, "requests", "mirror")
    specs: list[str] = list(args.packages)
    if args.file:
        try:
            loaded = _read_specs_from_file(Path(args.file))
            print(f"Loaded {len(loaded)} package(s) from {args.file}")
            specs.extend(loaded)
        except Exception as exc:
            print(f"Failed to read '{args.file}': {exc}")
            return 1
    if not specs:
        print("Error: No package name specified.")
        return 1
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        try:
            _mirror_process_spec(spec, args.backend, output_dir)
        except Exception as exc:
            print(f"Failed to process '{spec}': {exc}")
    return 0


_ARCH_RE = re.compile(
    r"-(cp\d+|pp\d+|py\d+)(-(cp\d+|pp\d+|py\d+))?" r"-(manylinux|musllinux|win|macosx|linux|darwin)",
    re.IGNORECASE,
)
_ARCH_TOKENS: tuple[str, ...] = (
    "manylinux",
    "musllinux",
    "macosx",
    "darwin",
    "x86_64",
    "amd64",
    "i686",
    "aarch64",
    "armv7l",
    "armv6l",
    "armv8l",
    "ppc64",
    "s390x",
    "riscv64",
)


def _is_windows_file(fname: str) -> bool:
    low = fname.lower()
    return "win32" in low or "win_amd64" in low or "win_arm64" in low or "-win-" in low


def _is_arch_specific(fname: str) -> bool:
    low = fname.lower()
    if _ARCH_RE.search(low):
        return True
    return any(tok in low for tok in _ARCH_TOKENS)


def _is_sdist(fname: str) -> bool:
    return fname.lower().endswith(".tar.gz")


def _is_pure_wheel(fname: str) -> bool:
    low = fname.lower()
    if not low.endswith(".whl"):
        return False
    return "py3-none-any" in low or "py2.py3-none-any" in low


def _pick_scrape_link(links: Iterable[Any]) -> Optional[tuple[str, str, str]]:
    sdists: list[tuple[str, str]] = []
    wheels: list[tuple[str, str]] = []
    others: list[tuple[str, str]] = []
    for link in links:
        href = (link.get("href") or "").strip()
        if not href:
            continue
        url = href.split("#")[0]
        text = (link.get_text() or "").strip() or url.split("/")[-1]
        if _is_windows_file(url):
            continue
        if _is_arch_specific(url):
            others.append((url, text))
            continue
        if _is_sdist(url):
            sdists.append((url, text))
        elif _is_pure_wheel(url):
            wheels.append((url, text))
    if sdists:
        u, t = sdists[-1]
        return u, t, "download"
    if wheels:
        u, t = wheels[-1]
        return u, t, "download"
    if others:
        u, t = others[-1]
        return u, t, "skip"
    return None


def _scrape_fetch_html(pkg: str, mirror_url: str, trailing_slash: bool) -> str:
    url = f"{mirror_url.rstrip('/')}/{pkg}/" if trailing_slash else f"{mirror_url.rstrip('/')}/{pkg}"
    buf = io.BytesIO()
    c = pycurl.Curl()
    c.setopt(c.URL, url)
    c.setopt(c.WRITEDATA, buf)
    c.setopt(c.FOLLOWLOCATION, 1)
    c.setopt(c.TIMEOUT, HTTP_TIMEOUT)
    c.setopt(c.USERAGENT, DEFAULT_USER_AGENT)
    c.setopt(c.ACCEPT_ENCODING, "gzip,deflate")
    c.setopt(
        c.HTTPHEADER,
        [
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language: en-US,en;q=0.5",
        ],
    )
    try:
        c.perform()
        code = c.getinfo(c.RESPONSE_CODE)
        if code != 200:
            _explain_http_error(code, pkg)
            return ""
        return buf.getvalue().decode("utf-8", errors="replace")
    except Exception:
        return ""
    finally:
        c.close()


def _explain_http_error(code: int, pkg: str) -> None:
    if code == 402:
        print("  HTTP 402: Payment Required — the mirror may require authentication")
    elif code == 403:
        print("  HTTP 403: Forbidden — access denied")
    elif code == 404:
        print(f"  Package '{pkg}' not found on mirror")
    elif code == 429:
        print("  HTTP 429: Too Many Requests — rate limited")


def _scrape_parse(html: str) -> Optional[tuple[str, str, str]]:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=True)
    if not links:
        return None
    return _pick_scrape_link(links)


def _scrape_download(url: str, filename: str, out_dir: Path) -> bool:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / filename
    if target.exists() and target.stat().st_size > 0:
        return True
    print(f"  Downloading: {filename}")
    for attempt in range(SCRAPE_RETRIES):
        if attempt > 0:
            time.sleep(SCRAPE_BACKOFF * attempt)
        with open(target, "wb") as fh:
            c = pycurl.Curl()
            c.setopt(c.URL, url)
            c.setopt(c.WRITEDATA, fh)
            c.setopt(c.FOLLOWLOCATION, 1)
            c.setopt(c.TIMEOUT, 120)
            c.setopt(c.USERAGENT, DEFAULT_USER_AGENT)
            c.setopt(c.ACCEPT_ENCODING, "gzip,deflate")
            c.setopt(c.HTTPHEADER, ["Accept: */*", "Accept-Language: en-US,en;q=0.5"])
            try:
                c.perform()
                code = c.getinfo(c.RESPONSE_CODE)
                if code == 200:
                    print()
                    return True
                _explain_http_error(code, filename)
                if target.exists():
                    target.unlink()
            except Exception:
                pass
            finally:
                c.close()
    return False


def _scrape_already_have(pkg: str, out_dir: Path) -> bool:
    lowered = pkg.lower().replace("-", "_").replace(".", "_")
    pattern = re.compile("^" + re.escape(lowered) + r"[-_.]v?\d", re.IGNORECASE)
    for f in out_dir.iterdir():
        if not f.is_file() or f.stat().st_size == 0:
            continue
        if pattern.match(f.name.lower().replace("-", "_")):
            return True
    return False


def _read_pkg_names_from_file(path: Path) -> list[str]:
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    names: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
                if m:
                    names.append(m.group(1))
    except OSError as exc:
        print(f"Error reading file {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    return names


def cmd_scrape(args: argparse.Namespace) -> int:
    _require(pycurl, "pycurl", "scrape")
    _require(BeautifulSoup, "beautifulsoup4", "scrape")
    if args.pypi:
        mirror_name = "pypi"
    elif args.china:
        mirror_name = "tsinghua"
    elif args.mirror:
        mirror_name = args.mirror
    else:
        mirror_name = "runflare"
    mirror_url = SIMPLE_MIRRORS[mirror_name]
    trailing_slash = mirror_name in ("pypi", "tsinghua")
    if args.directory:
        out_dir = Path(args.directory).expanduser().resolve()
        if not out_dir.is_dir():
            print(f"Error: download directory does not exist: {out_dir}", file=sys.stderr)
            return 1
    else:
        out_dir = Path.cwd()
    packages: list[str] = list(args.packages)
    if args.file:
        loaded = _read_pkg_names_from_file(Path(args.file))
        print(f"Loaded {len(loaded)} package(s) from {args.file}")
        packages.extend(loaded)
    if not packages:
        print("Error: No package name specified.")
        return 1
    seen: set[str] = set()
    deduped: list[str] = []
    for p in packages:
        low = p.lower()
        if low in seen:
            continue
        seen.add(low)
        deduped.append(p)
    packages = deduped
    print(f"Mirror       : {mirror_name} ({mirror_url})")
    print(f"Download dir : {out_dir}")
    print(f"Processing {len(packages)} package(s)...\n")
    ok_list: list[str] = []
    skip_list: list[str] = []
    present_list: list[str] = []
    fail_list: list[str] = []
    start = time.time()
    for pkg in packages:
        print(f"[{pkg}]")
        try:
            if _scrape_already_have(pkg, out_dir):
                print("  Already exists, skipping")
                present_list.append(pkg)
                continue
            html = _scrape_fetch_html(pkg, mirror_url, trailing_slash)
            picked = _scrape_parse(html)
            if picked is None:
                print("  No suitable file found")
                fail_list.append(pkg)
                continue
            url, filename, action = picked
            if action == "skip":
                skip_list.append(pkg)
                print("  Skipped (arch-specific; no source/pure wheel available)")
                continue
            print(f"  Download URL: {url}")
            if _scrape_download(url, filename, out_dir):
                ok_list.append(pkg)
            else:
                fail_list.append(pkg)
        except Exception as exc:
            print(f"  Error: {exc}")
            fail_list.append(pkg)
    elapsed = time.time() - start
    if ok_list:
        print("\nSuccessfully downloaded:")
        for p in ok_list:
            print(f"  ✓ {p}")
    if present_list:
        print("\nAlready present:")
        for p in present_list:
            print(f"  • {p}")
    if skip_list:
        print("\nSkipped (arch-specific, no pure source/wheel available):")
        for p in skip_list:
            print(f"  ⚠ {p}")
    if fail_list:
        print("\nFailed to download:")
        for p in fail_list:
            print(f"  ✗ {p}")
    print(
        f"\nDone in {elapsed:.1f}s — {len(ok_list)} downloaded, "
        f"{len(present_list)} already present, {len(skip_list)} skipped, "
        f"{len(fail_list)} failed"
    )
    return 1 if fail_list else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pypi_dl",
        description="Unified PyPI package downloader (merges pd.py / pdown.py / pdown2.py / pip_get.py / pipget.py).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Mapping from the original scripts:\n"
            "  pd.py      -> pypi_dl.py basic    <pkg>\n"
            "  pdown.py   -> pypi_dl.py download <pkg> [-v VER]\n"
            "  pdown2.py  -> pypi_dl.py wheels   <pkg>... [--python 3.14]\n"
            "  pip_get.py -> pypi_dl.py mirror   <pkg|pkg==ver>...\n"
            "  pipget.py  -> pypi_dl.py scrape   <pkg>... [-m runflare]\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("basic", help="Simple API download, prefers py3-none-any wheel (pd.py)")
    p.add_argument("package", help="Package name")
    p.add_argument(
        "-o",
        "--output",
        default=".",
        help="Output directory (default: current directory)",
    )
    p.set_defaults(func=cmd_basic)
    p = sub.add_parser("download", help="Version-aware streaming download (pdown.py)")
    p.add_argument("package", help="Package name")
    p.add_argument("-v", "--version", default=None, help="Specific version (default: latest)")
    p.add_argument(
        "-o",
        "--output",
        default=".",
        help="Output directory (default: current directory)",
    )
    p.set_defaults(func=cmd_download)
    p = sub.add_parser("wheels", help="Parallel wheel downloads scored by Python version (pdown2.py)")
    p.add_argument("packages", nargs="+", help="Package names")
    p.add_argument("--python", default="3.12", help="Target Python version (default: 3.12)")
    p.add_argument(
        "--output",
        type=Path,
        default=Path("wheels"),
        help="Output directory (default: wheels)",
    )
    p.set_defaults(func=cmd_wheels)
    p = sub.add_parser("mirror", help="Multi-mirror + hash-verified downloads (pip_get.py)")
    p.add_argument(
        "packages",
        nargs="*",
        help="Package specifiers (e.g. requests, requests==2.31.0)",
    )
    p.add_argument(
        "-f",
        "--file",
        default=None,
        help="Read package specifiers from a file (one per line)",
    )
    p.add_argument(
        "--backend",
        choices=["pycurl", "requests", "aria2c"],
        default="pycurl",
        help="Download backend (default: pycurl)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path(),
        help="Output directory (default: current directory)",
    )
    p.set_defaults(func=cmd_mirror)
    p = sub.add_parser("scrape", help="HTML /simple index scraper (pipget.py)")
    p.add_argument("packages", nargs="*", help="Package names")
    p.add_argument("-f", "--file", default=None, help="Read package names from file (one per line)")
    p.add_argument(
        "-d",
        "--dir",
        dest="directory",
        default=None,
        help="Download directory (default: current directory)",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("-p", "--pypi", action="store_true", help="Use official PyPI simple index")
    group.add_argument("-c", "--china", action="store_true", help="Use Tsinghua mirror")
    group.add_argument(
        "-m",
        "--mirror",
        choices=list(SIMPLE_MIRRORS.keys()),
        default=None,
        help="Choose a named mirror (default: runflare)",
    )
    p.set_defaults(func=cmd_scrape)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
