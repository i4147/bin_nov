#!/data/data/com.termux/files/usr/bin/env python
"""Remove comments from sources of many languages using the matching tree-sitter grammar.
Maps each file extension to its tree-sitter language (JavaScript/TypeScript, Python, Rust, Lua, TOML, CSS, HTML, Vim script and others), deletes comments with the grammar and processes the files in parallel, printing a per-file and total summary.
Usage: cleants2 [paths ...] Examples: $ cleants2 project/ Runs as a script (``python cleants2.py``) in the directory it should act on; the module can also be imported."""

from dataclasses import dataclass
import importlib
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time

from tree_sitter import Language, Node, Parser


PathLike = str | Path


@dataclass
class ProcessResult:
    path: Path
    success: bool
    comments_removed: int = 0
    error_message: str = ""
    processing_time: float = 0.0
    file_size: int = 0
    file_type: str = ""


EXTENSION_TO_LANGUAGE = {
    ".js": "tree_sitter_javascript",
    ".jsx": "tree_sitter_javascript",
    ".ts": "tree_sitter_typescript",
    ".tsx": "tree_sitter_tsx",
    ".rb": "tree_sitter_ruby",
    ".php": "tree_sitter_php",
    ".java": "tree_sitter_java",
    ".c": "tree_sitter_c",
    ".h": "tree_sitter_c",
    ".cpp": "tree_sitter_cpp",
    ".hpp": "tree_sitter_cpp",
    ".cc": "tree_sitter_cpp",
    ".cs": "tree_sitter_c_sharp",
    ".go": "tree_sitter_go",
    ".rs": "tree_sitter_rust",
    ".swift": "tree_sitter_swift",
    ".kt": "tree_sitter_kotlin",
    ".scala": "tree_sitter_scala",
    ".lua": "tree_sitter_lua",
    ".r": "tree_sitter_r",
    ".jl": "tree_sitter_julia",
    ".dart": "tree_sitter_dart",
    ".ex": "tree_sitter_elixir",
    ".exs": "tree_sitter_elixir",
    ".erl": "tree_sitter_erlang",
    ".hs": "tree_sitter_haskell",
    ".clj": "tree_sitter_clojure",
    ".cljs": "tree_sitter_clojure",
    ".fs": "tree_sitter_fsharp",
    ".fsx": "tree_sitter_fsharp",
    ".nim": "tree_sitter_nim",
    ".zig": "tree_sitter_zig",
    ".v": "tree_sitter_v",
    ".sol": "tree_sitter_solidity",
    ".html": "tree_sitter_html",
    ".htm": "tree_sitter_html",
    ".xml": "tree_sitter_xml",
    ".css": "tree_sitter_css",
    ".scss": "tree_sitter_scss",
    ".sass": "tree_sitter_sass",
    ".less": "tree_sitter_less",
    ".vue": "tree_sitter_vue",
    ".svelte": "tree_sitter_svelte",
    ".sh": "tree_sitter_bash",
    ".bash": "tree_sitter_bash",
    ".zsh": "tree_sitter_bash",
    ".fish": "tree_sitter_fish",
    ".ps1": "tree_sitter_powershell",
    ".vim": "tree_sitter_vim",
    ".json": "tree_sitter_json",
    ".jsonc": "tree_sitter_json",
    ".yaml": "tree_sitter_yaml",
    ".yml": "tree_sitter_yaml",
    ".toml": "tree_sitter_toml",
    ".ini": "tree_sitter_ini",
    ".cfg": "tree_sitter_ini",
    ".conf": "tree_sitter_ini",
    ".sql": "tree_sitter_sql",
    ".graphql": "tree_sitter_graphql",
    ".gql": "tree_sitter_graphql",
    ".md": "tree_sitter_markdown",
    ".markdown": "tree_sitter_markdown",
    ".rst": "tree_sitter_rst",
    ".tex": "tree_sitter_latex",
    ".org": "tree_sitter_org",
    ".proto": "tree_sitter_proto",
    ".cmake": "tree_sitter_cmake",
    ".dockerfile": "tree_sitter_dockerfile",
    ".make": "tree_sitter_make",
    ".nix": "tree_sitter_nix",
    ".elm": "tree_sitter_elm",
    ".pug": "tree_sitter_pug",
    ".wasm": "tree_sitter_wasm",
    ".wgsl": "tree_sitter_wgsl",
}


