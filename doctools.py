#!/data/data/com.termux/files/usr/bin/env python
"""Unified document conversion CLI.
Subcommands: ``chat-export`` turns a JSON conversation list into per-chat Markdown, ``info-to-md`` shells out to the ``info`` reader, ``man-to-md`` renders a roff page, ``md-to-html`` writes styled HTML with Tailwind and KaTeX, ``rst-to-html`` goes through docutils, ``mobi-to-html`` extracts a ``.mobi``, ``pptx-to-txt`` pulls the slide text and ``rst-to-md`` uses pandoc.
Usage: doctools {chat-export,info-to-md,man-to-md,md-to-html,rst-to-html,mobi-to-html,pptx-to-txt,rst-to-md} [options] Positional arguments: input Input JSON file (list of conversations) paths ...
Files or directories to convert Subcommands: chat-export JSON conversations -> per-chat Markdown files info-to-md Convert .info files to .md via `info` CLI man-to-md Convert a man page (roff) to Markdown md-to-html Markdown -> styled HTML (Tailwind/KaTeX) rst-to-html .rst / .txt / .md -> HTML via docutils mobi-to-html Extract a .mobi to HTML pptx-to-txt Extract text from a .pptx rst-to-md Convert .rst files to .md via pandoc Options: -o, --output Output directory (default: exported) -d, --directory Directory to scan (default: .) --out-dir Temp output directory (default: /sdcard/tmp) --asset-base Base URL for CSS/JS assets (default: /sdcard/_static/katex) --force Force re-conversion even if HTML is newer -r, --recursive Recurse into directories --no-backup Do not create .rst.bak backups --remove-original Delete the original .rst files after conversion Examples: $ doctools md-to-html README.md $ doctools man-to-md ls.1 Command-line script: run ``python doctools.py`` or the ``doctools`` command.
The module is importable as well."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Iterable, Optional, Sequence

from dh import MAX_WORKERS


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[ERR] {msg}", file=sys.stderr)


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.parent / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def _collect_by_ext(root: Path, exts: Iterable[str], recursive: bool = True) -> list[Path]:
    exts = {e.lower() for e in exts}
    iterator = root.rglob("*") if recursive else root.glob("*")
    return [p for p in iterator if p.is_file() and p.suffix.lower() in exts]


def cmd_chat_export(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.is_file():
        err(f"File not found: {src}")
        return 1
    out_dir = Path(args.output)
    out_dir.mkdir(exist_ok=True)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        err(f"Invalid JSON: {e}")
        return 1
    for i, chat in enumerate(data):
        title = chat.get("title") or f"chat_{i}"
        dest = out_dir / f"{title}.md"
        with dest.open("w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n")
            for msg in chat.get("messages", []):
                role = msg.get("author", {}).get("role", "unknown")
                parts = msg.get("content", {}).get("parts", [""])
                content = parts[0] if parts else ""
                f.write(f"## {role.capitalize()}\n\n")
                f.write(content)
                f.write("\n\n---\n\n")
    info(f"Wrote {len(data)} chat(s) to {out_dir}")
    return 0


def _convert_one_info(src: Path) -> Optional[Path]:
    stem = re.sub(r"\.info(-\d+)?$", "", src.name)
    dest = src.parent / f"{stem}.md"
    if dest.exists():
        k = 1
        while (src.parent / f"{stem}_{k}.md").exists():
            k += 1
        dest = src.parent / f"{stem}_{k}.md"
    try:
        result = subprocess.run(["info", str(src)], capture_output=True, text=True)
    except FileNotFoundError:
        err("'info' command not found (install GNU info)")
        return None
    if result.returncode == 0:
        dest.write_text(result.stdout, encoding="utf-8")
        src.unlink()
        print(f"Converted {src.name} -> {dest.name}")
        return dest
    warn(f"Failed to convert {src.name} (exit {result.returncode}): {result.stderr.strip()}")
    return None


def cmd_info_to_md(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        err(f"Not a directory: {root}")
        return 1
    files = list(root.glob("*.info*"))
    if not files:
        print("No .info files found.")
        return 0
    info(f"Converting {len(files)} .info file(s) with {MAX_WORKERS} worker(s).")
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_convert_one_info, f) for f in files]
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                err(f"Worker raised: {e}")
    return 0


def _render_roff(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_fence = False
    pending_tp = False
    _bold = re.compile(r"\.B\s+(.+)")
    _ital = re.compile(r"\.I\s+(.+)")
    _cmd_word = re.compile(r"\b(ls|cat|grep|echo|pwd|cd|mkdir|rm|touch|man)\b")
    _prompt = re.compile(r"^\s*\$")
    _cmd_start = re.compile(r"^\s*(ls|cat|grep|echo|pwd|cd|mkdir|rm|touch|man)\b")
    for raw in lines:
        if raw.startswith(".TH"):
            continue
        if raw.startswith(".SH"):
            name = raw[3:].strip()
            out.append(f"# {name.title()}")
            continue
        if raw.startswith(".SS"):
            name = raw[3:].strip()
            out.append(f"## {name.title()}")
            continue
        raw = _bold.sub(r"**\1**", raw)
        raw = _ital.sub(r"*\1*", raw)
        if raw.startswith(".BR"):
            parts = raw.split(maxsplit=1)
            if len(parts) > 1:
                tokens = parts[1].split('"')
                chunks = []
                for i, t in enumerate(tokens):
                    if not t.strip():
                        continue
                    chunks.append(f"**{t.strip()}**" if i % 2 == 0 else t.strip())
                out.append(" ".join(chunks))
                continue
        if raw.startswith(".IR"):
            parts = raw.split(maxsplit=1)
            if len(parts) > 1:
                tokens = parts[1].split('"')
                chunks = []
                for i, t in enumerate(tokens):
                    if not t.strip():
                        continue
                    chunks.append(f"*{t.strip()}*" if i % 2 == 0 else t.strip())
                out.append(" ".join(chunks))
                continue
        if raw.startswith(".PP"):
            out.append("")
            continue
        if raw.startswith(".IP"):
            parts = raw.split(maxsplit=2)
            if len(parts) >= 2 and parts[1].isdigit():
                num = parts[1]
                label = parts[2] if len(parts) > 2 else ""
                out.append(f"{num}. {label}")
                continue
            if len(parts) >= 2:
                a = parts[1] if len(parts) > 1 else ""
                b = parts[2] if len(parts) > 2 else ""
                out.append(f"-{a} {b}".strip())
                continue
        if raw.startswith(".TP"):
            pending_tp = True
            continue
        if pending_tp:
            body = raw.strip()
            pending_tp = False
            out.append(f"-{body}:")
            continue
        if raw.startswith((".nf", ".RS", ".EX")):
            if not in_fence:
                out.append("```sh")
                in_fence = True
            continue
        if raw.startswith((".fi", ".RE", ".EE")):
            if in_fence:
                out.append("```")
                in_fence = False
            continue
        if raw.startswith("."):
            continue
        if _prompt.match(raw) or _cmd_start.match(raw):
            if not in_fence:
                out.append("```sh")
                in_fence = True
            out.append(raw)
            continue
        if in_fence:
            out.append("```")
            in_fence = False
        raw = _cmd_word.sub(r"`\1`", raw)
        out.append(raw)
    if in_fence:
        out.append("```")
    return "\n".join(out)


def cmd_man_to_md(args: argparse.Namespace) -> int:
    src = Path(args.input)
    try:
        text = src.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        err(f"File not found: {src}")
        return 1
    rendered = _render_roff(text)
    stem = src.with_suffix("")
    dest = stem.with_suffix(".md")
    dest.write_text(rendered, encoding="utf-8")
    print(f"Converted {src} -> {dest}")
    return 0


def _apply_tailwind_classes(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    class_map = {
        "h1": "text-4xl font-bold mt-4 mb-2",
        "h2": "text-4xl font-semibold mt-4 mb-2",
        "h3": "text-2xl font-medium mt-4 mb-2",
        "h4": "text-xl font-medium mt-4 mb-2",
        "p": "text-base leading-relaxed mt-2 mb-4",
        "code": "bg-gray-100 p-1 rounded-md",
        "pre": "bg-gray-900 text-white p-4 rounded-md overflow-x-auto",
    }
    for tag, classes in class_map.items():
        for el in soup.find_all(tag):
            existing = el.get("class", [])
            merged = list(set(existing + classes.split()))
            el["class"] = merged
    return str(soup)


def _replace_latex(text: str) -> str:
    text = re.sub(
        r"\\\[(.*?)\\\]",
        '<div class="latex-displayr">\x01</div>',
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\\\((.*?)\\\)",
        '<span class="latex-inliner">\x01</span>',
        text,
        flags=re.DOTALL,
    )
    return text


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en" class="scroll-smooth bg-gray-50 text-gray-900 antialiased">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="{asset_base}/tailwind.min.css">
<link rel="stylesheet" href="{asset_base}/custom.css">
<link rel="stylesheet" href="{asset_base}/katex.min.css">
<script src="{asset_base}/tex.js"></script>
<script src="{asset_base}/auto-render.min.js"></script>
<script src="{asset_base}/katex.min.js"></script>
</head>
<body for="html-export" class="min-h-screen flex flex-col justify-between">
<main class="flex-1">
<div class="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8 prose prose-lg prose-slate">
{content}
</div>
</main>
</body>
</html>
"""


