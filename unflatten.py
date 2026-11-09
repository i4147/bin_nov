#!/data/data/com.termux/files/usr/bin/env python
"""Rebuild a readable shell script from its flattened Python form.
Tokenizes the flattened input, splits it back into statements, restores the indentation of the blocks and rewrites the heredocs that had been flattened, optionally formatting the result with black and extracting the heredoc bodies to files.
Usage: unflatten [flattened.py] [-o script.sh] [--extract] [--no-black] Positional arguments: input Flattened file (default: stdin) Options: -o, --output Restored shell script (default: stdout) --extract Also write each heredoc body as a file --no-black Skip black formatting Examples: $ unflatten flat.py -o restore.sh --extract Command-line script: run ``python unflatten.py`` or the ``unflatten`` command.
The module is importable as well."""

import argparse
import ast
from dataclasses import dataclass, field
import keyword
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence


_S: str = r'(?:"""(?:\\.|[^\\])*?"""' r"|'''(?:\\.|[^\\])*?'''" r'|"(?:\\.|[^"\\\n])*"' r"|'(?:\\.|[^'\\\n])*')"
TOKEN_RE = re.compile(
    "|".join([
        r"(?P<ws>[ \t\r]+)",
        r"(?P<nl>\n)",
        r"(?P<comment>#[^\n]*)",
        r"(?P<string>[rRbBuUfF]{0,2}" + _S + ")",
        r"(?P<number>\d[\d_]*(?:\.\d*)?(?:[eE][+-]?\d+)?[jJ]?)",
        r"(?P<name>[^\W\d]\w*)",
        (r"(?P<op>\.\.\.|->|:=|\*\*=?|//=?|>>=?|<<=?|[=!<>+\-*/%&|^@]=|" r"[-+*/%&|^~@<>=!.,:;()\[\]{}])"),
        r"(?P<other>.)",
    ]),
    re.DOTALL,
)
OPEN, CLOSE = "([{", ")]}"
COMPOUND: set[str] = {"def", "class", "if", "elif", "else", "for", "while", "try", "except", "finally", "with", "async"}
ALWAYS_START: set[str] = {
    "def",
    "class",
    "return",
    "assert",
    "raise",
    "pass",
    "break",
    "continue",
    "try",
    "except",
    "finally",
    "with",
    "while",
    "del",
    "global",
    "nonlocal",
    "elif",
    "async",
    "for",
}
CONTINUERS: set[str] = {"and", "or", "in", "is", "not", "as"}
NOT_OPERAND = set(keyword.kwlist) - {"True", "False", "None", "pass", "break", "continue"}
CONT: dict[str, set[str]] = {
    "elif": {"if", "elif"},
    "else": {"if", "elif", "for", "while", "try", "except"},
    "except": {"try", "except"},
    "finally": {"try", "except", "else"},
}
IFISH: set[str] = {"if", "elif", "else", "except", "finally"}
SCOPED: set[str] = {"for", "with", "ndef"}


@dataclass
class Tok:
    kind: str
    s: str
    start: int
    end: int


def lex(src: str) -> list[Tok]:
    toks, pos = [], 0
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        kind = m.lastgroup
        if kind != "ws":
            toks.append(Tok(kind, m.group(), m.start(), m.end()))
        pos = m.end()
    return toks


@dataclass
class Stmt:
    toks: list[Tok]
    header: bool = False
    kind: str = field(init=False)

    def __post_init__(self) -> None:
        t = self.toks[0]
        if t.kind == "comment":
            self.kind = "#"
        elif t.kind == "name":
            self.kind = self.toks[1].s if t.s == "async" and len(self.toks) > 1 else t.s
        elif t.s == "@":
            self.kind = "@"
        else:
            self.kind = "str" if t.kind == "string" else ""

    def text(self, src: str) -> str:
        return src[self.toks[0].start : self.toks[-1].end]


def operand_end(t: Tok) -> bool:
    if t.kind in ("number", "string"):
        return True
    if t.kind == "name":
        return t.s not in NOT_OPERAND
    return t.kind == "op" and t.s in (")", "]", "}", "...")


def is_ternary_if(toks: list[Tok], i: int) -> bool:
    d = 0
    for t in toks[i + 1 :]:
        if t.kind == "nl" and d == 0:
            return False
        if t.kind == "op":
            if t.s in OPEN:
                d += 1
            elif t.s in CLOSE:
                d -= 1
                if d < 0:
                    return False
            elif t.s == ":" and d == 0:
                return False
        elif t.kind == "name" and t.s == "else" and d == 0:
            return True
    return False


