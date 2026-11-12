#!/data/data/com.termux/files/usr/bin/env python
"""Strip the comments from TypeScript and JavaScript sources with tree-sitter.
Picks the grammar per suffix (``.ts``, ``.tsx``, ``.d.ts``, ``.js`` ...), collects the comment nodes, deletes them through atomic writes and processes the source files of the current directory in a pool.
Usage: tscleaner Examples: $ tscleaner Runs as a script (``python tscleaner.py``) in the directory it should act on; the module can also be imported.
"""

from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path
import sys
from typing import Any

from atomicio import atomic_write
from dh import MAX_WORKERS, SKIP_DIRS

ROOT = Path.cwd()


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    module_name: str
    language_function: str


SUFFIX_SPECS: tuple[tuple[str, LanguageSpec], ...] = (
    (".d.ts", LanguageSpec("tree_sitter_typescript", "language_typescript")),
    (".tsx", LanguageSpec("tree_sitter_typescript", "language_tsx")),
    (".mts", LanguageSpec("tree_sitter_typescript", "language_typescript")),
    (".cts", LanguageSpec("tree_sitter_typescript", "language_typescript")),
    (".ts", LanguageSpec("tree_sitter_typescript", "language_typescript")),
    (".jsx", LanguageSpec("tree_sitter_javascript", "language")),
    (".mjs", LanguageSpec("tree_sitter_javascript", "language")),
    (".cjs", LanguageSpec("tree_sitter_javascript", "language")),
    (".js", LanguageSpec("tree_sitter_javascript", "language")),
    (".cpp", LanguageSpec("tree_sitter_cpp", "language")),
    (".cxx", LanguageSpec("tree_sitter_cpp", "language")),
    (".cc", LanguageSpec("tree_sitter_cpp", "language")),
    (".c++", LanguageSpec("tree_sitter_cpp", "language")),
    (".hpp", LanguageSpec("tree_sitter_cpp", "language")),
    (".hxx", LanguageSpec("tree_sitter_cpp", "language")),
    (".hh", LanguageSpec("tree_sitter_cpp", "language")),
    (".h++", LanguageSpec("tree_sitter_cpp", "language")),
    (".css", LanguageSpec("tree_sitter_css", "language")),
    (".html", LanguageSpec("tree_sitter_html", "language")),
    (".htm", LanguageSpec("tree_sitter_html", "language")),
    (".xhtml", LanguageSpec("tree_sitter_html", "language")),
    (".lua", LanguageSpec("tree_sitter_lua", "language")),
    (".rs", LanguageSpec("tree_sitter_rust", "language")),
    (".vim", LanguageSpec("tree_sitter_vim", "language")),
    (".c", LanguageSpec("tree_sitter_c", "language")),
    (".h", LanguageSpec("tree_sitter_c", "language")),
    (".sh", LanguageSpec("tree_sitter_bash", "language")),
    (".bash", LanguageSpec("tree_sitter_bash", "language")),
    (".zsh", LanguageSpec("tree_sitter_bash", "language")),
    (".ksh", LanguageSpec("tree_sitter_bash", "language")),
)


def language_spec_for(path: Path) -> LanguageSpec | None:
    name = path.name.lower()
    for suffix, spec in SUFFIX_SPECS:
        if name.endswith(suffix):
            return spec
    return None


def is_source_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file() and language_spec_for(path) is not None


def iter_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for directory, directory_names, file_names in root.walk(
            top_down=True,
            follow_symlinks=False,
        ):
            directory_names[:] = sorted(name for name in directory_names if name not in SKIP_DIRS)
            for file_name in file_names:
                path = directory / file_name
                try:
                    if is_source_file(path):
                        files.append(path)
                except OSError as exc:
                    print(f"warning: cannot inspect {path}: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"error: cannot scan {root}: {exc}", file=sys.stderr)
    return files


def get_language(spec: LanguageSpec) -> Any:
    from importlib import import_module

    from tree_sitter import Language

    module = import_module(spec.module_name)
    language_factory = getattr(module, spec.language_function)
    return Language(language_factory())


def make_parser(spec: LanguageSpec) -> Any:
    from tree_sitter import Parser

    parser = Parser()
    parser.language = get_language(spec)
    return parser


def collect_comment_ranges(root_node: Any) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    stack = [root_node]
    while stack:
        node = stack.pop()
        if node.type == "comment":
            ranges.append((node.start_byte, node.end_byte))
            continue
        children = node.children
        if children:
            stack.extend(reversed(children))
    ranges.sort()
    return ranges


def remove_comment_ranges(source: bytes, ranges: list[tuple[int, int]]) -> bytes:
    if not ranges:
        return source
    output = bytearray()
    previous_end = 0
    for start, end in ranges:
        output.extend(source[previous_end:start])
        comment = source[start:end]
        output.extend(byte for byte in comment if byte in (10, 13))
        previous_end = end
    output.extend(source[previous_end:])
    return bytes(output)


def process_file(path_text: str) -> tuple[str, int, str | None]:
    path = Path(path_text)
    try:
        spec = language_spec_for(path)
        if spec is None:
            return str(path), 0, None
        source = path.read_bytes()
        parser = make_parser(spec)
        tree = parser.parse(source)
        ranges = collect_comment_ranges(tree.root_node)
        if not ranges:
            return str(path), 0, None
        updated = remove_comment_ranges(source, ranges)
        if updated != source:
            atomic_write(path, updated)
        return str(path), len(ranges), None
    except (OSError, ValueError, TypeError, ImportError, AttributeError) as exc:
        return str(path), 0, str(exc)


def main() -> int:
    files = iter_source_files(ROOT)
    if not files:
        print("No supported source files found.")
        return 0
    changed_files = 0
    comments_removed = 0
    failures = 0
    with Pool(processes=MAX_WORKERS) as pool:
        jobs = [pool.apply_async(process_file, (str(path),)) for path in files]
        for job in jobs:
            path, removed_count, error = job.get()
            if error is not None:
                failures += 1
                print(f"error: {path}: {error}", file=sys.stderr)
                continue
            if removed_count:
                changed_files += 1
                comments_removed += removed_count
                print(f"{path}: removed {removed_count} comment(s)")
    print(f"\nFiles scanned: {len(files)}\nChanged files: {changed_files}\nComments removed: {comments_removed}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