def cmd_md_to_html(args: argparse.Namespace) -> int:
    try:
        import markdown
    except ImportError:
        err("md-to-html requires: pip install markdown beautifulsoup4")
        return 2
    src = Path(args.input)
    if not src.is_file():
        err(f"File not found: {src}")
        return 1
    text = src.read_text(encoding="utf-8", errors="ignore")
    text = _replace_latex(text)
    stem = src.name.replace(".md", "")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_html = out_dir / f"{stem}.html"
    rendered = markdown.markdown(
        text,
        extensions=[
            "md_in_html",
            "fenced_code",
            "codehilite",
            "toc",
            "attr_list",
            "tables",
        ],
    )
    rendered = _apply_tailwind_classes(rendered)
    final_html = _HTML_TEMPLATE.format(
        title=stem,
        asset_base=args.asset_base,
        content=rendered,
    )
    tmp_html.write_text(final_html, encoding="utf-8")
    local = src.with_suffix(".html")
    shutil.copy(tmp_html, local)
    print(f"Output saved in {local}")
    return 0


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MD_CODE = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _md_to_rst(text: str) -> str:
    def heading(match: re.Match) -> str:
        level = len(match.group(1))
        title = match.group(2).strip()
        if level == 1:
            bar = "=" * len(title)
            return f"{bar}\n{title}\n{bar}"
        if level == 2:
            bar = "-" * len(title)
            return f"{title}\n{bar}"
        underline = "~^+"[min(level - 3, 2)]
        return f"{title}\n{underline * len(title)}"

    text = _MD_HEADING.sub(heading, text)
    text = _MD_LINK.sub(r"`\1<\2>`_", text)

    def code(match: re.Match) -> str:
        lang = match.group(1) or ""
        body = match.group(2).strip()
        indented = "\n".join("    " + ln for ln in body.split("\n"))
        if lang:
            return f".. code-block:: {lang}\n\n{indented}\n"
        return f"::\n\n{indented}\n"

    text = _MD_CODE.sub(code, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"**\1**", text)
    text = re.sub(r"\*(.+?)\*", r"*\1*", text)
    text = re.sub(r"`([^`]+)`", r"``\1``", text)
    text = re.sub(r"^---$", "-------", text, flags=re.MULTILINE)
    text = re.sub(r"^\*", "-", text, flags=re.MULTILINE)
    return text


