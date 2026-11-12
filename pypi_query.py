#!/data/data/com.termux/files/usr/bin/env python
"""PyPI queries: newest packages (RSS), name search, download stats.
Analysis / deduplication ------------------------ - ``get_latest_pkgs_added_to_pypi.py`` (``/rss/updates.xml`` -> file) and ``get_pypi_rssfeed.py`` (``/rss/packages.xml`` -> screen) parse the same RSS shape: one ``latest`` command with ``--feed updates|packages`` and txt/json/csv output.
(Note the feeds differ: *packages* = brand-new projects, *updates* = new releases.) - ``psearch.py`` downloads the whole ``pypi.org/pypi/_/json`` index and substring-filters locally -> ``search`` (plus ``info`` for one exact package via the normal JSON API, which is what most searches actually want).
- ``pypistats.py`` -> ``stats``; its disabled TLS verification (``ssl.CERT_NONE``) was removed, and its import-time ``PACKAGE = sys.argv[1]`` crash is gone.
HTTP uses ``requests`` when installed, stdlib ``urllib`` otherwise.
Examples -------- python3 pypi_query_tool.py latest --limit 20 python3 pypi_query_tool.py latest --feed packages --format json -o new.json python3 pypi_query_tool.py search requests --limit 5 python3 pypi_query_tool.py info requests python3 pypi_query_tool.py stats requests --period month
"""

import argparse
from collections import defaultdict
from collections.abc import Sequence
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import urllib.request
import xml.etree.ElementTree as ET

FEEDS: dict[str, str] = {
    "updates": "https://pypi.org/rss/updates.xml",
    "packages": "https://pypi.org/rss/packages.xml",
}
SIMPLE_INDEX: str = "https://pypi.org/simple/"


@dataclass(slots=True)
class FeedPackage:
    """One RSS item."""

    package_name: str
    version: str
    title: str
    link: str
    description: str
    pub_date: str


@dataclass(slots=True)
class SearchHit:
    """One package-index search result."""

    name: str
    url: str

    def __str__(self) -> str:
        return f"{self.name}\n  {self.url}"


def fetch(url: str, timeout: float, as_json: bool = False) -> object:
    """Fetch *url* as text (or parsed JSON)."""
    headers: dict[str, str] = {"User-Agent": "pypi-query-tool"}
    if as_json:
        headers["Accept"] = "application/vnd.pypi.simple.v1+json, application/json"
    try:
        import requests

        response = requests.get(url, timeout=timeout, headers=headers)
        response.raise_for_status()
        return response.json() if as_json else response.text
    except ImportError:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
            return json.loads(body.decode()) if as_json else body.decode("utf-8", errors="replace")


def parse_feed(xml_text: str) -> list[FeedPackage]:
    """Parse a PyPI RSS feed into package records."""
    root: ET.Element = ET.fromstring(xml_text)
    packages: list[FeedPackage] = []
    for item in root.findall(".//item"):
        title: str = item.findtext("title", "") or ""
        parts: list[str] = title.split()
        packages.append(
            FeedPackage(
                package_name=parts[0] if parts else "Unknown",
                version=parts[1] if len(parts) > 1 else "",
                title=title,
                link=item.findtext("link", "") or "",
                description=item.findtext("description", "") or "",
                pub_date=item.findtext("pubDate", "") or "",
            )
        )
    return packages