class UniversalCommentRemover:
    def __init__(self):
        self._parser_cache: dict[str, Parser] = {}
        self._language_cache: dict[str, Language] = {}

    def _load_language(self, module_name: str) -> Language | None:
        if module_name in self._language_cache:
            return self._language_cache[module_name]
        try:
            module = importlib.import_module(module_name)
            if hasattr(module, "language"):
                language = Language(module.language())
                self._language_cache[module_name] = language
                return language
            else:
                print(f"Warning: Module {module_name} doesn't have language() method")
                return None
        except ImportError:
            print(f"Warning: Tree-sitter parser for {module_name} not installed")
            return None
        except Exception as e:
            print(f"Warning: Failed to load {module_name}: {e}")
            return None

    def get_parser_for_file(self, path: Path) -> tuple[Parser, str] | None:
        extension = path.suffix.lower()
        if path.name.lower() == "dockerfile":
            extension = ".dockerfile"
        elif path.name.lower() == "makefile":
            extension = ".make"
        module_name = EXTENSION_TO_LANGUAGE.get(extension)
        if not module_name:
            return None
        if module_name in self._parser_cache:
            return self._parser_cache[module_name], extension
        language = self._load_language(module_name)
        if not language:
            return None
        parser = Parser()
        parser.language = language
        self._parser_cache[module_name] = parser
        return parser, extension

    def _get_comment_ranges(self, root_node: Node) -> list[tuple[int, int]]:
        comment_ranges = []

        def visit_node(node: Node):
            if node.type in (
                "comment",
                "line_comment",
                "block_comment",
                "comment_block",
                "comment_line",
                "doc_comment",
            ):
                comment_ranges.append((node.start_byte, node.end_byte))
                return
            for child in node.children:
                visit_node(child)

        visit_node(root_node)
        if comment_ranges:
            comment_ranges.sort(key=lambda x: x[0])
            merged = [comment_ranges[0]]
            for start, end in comment_ranges[1:]:
                last_start, last_end = merged[-1]
                if start <= last_end:
                    merged[-1] = (last_start, max(last_end, end))
                else:
                    merged.append((start, end))
            comment_ranges = merged
        return comment_ranges

    def _cleanup_empty_lines(self, content: bytes) -> bytes:
        while b"\n\n\n" in content:
            content = content.replace(b"\n\n\n", b"\n\n")
        return content

    def remove_comments(self, content: bytes, file_extension: str) -> tuple[bytes, int]:
        if file_extension in (".json", ".jsonc"):
            return self._remove_json_comments(content)
        parser_info = self.get_parser_for_file(Path(f"dummy{file_extension}"))
        if not parser_info:
            return content, 0
        parser, _ = parser_info
        tree = parser.parse(content)
        comment_ranges = self._get_comment_ranges(tree.root_node)
        if not comment_ranges:
            return content, 0
        result_parts = []
        last_end = 0
        comments_removed = 0
        for start, end in comment_ranges:
            before_comment = content[last_end:start]
            line_start = content.rfind(b"\n", 0, start) + 1
            prefix_on_line = content[line_start:start]
            if prefix_on_line.strip() == b"":
                line_end = content.find(b"\n", end)
                if line_end == -1:
                    line_end = len(content)
                else:
                    line_end += 1
                suffix_on_line = content[end:line_end].strip()
                if suffix_on_line == b"":
                    result_parts.append(before_comment[: len(prefix_on_line)])
                    last_end = line_end
                else:
                    result_parts.append(before_comment)
                    last_end = end
            else:
                result_parts.append(before_comment)
                last_end = end
            comments_removed += 1
        result_parts.append(content[last_end:])
        processed_content = b"".join(result_parts)
        processed_content = self._cleanup_empty_lines(processed_content)
        return processed_content, comments_removed

    def _remove_json_comments(self, content: bytes) -> tuple[bytes, int]:
        import re

        pattern_line = re.compile(rb"//.*?$", re.MULTILINE)
        pattern_block = re.compile(rb"/\*.*?\*/", re.DOTALL)
        comments_count = 0
        comments_count += len(pattern_line.findall(content))
        comments_count += len(pattern_block.findall(content))
        content = pattern_line.sub(b"", content)
        content = pattern_block.sub(b"", content)
        content = self._cleanup_empty_lines(content)
        return content, comments_count


def collect_supported_files(inputs: list[str]) -> list[Path]:
    supported_files = []
    supported_extensions = set(EXTENSION_TO_LANGUAGE.keys())
    if not inputs:
        inputs = ["."]
    for input_path in inputs:
        path = Path(input_path)
        if path.is_file():
            if path.suffix.lower() in supported_extensions or path.name.lower() in (
                "dockerfile",
                "makefile",
            ):
                supported_files.append(path)
            else:
                print(f"Warning: {path} has unsupported file type, skipping")
        elif path.is_dir():
            for path in path.rglob("*"):
                if path.is_file():
                    if path.suffix.lower() in supported_extensions:
                        supported_files.append(path)
                    elif path.name.lower() in ("dockerfile", "makefile"):
                        supported_files.append(path)
        else:
            print(f"Warning: {path} does not exist, skipping")
    seen = set()
    unique_files = []
    for f in supported_files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_files.append(resolved)
    return unique_files


