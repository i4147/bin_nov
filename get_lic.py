#!/data/data/com.termux/files/usr/bin/env python
"""Fetch a license text for an author and year and cache the answer.
Looks the license kind up in the local cache (``~/.license``), validates the author and year, inserts them into the template fetched with ``gh api`` and prints the result.
Driven by :mod:`click` options ``--author``, ``--year`` (default: this year) and ``--kind``.
Usage: get_lic [-a AUTHOR] [-y YEAR] [-k KIND] Examples: $ get_lic -a 'Jane Doe' -k mit Runs as a script (``python get_lic.py``) in the directory it should act on; the module can also be imported.
"""

from datetime import date
import os
from pathlib import Path
import re
import sys
from typing import Final

import click
import ecstasy


class LicenseError(Exception):
    ERROR: Final[str] = ecstasy.beautify("<Error>: ", ecstasy.Color.Red)

    def __init__(self, message: str) -> None:
        self.message: str = self.ERROR + message
        super().__init__(self.message)


CACHE_PATH: Final[str] = Path(os.environ["HOME"]).joinpath(".license")
HIT_MESSAGE: Final[str] = ecstasy.beautify("Cache-<Hit> for {0}: '{1}'.", ecstasy.Color.Green)
MISS_MESSAGE: Final[str] = ecstasy.beautify("Cache-<Miss> for {0}.", ecstasy.Color.Red)


def get_license_kinds() -> set[str]:
    kinds: set[str] = set()
    root = Path(Path(Path(__file__).absolute()).parent).joinpath(os.pardir)
    for license_path in os.listdir(Path(root).joinpath("files")):
        name = (str(Path(Path(license_path).name).with_suffix("")), Path(Path(license_path).name).suffix)[0]
        kinds.add(name)
    return kinds


LICENSE_KINDS: Final[set[str]] = get_license_kinds()


def read_cache() -> str:
    try:
        with open(CACHE_PATH, "rt") as source:
            return source.read()
    except OSError:
        msg = "Could not read from cache."
        raise LicenseError(msg)


def read_author(cache: str) -> str:
    match = re.search(r"\s*author\s*=\s*([a-zA-Z -]+)", cache)
    if match is None:
        raise LicenseError(MISS_MESSAGE.format("author") + "You must supply an author with the -a switch. ")
    author = match.group(1)
    click.echo(HIT_MESSAGE.format("author", author), file=sys.stderr)
    return author


def read_kind(cache: str) -> str:
    match = re.search(r"\s*kind\s*=\s*(\w+)", cache)
    if match is None:
        raise LicenseError(MISS_MESSAGE.format("kind") + "You must supply a kind with the -k switch.")
    kind = match.group(1)
    click.echo(HIT_MESSAGE.format("kind", kind), file=sys.stderr)
    return kind


def read(author: str | None, kind: str | None) -> tuple[str, str]:
    if not Path(CACHE_PATH).exists():
        msg = "No cache found. You must supply at least -a and -k."
        raise LicenseError(msg)
    cache_data = read_cache()
    if author is None:
        author = read_author(cache_data)
    if kind is None:
        kind = read_kind(cache_data)
    return author, kind


def write(author: str, kind: str) -> None:
    assert all(i is not None for i in [author, kind])
    template = "author={0}\nkind={1}"
    with open(CACHE_PATH, "wt") as destination:
        destination.write(template.format(author, kind))


def fetch(kind: str) -> str:
    path = Path(Path(Path(__file__).absolute()).parent).joinpath(os.pardir, f"files/{kind}.txt")
    with open(path, "rt") as source:
        return source.read()


def insert(template: str, author: str, year: str) -> str:
    template = re.sub(r"<author>", author, template)
    template = re.sub(r"<year>", year, template)
    return template


def validate(author: str, year: str, kind: str) -> None:
    author_pattern = r"^[a-zA-Z -]+$"
    year_pattern = r"^\d{4}$"
    if not re.match(author_pattern, author):
        msg = f"Invalid author: {author}. Must match '{author_pattern}'."
        raise LicenseError(msg)
    if not re.match(year_pattern, year):
        msg = f"Invalid year: {year}. Must match '{year_pattern}'."
        raise LicenseError(msg)
    if kind not in LICENSE_KINDS:
        raise LicenseError(f"Invalid license kind: {kind}. Must be one of: " + ", ".join(LICENSE_KINDS))


def get(author: str | None, year: str, kind: str | None) -> str:
    assert year is not None, "Year should have been defaulted by click"
    if not author or not kind:
        author, kind = read(author, kind)
    validate(author, year, kind)
    template = fetch(kind)
    text = insert(template, author, year)
    write(author, kind)
    return text


@click.command(help="A license fetcher.")
@click.option("-a", "--author", nargs=1, help="The name of the author.")
@click.option(
    "-y",
    "--year",
    nargs=1,
    default=str(date.today().year),
    help="The year the program was created in.",
)
@click.option(
    "-k",
    "--kind",
    nargs=1,
    help="The kind of license to fetch.",
    type=click.Choice(list(LICENSE_KINDS)),
)
def li(author: str | None, year: str, kind: str | None) -> None:
    try:
        result = get(author, year, kind.lower() if kind else None)
        click.echo(result, nl=False)
    except LicenseError as e:
        click.echo(e.message)


def main() -> None:
    li()


if __name__ == "__main__":
    main()
