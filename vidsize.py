#!/data/data/com.termux/files/usr/bin/env python
# -*- coding: utf-8 -*-
"""Report the size of videos behind direct links, web pages and HLS playlists."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib.parse import urljoin, urlparse
from typing import Any, Iterable, TextIO

from dh import MAX_WORKERS
import requests

UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS: dict[str, str] = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
QUALITY_ORDER: list[str] = [
    "2160p",
    "1440p",
    "1080p",
    "900p",
    "720p",
    "576p",
    "540p",
    "480p",
    "360p",
    "240p",
    "unknown",
]
MEDIA_EXT: str = r"(?:mp4|m3u8|mkv|webm|mov|m4v|ts|avi|flv)"
ABS_LINK_RE = re.compile(r"https?://[^\s\"'<>\\]+?\." + MEDIA_EXT + r"(?:\?[^\s\"'<>\\]*)?", re.IGNORECASE)
REL_LINK_RE = re.compile(
    r"""(?:src|href|data-src|data-video|data-file|file|url)\s*[=:]\s*"""
    r"""["']([^"']+?\.""" + MEDIA_EXT + r"""(?:\?[^"']*)?)["']""",
    re.IGNORECASE,
)
_thread_local = threading.local()


def session() -> requests.Session:
    s = getattr(_thread_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.s = s
    return s


def human_size(n: int) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.2f} TB"


def quality_rank(q: str) -> int:
    try:
        return QUALITY_ORDER.index(q)
    except ValueError:
        return len(QUALITY_ORDER)


def guess_quality(*texts: Any) -> str:
    blob = " ".join(t for t in texts if t)
    m = re.search(r"(?<![\d])(2160|1440|1080|900|720|576|540|480|360|240)\s*[pP](?!\w)", blob)
    if m:
        return m.group(1) + "p"
    m = re.search(r"(?<![\d])(2160|1440|1080|720|576|480|360|240)(?![\d])", blob)
    if m:
        return m.group(1) + "p"
    return "unknown"


def sanitize(name: str, maxlen: int = 90) -> str:
    name = re.sub(r"[^\w.\-]+", "_", name).strip("_")
    return (name or "report")[:maxlen]


def get_size(url: str, timeout: float):
    s = session()
    ctype = None
    final = url
    try:
        r = s.head(url, allow_redirects=True, timeout=timeout)
        ctype = r.headers.get("Content-Type")
        final = r.url
        cl = r.headers.get("Content-Length")
        if r.status_code < 400 and cl and cl.isdigit() and int(cl) > 0:
            return int(cl), ctype, final
    except requests.RequestException:
        pass
    try:
        r = s.get(url, headers={"Range": "bytes=0-0"}, stream=True, allow_redirects=True, timeout=timeout)
        ctype = r.headers.get("Content-Type", ctype)
        final = r.url
        status = r.status_code
        cr = r.headers.get("Content-Range")
        cl = r.headers.get("Content-Length")
        r.close()
        if cr and "/" in cr:
            total = cr.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return int(total), ctype, final
        if status in (200, 206) and cl and cl.isdigit() and int(cl) > 0:
            return int(cl), ctype, final
    except requests.RequestException:
        pass
    return None, ctype, final


def fetch_text(url: str, timeout: float, limit: int = 8_000_000) -> str:
    s = session()
    r = s.get(url, timeout=timeout, stream=True, allow_redirects=True)
    r.raise_for_status()
    enc = r.encoding or "utf-8"
    buf, total = [], 0
    for chunk in r.iter_content(65536):
        buf.append(chunk)
        total += len(chunk)
        if total > limit:
            break
    r.close()
    return b"".join(buf).decode(enc, errors="replace")


def parse_attr_list(s: str) -> dict:
    attrs = {}
    for m in re.finditer(r'([A-Za-z0-9\-]+)=("[^"]*"|[^,]*)', s):
        k, v = m.group(1).upper(), m.group(2).strip()
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        attrs[k] = v
    return attrs


def parse_master_playlist(text: str, base_url: str) -> list:
    lines = [l.strip() for l in text.splitlines()]
    variants, seen = [], set()
    for i, line in enumerate(lines):
        if not line.upper().startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = parse_attr_list(line.split(":", 1)[1] if ":" in line else "")
        uri = None
        for j in range(i + 1, len(lines)):
            if lines[j] and not lines[j].startswith("#"):
                uri = lines[j]
                break
        if not uri:
            continue
        full = urljoin(base_url, uri)
        if full in seen:
            continue
        seen.add(full)
        res = attrs.get("RESOLUTION", "")
        height = None
        if "x" in res.lower():
            try:
                height = int(res.lower().split("x")[1])
            except ValueError:
                height = None
        bw = attrs.get("BANDWIDTH", "")
        variants.append({
            "url": full,
            "height": height,
            "resolution": res or None,
            "bandwidth": int(bw) if bw.isdigit() else None,
            "codecs": attrs.get("CODECS"),
        })
    return variants