def process_file(path: Path) -> ProcessResult:
    start_time = time.perf_counter()
    try:
        remover = UniversalCommentRemover()
        with open(path, "rb") as f:
            content = f.read()
        file_size = len(content)
        if file_size == 0:
            return ProcessResult(
                path=path,
                success=True,
                comments_removed=0,
                processing_time=time.perf_counter() - start_time,
                file_size=0,
                file_type=path.suffix,
            )
        processed_content, comments_removed = remover.remove_comments(content, path.suffix.lower())
        if comments_removed > 0 and processed_content != content:
            temp_path = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(temp_path, "wb") as f:
                    f.write(processed_content)
                    f.flush()
                    os.fsync(f.fileno())
                original_mode = os.stat(path).st_mode
                os.chmod(temp_path, original_mode)
                temp_path.replace(path)
            except Exception:
                if temp_path.exists():
                    temp_path.unlink()
                raise
        processing_time = time.perf_counter() - start_time
        return ProcessResult(
            path=path,
            success=True,
            comments_removed=comments_removed,
            processing_time=processing_time,
            file_size=file_size,
            file_type=path.suffix,
        )
    except Exception as e:
        processing_time = time.perf_counter() - start_time
        return ProcessResult(
            path=path,
            success=False,
            error_message=str(e),
            processing_time=processing_time,
            file_type=path.suffix or "unknown",
        )


def process_files_parallel(files: list[Path], num_workers: int = 8) -> list[ProcessResult]:
    results = []
    total_files = len(files)
    completed = 0
    with mp.Pool(processes=num_workers) as pool:
        async_results = []
        for path in files:
            async_result = pool.apply_async(process_file, (path,))
            async_results.append(async_result)
        for async_result in async_results:
            try:
                result = async_result.get(timeout=30)
                results.append(result)
                completed += 1
                if result.success:
                    if result.comments_removed > 0:
                        size_kb = result.file_size / 1024 if result.file_size else 0
                        print(
                            f"✓ [{result.file_type:6}] {result.path}: "
                            f"removed {result.comments_removed} comments "
                            f"({size_kb:.1f} KB, {result.processing_time:.3f}s)"
                        )
                    else:
                        print(f"• [{result.file_type:6}] {result.path}: no comments ({result.processing_time:.3f}s)")
                else:
                    print(f"✗ [{result.file_type:6}] {result.path}: ERROR - {result.error_message}")
                if completed % 25 == 0 and completed < total_files:
                    print(f"\nProgress: {completed}/{total_files} files processed\n")
            except mp.TimeoutError:
                print(f"✗ Timeout processing file (30s limit)")
                results.append(
                    ProcessResult(
                        path=Path("unknown"),
                        success=False,
                        error_message="Timeout exceeded 30 seconds",
                    )
                )
                completed += 1
    return results


def print_summary(results: list[ProcessResult], total_files: int, start_time: float):
    total_time = time.perf_counter() - start_time
    successful = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    total_comments_removed = sum(r.comments_removed for r in results if r.success)
    files_with_comments = sum(1 for r in results if r.success and r.comments_removed > 0)
    total_size = sum(r.file_size for r in results if r.success and r.file_size)
    type_stats = {}
    for r in results:
        if r.success and r.comments_removed > 0:
            ext = r.file_type or "unknown"
            if ext not in type_stats:
                type_stats[ext] = {"files": 0, "comments": 0}
            type_stats[ext]["files"] += 1
            type_stats[ext]["comments"] += r.comments_removed
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total files processed:     {total_files}")
    print(f"Successful:                {successful}")
    print(f"Failed:                    {failed}")
    print(f"Files with comments:       {files_with_comments}")
    print(f"Files without comments:    {successful - files_with_comments}")
    print(f"Total comments removed:    {total_comments_removed}")
    if total_size > 0:
        size_mb = total_size / (1024 * 1024)
        print(f"Total size processed:      {size_mb:.2f} MB")
    print(f"Total processing time:     {total_time:.2f}s")
    if successful > 0:
        avg_time = total_time / successful
        print(f"Average time per file:     {avg_time:.3f}s")
    if type_stats:
        print(f"\nComments removed by file type:")
        for ext, stats in sorted(type_stats.items()):
            print(f"  {ext:12} {stats['files']:4} files, {stats['comments']:6} comments")
    if failed > 0:
        print(f"\nFailed files:")
        for r in results:
            if not r.success:
                print(f"  - {r.path}: {r.error_message}")
    print("=" * 70)


def main():
    inputs = sys.argv[1:]
    print("Universal Comment Remover")
    print("=" * 50)
    print("Supported file types:", len(EXTENSION_TO_LANGUAGE))
    print("Collecting files...")
    files = collect_supported_files(inputs)
    if not files:
        print("No supported files found to process.")
        return
    extensions_found = {f.suffix.lower() for f in files if f.suffix}
    print(f"Found {len(files)} file(s) to process")
    print(f"File types found: {', '.join(sorted(extensions_found))}")
    print(f"Using 8 worker processes\n")
    start_time = time.perf_counter()
    results = process_files_parallel(files, num_workers=8)
    print_summary(results, len(files), start_time)


if __name__ == "__main__":
    mp.freeze_support()
    main()
