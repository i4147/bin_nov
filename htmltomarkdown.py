#!/data/data/com.termux/files/usr/bin/env python
"""Convert an HTML file to Markdown.
Builds a small document tree from the HTML and renders the supported tags (headings, lists, links, images, emphasis, tables, pre/code blocks with language detection) as Markdown.
Usage: htmltomarkdown INPUT.html [OUTPUT.md] Examples: $ htmltomarkdown page.html page.md Runs as a script (``python htmltomarkdown.py``) in the directory it should act on; the module can also be imported.
"""

from abc import ABCMeta
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import re
import sys
from typing import Final, Optional, Union, cast

SELF_CLOSING_TAGS: Final[list[str]] = [
    "area",
    "base",
    "br",
    "col",
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
]
HIGHLIGHT_LANGUAGES: Final[list[str]] = [
    "1c",
    "abnf",
    "accesslog",
    "actionscript",
    "ada",
    "angelscript",
    "apache",
    "applescript",
    "arcade",
    "arduino",
    "armasm",
    "xml",
    "asciidoc",
    "aspectj",
    "autohotkey",
    "autoit",
    "avrasm",
    "awk",
    "axapta",
    "bash",
    "basic",
    "bnf",
    "brainfuck",
    "cal",
    "capnproto",
    "ceylon",
    "clean",
    "clojure",
    "clojure-repl",
    "cmake",
    "coffeescript",
    "coq",
    "cos",
    "cpp",
    "crmsh",
    "crystal",
    "csharp",
    "csp",
    "css",
    "d",
    "markdown",
    "dart",
    "delphi",
    "diff",
    "django",
    "dns",
    "dockerfile",
    "dos",
    "dsconfig",
    "dts",
    "dust",
    "ebnf",
    "elixir",
    "elm",
    "ruby",
    "erb",
    "erlang-repl",
    "erlang",
    "excel",
    "fix",
    "flix",
    "fortran",
    "fsharp",
    "gams",
    "gauss",
    "gcode",
    "gherkin",
    "glsl",
    "gml",
    "go",
    "golo",
    "gradle",
    "groovy",
    "haml",
    "handlebars",
    "haskell",
    "haxe",
    "hsp",
    "http",
    "hy",
    "inform7",
    "ini",
    "irpf90",
    "isbl",
    "java",
    "javascript",
    "jboss-cli",
    "json",
    "julia",
    "julia-repl",
    "kotlin",
    "lasso",
    "latex",
    "ldif",
    "leaf",
    "less",
    "lisp",
    "livecodeserver",
    "livescript",
    "llvm",
    "lsl",
    "lua",
    "makefile",
    "mathematica",
    "matlab",
    "maxima",
    "mel",
    "mercury",
    "mipsasm",
    "mizar",
    "perl",
    "mojolicious",
    "monkey",
    "moonscript",
    "n1ql",
    "nginx",
    "nim",
    "nix",
    "node-repl",
    "nsis",
    "objectivec",
    "ocaml",
    "openscad",
    "oxygene",
    "parser3",
    "pf",
    "pgsql",
    "php",
    "php-template",
    "pony",
    "powershell",
    "processing",
    "profile",
    "prolog",
    "properties",
    "protobuf",
    "puppet",
    "purebasic",
    "python",
    "python-repl",
    "q",
    "qml",
    "r",
    "reasonml",
    "rib",
    "roboconf",
    "routeros",
    "rsl",
    "ruleslanguage",
    "rust",
    "sas",
    "scala",
    "scheme",
    "scilab",
    "scss",
    "shell",
    "smali",
    "smalltalk",
    "sml",
    "sqf",
    "sql_more",
    "sql",
    "stan",
    "stata",
    "step21",
    "stylus",
    "subunit",
    "swift",
    "taggerscript",
    "yaml",
    "tap",
    "tcl",
    "thrift",
    "tp",
    "twig",
    "typescript",
    "vala",
    "vbnet",
    "vbscript",
    "vbscript-html",
    "verilog",
    "vhdl",
    "vim",
    "x86asm",
    "xl",
    "xquery",
    "zephir",
]
Attr = dict[str, Optional[str]]


class Content:
    def __init__(self, data: str) -> None:
        self.data = data

    def to_str(self) -> str:
        return self.data


class Tag(metaclass=ABCMeta):
    def __init__(self, name: str, attrs: Attr) -> None:
        self.name = name
        self.attrs = attrs
        self.data = ""
        self.children: list[Element] = []

    def attr(self, attr_name: str) -> str:
        attr_value = self.attrs.get(attr_name, "")
        return cast("str", attr_value)

    def append_child(self, child: Element) -> None:
        self.children.append(child)

    def inner(self) -> str:
        return "".join(child.to_str() for child in self.children)

    def to_str(self) -> str:
        return self.inner()


