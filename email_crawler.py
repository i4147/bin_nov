#!/data/data/com.termux/files/usr/bin/env python
"""Crawl a website two levels deep, cache the pages in SQLite and collect e-mail addresses.
Starts at the URL given on the command line, follows same-host links, stores each page in ``data/crawler.sqlite`` and its HTML in ``data/html/``, then writes the harvested addresses to ``data/emails.csv`` and the domains to ``data/domains.csv``.
Logs go to ``logs/``.
Usage: email_crawler URL Examples: $ email_crawler https://example.com Runs as a script (``python email_crawler.py``) in the directory it should act on; the module can also be imported."""

from pathlib import Path
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

from loguru import logger
from sqlalchemy import Boolean, Column, Integer, MetaData, Table, Unicode, create_engine, select


DEBUG = True
DATABASE_NAME = "data/crawler.sqlite"
HTML_DIR = Path("data/html")
EMAILS_FILENAME = Path("data/emails.csv")
DOMAINS_FILENAME = Path("data/domains.csv")
LOG_DIR = Path("logs")
MAX_SEARCH_RESULTS = 150


LOG_DIR.mkdir(parents=True, exist_ok=True)
logger.add(LOG_DIR / "pycrawler.log", rotation="midnight", retention=1, level="DEBUG" if DEBUG else "INFO")


class CrawlerDb:
    def __init__(self) -> None:
        self.connected = False
        self.engine = None
        self.connection = None
        self.metadata = None
        self.website_table = None

    def connect(self) -> bool:
        Path(DATABASE_NAME).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{DATABASE_NAME}")
        self.connection = self.engine.connect()
        self.connected = bool(self.connection)
        self.metadata = MetaData()
        self.website_table = Table(
            "website",
            self.metadata,
            Column("id", Integer, primary_key=True),
            Column("url", Unicode, nullable=False),
            Column("has_crawled", Boolean, default=False),
            Column("emails", Unicode, nullable=True),
        )
        self.metadata.create_all(self.engine)
        return self.connected

    def enqueue(self, url: str, emails: list[str] | None = None) -> bool:
        if not self.connected:
            return False
        s = select(self.website_table).where(self.website_table.c.url == url)
        result = self.connection.execute(s).fetchall()
        if result:
            return False
        values = {"url": str(url)}
        if emails is not None:
            values["has_crawled"] = True
            values["emails"] = ",".join(emails)
        self.connection.execute(self.website_table.insert().values(**values))
        self.connection.commit()
        return True

    def dequeue(self):
        if not self.connected:
            return False
        s = select(self.website_table).where(self.website_table.c.has_crawled.is_(False)).limit(1)
        result = self.connection.execute(s).fetchall()
        return result[0] if result else False

    def crawled(self, website, new_emails: str | None = None) -> None:
        if not self.connected:
            return
        stmt = (
            self.website_table
            .update()
            .where(self.website_table.c.id == website.id)
            .values(has_crawled=True, emails=new_emails)
        )
        self.connection.execute(stmt)
        self.connection.commit()

    def get_all_emails(self) -> set[str] | None:
        if not self.connected:
            return None
        results = self.connection.execute(select(self.website_table)).fetchall()
        email_set: set[str] = set()
        for row in results:
            if row.emails:
                email_set.update(e.strip() for e in row.emails.split(",") if e.strip())
        return email_set

    def get_all_domains(self) -> set[str] | None:
        if not self.connected:
            return None
        results = self.connection.execute(select(self.website_table)).fetchall()
        domain_set: set[str] = set()
        for row in results:
            if not row.url:
                continue
            parsed = urllib.parse.urlparse(row.url)
            if not parsed.hostname:
                continue
            parts = parsed.hostname.split(".")
            if len(parts) >= 3 and len(parts[-2]) < 4:
                domain = ".".join(parts[-3:])
            else:
                domain = ".".join(parts[-2:]) if len(parts) >= 2 else parsed.hostname
            domain_set.add(domain)
        return domain_set

    def save_html(self, filename: str, html: str) -> None:
        HTML_DIR.mkdir(parents=True, exist_ok=True)
        path = HTML_DIR / filename
        path.write_text(html, encoding="utf-8", errors="replace")

    def close(self) -> None:
        if self.connection:
            self.connection.close()
        if self.engine:
            self.engine.dispose()
        self.connected = False


