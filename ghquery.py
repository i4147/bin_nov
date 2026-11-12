#!/data/data/com.termux/files/usr/bin/env python
"""Query GitHub users, repositories and trending pages.
Subcommands: ``user-repos`` lists the repositories of a user (``--python-only``), ``search`` runs the search API with the presets, ``trending``/``trending-users`` scrape github.com/trending and ``repo-size`` prints the size of one repository; the output goes to the screen or a file.
Usage: github_query_tool {user-repos,search,trending,trending-users,repo-size} [options] Positional arguments: username repo User/repo or https://github.com/user/repo Subcommands: user-repos List a user's repos search Search repositories trending Github.com/trending scrape trending-users New users by followers + their Python repos repo-size Size of one repository Options: -o, --output Output file (default: print) --format --python-only -q, --query Raw GitHub search query --preset Active: latest updated Python (ghpy); new: created recently (ghpy10) --days Window for --preset new (default: 10) --sort --per-page --language --timeframe ...
plus 1 more; run ``github_query_tool --help`` for the full list.
Examples: $ github_query_tool trending --language python --days 7 Command-line script: run ``python github_query_tool.py`` or the ``github_query_tool`` command.
The module is importable as well."""

import argparse
from collections.abc import Sequence
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

API: str = "https://api.github.com"


@dataclass(slots=True)
class Repo:
    name: str
    full_name: str
    url: str
    description: str = ""
    stars: int = 0
    forks: int = 0
    language: str = ""
    created_at: str = ""
    updated_at: str = ""


def load_token() -> str | None:
    token: str | None = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    env_path: Path = Path.home() / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "GITHUB_TOKEN":
                return value.strip().strip('"').strip("'")
    return None


