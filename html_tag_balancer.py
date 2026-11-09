#!/data/data/com.termux/files/usr/bin/env python
"""Check and fix the tag balance of HTML files.
Walks the markup with the stdlib parser, reports the missing, unclosed and unexpected closing tags and with ``-a`` appends the missing ones and removes the stray ones in place.
Usage: html_tag_balancer [-a] [paths ...] Options: -a, --autofix Fix files in-place (append missing closing tags, remove unexpected ones) Examples: $ html_tag_balancer -a pages/ Command-line script: run ``python html_tag_balancer.py`` or the ``html_tag_balancer`` command.
The module is importable as well."""

import argparse
import contextlib
from html.parser import HTMLParser
from pathlib import Path
import sys


VOID_ELEMENTS = frozenset({
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
})


class TagBalanceChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack = []
        self.errors = []
        self.raw_source = ""
        self.fix_needed = False

    def set_source(self, source: str) -> None:
        self.raw_source = source

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() in VOID_ELEMENTS:
            return
        self.stack.append((tag.lower(), self.getpos()))

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if not self.stack or self.stack[-1][0] != tag:
            try:
                idx = len(self.stack) - 1
                while idx >= 0 and self.stack[idx][0] != tag:
                    idx -= 1
                if idx >= 0:
                    self.stack.pop(idx)
                else:
                    self.errors.append(("unexpected_closing", tag, self.getpos()))
                    self.fix_needed = True
            except Exception:
                self.errors.append(("unexpected_closing", tag, self.getpos()))
                self.fix_needed = True
        else:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs) -> None:
        pass


def check_html_file(path: Path) -> tuple[bool, list[str]]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, [f"⚠️  Could not read file: {e}"]
    parser = TagBalanceChecker()
    parser.set_source(source)
    try:
        parser.feed(source)
    except Exception as e:
        return False, [f"⚠️  Parsing error: {e}"]
    missing_closings = [f"Missing </{tag}> (opened at line {pos[0]}, col {pos[1]})" for tag, pos in parser.stack]
    unexpected_closings = [f"Unexpected </{tag}> at line {pos[0]}, col {pos[1]}" for _, tag, pos in parser.errors]
    issues = missing_closings + unexpected_closings
    is_balanced = len(issues) == 0
    return is_balanced, issues


def fix_html_file(path: Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"❌ Cannot read '{path}': {e}", file=sys.stderr)
        return False
    parser = TagBalanceChecker()
    parser.set_source(source)
    try:
        parser.feed(source)
    except Exception as e:
        print(f"❌ Parsing error in '{path}': {e}", file=sys.stderr)
        return False
    for _, tag, pos in parser.errors:
        line, col = pos

    class TagScanner(HTMLParser):
        def __init__(self, source) -> None:
            super().__init__()
            self.source = source
            self.chars = list(source)
            self.tokens = []

        def get_char_pos(self, line, col):
            lines = self.source.splitlines(keepends=True)
            idx = 0
            for i in range(line - 1):
                if i < len(lines):
                    idx += len(lines[i])
            return idx + col

        def handle_starttag(self, tag, attrs) -> None:
            if tag.lower() not in VOID_ELEMENTS:
                pos = self.getpos()
                start = self.get_char_pos(*pos)
                end = self.source.find(">", start)
                if end != -1:
                    self.tokens.append(("start", tag, start, end + 1))
                else:
                    self.tokens.append(("start", tag, start, start + len(f"<{tag}")))

        def handle_endtag(self, tag) -> None:
            pos = self.getpos()
            tag_str = f"</{tag}>"
            start = self.source.find(tag_str, self.get_char_pos(*pos))
            if start == -1:
                import re

                m = re.search(f"</\\s*{tag}\\s*>", self.source, re.IGNORECASE)
                if m:
                    start = m.start()
            if start != -1:
                self.tokens.append(("end", tag, start, start + len(tag_str)))

        def handle_startendtag(self, tag, attrs) -> None:
            pos = self.getpos()
            start = self.get_char_pos(*pos)
            end = self.source.find(">", start)
            if end != -1:
                self.tokens.append(("startend", tag, start, end + 1))

    scanner = TagScanner(source)
    with contextlib.suppress(Exception):
        scanner.feed(source)
    ranges_to_remove = []
    for _, tag, pos in parser.errors:
        line, col = pos
        base_idx = 0
        for _ in range(line - 1):
            idx = source.find("\n", base_idx)
            if idx == -1:
                break
            base_idx = idx + 1
        target = f"</{tag}>"
        search_start = max(0, base_idx + col - 5)
        idx = source.find(target, search_start)
        if idx != -1:
            ranges_to_remove.append((idx, idx + len(target)))
        else:
            target_lower = target.lower()
            idx = source.lower().find(target_lower, search_start)
            if idx != -1:
                ranges_to_remove.append((idx, idx + len(target)))
    ranges_to_remove.sort()
    merged = []
    for r in ranges_to_remove:
        if merged and r[0] <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], r[1])
        else:
            merged.append(r)
    new_source = source
    if merged:
        for start, end in reversed(merged):
            new_source = new_source[:start] + new_source[end:]
    missing_tags = [tag for tag, _ in parser.stack]
    missing_tags.reverse()
    insert_pos = len(new_source)
    for end_tag in ("</body>", "</html>"):
        idx = new_source.rfind(end_tag)
        if idx != -1:
            idx_end = new_source.find(">", idx)
            insert_pos = idx_end + 1 if idx_end != -1 else idx + len(end_tag)
            break
    if missing_tags:
        closing_html = "".join(f"</{tag}>" for tag in missing_tags)
        new_source = new_source[:insert_pos] + closing_html + new_source[insert_pos:]
    try:
        path.write_text(new_source, encoding="utf-8")
        return True
    except Exception as e:
        print(f"❌ Cannot write '{path}': {e}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check and optionally fix HTML tag balance in files recursively.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-a",
        "--autofix",
        action="store_true",
        help="Fix files in-place (append missing closing tags, remove unexpected ones)",
    )
    args = parser.parse_args()
    cwd = Path()
    html_files = list(cwd.rglob("*.html")) + list(cwd.rglob("*.htm"))
    html_files = sorted(set(html_files))
    if not html_files:
        print("ℹ️  No HTML files found in current directory (recursively).")
        return
    print(f"🔍 Found {len(html_files)} HTML file(s). Checking...")
    fixed_count = 0
    problem_count = 0
    for path in html_files:
        is_balanced, issues = check_html_file(path)
        if is_balanced:
            print(f"✅ {path} — OK")
        else:
            problem_count += 1
            print(f"❌ {path} — {len(issues)} issue(s):")
            for issue in issues:
                print(f"   • {issue}")
            if args.autofix:
                if fix_html_file(path):
                    print("   🔧 Fixed in-place.")
                    fixed_count += 1
                else:
                    print("   ⚠️  Fix failed.")
    print()
    print(f"Summary: {len(html_files) - problem_count} OK, {problem_count} with issues")
    if args.autofix:
        print(f"   → Fixed {fixed_count} file(s) in-place.")


if __name__ == "__main__":
    raise SystemExit(main())