Element = Union[Content, Tag]


class HTML(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("html", attrs)


class A(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("a", attrs)

    def to_str(self) -> str:
        ref = self.attr("href")
        return f"[{self.inner()}]({ref})"


class B(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("b", attrs)

    def to_str(self) -> str:
        return f"**{self.inner()}**"


class Br(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("br", attrs)

    def to_str(self) -> str:
        return "\n"


class Blockquote(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("blockquote", attrs)

    def to_str(self) -> str:
        lines = []
        for idx, child in enumerate(self.children):
            child_inner = child.to_str()
            if idx == len(self.children) - 1:
                child_inner = child_inner.rstrip("\n")
            child_lines = child_inner.split("\n")
            lines.extend([f"> {line}" for line in child_lines])
        result = "\n".join(lines)
        return result + "\n"


class Body(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("body", attrs)

    def to_str(self) -> str:
        return "\n".join(child.to_str() for child in self.children)


class Caption(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("caption", attrs)


class Code(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("code", attrs)

    def detect_language(self) -> str:
        attr_cls = self.attr("class")
        if m := re.search("(language|lang)-([a-z0-9]+)", attr_cls):
            return m.group(2)
        for c in attr_cls.split(" "):
            if c in HIGHLIGHT_LANGUAGES:
                return c
        return ""

    def to_str(self) -> str:
        inner_lines = self.inner().split("\n")
        lang = self.detect_language()
        if len(inner_lines) == 1:
            return f"`{self.inner()}`"
        return f"```{lang}\n{self.inner()}```\n"


class Del(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("del", attrs)

    def to_str(self) -> str:
        return f"~~{self.inner()}~~"


class Em(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("em", attrs)

    def to_str(self) -> str:
        return f"*{self.inner()}*"


class H(Tag):
    def __init__(self, attrs: Attr, h_num: int) -> None:
        super().__init__(f"h{h_num}", attrs)
        self.h_num = h_num

    def to_str(self) -> str:
        return f"{'#' * self.h_num} {self.inner()}\n"


class Hr(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("hr", attrs)

    def to_str(self) -> str:
        return "---\n"


class Img(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("img", attrs)

    def to_str(self) -> str:
        alt = self.attr("alt")
        src = self.attr("src")
        return f"![{alt}]({src})"


class Ul(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("ul", attrs)

    def append_child(self, child: Element) -> None:
        if isinstance(child, Content):
            msg = ""
            raise ValueError(msg)
        if child.name not in ["li", "ul", "ol"]:
            msg = ""
            raise ValueError(msg)
        if isinstance(child, Li):
            child.mark = "-"
        self.children.append(child)

    def to_str(self) -> str:
        lines = []
        for child in self.children:
            lines.extend([f"  {s}\n" for s in child.to_str().rstrip().split("\n")])
        return "".join(lines)


class Ol(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("ol", attrs)

    def append_child(self, child: Element) -> None:
        if isinstance(child, Content):
            msg = ""
            raise ValueError(msg)
        if child.name not in ["li", "ul", "ol"]:
            msg = ""
            raise ValueError(msg)
        if isinstance(child, Li):
            child.mark = "1."
        self.children.append(child)

    def to_str(self) -> str:
        lines = []
        for child in self.children:
            lines.extend([f"  {s}\n" for s in child.to_str().rstrip().split("\n")])
        return "".join(lines)


class Li(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("li", attrs)
        self.mark = ""

    def to_str(self) -> str:
        return f"{self.mark} {self.inner()}"


class P(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("p", attrs)

    def to_str(self) -> str:
        return f"{self.inner()}\n"


class Span(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("span", attrs)


class Strong(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("strong", attrs)

    def to_str(self) -> str:
        return f"**{self.inner()}**"


class Table(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("table", attrs)
        self.head: Optional[Thead] = None
        self.body: Optional[Tbody] = None
        self.tmp_tr: list[Tr] = []
        self.caption: Optional[Caption] = None

    def append_child(self, child: Element) -> None:
        if isinstance(child, Content):
            raise ValueError
        if child.name not in ["tr", "thead", "tbody", "caption", "colgroup"]:
            raise ValueError
        if isinstance(child, Tr):
            if self.head is None:
                head = Thead(child.attrs)
                head.append_child(child)
                self.head = head
            else:
                self.tmp_tr.append(child)
        if isinstance(child, Thead):
            self.head = child
        elif isinstance(child, Caption):
            self.caption = child
        elif isinstance(child, Tbody):
            self.body = child

    def create_body_from_tr(self) -> None:
        self.body = Tbody({})
        for tr in self.tmp_tr:
            self.body.append_child(tr)

    def to_str(self) -> str:
        if not self.body and self.tmp_tr:
            self.create_body_from_tr()
        if not self.head and not self.body:
            return ""
        head = cast("Thead", self.head)
        body = cast("Tbody", self.body)
        caption = f"{self.caption.inner()}\n" if self.caption else ""
        table = "\n".join(elem.to_str() for elem in [head, body])
        return caption + table + "\n"


class Tr(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("tr", attrs)

    def to_str(self) -> str:
        return "|" + "|".join(child.to_str() for child in self.children) + "|"


class Th(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("th", attrs)


class Td(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("td", attrs)


class Thead(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("thead", attrs)
        self.col_num = 0

    def append_child(self, child: Element) -> None:
        if isinstance(child, Content):
            raise ValueError
        if not isinstance(child, Tr):
            raise ValueError
        self.children.append(child)

    def to_str(self) -> str:
        col_num = 0
        if self.children and isinstance(self.children[-1], Tr):
            col_num = len(self.children[-1].children)
        if col_num:
            head_line = f"|{'|'.join(['---'] * col_num)}|"
            return self.inner() + "\n" + head_line
        return self.inner()


class Tbody(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("tbody", attrs)

    def to_str(self) -> str:
        return "\n".join(child.to_str() for child in self.children)


class Pre(Tag):
    def __init__(self, attrs: Attr) -> None:
        super().__init__("pre", attrs)


TAG_CLS: dict[str, type[Tag]] = {
    "a": A,
    "b": B,
    "br": Br,
    "blockquote": Blockquote,
    "body": Body,
    "caption": Caption,
    "code": Code,
    "del": Del,
    "em": Em,
    "hr": Hr,
    "html": HTML,
    "img": Img,
    "ul": Ul,
    "ol": Ol,
    "li": Li,
    "p": P,
    "span": Span,
    "strong": Strong,
    "table": Table,
    "thead": Thead,
    "tbody": Tbody,
    "tr": Tr,
    "th": Th,
    "td": Td,
    "pre": Pre,
}


@dataclass(slots=True)
class ParserState:
    tag_stack: list[Tag] = field(default_factory=list)
    unhandeld_tags: list[str] = field(default_factory=list)
    in_pre: bool = False

    def current_tag_name(self) -> str:
        if self.tag_stack:
            return self.tag_stack[-1].name
        return ""

    def current_tag(self) -> Optional[Tag]:
        if self.tag_stack:
            return self.tag_stack[-1]
        return None


class Parser(HTMLParser):
    def feed(self, feed: str) -> None:
        self.state = ParserState()
        self.parse_result: list[Element] = []
        super().feed(feed)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        dict_attrs = dict(attrs)
        if tag == "pre":
            self.state.in_pre = True
        if tag in TAG_CLS:
            tag_class = TAG_CLS[tag]
            new_tag = tag_class(dict_attrs)
        elif m := re.match("h([1-6])", tag):
            h_num = int(m.group(1))
            new_tag = H(dict_attrs, h_num)
        else:
            if tag not in SELF_CLOSING_TAGS:
                self.state.unhandeld_tags.append(tag)
            return
        curr_tag = self.state.current_tag()
        if curr_tag:
            curr_tag.append_child(new_tag)
        if tag not in SELF_CLOSING_TAGS:
            self.state.tag_stack.append(new_tag)
            return
        if not self.state.tag_stack:
            self.parse_result.append(new_tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in SELF_CLOSING_TAGS:
            return
        if self.state.unhandeld_tags and tag == self.state.unhandeld_tags[-1]:
            self.state.unhandeld_tags.pop()
            return
        if tag == "pre":
            self.state.in_pre = False
        if len(self.state.tag_stack) != 1:
            self.state.tag_stack.pop()
            return
        if tag == self.state.current_tag_name():
            self.parse_result.append(self.state.tag_stack.pop())

    def handle_data(self, data: str) -> None:
        if self.state.unhandeld_tags:
            return
        data = unescape(data)
        if not self.state.in_pre:
            data = re.sub(r"\n\s+$", "", data)
            data = data.strip("\n")
            if not data:
                return
        if not self.state.tag_stack:
            self.parse_result.append(Content(data))
            return
        curr_tag = self.state.tag_stack[-1]
        curr_tag.append_child(Content(data))


def convert(html: str) -> str:
    parser = Parser()
    parser.feed(html)
    result = []
    for elem in parser.parse_result:
        result.append(elem.to_str())
    return "\n".join(result)


def main() -> None:
    html = sys.stdin.read()
    sys.stdout.write(convert(html))


if __name__ == "__main__":
    main()