def cmd_latest(args: argparse.Namespace) -> int:
    """Show/save the newest packages/releases from a PyPI RSS feed."""
    xml_text: object = fetch(FEEDS[args.feed], args.timeout)
    packages: list[FeedPackage] = parse_feed(str(xml_text))
    shown: list[FeedPackage] = packages[: args.limit] if args.limit else packages
    stamp: str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if args.output is None and args.format is None:
        print(f"PyPI {args.feed} feed: {len(packages)} item(s), showing {len(shown)} (fetched {stamp})")
        for index, pkg in enumerate(shown, start=1):
            print(f"\n#{index} {pkg.package_name} {pkg.version}\n  {pkg.link}\n  {pkg.pub_date}")
            if pkg.description:
                print(f"  {pkg.description[:160]}")
        return 0
    fmt: str = args.format or "json"
    output: Path = args.output or Path(f"pypi_{args.feed}.{fmt}")
    if fmt == "json":
        output.write_text(json.dumps([asdict(p) for p in shown], indent=2, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(asdict(shown[0]).keys()) if shown else ["package_name"])
            writer.writeheader()
            for pkg in shown:
                writer.writerow(asdict(pkg))
    else:
        lines: list[str] = [f"{p.package_name} {p.version} - {p.link} ({p.pub_date})" for p in shown]
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved {len(shown)} package(s) to {output} ({stamp})")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Substring search over every PyPI project name (simple index)."""
    data: object = fetch(SIMPLE_INDEX, args.timeout, as_json=True)
    projects: object = data.get("projects", []) if isinstance(data, dict) else []
    needle: str = args.query.lower()
    hits: list[SearchHit] = []
    if isinstance(projects, list):
        for entry in projects:
            name: object = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and needle in name.lower():
                hits.append(SearchHit(name=name, url=f"https://pypi.org/project/{name}/"))
    hits.sort(key=lambda hit: hit.name.lower())
    if args.limit:
        hits = hits[: args.limit]
    if not hits:
        print(f"No packages found for {args.query!r}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(h) for h in hits], indent=2))
    else:
        print(f"Found {len(hits)} package(s):\n")
        for hit in hits:
            print(f"{hit}\n")
    print("(Exact metadata: use the `info` subcommand.)", file=sys.stderr)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Show PyPI JSON metadata for one exact package name."""
    data: object = fetch(f"https://pypi.org/pypi/{args.package}/json", args.timeout, as_json=True)
    info: object = data.get("info", {}) if isinstance(data, dict) else {}
    if not isinstance(info, dict):
        print("Unexpected API response.", file=sys.stderr)
        return 1
    print(f"{info.get('name')} ({info.get('version')})")
    print(f"  {info.get('summary')}")
    print(f"  {info.get('package_url')}")
    for key in ("author", "license", "requires_python", "home_page"):
        if info.get(key):
            print(f"  {key}: {info[key]}")
    if args.full:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def stats_data(package: str, kind: str, period: str, timeout: float) -> dict[str, float]:
    """Average daily downloads per category from pypistats.org."""
    url: str = f"https://pypistats.org/api/packages/{package}/{kind}?period={period}"
    data: object = fetch(url, timeout, as_json=True)
    rows: object = data.get("data", []) if isinstance(data, dict) else []
    counts: dict[str, int] = defaultdict(int)
    days: dict[str, int] = defaultdict(int)
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                category: str = str(row.get("category", "?"))
                counts[category] += int(str(row.get("downloads", 0)))
                days[category] += 1
    return {cat: counts[cat] / days[cat] for cat in counts}


def version_key(item: tuple[str, float]) -> tuple[int, ...]:
    """Sort key: numeric python-minor versions, newest first-friendly."""
    version: str = item[0]
    return tuple(int(p) for p in version.split(".")) if version.replace(".", "").isdigit() else (2**32,)


def print_stats(title: str, stats: dict[str, float], by_version: bool) -> float:
    """Print one stats table; return the total per-day downloads."""
    print(title)
    if not stats:
        print("  (no data)")
        return 0.0
    total: float = sum(stats.values())
    width: int = max(len(k) for k in stats)
    order: list[tuple[str, float]] = sorted(stats.items(), key=version_key if by_version else None, reverse=True)
    cumulative: float = 0.0
    for category, count in order:
        cumulative += count
        share: float = cumulative / total * 100 if total else 0.0
        print(f"  {category:<{width}}: {count:12,.1f} / day ({share:5.1f}%)")
    return total


def cmd_stats(args: argparse.Namespace) -> int:
    """Download statistics by Python version and OS (pypistats.org)."""
    by_python: dict[str, float] = stats_data(args.package, "python_minor", args.period, args.timeout)
    print_stats("Downloads by Python version:", by_python, by_version=True)
    print()
    by_system: dict[str, float] = stats_data(args.package, "system", args.period, args.timeout)
    total: float = print_stats("Downloads by system:", by_system, by_version=False)
    days: dict[str, int] = {"month": 30, "week": 7, "day": 1}
    print(f"\nTotal downloads per month: {total * days.get(args.period, 30):,.1f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="PyPI newest/search/stats queries.")
    parser.add_argument("-t", "--timeout", type=float, default=30.0, help="HTTP timeout s (default: 30)")
    sub = parser.add_subparsers(dest="command", required=True)
    la = sub.add_parser("latest", help="Newest packages/releases (RSS)")
    la.add_argument(
        "--feed", choices=sorted(FEEDS), default="updates", help="updates: new releases; packages: new projects"
    )
    la.add_argument("-n", "--limit", "--num-packages", type=int, default=None, dest="limit")
    la.add_argument("-f", "--format", choices=("json", "csv", "txt"), default=None)
    la.add_argument("-o", "--output", type=Path, default=None, help="Default: print to screen")
    la.set_defaults(handler=cmd_latest)
    se = sub.add_parser("search", help="Substring search over project names")
    se.add_argument("query")
    se.add_argument("-l", "--limit", type=int, default=None)
    se.add_argument("-j", "--json", action="store_true")
    se.set_defaults(handler=cmd_search)
    inf = sub.add_parser("info", help="Metadata for one exact package")
    inf.add_argument("package")
    inf.add_argument("--full", action="store_true", help="Dump the full info dict")
    inf.set_defaults(handler=cmd_info)
    st = sub.add_parser("stats", help="Download stats (pypistats.org)")
    st.add_argument("package")
    st.add_argument("--period", choices=("day", "week", "month"), default="month")
    st.set_defaults(handler=cmd_stats)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