def parse_media_playlist(text: str, base_url: str) -> tuple[bool,]:
    segs, duration, byterange = [], 0.0, False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.upper().startswith("#EXTINF:"):
                m = re.match(r"#EXTINF:\s*([\d.]+)", line, re.IGNORECASE)
                if m:
                    with contextlib.suppress(ValueError):
                        duration += float(m.group(1))
            elif line.upper().startswith("#EXT-X-BYTERANGE"):
                byterange = True
            continue
        segs.append(urljoin(base_url, line))
    return segs, duration, byterange


def sum_sizes(urls: list, timeout: float, workers: int = 16):
    total, ok, unknown = 0, 0, 0

    def one(u: Any):
        return get_size(u, timeout)[0]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(one, u) for u in urls]
        for f in as_completed(futures):
            try:
                size = f.result()
            except Exception:
                size = None
            if size is None:
                unknown += 1
            else:
                total += size
                ok += 1
    return total, ok, unknown


def hls_entry(url: str, quality: str, args: list[Any], extra: dict | None = None) -> dict:
    text = fetch_text(url, args.timeout)
    segs, duration, byterange = parse_media_playlist(text, url)
    entry = {
        "url": url,
        "quality": quality,
        "kind": "hls",
        "segments": len(segs),
        "duration_seconds": round(duration, 2) if duration else None,
        "size_bytes": None,
        "size_human": "unknown",
    }
    if extra:
        entry.update(extra)
    if args.skip_segments:
        bw = entry.get("bandwidth")
        est = int(bw * duration / 8) if (bw and duration) else None
        entry["size_bytes"] = est
        entry["size_human"] = human_size(est)
        entry["size_is_estimate"] = True
        entry["note"] = "estimated from BANDWIDTH x duration (--skip-segments)"
        return entry
    total, ok, unknown = sum_sizes(segs, args.timeout, MAX_WORKERS)
    entry["size_bytes"] = total if ok else None
    entry["size_human"] = human_size(entry["size_bytes"])
    entry["size_is_estimate"] = False
    entry["segments_sized"] = ok
    entry["segments_unknown"] = unknown
    if byterange:
        entry["note"] = "playlist uses EXT-X-BYTERANGE; summed sizes may be wrong"
    return entry


def extract_links(html: str, base_url: str) -> list:
    html = html.replace("\\/", "/")
    found, seen = [], set()

    def add(u: str) -> None:
        u = u.strip().rstrip(".,;)\"'")
        if not u:
            return
        full = urljoin(base_url, u)
        if full not in seen:
            seen.add(full)
            found.append(full)

    for m in ABS_LINK_RE.finditer(html):
        add(m.group(0))
    for m in REL_LINK_RE.finditer(html):
        add(m.group(1))
    return found


def analyze(url: str, args: list[Any], depth: int = 0, entries=None, seen=None) -> list:
    entries = [] if entries is None else entries
    seen = set() if seen is None else seen
    if url in seen or depth > args.max_depth:
        return entries
    seen.add(url)
    size, ctype, final = get_size(url, args.timeout)
    ctype_l = (ctype or "").lower()
    path = urlparse(final).path.lower()
    is_m3u8 = path.endswith(".m3u8") or "mpegurl" in ctype_l
    is_html = "text/html" in ctype_l or path.endswith((".html", ".htm", ".php"))
    if is_m3u8:
        try:
            text = fetch_text(final, args.timeout)
        except requests.RequestException as e:
            entries.append({
                "url": final,
                "quality": guess_quality(final),
                "kind": "hls",
                "size_bytes": None,
                "size_human": "unknown",
                "error": str(e),
            })
            return entries
        if "#EXT-X-STREAM-INF" in text.upper():
            for v in parse_master_playlist(text, final):
                q = f"{v['height']}p" if v["height"] else guess_quality(v["url"])
                extra = {
                    "resolution": v["resolution"],
                    "bandwidth": v["bandwidth"],
                    "codecs": v["codecs"],
                }
                try:
                    entries.append(hls_entry(v["url"], q, args, extra))
                except requests.RequestException as e:
                    entries.append({
                        "url": v["url"],
                        "quality": q,
                        "kind": "hls",
                        "size_bytes": None,
                        "size_human": "unknown",
                        "error": str(e),
                        **extra,
                    })
            return entries
        try:
            entries.append(hls_entry(final, guess_quality(final), args))
        except requests.RequestException as e:
            entries.append({
                "url": final,
                "quality": guess_quality(final),
                "kind": "hls",
                "size_bytes": None,
                "size_human": "unknown",
                "error": str(e),
            })
        return entries
    if is_html:
        try:
            html = fetch_text(final, args.timeout)
        except requests.RequestException as e:
            print(f"  ! cannot read page {final}: {e}", file=sys.stderr)
            return entries
        links = extract_links(html, final)
        print(f"  > {len(links)} video link(s) found on {final}")
        for link in links:
            analyze(link, args, depth + 1, entries, seen)
        return entries
    entries.append({
        "url": final,
        "quality": guess_quality(final, Path(urlparse(final).path).name),
        "kind": "file",
        "content_type": ctype,
        "size_bytes": size,
        "size_human": human_size(size),
        "size_is_estimate": False,
    })
    return entries