def get_json(url: str, params: dict[str, object] | None = None) -> object:
    full_url: str = url
    if params:
        full_url = url + "?" + urllib.parse.urlencode(params)
    token: str | None = load_token()
    try:
        import requests

        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.get(full_url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except ImportError:
        request = urllib.request.Request(full_url)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", "github-query-tool")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())


def get_textpage(url: str) -> str:
    try:
        import requests

        response = requests.get(url, timeout=30, headers={"User-Agent": "github-query-tool"})
        response.raise_for_status()
        return response.text
    except ImportError:
        request = urllib.request.Request(url, headers={"User-Agent": "github-query-tool"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")


def to_repo(raw: dict[str, object]) -> Repo:

    def text(key: str) -> str:
        value: object = raw.get(key)
        return value if isinstance(value, str) else ""

    stars: object = raw.get("stargazers_count")
    forks: object = raw.get("forks_count")
    return Repo(
        name=text("name"),
        full_name=text("full_name"),
        url=text("html_url"),
        description=text("description"),
        stars=stars if isinstance(stars, int) else 0,
        forks=forks if isinstance(forks, int) else 0,
        language=text("language"),
        created_at=text("created_at"),
        updated_at=text("updated_at"),
    )


def save_repos(repos: list[Repo], output: Path | None, fmt: str) -> None:
    if output is None:
        for repo in repos:
            print(f"{repo.full_name}  ★{repo.stars}  {repo.url}")
            if repo.description:
                print(f"    {repo.description}")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json" or output.suffix == ".json":
        output.write_text(json.dumps([asdict(r) for r in repos], indent=2, ensure_ascii=False) + "\n")
    elif fmt == "csv" or output.suffix == ".csv":
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(asdict(repos[0]).keys()) if repos else ["name"])
            writer.writeheader()
            for repo in repos:
                writer.writerow(asdict(repo))
    else:
        lines: list[str] = []
        for repo in repos:
            lines.append(f"{repo.full_name}\n  URL: {repo.url}\n  Updated: {repo.updated_at}\n  Stars: {repo.stars}\n")
        output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {len(repos)} repo(s) to {output}")


def cmd_user_repos(args: argparse.Namespace) -> int:
    repos: list[Repo] = []
    page: int = 1
    while True:
        batch: object = get_json(
            f"{API}/users/{args.username}/repos", {"page": page, "per_page": 100, "sort": "updated"}
        )
        if not isinstance(batch, list) or not batch:
            break
        for raw in batch:
            if isinstance(raw, dict):
                repo = to_repo(raw)
                if not args.python_only or repo.language == "Python":
                    repos.append(repo)
        if len(batch) < 100:
            break
        page += 1
    save_repos(repos, args.output, args.format)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if args.query:
        query: str = args.query
        sort: str = args.sort
    elif args.preset == "new":
        since: str = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
        query = f"language:Python created:>{since}"
        sort = "stars"
    else:
        query = "language:Python"
        sort = "updated"
    data: object = get_json(
        f"{API}/search/repositories", {"q": query, "sort": sort, "order": "desc", "per_page": args.per_page}
    )
    items: object = data.get("items") if isinstance(data, dict) else []
    repos: list[Repo] = [to_repo(raw) for raw in items if isinstance(raw, dict)] if isinstance(items, list) else []
    save_repos(
        repos,
        (
            args.output or Path("ghpy.txt" if args.preset == "active" else "ghpy10.txt")
            if args.query is None and args.preset
            else args.output
        ),
        args.format,
    )
    return 0


def cmd_trending(args: argparse.Namespace) -> int:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("Error: trending needs beautifulsoup4 (pip install beautifulsoup4 requests).", file=sys.stderr)
        return 1
    url: str = f"https://github.com/trending/{args.language}?since={args.timeframe}"
    soup = BeautifulSoup(get_textpage(url), "html.parser")
    repos: list[Repo] = []
    for article in soup.select("article.Box-row"):
        link = article.select_one("h2 a")
        if link is None:
            continue
        href: str = str(link.get("href", "")).strip("/")
        desc: str = article.p.get_text(strip=True) if article.p else ""
        repos.append(
            Repo(
                name=href.rsplit("/", maxsplit=1)[-1],
                full_name=href,
                url=f"https://github.com/{href}",
                description=desc,
            )
        )
    out: Path | None = args.output or Path("trending_repos") / f"{args.timeframe}.{args.format}"
    save_repos(repos, out, args.format)
    return 0


def cmd_trending_users(args: argparse.Namespace) -> int:
    since: str = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    data: object = get_json(
        f"{API}/search/users",
        {"q": f"language:python created:>{since}", "sort": "followers", "order": "desc", "per_page": args.limit},
    )
    items: object = data.get("items") if isinstance(data, dict) else []
    users: list[dict[str, object]] = []
    if isinstance(items, list):
        for raw in items:
            if not isinstance(raw, dict):
                continue
            detail: object = get_json(str(raw.get("url", "")))
            if not isinstance(detail, dict):
                continue
            login: str = str(detail.get("login", ""))
            repos_raw: object = get_json(f"{API}/users/{login}/repos", {"per_page": 100, "sort": "updated"})
            repos: list[dict[str, object]] = (
                [asdict(to_repo(r)) for r in repos_raw if isinstance(r, dict) and r.get("language") == "Python"]
                if isinstance(repos_raw, list)
                else []
            )
            users.append({
                "username": login,
                "name": detail.get("name", ""),
                "followers": detail.get("followers", 0),
                "repositories": repos,
            })
            print(
                f"{login} ({detail.get('name', '')}) followers={detail.get('followers', 0)} python-repos={len(repos)}"
            )
    payload: dict[str, object] = {"timestamp": datetime.now(timezone.utc).isoformat(), "trending_users": users}
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")
    return 0


def parse_repo_spec(spec: str) -> tuple[str, str]:
    cleaned: str = spec.strip().removeprefix("https://github.com/").removeprefix("http://github.com/")
    cleaned = cleaned.rstrip("/").removesuffix(".git")
    parts: list[str] = cleaned.split("/")
    if len(parts) != 2 or not all(parts):
        msg = f"Error: expected user/repo or a github.com URL, got {spec!r}"
        raise SystemExit(msg)
    return parts[0], parts[1]


def cmd_repo_size(args: argparse.Namespace) -> int:
    user, repo = parse_repo_spec(args.repo)
    data: object = get_json(f"{API}/repos/{user}/{repo}")
    if not isinstance(data, dict):
        print("Error: unexpected API response.", file=sys.stderr)
        return 1
    size_kb: object = data.get("size")
    kib: int = size_kb if isinstance(size_kb, int) else 0
    print(f"{user}/{repo}: {kib:,} KiB ({kib / 1024:.2f} MiB, {kib / 1024 / 1024:.3f} GiB)")
    print(
        f"  stars={data.get('stargazers_count')} forks={data.get('forks_count')} "
        f"language={data.get('language')} default_branch={data.get('default_branch')}"
    )
    return 0


def add_output(child: argparse.ArgumentParser) -> None:
    child.add_argument("-o", "--output", type=Path, default=None, help="Output file (default: print)")
    child.add_argument("--format", choices=("txt", "json", "csv"), default="txt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitHub repository/user queries.")
    sub = parser.add_subparsers(dest="command", required=True)
    ur = sub.add_parser("user-repos", help="List a user's repos")
    ur.add_argument("username")
    ur.add_argument("--python-only", action="store_true")
    add_output(ur)
    ur.set_defaults(handler=cmd_user_repos)
    se = sub.add_parser("search", help="Search repositories")
    se.add_argument("-q", "--query", default=None, help="Raw GitHub search query")
    se.add_argument(
        "--preset",
        choices=("active", "new"),
        default="active",
        help="active: latest updated Python (ghpy); new: created recently (ghpy10)",
    )
    se.add_argument("--days", type=int, default=10, help="Window for --preset new (default: 10)")
    se.add_argument("--sort", choices=("stars", "updated", "forks"), default="updated")
    se.add_argument("--per-page", type=int, default=50)
    add_output(se)
    se.set_defaults(handler=cmd_search)
    tr = sub.add_parser("trending", help="github.com/trending scrape")
    tr.add_argument("--language", default="python")
    tr.add_argument("--timeframe", choices=("daily", "weekly", "monthly"), default="daily")
    add_output(tr)
    tr.set_defaults(handler=cmd_trending)
    tu = sub.add_parser("trending-users", help="New users by followers + their Python repos")
    tu.add_argument("--days", type=int, default=7)
    tu.add_argument("--limit", type=int, default=10)
    tu.add_argument("-o", "--output", type=Path, default=Path("trending_github_python_users.json"))
    tu.set_defaults(handler=cmd_trending_users)
    rs = sub.add_parser("repo-size", help="Size of one repository")
    rs.add_argument("repo", help="user/repo or https://github.com/user/repo")
    rs.set_defaults(handler=cmd_repo_size)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