def _find_rst2html_helper() -> Optional[Path]:
    candidates = [
        Path.cwd() / "doc" / "rest2html.py",
        Path.cwd() / "rest2html.py",
        Path(sys.prefix) / "doc" / "rest2html.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _style_hash(style_path: Path) -> str:
    data = style_path.read_bytes()
    return f"style_{hashlib.sha256(data).hexdigest()[:32]}.css"


def _convert_one_to_html(
    args_tuple: tuple[Path, Optional[str]],
) -> tuple[Path, Optional[Path]]:
    src, stylesheet = args_tuple
    dest = src.with_suffix(".html")
    if dest.exists() and dest.stat().st_mtime > src.stat().st_mtime:
        return src, dest
    text = src.read_text(encoding="utf-8")
    temp_rst: Optional[Path] = None
    try:
        if src.suffix.lower() == ".md":
            rst_text = _md_to_rst(text)
            temp_rst = src.with_suffix(".rst")
            temp_rst.write_text(rst_text, encoding="utf-8")
            src = temp_rst
        cmd = [sys.executable, "-m", "docutils.__main__", str(src), str(dest)]
        if stylesheet:
            cmd.extend(["--stylesheet", stylesheet, "--link-stylesheet"])
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        except (subprocess.CalledProcessError, FileNotFoundError):
            helper = _find_rst2html_helper()
            if helper is None:
                msg = "No RST to HTML converter found"
                raise RuntimeError(msg)
            cmd = [
                sys.executable,
                str(helper),
                "--no-toc-backlinks",
                "--strip-comments",
                "--language",
                "en",
                "--date",
            ]
            if stylesheet:
                cmd.extend(["--stylesheet", stylesheet, "--link-stylesheet"])
            cmd.extend([str(src), str(dest)])
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        return src, dest
    finally:
        if temp_rst is not None and temp_rst.exists():
            temp_rst.unlink()


def cmd_rst_to_html(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        err(f"Not a directory: {root}")
        return 1
    stylesheet: Optional[str] = None
    style_src = root / "style.css"
    if style_src.exists():
        hashed = _style_hash(style_src)
        style_dest = root / hashed
        if not style_dest.exists():
            shutil.copy(style_src, style_dest)
        stylesheet = hashed
    sources: list[Path] = []
    for ext in (".rst", ".txt", ".md"):
        sources.extend(root.rglob(f"*{ext}"))
    if args.force:
        pass
    if not sources:
        print(f"No source files found in {root}")
        return 0
    info(f"Found {len(sources)} file(s) to convert (workers={MAX_WORKERS})")
    jobs = [(s, stylesheet) for s in sources]
    converted = 0
    failures = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_convert_one_to_html, j) for j in jobs]
        for fut in futures:
            try:
                src, dest = fut.result()
                if dest:
                    converted += 1
                    try:
                        rel_src = src.relative_to(root)
                        rel_dst = dest.relative_to(root)
                    except ValueError:
                        rel_src, rel_dst = src, dest
                    print(f"Converted: {rel_src} -> {rel_dst}")
                else:
                    failures += 1
            except Exception as e:
                failures += 1
                err(f"Error processing file: {e}")
    print(f"\nConversion complete: {converted} converted, {failures} errors")
    return 0