def build_report(source: str, entries: list) -> dict:
    entries = sorted(entries, key=lambda e: (quality_rank(e.get("quality", "unknown")), e.get("url", "")))
    known = [e["size_bytes"] for e in entries if e.get("size_bytes")]
    total = sum(known) if known else None
    return {
        "source": source,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "video_count": len(entries),
        "videos_with_known_size": len(known),
        "total_bytes": total,
        "total_human": human_size(total),
        "total_is_complete": len(known) == len(entries) and bool(entries),
        "videos": entries,
    }


def write_json(path: str, data: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def save_reports(report: dict, outdir: str) -> list:
    os.makedirs(outdir, exist_ok=True)
    p = urlparse(report["source"])
    base = sanitize(f"{p.netloc}_{Path(p.path).name or 'index'}")
    written = []
    written.append(write_json(Path(outdir).joinpath(f"{base}_all.json"), report))
    groups = {}
    for v in report["videos"]:
        groups.setdefault(v.get("quality", "unknown"), []).append(v)
    if len(groups) > 1:
        for q, vids in sorted(groups.items(), key=lambda kv: quality_rank(kv[0])):
            known = [v["size_bytes"] for v in vids if v.get("size_bytes")]
            sub = {
                "source": report["source"],
                "generated_at": report["generated_at"],
                "quality": q,
                "video_count": len(vids),
                "total_bytes": sum(known) if known else None,
                "total_human": human_size(sum(known)) if known else "unknown",
                "videos": vids,
            }
            written.append(write_json(Path(outdir).joinpath(f"{base}_{q}.json"), sub))
    return written


def print_report(report: dict) -> None:
    line = "=" * 100
    print(line)
    print(f"SOURCE : {report['source']}")
    print(
        f"VIDEOS : {report['video_count']}   "
        f"TOTAL : {report['total_human']}"
        f"{'' if report['total_is_complete'] else '  (partial)'}"
    )
    print("-" * 100)
    for v in report["videos"]:
        flags = []
        if v.get("size_is_estimate"):
            flags.append("estimate")
        if v.get("error"):
            flags.append("ERROR")
        if v.get("segments"):
            flags.append(f"{v['segments']} segs")
        note = ("  [" + ", ".join(flags) + "]") if flags else ""
        print(f"  {v.get('quality', 'unknown'):>7} | {v['size_human']:>11} | {v['url']}{note}")
    print(line)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Report the size of videos behind direct links, web pages and HLS playlists."
    )
    ap.add_argument("urls", nargs="+", help="video url / web page / m3u8 playlist")
    ap.add_argument("--outdir", default="reports", help="where to write JSON (default: reports)")
    ap.add_argument("--timeout", type=float, default=25.0, help="HTTP timeout in seconds")
    ap.add_argument("--max-depth", type=int, default=2, help="how deep to follow links inside pages (default 2)")
    ap.add_argument(
        "--skip-segments", action="store_true", help="do not HEAD every HLS segment; estimate from BANDWIDTH"
    )
    ap.add_argument("--no-print-json", action="store_true", help="do not dump the JSON to the console")
    args = ap.parse_args(argv)
    exit_code = 0
    for url in args.urls:
        print(f"\n>>> analyzing {url}")
        try:
            entries = analyze(url, args)
        except Exception as e:
            print(f"  ! failed: {e}", file=sys.stderr)
            exit_code = 1
            continue
        if not entries:
            print("  ! no video found", file=sys.stderr)
            exit_code = 1
            continue
        report = build_report(url, entries)
        print_report(report)
        if not args.no_print_json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        for path in save_reports(report, args.outdir):
            print(f"  -> saved {path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
