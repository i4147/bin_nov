#!/data/data/com.termux/files/usr/bin/env python
import argparse
import multiprocessing as mp
import os

import requests
from loguru import logger

API_URL = "https://api.github.com/gists/public"
TOKEN = os.environ.get("GIST_TOKEN")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "gist-search",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def fetch_page(job: tuple[int, int]) -> list[dict]:
    page, per_page = job
    try:
        resp = requests.get(
            API_URL,
            headers=HEADERS,
            params={"page": page, "per_page": per_page},
            timeout=15,
        )
        resp.raise_for_status()
        logger.debug("page {} returned {} gists", page, len(resp.json()))
        return resp.json()
    except requests.RequestException as exc:
        logger.warning("page {} failed: {}", page, exc)
        return []


def matches(gist: dict, terms: list[str], language: str | None) -> bool:
    files = gist.get("files", {})
    haystack = " ".join([gist.get("description") or "", *files.keys()]).lower()
    if not all(term in haystack for term in terms):
        return False
    if language:
        return any((f.get("language") or "").lower() == language.lower() for f in files.values())
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Search public GitHub gists by keyword")
    parser.add_argument("query", nargs="+", help="Terms that must all appear in the description or filenames")
    parser.add_argument("-n", "--pages", type=int, default=5, help="Number of pages to scan (default: 5)")
    parser.add_argument("-s", "--per-page", type=int, default=100, choices=range(1, 101), metavar="1-100")
    parser.add_argument("-l", "--language", help="Only show gists containing this language, e.g. python")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug logging")
    args = parser.parse_args()

    logger.remove()
    logger.add(lambda msg: print(msg, end=""), level="DEBUG" if args.verbose else "INFO")

    terms = [t.lower() for t in args.query]
    jobs = [(page, args.per_page) for page in range(1, args.pages + 1)]
    seen: set[str] = set()
    hits = 0

    with mp.Pool(processes=4) as pool:
        for gists in pool.imap_unordered(fetch_page, jobs):
            for gist in gists:
                gid = gist["id"]
                if gid in seen or not matches(gist, terms, args.language):
                    continue
                seen.add(gid)
                hits += 1
                files = ", ".join(gist.get("files", {}).keys())
                logger.success(
                    "{}\n  {}\n  files: {}", gist.get("description") or "(no description)", gist["html_url"], files
                )

    logger.info("Scanned {} pages, found {} matching gists", args.pages, hits)


if __name__ == "__main__":
    main()
