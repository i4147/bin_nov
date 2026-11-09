#!/data/data/com.termux/files/usr/bin/env python
"""Translate non-English string literals in Python scripts in place."""

import argparse
import ast
from functools import lru_cache
from pathlib import Path
import re
import sys
from typing import Any, Iterable


try:
    from translate import Translator
except ImportError:
    Translator = None
try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None


def contains_non_english(text: str) -> bool:
    if not text or not text.strip():
        return False
    return any(ord(char) > 127 for char in text)


@lru_cache(maxsize=1024)
def translate_text(text: str, target_lang: str = "en") -> str:
    if Translator is not None:
        try:
            translator = Translator(to_lang=target_lang)
            result = translator.translate(text)
            if result and not result.startswith("MYMEMORY WARNING"):
                return result
        except Exception:
            pass
    if GoogleTranslator is not None:
        try:
            result = GoogleTranslator(source="auto", target=target_lang).translate(text)
            if result:
                return result
        except Exception as e:
            print(f"    [Warning] Fallback translation failed for '{text[:20]}...': {e}")
    return text


def get_node_char_offsets(source_code: str, node: ast.AST) -> tuple[int, int]:
    source_bytes = source_code.encode("utf-8")
    lines_bytes = source_bytes.splitlines(keepends=True)
    start_byte = sum(len(lines_bytes[i]) for i in range(node.lineno - 1)) + node.col_offset
    end_byte = sum(len(lines_bytes[i]) for i in range(node.end_lineno - 1)) + node.end_col_offset
    start_char = len(source_bytes[:start_byte].decode("utf-8", errors="ignore"))
    end_char = len(source_bytes[:end_byte].decode("utf-8", errors="ignore"))
    return start_char, end_char


def reformat_literal(orig_slice: str, translated_text: str) -> str:
    if not orig_slice:
        return f'"{translated_text}"'
    prefix_match = re.match(r"^([fFrRbBuU]{1,2})", orig_slice)
    prefix = prefix_match.group(1) if prefix_match else ""
    rest = orig_slice[len(prefix) :]
    if rest.startswith('"""') and rest.endswith('"""'):
        quote = '"""'
    elif rest.startswith("'''") and rest.endswith("'''"):
        quote = "'''"
    elif rest.startswith('"') and rest.endswith('"'):
        quote = '"'
    elif rest.startswith("'") and rest.endswith("'"):
        quote = "'"
    else:
        return f'"{translated_text}"'
    if quote in ('"""', "'''"):
        escaped = translated_text.replace(quote, "\\" + quote[0] * 3)
    else:
        escaped = translated_text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        if quote == '"':
            escaped = escaped.replace('"', '\\"')
        else:
            escaped = escaped.replace("'", "\\'")
    return f"{prefix}{quote}{escaped}{quote}"


def process_python_file(file_path: Path) -> bool:
    try:
        source_code = file_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return False
    try:
        tree = ast.parse(source_code, filename=str(file_path))
    except SyntaxError as e:
        print(f"Skipping {file_path} (Syntax Error: {e})")
        return False
    targets: list[tuple[int, int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and contains_non_english(node.value):
            if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
                start_idx, end_idx = get_node_char_offsets(source_code, node)
                orig_slice = source_code[start_idx:end_idx]
                targets.append((start_idx, end_idx, node.value, orig_slice))
    if not targets:
        print(f"No non-English string literals found in: {file_path}")
        return False
    targets.sort(key=lambda x: x[0], reverse=True)
    modified_code = source_code
    changes_count = 0
    print(f"\nProcessing {file_path} ({len(targets)} candidate string(s) found)...")
    for start_idx, end_idx, orig_text, orig_slice in targets:
        translated_text = translate_text(orig_text)
        if translated_text and translated_text != orig_text:
            new_literal = reformat_literal(orig_slice, translated_text)
            modified_code = modified_code[:start_idx] + new_literal + modified_code[end_idx:]
            changes_count += 1
            print(f"  - Translated: {orig_text.strip()!r} -> {translated_text.strip()!r}")
    if changes_count > 0:
        file_path.write_text(modified_code, encoding="utf-8")
        print(f"Updated in-place: {file_path} ({changes_count} literal(s) updated)")
        return True
    else:
        print(f"No changes made to: {file_path}")
        return False


def resolve_input_paths(inputs: list[str]) -> list[Path]:
    current_script = Path(__file__).resolve()
    target_files: list[Path] = []
    if not inputs:
        print("No input files specified. Recursively scanning current directory for .py files...")
        target_files = [p for p in Path().rglob("*.py") if p.is_file()]
    else:
        for item in inputs:
            p = Path(item)
            if p.is_dir():
                target_files.extend([f for f in p.rglob("*.py") if f.is_file()])
            elif p.is_file() and p.suffix == ".py":
                target_files.append(p)
            else:
                print(f"Warning: Ignored '{item}' (not a valid .py file or directory)")
    unique_files = sorted({f.resolve() for f in target_files if f.resolve() != current_script})
    return unique_files


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate non-English string literals in Python scripts in-place.")
    parser.add_argument(
        "files",
        nargs="*",
        help="One or more .py files or directories to process. If omitted, recursively processes current working folder.",
    )
    args = parser.parse_args()
    if Translator is None and GoogleTranslator is None:
        print("Error: Neither 'translate' nor 'deep-translator' is installed.")
        print("Please install at least one using:")
        print("  pip install translate deep-translator")
        sys.exit(1)
    files_to_process = resolve_input_paths(args.files)
    if not files_to_process:
        print("No Python files found to process.")
        return
    print(f"Found {len(files_to_process)} Python file(s) to inspect.")
    updated_count = 0
    for file_path in files_to_process:
        if process_python_file(file_path):
            updated_count += 1
    print(f"\nDone! Updated {updated_count}/{len(files_to_process)} file(s).")


if __name__ == "__main__":
    main()
