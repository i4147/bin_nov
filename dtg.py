#!/data/data/com.termux/files/usr/bin/env python
"""Translate the comments, docstrings and print statements of Python files in place.
Collects the translatable strings with tokenize/ast, batches them through ``deep_translator`` (or another backend), applies the replacements, keeps a ``.translation_state.json`` for the resume and works through the files concurrently.
Usage: dtg [paths ...] [-b BACKEND] Positional arguments: inputs ...
Files or directories to process (default: current directory) Options: -b, --backend Translation backend (default: deep_translator) Examples: $ dtg src/ -b deep_translator Command-line script: run ``python dtg.py`` or the ``dtg`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import time
import tokenize
from typing import Any, Callable


STRING_RE = re.compile(r'^([rufbRUFB]*)([\'"]{3}|[\'"]{1})(.*)\2$', flags=re.DOTALL)
COMMENT_RE = re.compile(r"^(#+)(\s*)(.*)$", flags=re.DOTALL)
STATE_FILE: Path = Path(".translation_state.json")


class TargetFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.target_lines = set()

    def visit_Call(self, node: ast.AST) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            for arg in ast.walk(node):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.target_lines.add(arg.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.AST) -> None:
        self._add_docstring(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AST) -> None:
        self._add_docstring(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.AST) -> None:
        self._add_docstring(node)
        self.generic_visit(node)

    def visit_Module(self, node: ast.AST) -> None:
        self._add_docstring(node)
        self.generic_visit(node)

    def _add_docstring(self, node: ast.AST) -> None:
        if node.body and isinstance(node.body[0], ast.Expr):
            val = node.body[0].value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                self.target_lines.add(val.lineno)


def get_translator_func(backend_name: str):
    try:
        if backend_name == "deep_translator":
            from deep_translator import GoogleTranslator

            translator = GoogleTranslator(source="auto", target="en")
            return translator.translate
        elif backend_name == "googletrans":
            from googletrans import Translator

            translator = Translator()
            return lambda text: translator.translate(text, dest="en").text
        elif backend_name == "translate":
            from translate import Translator

            translator = Translator(to_lang="en")
            return translator.translate
        elif backend_name == "translators":
            import translators as ts

            return lambda text: ts.translate_text(text, translator="google", to_language="en")
        else:
            msg = f"Unknown backend: {backend_name}"
            raise ValueError(msg)
    except ImportError:
        print(f"Error: Required package for backend '{backend_name}' is missing.")
        print(f"Please install it (e.g., `pip install {backend_name.replace('_', '-')}`)")
        sys.exit(1)


def safe_translate(text: str, translate_func: Callable[..., Any], retries=3):
    if not text.strip():
        return text
    for attempt in range(retries):
        try:
            res = translate_func(text)
            return res or text
        except Exception as e:
            time.sleep((attempt + 1) * 2)
    return text


def batch_translate(items: list[Any], translate_func: Any) -> None:
    batch_size = 4
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        batch_results = [None] * len(batch)
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = {
                executor.submit(safe_translate, item["content"], translate_func): idx for idx, item in enumerate(batch)
            }
            for future in as_completed(futures):
                idx = futures[future]
                batch_results[idx] = future.result()
        for item, translated_content in zip(batch, batch_results):
            item["translated_content"] = translated_content
        time.sleep(1.0)


def apply_replacements(source_code: str, replacements: list[Any]) -> str:
    lines = source_code.splitlines(keepends=True)
    replacements.sort(key=lambda x: (x[0], x[1]), reverse=True)
    for start_row, start_col, end_row, end_col, new_text in replacements:
        r1, r2 = start_row - 1, end_row - 1
        if r1 == r2:
            line = lines[r1]
            lines[r1] = line[:start_col] + new_text + line[end_col:]
        else:
            start_line, end_line = lines[r1], lines[r2]
            lines[r1] = start_line[:start_col] + new_text + end_line[end_col:]
            for i in range(r1 + 1, r2 + 1):
                lines[i] = ""
    return "".join(lines)


def process_file(filepath: Path, translate_func: Any) -> bool:
    try:
        source_code = filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"  -> Skipping (not UTF-8 readable)")
        return False
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        print(f"  -> Skipping (Original file has syntax errors)")
        return False
    finder = TargetFinder()
    finder.visit(tree)
    tokens = list(tokenize.tokenize(BytesIO(source_code.encode("utf-8")).readline))
    items = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            m = COMMENT_RE.match(tok.string)
            if m:
                items.append({
                    "tok": tok,
                    "type": "comment",
                    "prefix": m.group(1),
                    "space": m.group(2),
                    "content": m.group(3),
                    "quote": "",
                })
        elif tok.type == tokenize.STRING and tok.start[0] in finder.target_lines:
            m = STRING_RE.match(tok.string)
            if m:
                items.append({
                    "tok": tok,
                    "type": "string",
                    "prefix": m.group(1),
                    "space": "",
                    "quote": m.group(2),
                    "content": m.group(3),
                })
    if not items:
        return True
    batch_translate(items, translate_func)
    replacements = []
    for item in items:
        orig = item["content"]
        trans = item.get("translated_content", orig)
        if trans and trans != orig:
            if item["type"] == "comment":
                new_text = f"{item['prefix']}{item['space']}{trans}"
            else:
                new_text = f"{item['prefix']}{item['quote']}{trans}{item['quote']}"
            replacements.append((
                item["tok"].start[0],
                item["tok"].start[1],
                item["tok"].end[0],
                item["tok"].end[1],
                new_text,
            ))
    if not replacements:
        return True
    new_source = apply_replacements(source_code, replacements)
    if new_source != source_code:
        try:
            ast.parse(new_source)
            filepath.write_text(new_source, encoding="utf-8")
            print(f"  -> Successfully translated and updated.")
        except SyntaxError as e:
            print(f"  -> Validation failed after translation: {e}. Reverting changes.")
            return False
    return True


def load_state() -> dict[str, list[Any]]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"processed_files": []}


def save_state(state: Any) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="In-place Python Comment, Docstring, and Print Statement Translator")
    parser.add_argument(
        "inputs",
        nargs="*",
        default=["."],
        help="Files or directories to process (default: current directory)",
    )
    parser.add_argument(
        "-b",
        "--backend",
        default="deep_translator",
        choices=["deep_translator", "googletrans", "translate", "translators"],
        help="Translation backend (default: deep_translator)",
    )
    args = parser.parse_args()
    files_to_process = []
    for inp in args.inputs:
        p = Path(inp)
        if p.is_file() and p.suffix == ".py":
            files_to_process.append(p)
        elif p.is_dir():
            files_to_process.extend(p.rglob("*.py"))
    files_to_process = sorted({p.resolve() for p in files_to_process})
    if not files_to_process:
        print("No Python files found to process.")
        return
    print(f"Initializing {args.backend}...")
    translate_func = get_translator_func(args.backend)
    state = load_state()
    print(f"Found {len(files_to_process)} file(s). Beginning batch translation...")
    for p in files_to_process:
        if str(p) in state["processed_files"]:
            print(f"[{p.name}] - Skipping (Already processed)")
            continue
        print(f"[{p.name}] - Processing...")
        success = process_file(p, translate_func)
        if success:
            state["processed_files"].append(str(p))
            save_state(state)
    print("\nAll tasks completed!")


if __name__ == "__main__":
    main()