def cmd_mobi_to_html(args: argparse.Namespace) -> int:
    try:
        import mobi
    except ImportError:
        err("mobi-to-html requires: pip install mobi")
        return 2
    src = Path(args.input)
    if not src.is_file():
        err(f"File not found: {src}")
        return 1
    temp_dir, extracted_path = mobi.extract(str(src))
    extracted_path = Path(extracted_path)
    print(f"Extracted to: {extracted_path}")
    html_content = extracted_path.read_text(encoding="utf-8")
    base_name = src.stem
    parent = src.parent
    dest_html = parent / f"{base_name}.html"
    if extracted_path.suffix.lower() == ".epub":
        epub_dir = extracted_path.parent
        files_dir = parent / f"{base_name}_files"
        if files_dir.exists():
            shutil.rmtree(files_dir)
        shutil.copytree(epub_dir, files_dir)
        dest_html.write_text(html_content, encoding="utf-8")
    else:
        dest_html.write_text(html_content, encoding="utf-8")
    print(f"HTML saved to: {dest_html}")
    shutil.rmtree(temp_dir, ignore_errors=True)
    return 0


def cmd_pptx_to_txt(args: argparse.Namespace) -> int:
    try:
        from pptx import Presentation
    except ImportError:
        err("pptx-to-txt requires: pip install python-pptx")
        return 2
    src = Path(args.input)
    if not src.is_file():
        err(f"File not found: {src}")
        return 1
    try:
        prs = Presentation(str(src))
    except Exception as e:
        err(f"Error opening file: {e}")
        return 1
    dest = src.with_suffix(".txt")
    with dest.open("w", encoding="utf-8") as f:
        for i, slide in enumerate(prs.slides, 1):
            f.write("\n" + "=" * 40 + "\n")
            f.write(f"Slide {i}\n")
            f.write("=" * 40 + "\n\n")
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    f.write(f"{shape.text}\n")
                if getattr(shape, "has_table", False):
                    table = shape.table
                    for row in table.rows:
                        cells = [c.text for c in row.cells]
                        f.write("|".join(cells) + "\n")
                f.write("\n")
    print(f"Text extracted to: {dest}")
    return 0