def begins_new(toks: list[Tok], i: int, cur: list[Tok], tern: int) -> bool:
    t, prev = toks[i], cur[-1]
    if not operand_end(prev):
        return False
    s, first = t.s, cur[0].s
    if t.kind == "name":
        if s == "from":
            return first != "raise"
        if s == "import":
            return not (first == "from" and not any(x.s == "import" for x in cur))
        if s == "else":
            return tern == 0
        if s == "if":
            return not is_ternary_if(toks, i)
        return s not in CONTINUERS
    if t.kind == "number":
        return True
    if t.kind == "string":
        return prev.kind != "string"
    if t.kind == "op":
        if s in ("(", "["):
            return t.start != prev.end
        return s == "@"
    return False


def split_statements(toks: list[Tok]) -> list[Stmt]:
    stmts: list[Stmt] = []
    cur: list[Tok] = []
    depth = lam = tern = 0

    def flush(header: bool = False) -> None:
        nonlocal cur, lam, tern
        if cur:
            stmts.append(Stmt(cur, header))
        cur, lam, tern = [], 0, 0

    for i, t in enumerate(toks):
        if t.kind == "nl":
            if depth == 0:
                flush()
            continue
        if t.kind == "comment":
            if depth == 0:
                flush()
                stmts.append(Stmt([t]))
            continue
        if cur and depth == 0 and begins_new(toks, i, cur, tern):
            flush()
        was_empty = not cur
        cur.append(t)
        if depth == 0 and t.kind == "name":
            if not was_empty and t.s == "if":
                tern += 1
            elif not was_empty and t.s == "else" and tern > 0:
                tern -= 1
            elif t.s == "lambda":
                lam += 1
        if t.kind == "op":
            if t.s in OPEN:
                depth += 1
            elif t.s in CLOSE:
                depth = max(0, depth - 1)
            elif t.s == ":" and depth == 0:
                if lam > 0:
                    lam -= 1
                elif cur[0].kind == "name" and cur[0].s in COMPOUND:
                    flush(header=True)
    flush()
    return stmts


def referenced(st: Stmt) -> set[str]:
    out, depth, tk = set(), 0, st.toks
    for i, t in enumerate(tk):
        if t.kind == "op":
            depth += (t.s in OPEN) - (t.s in CLOSE)
        elif t.kind == "name" and t.s not in keyword.kwlist:
            if i and tk[i - 1].s == ".":
                continue
            if depth > 0 and i + 1 < len(tk) and tk[i + 1].s == "=":
                continue
            out.add(t.s)
    return out


def assigned(st: Stmt) -> set[str]:
    if st.header or st.kind in COMPOUND:
        return set()
    out, depth, tk = set(), 0, st.toks
    for i, t in enumerate(tk):
        if t.kind == "op":
            depth += (t.s in OPEN) - (t.s in CLOSE)
            if t.s == "=" and depth == 0:
                return out
        elif t.kind == "name" and t.s not in keyword.kwlist and not (i and tk[i - 1].s == "."):
            out.add(t.s)
    return set()


def header_names(st: Stmt) -> set[str]:
    tk, out = st.toks, set()
    if st.kind == "for":
        for t in tk[1:]:
            if t.s == "in":
                break
            if t.kind == "name":
                out.add(t.s)
    elif st.kind in ("with", "except"):
        for i, t in enumerate(tk):
            if t.s == "as" and i + 1 < len(tk):
                out.add(tk[i + 1].s)
    elif st.kind == "def":
        depth = 0
        for i, t in enumerate(tk):
            if t.kind == "op":
                depth += (t.s in OPEN) - (t.s in CLOSE)
            elif t.kind == "name" and depth == 1 and tk[i - 1].s in ("(", ",", "*", "**"):
                out.add(t.s)
    return out


@dataclass
class Frame:
    kind: str
    names: set
    body: int = 0
    closed: bool = False


def is_terminator(st: Stmt, text: str) -> bool:
    return st.kind in ("return", "raise", "continue", "break") or text.startswith((
        "pytest.skip(",
        "sys.exit(",
        "exit(",
    ))


