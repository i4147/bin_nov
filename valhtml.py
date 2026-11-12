#!/data/data/com.termux/files/usr/bin/env python
"""Check HTML files for unbalanced tags with several parsers.
``--backend`` selects ``html.parser`` (default), lxml, html5lib, BeautifulSoup or tidy; each backend reports the mismatched closing tags, the unclosed elements and the stray end tags of the files below the current directory.
Usage: valhtml [-b html.parser|lxml|html5lib|bs4|tidy] [files ...] Options: -b, --backend Examples: $ valhtml -b html5lib page.html Command-line script: run ``python valhtml.py`` or the ``valhtml`` command.
The module is importable as well."""

import argparse
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from loguru import logger

VOID_ELEMENTS: set[str] = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def _pos(self) -> str:
        line, col = self.getpos()
        return f"line {line}, col {col}"

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in VOID_ELEMENTS:
            return
        self.stack.append((tag, self._pos()))

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            self.errors.append(f"{self._pos()}: closing tag </{tag}> for void element")
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                while len(self.stack) - 1 > i:
                    t, p = self.stack.pop()
                    self.errors.append(f"unclosed <{t}> opened at {p}")
                self.stack.pop()
                return
        self.errors.append(f"{self._pos()}: stray closing tag </{tag}>")

    def finish(self):
        super().close()
        for tag, pos in self.stack:
            self.errors.append(f"unclosed <{tag}> opened at {pos}")
        return self.errors


def check_tags(raw: str):
    checker = _TagChecker()
    try:
        checker.feed(raw)
        return checker.finish()
    except Exception as e:
        return [f"tokenizer error: {e}"]


class _StackParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.errors = []
        self.output = []

    def _pos(self) -> str:
        line, col = self.getpos()
        return f"line {line}, col {col}"

    @staticmethod
    def _attrs(attrs: Iterable[Any]) -> str:
        out = []
        for k, v in attrs:
            out.append(f" {k}" if v is None else f' {k}="{escape(v, quote=True)}"')
        return "".join(out)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.output.append(f"<{tag}{self._attrs(attrs)}>")
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, self._pos()))

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        self.output.append(f"<{tag}{self._attrs(attrs)}/>")

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            self.errors.append(f"{self._pos()}: closing </{tag}> for void element; removed")
            return
        match = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                match = i
                break
        if match is None:
            self.errors.append(f"{self._pos()}: unexpected </{tag}>; removed")
            return
        while len(self.stack) - 1 > match:
            t, p = self.stack.pop()
            self.errors.append(f"{self._pos()}: unclosed <{t}> at {p}; inserted </{t}>")
            self.output.append(f"</{t}>")
        self.stack.pop()
        self.output.append(f"</{tag}>")

    def handle_data(self, data: Any) -> None:
        self.output.append(data)

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.output.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.output.append(f"<?{data}>")

    def finish(self) -> tuple[str,]:
        super().close()
        while self.stack:
            tag, pos = self.stack.pop()
            self.errors.append(f"EOF: unclosed <{tag}> at {pos}; inserted </{tag}>")
            self.output.append(f"</{tag}>")
        return "".join(self.output), self.errors


def backend_html_parser(raw: str) -> tuple[str,]:
    p = _StackParser()
    p.feed(raw)
    return p.finish()


def backend_lxml(raw: str):
    from lxml import (
        etree,
        html as lxml_html,
    )

    errors = []
    try:
        doc = lxml_html.fromstring(raw)
    except Exception as e:
        return raw, [f"lxml: {e}"]
    fixed = etree.tostring(doc, method="html", encoding="unicode")
    if fixed.strip() != raw.strip():
        errors.append("lxml: document was re-serialized (structure changed)")
    return fixed, errors


def backend_html5lib(raw: str):
    from xml.etree import ElementTree as ET

    import html5lib

    errors = []
    try:
        tree = html5lib.parse(raw, treebuilder="etree", namespaceHTMLElements=False)
    except Exception as e:
        return raw, [f"html5lib: {e}"]
    ET.register_namespace("", "")
    fixed = ET.tostring(tree.getroot(), encoding="unicode", method="html")
    if fixed.strip() != raw.strip():
        errors.append("html5lib: browser-equivalent tree differs from source")
    return fixed, errors


def backend_beautifulsoup(raw: str):
    from bs4 import BeautifulSoup

    errors = []
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception as e:
        return raw, [f"bs4: {e}"]
    fixed = str(soup)
    if fixed != raw:
        errors.append("bs4: re-serialized output differs from source")
    return fixed, errors


def backend_tidy(raw: str):
    from tidylib import tidy_document

    options = {
        "output-xhtml": 0,
        "output-html": 1,
        "show-warnings": 1,
        "show-errors": 0,
        "quiet": 1,
        "indent": 0,
        "wrap": 0,
        "char-encoding": "utf8",
    }
    try:
        fixed, msgs = tidy_document(raw, options=options)
    except Exception as e:
        return raw, [f"tidy: {e}"]
    errors = [f"tidy: {m.strip()}" for m in msgs.splitlines() if m.strip()]
    return fixed, errors


BACKENDS: dict[str, Any] = {
    "html.parser": backend_html_parser,
    "lxml": backend_lxml,
    "html5lib": backend_html5lib,
    "bs4": backend_beautifulsoup,
    "tidy": backend_tidy,
}


def process_file(path: Path, backend_fn: Callable[..., Any]) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            raw = path.read_text(encoding="latin-1")
            logger.debug(f"{path}: latin-1 fallback")
        except Exception as e:
            logger.error(f"{path}: cannot decode: {e}")
            return "error"
    except Exception as e:
        logger.error(f"{path}: cannot read: {e}")
        return "error"
    balance_errors = check_tags(raw)
    if balance_errors:
        logger.warning(f"{path}: {len(balance_errors)} unbalanced tag(s) detected in source")
        for err in balance_errors:
            logger.warning(f"{path}: [balance] {err}")
    else:
        logger.debug(f"{path}: opening/closing tags balanced")
    try:
        fixed, errors = backend_fn(raw)
    except Exception as e:
        logger.error(f"{path}: backend exception: {e}")
        return "error"
    backend_errors = [e for e in errors if e not in balance_errors]
    for err in backend_errors:
        logger.warning(f"{path}: [backend] {err}")
    if fixed == raw:
        if balance_errors or backend_errors:
            logger.info(f"{path}: issues detected but output unchanged")
        else:
            logger.debug(f"{path}: OK")
        return "ok" if not balance_errors else "error"
    try:
        path.write_text(fixed, encoding="utf-8")
    except Exception as e:
        logger.error(f"{path}: cannot write: {e}")
        return "error"
    logger.success(f"{path}: repaired")
    return "fixed"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-b", "--backend", choices=sorted(BACKENDS), default="html.parser")
    args = ap.parse_args()
    backend_fn = BACKENDS[args.backend]
    logger.info(f"Using backend: {args.backend}")
    cwd = Path.cwd()
    files = sorted(set(cwd.rglob("*.html")) | set(cwd.rglob("*.htm")))
    if not files:
        logger.info(f"No HTML files under {cwd}")
        return
    logger.info(f"Found {len(files)} HTML file(s)")
    counts = {"ok": 0, "fixed": 0, "error": 0}
    for f in files:
        counts[process_file(f, backend_fn)] += 1
    logger.info(f"Summary -> OK: {counts['ok']}, Fixed: {counts['fixed']}, Errors: {counts['error']}")


if __name__ == "__main__":
    main()
