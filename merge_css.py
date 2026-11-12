#!/data/data/com.termux/files/usr/bin/env python
"""CSS merger and minifier.
The ``join`` subcommand concatenates the CSS files of the given directories into one file and rewrites the Google-Fonts imports and the remote ``@font-face`` URLs to ``--font-dir``, ``minify`` runs the ``cleancss`` utility at the requested ``-l`` optimisation level.
Usage: merge_css {join,minify} [-i INPUT] [-o OUTPUT] [--font-dir DIR] Subcommands: join Join CSS files and rewrite font URLs minify Minify CSS files using 'cleancss' utility Options: -i, --input Input directories or CSS files -o, --output Output file path --font-dir Local path to rewrite external fonts to -l, --level Optimization level (e.g., 2 for -O2) Examples: $ merge_css join -i static/css -o bundle.css --font-dir fonts/ $ merge_css minify -i bundle.css -l 2 Command-line script: run ``python merge_css.py`` or the ``merge_css`` command.
The module is importable as well."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
from typing import Optional

GOOGLE_FONTS_RE = re.compile(r"@import\s+url\([^)]+fonts\.googleapis[^)]+\);?", re.IGNORECASE)
FONT_URL_RE = re.compile(r'url\((["\']?)(https?://[^)]+?\.(?:woff2?|ttf|otf|eot))\1\)', re.IGNORECASE)


def get_css_files(paths: list[str]) -> list[Path]:
    seen = set()
    files = []
    for path_str in paths:
        p = Path(path_str)
        if p.is_file() and p.suffix.lower() == ".css":
            resolved = p.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
        elif p.is_dir():
            for f in sorted(p.glob("**/*.css")):
                resolved = f.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    files.append(resolved)
        else:
            print(f"Skipping invalid path: {p}", file=sys.stderr)
    return files


def process_css_files(files: list[Path], font_dir: str) -> tuple[Optional[str], list[tuple[Path, str]]]:
    charset = None
    processed_files = []

    def replace_font_url(match: re.Match) -> str:
        url = match.group(2)
        filename = url.split("/")[-1]
        return f'url("{font_dir}/{filename}")'

    for file_path in files:
        content = file_path.read_text(errors="ignore")
        content = GOOGLE_FONTS_RE.sub("", content)
        content = FONT_URL_RE.sub(replace_font_url, content)
        lines = content.splitlines()
        filtered_lines = []
        for line in lines:
            stripped_lower = line.strip().lower()
            if stripped_lower.startswith("@charset"):
                if charset is None:
                    charset = line.strip()
                continue
            filtered_lines.append(line)
        processed_files.append((file_path, "\n".join(filtered_lines).strip()))
    return charset, processed_files


def cmd_join(args: argparse.Namespace) -> None:
    files = get_css_files(args.input)
    if not files:
        print("No CSS files found.", file=sys.stderr)
        sys.exit(1)
    charset, processed_files = process_css_files(files, args.font_dir)
    output_lines = []
    if charset:
        output_lines.append(charset + "\n")
    for file_path, content in processed_files:
        output_lines.append(f"\n/*====={file_path.name}=====*/\n{content}\n")
    final_output = "\n".join(output_lines).strip() + "\n"
    Path(args.output).write_text(final_output, encoding="utf-8")
    print(f"Joined {len(files)} files -> {args.output}")


def cmd_minify(args: argparse.Namespace) -> None:
    cmd = ["cleancss", f"-O{args.level}", "removeDuplicateRules:on", args.input, "-o", args.output]
    try:
        subprocess.run(cmd, check=True)
        print(f"Successfully minified '{args.input}' -> {args.output}")
    except FileNotFoundError:
        print("Error: 'cleancss' command not found. Please install it via npm.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error executing cleancss: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified CSS Merger and Minifier")
    subparsers = parser.add_subparsers(dest="command", required=True)
    parser_join = subparsers.add_parser("join", help="Join CSS files and rewrite font URLs")
    parser_join.add_argument("-i", "--input", nargs="+", default=["."], help="Input directories or CSS files")
    parser_join.add_argument("-o", "--output", default="merged.css", help="Output file path")
    parser_join.add_argument(
        "--font-dir", default="/sdcard/_static/fonts", help="Local path to rewrite external fonts to"
    )
    parser_join.set_defaults(func=cmd_join)
    parser_minify = subparsers.add_parser("minify", help="Minify CSS files using 'cleancss' utility")
    parser_minify.add_argument("-i", "--input", default="*.css", help="Input glob pattern for CSS files")
    parser_minify.add_argument("-o", "--output", default="merged.css", help="Output file path")
    parser_minify.add_argument("-l", "--level", type=int, default=2, help="Optimization level (e.g., 2 for -O2)")
    parser_minify.set_defaults(func=cmd_minify)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