google_adurl_regex = re.compile(r'adurl=(.*?)"', re.IGNORECASE)
google_url_regex = re.compile(r"url\?q=(.*?)&amp;sa=", re.IGNORECASE)
email_regex = re.compile(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,24})", re.IGNORECASE)
url_regex = re.compile(r"""<a\s[^>]*?href=['"](.*?)['"]""", re.IGNORECASE)
db = CrawlerDb()
db.connect()


def retrieve_html(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Just-Crawling/0.2"})
    try:
        logger.info("Crawling {}", url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception as exc:
        logger.error("Exception at url: {}\n{}", url, exc)
        return None


def find_emails_in_html(html: str | None) -> set[str]:
    if not html:
        return set()
    return {m.group(1).lower() for m in email_regex.finditer(html)}


def find_links_in_html_with_same_hostname(url: str, html: str | None) -> set[str]:
    if not html:
        return set()
    parsed = urllib.parse.urlparse(url)
    base_netloc = parsed.netloc.lower()
    links: set[str] = set()
    for href in url_regex.findall(html):
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        try:
            absolute = urllib.parse.urljoin(url, href)
            link_parsed = urllib.parse.urlparse(absolute)
            if link_parsed.netloc.lower() == base_netloc:
                links.add(absolute)
        except Exception:
            continue
    return links


def find_emails_2_level_deep(url: str) -> set[str]:
    html = retrieve_html(url)
    emails = find_emails_in_html(html)
    if emails:
        return emails
    logger.info("No email at level 1 – proceeding to level 2")
    for link in find_links_in_html_with_same_hostname(url, html):
        page = retrieve_html(link)
        if page is None:
            continue
        found = find_emails_in_html(page)
        if found:
            db.enqueue(link, list(found))
    return set()


def crawl(keywords: str) -> None:
    logger.info("-" * 40)
    logger.info("Keywords to Google for: {}", keywords)
    logger.info("-" * 40)
    for start in range(0, MAX_SEARCH_RESULTS, 10):
        query = urllib.parse.urlencode({"q": keywords})
        search_url = f"https://www.google.com/search?{query}&start={start}"
        data = retrieve_html(search_url)
        if not data:
            continue
        for u in google_url_regex.findall(data):
            db.enqueue(urllib.parse.unquote(u))
        for u in google_adurl_regex.findall(data):
            db.enqueue(urllib.parse.unquote(u))
    while True:
        uncrawled = db.dequeue()
        if uncrawled is False:
            break
        email_set = find_emails_2_level_deep(uncrawled.url)
        db.crawled(uncrawled, ",".join(email_set) if email_set else None)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python email_crawler.py <keywords>")
        print("  python email_crawler.py --emails | -e")
        print("  python email_crawler.py --domains | -d")
        sys.exit(1)
    arg = sys.argv[1].lower()
    try:
        if arg in ("--emails", "-e"):
            logger.info("=" * 40)
            emails = db.get_all_emails() or set()
            logger.info("There are {} emails", len(emails))
            EMAILS_FILENAME.parent.mkdir(parents=True, exist_ok=True)
            EMAILS_FILENAME.write_text("\n".join(sorted(emails)), encoding="utf-8")
            logger.info("All emails saved to {}", EMAILS_FILENAME)
            logger.info("=" * 40)
        elif arg in ("--domains", "-d"):
            logger.info("=" * 40)
            domains = db.get_all_domains() or set()
            logger.info("There are {} domains", len(domains))
            DOMAINS_FILENAME.parent.mkdir(parents=True, exist_ok=True)
            DOMAINS_FILENAME.write_text("\n".join(sorted(domains)), encoding="utf-8")
            logger.info("All domains saved to {}", DOMAINS_FILENAME)
            logger.info("=" * 40)
        else:
            crawl(sys.argv[1])
    except KeyboardInterrupt:
        logger.error("Stopping (KeyboardInterrupt)")
        sys.exit(1)
    except Exception as exc:
        logger.error("EXCEPTION: {}", exc)
        traceback.print_exc()
    finally:
        db.close()


if __name__ == "__main__":
    main()