def rebuild_python(source: str) -> str:
    src = "\n".join(line.strip() for line in source.strip().splitlines())
    toks = lex(src)
    stmts = split_statements(toks)
    out: list[str] = []
    stack: list[Frame] = []
    prev, base_return = None, False

    def ensure_blank(n: int) -> None:
        have = 0
        while have < len(out) and out[-1 - have] == "":
            have += 1
        out.extend([""] * (n - have))

    for st in stmts:
        text, k = st.text(src), st.kind
        if k == "#":
            out.append("    " * len(stack) + text)
            continue
        names = referenced(st)
        cleared = False
        if k in CONT:
            fam = CONT[k]
            while stack and stack[-1].kind not in fam:
                stack.pop()
            if stack:
                stack.pop()
        else:
            if k == "@":
                stack.clear()
                cleared = True
            elif k in ("def", "class") and prev != "@":
                idx = 2 if st.toks[0].s == "async" else 1
                name = st.toks[idx].s if len(st.toks) > idx else ""
                if k == "class" or not stack or name.startswith("test") or base_return:
                    stack.clear()
                    cleared = True
            if not cleared and not (k in ("def", "class") and prev == "@"):
                while stack:
                    top = stack[-1]
                    if top.closed or (top.kind in SCOPED and top.body > 0 and not (top.names & names)):
                        stack.pop()
                    else:
                        break
        indent = len(stack)
        if indent == 0 and out:
            if k in ("def", "class", "@") and prev != "@":
                ensure_blank(2)
            elif prev in ("import", "from") and k not in ("import", "from"):
                ensure_blank(1)
            elif prev == "str" and len(out) == 1:
                ensure_blank(1)
        out.append("    " * indent + text)
        asg = assigned(st)
        for f in stack:
            f.body += 1
            if f.kind in SCOPED:
                f.names |= asg
        if st.header:
            if k == "def":
                kind = "def" if (not stack or stack[-1].kind == "class") else "ndef"
            else:
                kind = k
            stack.append(Frame(kind, header_names(st)))
        elif stack and stack[-1].kind in IFISH and is_terminator(st, text):
            stack[-1].closed = True
        base_return = k == "return" and len(stack) == 1 and stack[0].kind == "def"
        prev = k
    return "\n".join(out) + "\n"


def polish(code: str, use_black: bool) -> str:
    try:
        ast.parse(code)
    except SyntaxError as exc:
        print(f"warning: rebuilt Python does not parse (line {exc.lineno}: {exc.msg}) - check by hand", file=sys.stderr)
        return code
    if use_black:
        try:
            import black

            return black.format_str(code, mode=black.Mode())
        except Exception:
            pass
    return code


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
PY_CMD_RE = re.compile(r"\bpython[\d.]*\b")
TARGET_RE = re.compile(r"(?:>>?|\btee(?:\s+-a)?)\s+(\S+)")


def clean_cmd(cmd: str) -> str:
    cmd = re.sub(r"^\s*\$\s+", "", cmd.strip())
    return re.sub(r"\s+\$\s+", "\n", cmd)


def convert(text: str, use_black: bool = True) -> tuple[str, list[tuple[str, str]]]:
    text, pos = text.strip(), 0
    lines: list[str] = ["#!/usr/bin/env bash"]
    files: list[tuple[str, str]] = []
    n_patch = 0
    while True:
        m = HEREDOC_RE.search(text, pos)
        if not m:
            tail = clean_cmd(text[pos:])
            if tail:
                lines.append(tail)
            break
        cmd, tag = clean_cmd(text[pos : m.start()]), m.group(2)
        end = re.compile(r"(?<!\S)" + re.escape(tag) + r"(?!\S)").search(text, m.end())
        body = text[m.end() : end.start() if end else len(text)].strip()
        pos = end.end() if end else len(text)
        target = TARGET_RE.search(cmd)
        target = target.group(1) if target else None
        is_py = bool(PY_CMD_RE.search(cmd)) or bool(target and target.endswith(".py"))
        if is_py:
            body = polish(rebuild_python(body), use_black).rstrip("\n")
        if target:
            files.append((target, body + "\n"))
        elif is_py:
            n_patch += 1
            files.append((f"script_{n_patch}.py", body + "\n"))
        lines += [f"{cmd} {m.group(0)}".strip(), body, tag]
    return "\n".join(lines) + "\n", files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="flattened file (default: stdin)")
    ap.add_argument("-o", "--output", help="restored shell script (default: stdout)")
    ap.add_argument("--extract", metavar="DIR", help="also write each heredoc body as a file")
    ap.add_argument("--no-black", action="store_true", help="skip black formatting")
    a = ap.parse_args()
    text = Path(a.input).read_text(encoding="utf-8") if a.input else sys.stdin.read()
    script, files = convert(text, not a.no_black)
    if a.output:
        Path(a.output).write_text(script, encoding="utf-8")
    else:
        sys.stdout.write(script)
    if a.extract:
        for rel, body in files:
            parts = [p for p in Path(rel).parts if p not in ("/", "..", ".")]
            dest = Path(a.extract).joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
            print(f"wrote {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
