#!/data/data/com.termux/files/usr/bin/env python
"""Strip selected tags and attributes from HTML with BeautifulSoup.
A click CLI that removes the tags by bundle (``hs`` for the headings, ``metadata``, ``structure`` ...) or by name, can keep only the ``href``/``alt``/``meta`` attributes, drops the hidden elements and rewrites the document with normalised whitespace.
Usage: striptags [options] FILE.html Examples: $ striptags page.html --remove hs $ striptags --help Runs as a script (``python striptags.py``) in the directory it should act on; the module can also be imported."""

import re
from typing import Any, Iterable, Optional, TextIO

from bs4 import BeautifulSoup, Comment, NavigableString
import click


NEWLINE_ELEMENTS: tuple[str, ...] = (
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "center",
    "dd",
    "dir",
    "div",
    "dl",
    "dt",
    "figure",
    "figcaption",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hgroup",
    "hr",
    "html",
    "legend",
    "listing",
    "menu",
    "nav",
    "ol",
    "p",
    "plaintext",
    "pre",
    "section",
    "summary",
    "ul",
    "xmp",
    "li",
)
DISPLAY_NONE_SELECTORS: list[str] = [
    "[hidden]",
    "area",
    "base",
    "basefont",
    "command",
    "datalist",
    "head",
    "input[type=hidden]",
    "link",
    "menu[type=context]",
    "meta",
    "noembed",
    "noframes",
    "param",
    "rp",
    "script",
    "source",
    "style",
    "track",
    "title",
]
SELF_CLOSING_TAGS: set[str] = {
    "area",
    "base",
    "br",
    "col",
    "command",
    "embed",
    "hr",
    "img",
    "input",
    "keygen",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
BUNDLES: dict[str, tuple[Any, ...]] = {
    "hs": ("h1", "h2", "h3", "h4", "h5", "h6"),
    "metadata": ("title", "meta"),
    "structure": ("header", "nav", "main", "article", "section", "aside", "footer"),
    "tables": (
        "table",
        "tr",
        "td",
        "th",
        "thead",
        "tbody",
        "tfoot",
        "caption",
        "colgroup",
        "col",
    ),
    "lists": ("ul", "ol", "li", "dl", "dd", "dt"),
}
ATTRS_TO_KEEP: dict[str, set[str]] = {
    "a": {"href"},
    "img": {"alt"},
    "meta": {"name", "value", "property", "content"},
}
_whitespace_re = re.compile(r"\s+")


def repl(m: re.Match[str]) -> str:
    newline_count = m.group(0).count("\n")
    if newline_count >= 2:
        return "\n\n"
    elif newline_count == 1:
        return "\n"
    else:
        return " "


def tag_with_attributes(node: Any, content: str, all_attrs = False) -> str:
    to_keep = {"id", "class"}
    to_keep.update(ATTRS_TO_KEEP.get(node.name) or [])
    bits = [f"<{node.name}"]
    for key, value in list(dict(node.attrs).items()):
        if all_attrs or (key in to_keep):
            bits.append(f'{key}="{value}"')
    output = " ".join(bits) + ">" + content
    if node.name not in SELF_CLOSING_TAGS:
        output += f"</{node.name}>"
    return output


def process_node(node: Any, minify: Any, keep_tags: str, all_attrs = False):
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        if minify:
            minified = _whitespace_re.sub(repl, node)
            if minified == "\n":
                minified = " "
            return minified
        else:
            return node
    elif node.name == "pre":
        s = str(node.text)
        if "pre" in keep_tags:
            return tag_with_attributes(node, s, all_attrs)
        else:
            return s
    else:
        bits = [process_node(child, minify, keep_tags, all_attrs) for child in node.contents]
        s = "".join(bits)
        if node.name in keep_tags:
            s = tag_with_attributes(node, s, all_attrs)
        return s


def strip_tags(
    input: str,
    selectors: Optional[Iterable[str]] = None,
    *,
    removes: Optional[Iterable[str]] = None,
    minify: bool = False,
    remove_blank_lines: bool = False,
    first: bool = False,
    keep_tags: Optional[Iterable[str]] = None,
    all_attrs: bool = False,
) -> str:
    soup = BeautifulSoup(input, "html5lib", multi_valued_attributes=False)
    if not selectors:
        selectors = ["html"]
    output_bits = []
    if removes:
        for remove in removes:
            for tag in soup.select(remove):
                tag.decompose()
    keep_tags = keep_tags or []
    if keep_tags:
        expanded_keep_tags = []
        for tag in keep_tags:
            if tag in BUNDLES:
                expanded_keep_tags.extend(BUNDLES[tag])
            else:
                expanded_keep_tags.append(tag)
        keep_tags = expanded_keep_tags

    def should_keep(element: Any) -> bool:
        if element.name in keep_tags:
            return True
        return any(element.find(tag_name) for tag_name in keep_tags)

    for none_selector in DISPLAY_NONE_SELECTORS:
        for tag in soup.select(none_selector):
            if should_keep(tag):
                continue
            tag.decompose()
    if "img" not in keep_tags:
        for img in soup.select("img[alt]"):
            img.replace_with(img["alt"])
    break_out = False
    for selector in selectors:
        for element in soup.select(selector):
            output_bits.append(process_node(element, minify, keep_tags, all_attrs))
            if element.name in NEWLINE_ELEMENTS:
                output_bits.append("\n")
            if element.tail:
                output_bits.append(element.tail)
            if first:
                break_out = True
                break
        if break_out:
            break
    output = "".join(output_bits).strip()
    if remove_blank_lines:
        output = "\n".join(line for line in output.splitlines() if line.strip())
    return output


@click.command()
@click.version_option()
@click.argument("selectors", nargs=-1)
@click.option(
    "removes",
    "-r",
    "--remove",
    multiple=True,
    help="Remove content in these selectors",
)
@click.option("-i", "--input", type=click.File("rb"), default="-", help="Input file")
@click.option("-m", "--minify", is_flag=True, help="Minify whitespace")
@click.option("keep_tags", "-t", "--keep-tag", multiple=True, help="Keep these <tags>")
@click.option("--all-attrs", is_flag=True, help="Include all attributes on kept tags")
@click.option("--first", is_flag=True, help="First element matching the selectors")
def cli(selectors: Any, removes: Any, input: TextIO, minify: Any, keep_tags: Any, all_attrs: list[Any], first: Any) -> None:
    input_text = input.read()
    if isinstance(input_text, bytes):
        input_text = input_text.decode("utf-8")
    final = strip_tags(
        input_text,
        selectors,
        removes=removes,
        minify=minify,
        remove_blank_lines=minify,
        first=first,
        keep_tags=keep_tags,
        all_attrs=all_attrs,
    )
    click.echo(final)


if __name__ == "__main__":
    cli()
