#!/data/data/com.termux/files/usr/bin/env python
"""Unified code-snippet extractor for Markdown, HTML and other text files."""

import argparse
from dataclasses import dataclass
import json
from multiprocessing import Pool
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Iterator, Sequence

from dh import MAX_WORKERS
from loguru import logger


SOURCE_CODE_EXTS: frozenset[str] = frozenset({
    ".py",
    ".h",
    ".c",
    ".cpp",
    ".cc",
    ".cxx",
    ".hh",
    ".hpp",
    ".hxx",
})
LANG_TO_EXT: dict[str, str] = {
    "python": ".py",
    "py": ".py",
    "javascript": ".js",
    "js": ".js",
    "typescript": ".ts",
    "ts": ".ts",
    "c": ".c",
    "h": ".h",
    "cpp": ".cpp",
    "c++": ".cpp",
    "cc": ".cc",
    "java": ".java",
    "csharp": ".cs",
    "c#": ".cs",
    "cs": ".cs",
    "go": ".go",
    "golang": ".go",
    "rust": ".rs",
    "ruby": ".rb",
    "rails": ".rb",
    "php": ".php",
    "swift": ".swift",
    "kotlin": ".kt",
    "scala": ".scala",
    "sql": ".sql",
    "bash": ".sh",
    "sh": ".sh",
    "zsh": ".sh",
    "shell": ".sh",
    "powershell": ".ps1",
    "ps1": ".ps1",
    "yaml": ".yml",
    "yml": ".yml",
    "json": ".json",
    "html": ".html",
    "htm": ".html",
    "css": ".css",
    "dockerfile": "",
    "make": "",
    "makefile": "",
    "text": ".txt",
    "plain": ".txt",
    "md": ".md",
    "markdown": ".md",
}
FENCE_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_+\-.]*)[ \t]*\n(?P<code>.*?)(?<=\n)```",
    re.DOTALL | re.IGNORECASE,
)
FENCE_OPEN_RE = re.compile(r"^```+(\w*)")
PKG_FILENAMES: frozenset[str] = frozenset({"PKGINFO", "METADATA", "PKG-INFO"})
PYTEXT_EXTS: frozenset[str] = frozenset({".md", ".txt", ".html"})


def lang_to_ext(lang: str) -> str:
    lang = (lang or "").strip().lower()
    if not lang:
        return ".txt"
    if lang in LANG_TO_EXT:
        return LANG_TO_EXT[lang]
    if lang.startswith("."):
        return lang
    if "." in lang:
        return "." + lang.rsplit(".", 1)[-1]
    return "." + lang


def slug(text: str, max_len: int = 200) -> str:
    cleaned = re.sub(r"[^\w\-.]", "_", text)
    return cleaned[:max_len].rstrip("_") or "code_block"


def normalize_exts(exts: Iterable[str]) -> frozenset[str]:
    return frozenset(e if e.startswith(".") else "." + e for e in exts)


def _head_collect(root: Path, exts: frozenset[str], n_lines: int, skip: Path) -> list[str]:
    out: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in exts:
            continue
        try:
            if path.resolve() == skip:
                continue
        except OSError:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        snippet = "".join(text.splitlines(keepends=True)[:n_lines])
        if snippet:
            out.append(snippet)
    return out