def _rst_to_md_one(src: Path, *, backup: bool, remove_original: bool) -> bool:
    if not src.exists():
        err(f"{src} not found")
        return False
    if src.suffix.lower() != ".rst":
        warn(f"Skipping {src}: not an .rst file")
        return False
    dest = src.with_suffix(".md")
    if backup and not remove_original:
        bak = src.with_suffix(".rst.bak")
        shutil.copy2(src, bak)
        print(f"Backup created: {bak}")
    try:
        subprocess.run(
            ["pandoc", "-f", "rst", "-t", "gfm", "-o", str(dest), str(src)],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        err(f"Error converting {src}: {e.stderr}")
        return False
    if remove_original:
        src.unlink()
        print(f"Converted and removed original: {src} -> {dest}")
    else:
        print(f"Converted: {src} -> {dest}")
    return True


def cmd_rst_to_md(args: argparse.Namespace) -> int:
    try:
        subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        err("pandoc is not installed. Install with:")
        print("  Termux:         pkg install pandoc")
        print("  Ubuntu/Debian:  sudo apt install pandoc")
        print("  macOS:          brew install pandoc")
        return 1
    backup = not args.no_backup
    any_processed = False
    for raw in args.paths:
        target = Path(raw)
        if target.is_dir():
            if not args.recursive:
                print(f"Skipping directory {raw}. Use -r for recursive processing.")
                continue
            files = list(target.rglob("*.rst"))
            if not files:
                print(f"No .rst files found in {target}")
                continue
            print(f"Found {len(files)} .rst files")
            count = sum(1 for f in files if _rst_to_md_one(f, backup=backup, remove_original=args.remove_original))
            print(f"\nConverted {count}/{len(files)} files")
            any_processed = True
        elif target.is_file():
            _rst_to_md_one(target, backup=backup, remove_original=args.remove_original)
            any_processed = True
        else:
            err(f"{raw} is not valid")
    return 0 if any_processed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc_convert.py",
        description="Unified document conversion CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original-script mapping:\n"
            "  export_chat.py  ->  chat-export\n"
            "  info2md.py      ->  info-to-md\n"
            "  man2md.py       ->  man-to-md\n"
            "  md2html.py      ->  md-to-html\n"
            "  mk_html.py      ->  rst-to-html\n"
            "  mobi2html.py    ->  mobi-to-html\n"
            "  pptx2txt.py     ->  pptx-to-txt\n"
            "  rst2md2.py      ->  rst-to-md\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("chat-export", help="JSON conversations -> per-chat Markdown files")
    p.add_argument("input", help="Input JSON file (list of conversations)")
    p.add_argument(
        "-o",
        "--output",
        default="exported",
        help="Output directory (default: exported)",
    )
    p.set_defaults(func=cmd_chat_export)
    p = sub.add_parser("info-to-md", help="Convert .info files to .md via `info` CLI")
    p.add_argument("-d", "--directory", default=".", help="Directory to scan (default: .)")
    p.set_defaults(func=cmd_info_to_md)
    p = sub.add_parser("man-to-md", help="Convert a man page (roff) to Markdown")
    p.add_argument("input", help="Man page file (e.g. /usr/share/man/man1/ls.1)")
    p.set_defaults(func=cmd_man_to_md)
    p = sub.add_parser("md-to-html", help="Markdown -> styled HTML (Tailwind/KaTeX)")
    p.add_argument("input", help="Input Markdown file")
    p.add_argument(
        "--out-dir",
        default="/sdcard/tmp",
        help="Temp output directory (default: /sdcard/tmp)",
    )
    p.add_argument(
        "--asset-base",
        default="/sdcard/_static/katex",
        help="Base URL for CSS/JS assets (default: /sdcard/_static/katex)",
    )
    p.set_defaults(func=cmd_md_to_html)
    p = sub.add_parser("rst-to-html", help=".rst / .txt / .md -> HTML via docutils")
    p.add_argument("-d", "--directory", default=".", help="Root directory to process (default: .)")
    p.add_argument("--force", action="store_true", help="Force re-conversion even if HTML is newer")
    p.set_defaults(func=cmd_rst_to_html)
    p = sub.add_parser("mobi-to-html", help="Extract a .mobi to HTML")
    p.add_argument("input", help="Input .mobi file")
    p.set_defaults(func=cmd_mobi_to_html)
    p = sub.add_parser("pptx-to-txt", help="Extract text from a .pptx")
    p.add_argument("input", help="Input .pptx file")
    p.set_defaults(func=cmd_pptx_to_txt)
    p = sub.add_parser("rst-to-md", help="Convert .rst files to .md via pandoc")
    p.add_argument("paths", nargs="+", help="Files or directories to convert")
    p.add_argument("-r", "--recursive", action="store_true", help="Recurse into directories")
    p.add_argument("--no-backup", action="store_true", help="Do not create .rst.bak backups")
    p.add_argument(
        "--remove-original",
        action="store_true",
        help="Delete the original .rst files after conversion",
    )
    p.set_defaults(func=cmd_rst_to_md)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(130)
