#!/data/data/com.termux/files/usr/bin/env python
"""Strip comments from configuration, shell and make-style files in place.
Walks the tree, picks the files whose name or extension is in the allow-list (``Makefile``, ``Dockerfile``, ``.env*``, ``.ini``, ``.conf``, ``requirements.txt``, shell dialects ...) and removes the full-line comments while leaving shebangs, ``#!`` lines and code-only ``#`` characters untouched.
Python, YAML and other languages are skipped.
Usage: frmc [paths ...] Examples: $ frmc .
$ frmc ~/dotfiles Runs as a script (``python frmc.py``) in the directory it should act on; the module can also be imported.
"""

import os
from pathlib import Path
import sys

from dh import SKIP_DIRS

SKIP_EXTS: frozenset[str] = frozenset({
    ".yaml",
    ".yml",
    ".toml",
    ".py",
    ".pyi",
    ".pyw",
    ".rb",
    ".pl",
    ".pm",
    ".r",
    ".jl",
    ".ex",
    ".exs",
    ".cr",
    ".nim",
    ".ps1",
    ".psm1",
    ".php",
    ".coffee",
    ".hx",
    ".wl",
    ".wls",
    ".tex",
    ".ltx",
    ".f",
    ".f77",
    ".f90",
    ".f95",
    ".for",
    ".ftn",
    ".gd",
    ".hy",
    ".clj",
    ".cljs",
    ".cljc",
    ".md",
    ".markdown",
    ".rst",
    ".adoc",
    ".asciidoc",
    ".rdf",
    ".ttl",
    ".sparql",
    ".cql",
    ".cypher",
    ".graphql",
    ".gql",
    ".json5",
    ".jsonc",
    ".hjson",
    ".csv",
    ".tsv",
    ".properties",
    ".asm",
    ".s",
    ".v",
    ".vh",
    ".vhd",
    ".vhdl",
    ".sv",
    ".svh",
    ".vim",
    ".sql",
    ".mysql",
})
SKIP_NAMES: frozenset[str] = frozenset({
    "requirements.in",
})
PROCESS_NAMES: frozenset[str] = frozenset({
    "Makefile",
    "makefile",
    "GNUmakefile",
    "Dockerfile",
    "crontab",
    "requirements.txt",
    "pip.conf",
    "setup.cfg",
    ".editorconfig",
    ".npmrc",
    ".pylintrc",
    ".gitconfig",
    ".gitignore",
    ".gitattributes",
    "nginx.conf",
    "httpd.conf",
})
PROCESS_EXTS: frozenset[str] = frozenset({
    ".zsh",
    ".ksh",
    ".fish",
    ".csh",
    ".tcsh",
    ".awk",
    ".sed",
    ".tcl",
    ".exp",
    ".ini",
    ".conf",
    ".cfg",
    ".cnf",
    ".service",
    ".socket",
    ".timer",
    ".target",
    ".mount",
    ".path",
    ".pp",
    ".desktop",
})
PROCESS_PREFIXES: tuple[str, ...] = ("Makefile", "Dockerfile", ".env")


def is_processable(p: Path) -> bool:
    name = p.name
    suffix = p.suffix.lower()
    if suffix in SKIP_EXTS:
        return False
    if name in SKIP_NAMES:
        return False
    if name in PROCESS_NAMES:
        return True
    if suffix in PROCESS_EXTS:
        return True
    return any(name.startswith(prefix) for prefix in PROCESS_PREFIXES)


def is_allowed(p: Path, explicit: bool) -> bool:
    if explicit:
        return True
    if p.suffix.lower() in SKIP_EXTS:
        return False
    if p.name in SKIP_NAMES:
        return False
    return is_processable(p)


def strip_comment(body: str) -> str:
    if body.lstrip().startswith("#!"):
        return body
    out: list[str] = []
    i = 0
    n = len(body)
    quote = ""
    while i < n:
        c = body[i]
        if quote:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(body[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(body[i + 1])
            i += 2
            continue
        if c in {"'", '"'}:
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "#":
            break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


def process_file(path_str: str) -> tuple[str, int] | None:
    p = Path(path_str)
    try:
        if not p.is_file():
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    original_size = len(data)
    encoding = "utf-8"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            encoding = "latin-1"
            text = data.decode("latin-1")
        except Exception:
            return None
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    changed = False
    for line in lines:
        if line.endswith("\r\n"):
            body, nl = line[:-2], "\r\n"
        elif line.endswith("\n"):
            body, nl = line[:-1], "\n"
        elif line.endswith("\r"):
            body, nl = line[:-1], "\r"
        else:
            body, nl = line, ""
        new_body = strip_comment(body)
        if new_body == body:
            out.append(line)
            continue
        changed = True
        if new_body == "" and body.strip() != "":
            continue
        out.append(new_body + nl)
    if not changed:
        return (path_str, 0)
    new_text = "".join(out)
    try:
        new_data = new_text.encode(encoding)
    except UnicodeEncodeError:
        return None
    if new_data == data:
        return (path_str, 0)
    try:
        p.write_bytes(new_data)
    except OSError:
        return None
    saved = original_size - len(new_data)
    return (path_str, max(0, saved))


def collect_files(roots: list[Path]) -> tuple[list[Path], set[str]]:
    files: list[Path] = []
    explicit: set[str] = set()
    seen: set[str] = set()

    def add(p: Path, is_explicit: bool) -> None:
        key = Path(p).resolve()
        if key in seen:
            if is_explicit:
                explicit.add(key)
            return
        seen.add(key)
        files.append(p)
        if is_explicit:
            explicit.add(key)

    for root in roots:
        try:
            if root.is_file():
                add(root, True)
                continue
            if root.is_symlink():
                continue
            if not root.is_dir():
                continue
        except OSError:
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            kept: list[str] = []
            for d in dirnames:
                if d in SKIP_DIRS:
                    continue
                dp = Path(dirpath) / d
                try:
                    if dp.is_symlink():
                        continue
                except OSError:
                    continue
                kept.append(d)
            dirnames[:] = kept
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    if fp.is_symlink():
                        continue
                except OSError:
                    continue
                add(fp, False)
    return files, explicit


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args:
        roots = [Path(a) for a in args]
    else:
        roots = [Path.cwd()]
    all_files, explicit = collect_files(roots)
    targets: list[Path] = []
    for f in all_files:
        key = Path(f).resolve()
        if is_allowed(f, key in explicit):
            targets.append(f)
    use_color = sys.stdout.isatty()
    green = "\033[32m" if use_color else ""
    reset = "\033[0m" if use_color else ""
    cwd = Path.cwd()
    total = 0
    for t in targets:
        r = process_file(str(t))
        if r is None:
            continue
        name, saved = r
        try:
            display = str(Path(name).relative_to(cwd))
        except ValueError:
            display = name
        if saved > 0:
            print(f"{green}{display}{reset}: saved {saved} bytes")
            total += saved
        else:
            print(f"{display}: unchanged")
    if total:
        print(f"total saved: {total} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