def cmd_head(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    out = Path(args.output).resolve()
    exts = normalize_exts(args.ext)
    snippets = _head_collect(root, exts, args.lines, out)
    unique = list(set(snippets))
    out.write_text("\n\n\n".join(unique), encoding="utf-8")
    print(f"Unique snippets saved → {out}")
    print(f"Total unique blocks: {len(unique)}")
    return 0


def _parse_fenced_regex(text: str) -> Iterator[tuple[str, str]]:
    for m in FENCE_RE.finditer(text):
        yield m.group("lang") or "", m.group("code")


def _parse_fenced_lines(text: str, include_unclosed: bool) -> list[dict]:
    lines = text.splitlines()
    blocks: list[dict] = []
    in_block = False
    start = -1
    lang = ""
    buf: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_block:
                blocks.append({
                    "language": lang,
                    "start_line": start,
                    "end_line": i,
                    "content": "\n".join(buf),
                })
                in_block = False
                buf = []
                lang = ""
            else:
                in_block = True
                start = i + 1
                m = FENCE_OPEN_RE.match(stripped)
                lang = m.group(1).lower() if m and m.group(1) else ""
                buf = []
        elif in_block:
            buf.append(line)
    if in_block and include_unclosed:
        blocks.append({
            "language": lang,
            "start_line": start,
            "end_line": len(lines),
            "content": "\n".join(buf),
        })
    return blocks


def _write_md_block_mode(md_path: Path, out_dir: Path) -> int:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    stem = slug(md_path.stem)
    count = 0
    for i, (lang, code) in enumerate(_parse_fenced_regex(text), start=1):
        ext = lang_to_ext(lang)
        name = f"{stem}_block_{i}{ext}" if ext else f"{stem}_block_{i}"
        (out_dir / name).write_text(code.rstrip("\n") + "\n", encoding="utf-8")
        count += 1
    return count


def _write_md_lines_mode(md_path: Path, out_dir: Path, include_unclosed: bool) -> int:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    stem = slug(md_path.stem)
    count = 0
    for block in _parse_fenced_lines(text, include_unclosed):
        ext = lang_to_ext(block["language"])
        name = f"{stem}_lines_{block['start_line']}-{block['end_line']}{ext}"
        (out_dir / name).write_text(block["content"].strip(), encoding="utf-8")
        count += 1
    return count


def _collect_md_targets(roots: Sequence[Path], wide: bool) -> list[Path]:
    targets: list[Path] = []
    for root in roots:
        if root.is_file():
            targets.append(root)
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if wide:
                if p.suffix.lower() in {".md", ".markdown", ".metadata"} or p.name in PKG_FILENAMES:
                    targets.append(p)
            elif p.suffix.lower() == ".md":
                targets.append(p)
    return sorted(set(targets))


def cmd_md_blocks(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    out_dir = Path(args.output)
    if not out_dir.is_absolute():
        out_dir = cwd / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    roots = [Path(p).resolve() for p in args.paths] if args.paths else [cwd]
    targets = _collect_md_targets(roots, wide=(args.target == "wide"))
    total = 0
    for md_path in targets:
        if args.naming == "lines":
            total += _write_md_lines_mode(md_path, out_dir, args.include_unclosed)
        else:
            total += _write_md_block_mode(md_path, out_dir)
    print(f"Extracted {total} code block(s) into {out_dir}")
    return 0


def _iter_snips(text: str) -> Iterator[tuple[int, str]]:
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            m = re.match(r"^```+(\w*)", stripped)
            if m:
                start_line = i + 1
                i += 1
                buf: list[str] = []
                while i < n and not lines[i].strip().startswith("```"):
                    buf.append(lines[i])
                    i += 1
                snippet = "\n".join(buf).strip()
                if snippet:
                    yield start_line, snippet
                i += 1
                continue
        if stripped.startswith(">>>"):
            start_line = i + 1
            buf = []
            while i < n:
                raw = lines[i]
                s = raw.strip()
                if s.startswith(">>>"):
                    buf.append(s[3:].lstrip())
                    i += 1
                elif s.startswith("..."):
                    buf.append(s[3:].lstrip())
                    i += 1
                elif s.startswith(">>"):
                    i += 1
                elif s == "" or s.startswith("#"):
                    i += 1
                    if not s.startswith("#"):
                        break
                else:
                    if buf and not any(c in s for c in ("=", "True", "False", "[")):
                        break
                    i += 1
            snippet = "\n".join(buf).strip()
            if snippet:
                yield start_line, snippet
            continue
        i += 1


def _snips_process_file(path: Path, out_dir: Path) -> dict:
    result = {"file": str(path), "count": 0, "errors": 0}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        result["errors"] += 1
        return result
    stem = str(path.relative_to(path.anchor)).replace("/", "_").replace(".", "_")
    for line_no, snippet in _iter_snips(text):
        try:
            out_path = out_dir / f"{stem}_line{line_no}.py"
            header = f"# Source: {path}\n# Line: {line_no}\n\n"
            out_path.write_text(header + snippet, encoding="utf-8")
            result["count"] += 1
        except Exception:
            result["errors"] += 1
    return result


def _snips_worker(pair: tuple[Path, Path]) -> dict:
    path, out_dir = pair
    return _snips_process_file(path, out_dir)


def _collect_snips_targets(roots: Sequence[Path], exts: frozenset[str]) -> list[Path]:
    targets: list[Path] = []
    for root in roots:
        if root.is_file():
            targets.append(root)
            continue
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file() and ".git" not in p.parts and not p.is_symlink() and p.suffix in exts:
                targets.append(p)
    return targets


def cmd_snips(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    out_dir = Path(args.output)
    if not out_dir.is_absolute():
        out_dir = cwd / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    roots = [Path(p).resolve() for p in args.paths] if args.paths else [cwd]
    exts = normalize_exts(args.ext)
    targets = _collect_snips_targets(roots, exts)
    if not targets:
        print("No files found.")
        return 0
    print(f"📂 Found {len(targets)} files. Processing with {MAX_WORKERS} workers...")
    total, errors = 0, 0
    if MAX_WORKERS > 1:
        with Pool(MAX_WORKERS) as pool:
            futures = [pool.apply_async(_snips_worker, ((t, out_dir),)) for t in targets]
            for fut in futures:
                try:
                    res = fut.get(timeout=30)
                    total += res["count"]
                    errors += res["errors"]
                    if res["count"] > 0:
                        print(f"  ✓ {res['file']}: {res['count']} snippet(s)")
                except Exception as exc:
                    print(f"  ✗ Error: {exc}")
                    errors += 1
    else:
        for t in targets:
            res = _snips_process_file(t, out_dir)
            total += res["count"]
            errors += res["errors"]
            if res["count"] > 0:
                print(f"  ✓ {res['file']}: {res['count']} snippet(s)")
    print(f"\n✅ Complete: {total} snippets extracted, {errors} error(s).")
    print(f"📁 Output saved to: {out_dir.resolve()}")
    return 0


PY_FENCE_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
PY_DOCTEST_RE = re.compile(
    r"(?:^|\n)((?:>>>|\.\.\.).*?)(?=\n\s*\n|\Z)",
    re.DOTALL | re.MULTILINE,
)
PY_TOPLEVEL_RE = re.compile(
    r"(?:^|\n)((?:import\s+\w+|from\s+\w+\s+import|def\s+\w+|class\s+\w+).*?)(?=\n\s*\n|\Z)",
    re.DOTALL | re.MULTILINE,
)


def _doctest_to_code(block: str) -> str:
    out: list[str] = []
    in_doctest = False
    for line in block.strip().split("\n"):
        s = line.strip()
        if s.startswith((">>>", "...")):
            out.append(s[3:].strip())
            in_doctest = True
        elif in_doctest and s:
            out.append(f"# {s}")
        elif not s:
            out.append("")
    return "\n".join(out)


def _pytext_extract(text: str, filename: str) -> list[str]:
    blocks: list[str] = []
    for m in PY_FENCE_RE.finditer(text):
        code = m.group(1).strip()
        if code:
            if ">>>" in code:
                code = _doctest_to_code(code)
            blocks.append(code)
    for m in PY_DOCTEST_RE.finditer(text):
        code = _doctest_to_code(m.group(1))
        if code.strip():
            blocks.append(code)
    if not blocks and filename in PKG_FILENAMES:
        for m in PY_TOPLEVEL_RE.finditer(text):
            code = m.group(1).strip()
            if code and ("import" in code or "def " in code or "class " in code):
                blocks.append(code)
    return blocks


def _is_pytext_target(path: Path) -> bool:
    if path.name in PKG_FILENAMES:
        return True
    return path.suffix.lower() in PYTEXT_EXTS


def _collect_pytext_targets(roots: Sequence[Path]) -> list[Path]:
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            if _is_pytext_target(root):
                found.add(root)
        elif root.is_dir():
            for p in root.rglob("*"):
                if p.is_file() and _is_pytext_target(p):
                    found.add(p)
    return sorted(found)


def _pytext_process_file(path: Path, out_dir: Path) -> tuple[str, int]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return str(path), 0
    blocks = _pytext_extract(text, path.name)
    stem = path.stem.replace(" ", "_")
    for idx, block in enumerate(blocks, start=1):
        out = out_dir / f"{stem}_{idx:03d}.py"
        header = f"# Source: {path}\n# Block: {idx}\n# Extracted: {path.name}\n\n"
        out.write_text(header + block + "\n", encoding="utf-8")
    return str(path), len(blocks)


def _pytext_worker(pair: tuple[Path, Path]) -> tuple[str, int]:
    path, out_dir = pair
    return _pytext_process_file(path, out_dir)


def cmd_pytext(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    roots = [Path(p).resolve() for p in args.paths] if args.paths else [cwd]
    out_dir = Path(args.output)
    if not out_dir.is_absolute():
        out_dir = cwd / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = _collect_pytext_targets(roots)
    if not targets:
        print("No target files found.")
        return 0
    print(f"Found {len(targets)} target file(s). Processing...")
    total = 0
    workers = max(1, MAX_WORKERS)
    if workers > 1:
        with Pool(processes=workers) as pool:
            futures = [pool.apply_async(_pytext_worker, ((t, out_dir),)) for t in targets]
            for fut in futures:
                path_str, count = fut.get()
                total += count
                print(f"  ✓ {path_str}: {count} block(s) extracted")
    else:
        for t in targets:
            path_str, count = _pytext_process_file(t, out_dir)
            total += count
            print(f"  ✓ {path_str}: {count} block(s) extracted")
    print(f"Done! Extracted {total} Python block(s) to '{out_dir}/'")
    print("Reference headers in each file indicate the source.")
    return 0


HTML_PY_HINTS: tuple[str, ...] = (
    "def ",
    "class ",
    "import ",
    "from ",
    "if ",
    "for ",
    "while ",
    "try:",
    "except",
    "with ",
    "lambda",
    "return ",
    "yield ",
    "async ",
    "await ",
    "@",
    "elif ",
    "else:",
    "self.",
)
HTML_PY_REGEXES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\bdef\s+\w+\s*\(",
        r"\bclass\s+\w+",
        r"\bif\s+.*:",
        r"\bfor\s+.*\s+in\s+",
        r"\bimport\s+",
        r"\breturn\s+",
        r"\b(True|False|None)\b",
    )
)
HTML_FILENAME_RE = re.compile(
    r"#\s*(?:filename|name|file)\s*:?\s*([\w\-._]+\.py)",
    re.IGNORECASE,
)
HTML_PY_MARKERS: tuple[str, ...] = ("def ", "import ", "class ", "if __name__")


@dataclass
class CodeBlock:
    content: str
    language: str
    source_file: str
    block_index: int
    suggested_name: str | None = None


class HtmlCodeExtractor:
    def __init__(self, retries: int = 3, timeout: int = 10) -> None:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        self.session = requests.Session()
        retry = Retry(total=retries, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.timeout = timeout

    def fetch(self, url: str) -> str | None:
        from loguru import logger

        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            logger.exception("Failed to fetch {}: {}", url, exc)
            return None

    def close(self) -> None:
        self.session.close()

    def extract_from_html(self, html: str, source: str) -> list[CodeBlock]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        blocks: list[CodeBlock] = []
        blocks.extend(self._from_pre_code(soup, source))
        blocks.extend(self._from_code_tags(soup, source))
        blocks.extend(self._from_script_json(soup, source))
        return blocks

    def _from_pre_code(self, soup: Any, source: str) -> list[CodeBlock]:
        out: list[CodeBlock] = []
        for idx, pre in enumerate(soup.find_all("pre")):
            code = pre.find("code")
            if code is None:
                continue
            text = code.get_text()
            if self._is_python(text):
                out.append(
                    CodeBlock(
                        content=text,
                        language="python",
                        source_file=source,
                        block_index=idx,
                        suggested_name=self._extract_filename(text),
                    )
                )
        return out

    def _from_code_tags(self, soup: Any, source: str) -> list[CodeBlock]:
        out: list[CodeBlock] = []
        offset = len(soup.find_all("pre"))
        for idx, code in enumerate(soup.find_all("code")):
            parent = code.parent
            if parent is not None and getattr(parent, "name", None) == "pre":
                continue
            text = code.get_text()
            if self._is_python(text):
                out.append(
                    CodeBlock(
                        content=text,
                        language="python",
                        source_file=source,
                        block_index=offset + idx,
                        suggested_name=self._extract_filename(text),
                    )
                )
        return out

    def _from_script_json(self, soup: Any, source: str) -> list[CodeBlock]:
        out: list[CodeBlock] = []
        offset = len(soup.find_all("pre")) + len(soup.find_all("code"))
        for idx, script in enumerate(soup.find_all("script")):
            stype = script.get("type")
            sid = str(script.get("id", "")).lower()
            if stype != "application/json" and "canvas" not in sid:
                continue
            raw = script.string
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for chunk in self._walk_json(data):
                if self._is_python(chunk):
                    out.append(
                        CodeBlock(
                            content=chunk,
                            language="python",
                            source_file=source,
                            block_index=offset + idx,
                            suggested_name=self._extract_filename(chunk),
                        )
                    )
        return out

    def _walk_json(self, obj: Iterable[Any], depth: int = 0, max_depth: int = 5) -> list[str]:
        if depth > max_depth:
            return []
        found: list[str] = []
        if isinstance(obj, dict):
            for v in obj.values():
                found.extend(self._walk_json(v, depth + 1, max_depth))
        elif isinstance(obj, list):
            for v in obj:
                found.extend(self._walk_json(v, depth + 1, max_depth))
        elif isinstance(obj, str) and any(m in obj for m in HTML_PY_MARKERS):
            found.append(obj)
        return found

    @staticmethod
    def _is_python(text: str) -> bool:
        if not text.strip():
            return False
        lower = text.lower()
        hint_hits = sum(1 for h in HTML_PY_HINTS if h.lower() in lower)
        regex_hits = sum(1 for r in HTML_PY_REGEXES if r.search(text))
        return hint_hits >= 2 or regex_hits >= 2

    @staticmethod
    def _extract_filename(text: str) -> str | None:
        for line in text.split("\n")[:10]:
            m = HTML_FILENAME_RE.search(line)
            if m is not None:
                return m.group(1)
        return None


class HtmlExtractorSink:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.extractor = HtmlCodeExtractor()

    def process_file(self, path: Path) -> int:
        from loguru import logger

        path = Path(path)
        if path.suffix.lower() != ".html":
            return 0
        try:
            html = path.read_text(encoding="utf-8", errors="ignore")
            blocks = self.extractor.extract_from_html(html, str(path))
            if blocks:
                self._save(blocks, str(path))
                print(f"Extracted {len(blocks)} code blocks from {path}")
            return len(blocks)
        except Exception as exc:
            logger.exception("Error processing {}: {}", path, exc)
            return 0

    def process_url(self, url: str) -> int:
        from loguru import logger

        try:
            html = self.extractor.fetch(url)
            if not html:
                return 0
            blocks = self.extractor.extract_from_html(html, url)
            if blocks:
                self._save(blocks, url)
                print(f"Extracted {len(blocks)} code blocks from {url}")
            return len(blocks)
        except Exception as exc:
            logger.exception("Error processing URL {}: {}", url, exc)
            return 0

    def _save(self, blocks: Sequence[CodeBlock], source: str) -> None:
        stem = "url_content" if source.startswith("http") else Path(source).stem
        target_dir = self.output_dir / stem
        target_dir.mkdir(parents=True, exist_ok=True)
        for block in blocks:
            name = block.suggested_name or f"{stem}_block_{block.block_index:03d}.py"
            out = target_dir / name
            counter = 1
            base = out.stem
            while out.exists():
                parts = base.rsplit("_", 1)
                if len(parts) == 2 and parts[1].isdigit():
                    base = parts[0]
                out = target_dir / f"{base}_{counter}.py"
                counter += 1
            out.write_text(block.content, encoding="utf-8")

    def close(self) -> None:
        self.extractor.close()


def _collect_html_files(root: Path) -> list[str]:
    return [str(p) for p in root.rglob("*.html")]


def _html_file_worker(pair: tuple[str, str]) -> int:
    path, out_dir = pair
    sink = HtmlExtractorSink(Path(out_dir))
    try:
        return sink.process_file(Path(path))
    finally:
        sink.close()


def _html_run_multi(files: Sequence[str], output_dir: Path, workers: int) -> int:
    total = 0
    pool = Pool(processes=workers)
    try:
        futures = [pool.apply_async(_html_file_worker, ((f, str(output_dir)),)) for f in files]
        pool.close()
        for fut in futures:
            try:
                total += fut.get()
            except Exception as exc:
                print(f"Worker failed: {exc}")
        pool.join()
    except Exception:
        pool.terminate()
        raise
    return total


def cmd_html(args: argparse.Namespace) -> int:
    try:
        import bs4
        import loguru
        import requests
    except ImportError as exc:
        print(
            f"html subcommand requires: requests, beautifulsoup4, loguru ({exc})",
            file=sys.stderr,
        )
        return 2
    output_dir = Path(args.output)
    total = 0
    if args.url:
        print(f"Processing URL: {args.url}")
        sink = HtmlExtractorSink(output_dir)
        try:
            total += sink.process_url(args.url)
        finally:
            sink.close()
    elif args.file:
        print(f"Processing file: {args.file}")
        sink = HtmlExtractorSink(output_dir)
        try:
            total += sink.process_file(Path(args.file))
        finally:
            sink.close()
    elif args.path:
        print(f"Processing directory: {args.path}")
        files = _collect_html_files(Path(args.path))
        if not files:
            print(f"No HTML files found in {args.path}")
            return 0
        print(f"Found {len(files)} HTML file(s)")
        total += _html_run_multi(files, output_dir, MAX_WORKERS)
    else:
        print("Processing HTML files in current directory recursively")
        files = _collect_html_files(Path())
        if not files:
            print("No HTML files found.")
            return 0
        print(f"Found {len(files)} HTML file(s)")
        total += _html_run_multi(files, output_dir, MAX_WORKERS)
    print(f"Total code blocks extracted: {total}")
    print(f"Results saved to: {Path(args.output)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snippetforge.py",
        description="Unified code-snippet extractor (merges 7 standalone scripts).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_head = sub.add_parser(
        "head",
        help="Extract first N lines of source files, dedupe (was 23line.py)",
    )
    p_head.add_argument("--root", default=".", help="Root directory to scan (default: cwd)")
    p_head.add_argument(
        "-n",
        "--lines",
        type=int,
        default=23,
        help="Number of lines per file (default: 23)",
    )
    p_head.add_argument("-o", "--output", default="all.txt", help="Output file (default: all.txt)")
    p_head.add_argument(
        "--ext",
        nargs="+",
        default=sorted(SOURCE_CODE_EXTS),
        help="Extensions to include",
    )
    p_head.set_defaults(func=cmd_head)
    p_md = sub.add_parser(
        "md-blocks",
        help="Extract fenced code blocks from Markdown (excode / exmd / extcode_md)",
    )
    p_md.add_argument("paths", nargs="*", help="Files or dirs to scan (default: cwd)")
    p_md.add_argument("-o", "--output", default="output", help="Output directory (default: output)")
    p_md.add_argument(
        "--naming",
        choices=("block", "lines"),
        default="block",
        help="File naming: 'block' → {stem}_block_N.ext (excode / extcode_md); 'lines' → {stem}_lines_S-E.ext (exmd)",
    )
    p_md.add_argument(
        "--target",
        choices=("md", "wide"),
        default="md",
        help="File filter: 'md' → *.md only (default, excode / extcode_md); "
        "'wide' → *.md, *.markdown, *.metadata and PKGINFO/METADATA (exmd)",
    )
    p_md.add_argument(
        "--include-unclosed",
        action="store_true",
        help="Only with --naming lines: also emit unterminated blocks",
    )
    p_md.set_defaults(func=cmd_md_blocks)
    p_snips = sub.add_parser(
        "snips",
        help="Extract fenced + doctest snippets with line numbers (code_snip_extractor.py)",
    )
    p_snips.add_argument("paths", nargs="*", help="Files or dirs to scan (default: cwd)")
    p_snips.add_argument("-o", "--output", default="output", help="Output directory (default: output)")
    p_snips.add_argument(
        "--ext",
        nargs="+",
        default=[".md", ".rst", ".txt", ".METADATA", ".PKG-INFO", ".cfg", ".ini"],
        help="Extensions to include",
    )
    p_snips.set_defaults(func=cmd_snips)
    p_py = sub.add_parser(
        "pytext",
        help="Extract Python blocks from md/txt/html/PKGINFO (xpy_code.py)",
    )
    p_py.add_argument("paths", nargs="*", help="Files or dirs to scan (default: cwd)")
    p_py.add_argument(
        "-o",
        "--output",
        default="extracted_code",
        help="Output directory (default: extracted_code)",
    )
    p_py.set_defaults(func=cmd_pytext)
    p_html = sub.add_parser(
        "html",
        help="Extract Python blocks from HTML files or URLs (pycodex.py). Requires: requests, beautifulsoup4, loguru.",
    )
    grp = p_html.add_mutually_exclusive_group()
    grp.add_argument("-f", "--file", help="A single HTML file")
    grp.add_argument("-p", "--path", help="Directory of HTML files")
    grp.add_argument("-u", "--url", help="URL to fetch HTML from")
    p_html.add_argument(
        "-o",
        "--output",
        default="./output",
        help="Output directory (default: ./output)",
    )
    p_html.set_defaults(func=cmd_html)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
