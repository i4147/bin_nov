#!/data/data/com.termux/files/usr/bin/env python
"""Rename HTML files after the title of the page.
Extracts the ``<title>`` (tree-sitter when available, otherwise a regex), transliterates non-Latin titles, normalises the result into a safe file name within the length limit and renames the files below the current directory in parallel, reporting the results.
Usage: rename_html_by_title Examples: $ rename_html_by_title Runs as a script (``python rename_html_by_title.py``) in the directory it should act on; the module can also be imported."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from multiprocessing import Pool
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata

from dh import WORKERS
from loguru import logger


try:
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
try:
    from unidecode import unidecode

    UNIDECODE_AVAILABLE = True
except ImportError:
    UNIDECODE_AVAILABLE = False
try:
    import regex

    REGEX_AVAILABLE = True
except ImportError:
    REGEX_AVAILABLE = False
    regex = re
SUPPORTED_EXT: set[str] = {".html", ".htm"}
CHUNK_SIZE: int = 8192
MAX_FILE_SIZE: int = 50 * 1024 * 1024
MIN_TITLE_LENGTH: int = 2
MAX_FILENAME_LENGTH: int = 200
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO",
    colorize=True,
)
logger.add(
    "rename_html_by_title_{time:YYYYMMDD}.log",
    rotation="500 MB",
    retention="10 days",
    compression="zip",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG",
    backtrace=True,
    diagnose=True,
)
PERSIAN_MAP: dict[str, str] = {
    "ا": "a",
    "ب": "b",
    "پ": "p",
    "ت": "t",
    "ث": "s",
    "ج": "j",
    "چ": "ch",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ذ": "z",
    "ر": "r",
    "ز": "z",
    "ژ": "zh",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "z",
    "ط": "t",
    "ظ": "z",
    "ع": "a",
    "غ": "gh",
    "ف": "f",
    "ق": "q",
    "ک": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "و": "o",
    "ه": "h",
    "ی": "i",
    "ء": "",
    "ة": "e",
    "َ": "",
    "ِ": "",
    "ُ": "",
    "ّ": "",
    "ْ": "",
    "ً": "",
    "ٌ": "",
    "ٍ": "",
}
ARABIC_MAP: dict[str, str] = {
    "ا": "a",
    "ب": "b",
    "ت": "t",
    "ث": "th",
    "ج": "j",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ذ": "dh",
    "ر": "r",
    "ز": "z",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "d",
    "ط": "t",
    "ظ": "z",
    "ع": "a",
    "غ": "gh",
    "ف": "f",
    "ق": "q",
    "ك": "k",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "ه": "h",
    "و": "w",
    "ي": "i",
    "أ": "a",
    "إ": "i",
    "آ": "aa",
    "ؤ": "u",
    "ئ": "i",
    "ة": "a",
}
CYRILLIC_MAP: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "А": "a",
    "Б": "b",
    "В": "v",
    "Г": "g",
    "Д": "d",
    "Е": "e",
    "Ё": "yo",
    "Ж": "zh",
    "З": "z",
    "И": "i",
    "Й": "y",
    "К": "k",
    "Л": "l",
    "М": "m",
    "Н": "n",
    "О": "o",
    "П": "p",
    "Р": "r",
    "С": "s",
    "Т": "t",
    "У": "u",
    "Ф": "f",
    "Х": "h",
    "Ц": "ts",
    "Ч": "ch",
    "Ш": "sh",
    "Щ": "sch",
    "Ъ": "",
    "Ы": "y",
    "Ь": "",
    "Э": "e",
    "Ю": "yu",
    "Я": "ya",
}
GREEK_MAP: dict[str, str] = {
    "α": "a",
    "β": "b",
    "γ": "g",
    "δ": "d",
    "ε": "e",
    "ζ": "z",
    "η": "h",
    "θ": "th",
    "ι": "i",
    "κ": "k",
    "λ": "l",
    "μ": "m",
    "ν": "n",
    "ξ": "x",
    "ο": "o",
    "π": "p",
    "ρ": "r",
    "σ": "s",
    "τ": "t",
    "υ": "u",
    "φ": "ph",
    "χ": "ch",
    "ψ": "ps",
    "ω": "o",
    "Α": "a",
    "Β": "b",
    "Γ": "g",
    "Δ": "d",
    "Ε": "e",
    "Ζ": "z",
    "Η": "h",
    "Θ": "th",
    "Ι": "i",
    "Κ": "k",
    "Λ": "l",
    "Μ": "m",
    "Ν": "n",
    "Ξ": "x",
    "Ο": "o",
    "Π": "p",
    "Ρ": "r",
    "Σ": "s",
    "Τ": "t",
    "Υ": "u",
    "Φ": "ph",
    "Χ": "ch",
    "Ψ": "ps",
    "Ω": "o",
}
CHINESE_MAP: dict[str, str] = {
    "中": "zhong",
    "国": "guo",
    "人": "ren",
    "大": "da",
    "小": "xiao",
    "文": "wen",
    "字": "zi",
    "日": "ri",
    "月": "yue",
    "年": "nian",
    "天": "tian",
    "地": "di",
    "水": "shui",
    "火": "huo",
    "木": "mu",
}
TRANSLITERATION_MAPS: list[dict[str, str]] = [
    PERSIAN_MAP,
    ARABIC_MAP,
    CYRILLIC_MAP,
    GREEK_MAP,
    CHINESE_MAP,
]


@dataclass(slots=True)
class HtmlFile:
    path: Path
    title: str | None = None
    new_name: str | None = None
    renamed: bool = False
    error: str | None = None


@dataclass(slots=True)
class ProcessingResult:
    path: Path
    original_name: str
    new_name: str | None
    title: str | None
    success: bool
    error: str | None = None
    duration: float = 0.0


class LanguageTransliterator:
    @staticmethod
    @lru_cache(maxsize=1024)
    def detect_script(text: str) -> str:
        if not text:
            return "english"
        persian_count: int = sum(1 for c in text if "\u0600" <= c <= "\u06ff")
        cyrillic_count: int = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
        greek_count: int = sum(1 for c in text if "\u0370" <= c <= "\u03ff")
        chinese_count: int = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        hangul_count: int = sum(1 for c in text if "\uac00" <= c <= "\ud7af")
        hiragana_count: int = sum(1 for c in text if "\u3040" <= c <= "\u309f")
        katakana_count: int = sum(1 for c in text if "\u30a0" <= c <= "\u30ff")
        hebrew_count: int = sum(1 for c in text if "\u0590" <= c <= "\u05ff")
        devanagari_count: int = sum(1 for c in text if "\u0900" <= c <= "\u097f")
        total: int = len(text)
        script_counts: dict[str, int] = {
            "persian": persian_count,
            "cyrillic": cyrillic_count,
            "greek": greek_count,
            "chinese": chinese_count,
            "hangul": hangul_count,
            "japanese": hiragana_count + katakana_count,
            "hebrew": hebrew_count,
            "devanagari": devanagari_count,
        }
        if not any(script_counts.values()):
            return "english"
        primary_script: str = max(script_counts, key=script_counts.get)
        primary_count: int = script_counts[primary_script]
        if primary_count / total > 0.5:
            return primary_script
        return "mixed"

    @staticmethod
    def transliterate(text: str) -> str:
        if not text:
            return text
        if UNIDECODE_AVAILABLE:
            return unidecode(text)
        result: str = text
        for char_map in TRANSLITERATION_MAPS:
            for non_english, english in char_map.items():
                result = result.replace(non_english, english)
        result = unicodedata.normalize("NFKD", result)
        result = "".join(c for c in result if unicodedata.category(c) != "Mn")
        return result

    @staticmethod
    def transliterate_smart(text: str) -> str:
        if not text:
            return text
        script: str = LanguageTransliterator.detect_script(text)
        if script == "english":
            return text
        return LanguageTransliterator.transliterate(text)


class HtmlTitleExtractor:
    def __init__(self) -> None:
        self.parser: TreeSitterParser | None = TreeSitterParser() if TREE_SITTER_AVAILABLE else None

    def extract_title(self, path: Path) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content: str = f.read()
        except Exception as e:
            logger.error(f"Failed to read {path}: {e}")
            return None
        if self.parser and self.parser.available:
            title: str | None = self._extract_with_tree_sitter(content)
            if title:
                return title
        return self._extract_with_regex(content)

    def _extract_with_tree_sitter(self, html_content: str) -> str | None:
        try:
            if self.parser and hasattr(self.parser, "parse"):
                tree = self.parser.parse(html_content.encode("utf-8"))
                if tree:
                    return self._query_tree_for_title(tree)
        except Exception as e:
            logger.debug(f"Tree-sitter extraction failed: {e}")
        return None

    def _query_tree_for_title(self, tree: Any) -> str | None:
        try:

            def traverse(node: Any) -> str | None:
                if hasattr(node, "type") and node.type == "tag_name":
                    if hasattr(node, "text") and b"title" in node.text:
                        parent = node.parent if hasattr(node, "parent") else None
                        if parent:
                            children = parent.children if hasattr(parent, "children") else []
                            for child in children:
                                if hasattr(child, "type") and child.type == "text":
                                    if hasattr(child, "text"):
                                        text = child.text
                                        if isinstance(text, bytes):
                                            return text.decode("utf-8").strip()
                                        return str(text).strip()
                if hasattr(node, "children"):
                    for child in node.children:
                        result = traverse(child)
                        if result:
                            return result
                return None

            return traverse(tree.root_node) if hasattr(tree, "root_node") else None
        except Exception as e:
            logger.debug(f"Tree query failed: {e}")
            return None

    @staticmethod
    def _extract_with_regex(html_content: str) -> str | None:
        patterns: list[str] = [
            r"<title[^>]*>(.*?)</title>",
            r"<TITLE[^>]*>(.*?)</TITLE>",
            r"<Title[^>]*>(.*?)</Title>",
        ]
        for pattern in patterns:
            match = re.search(pattern, html_content, re.IGNORECASE | re.DOTALL)
            if match:
                title: str = match.group(1).strip()
                title = re.sub(r"<[^>]+>", "", title)
                title = HtmlTitleExtractor._decode_html_entities(title)
                if title and len(title) >= MIN_TITLE_LENGTH:
                    return title
        return None

    @staticmethod
    def _decode_html_entities(text: str) -> str:
        import html

        return html.unescape(text)


class TreeSitterParser:
    def __init__(self) -> None:
        self.available: bool = TREE_SITTER_AVAILABLE
        self.parser: Any | None = None
        if self.available:
            try:
                self._init_parser()
            except Exception as e:
                logger.warning(f"Tree-sitter init failed: {e}")
                self.available = False

    def _init_parser(self) -> None:
        try:
            pass
        except Exception as e:
            logger.debug(f"Could not initialize tree-sitter parser: {e}")
            self.available = False

    def parse(self, content: bytes) -> Any | None:
        if self.parser:
            return self.parser.parse(content)
        return None


class FilenameNormalizer:
    PUNCTUATION_PATTERN: re.Pattern = re.compile(r"[^\w\s-]", re.UNICODE)
    SPACE_PATTERN: re.Pattern = re.compile(r"\s+")
    UNDERSCORE_PATTERN: re.Pattern = re.compile(r"_+")
    DASH_PATTERN: re.Pattern = re.compile(r"-+")
    LEADING_TRAILING_PATTERN: re.Pattern = re.compile(r"^[\s_-]+|[\s_-]+$")
    RESERVED_NAMES: set[str] = {
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
    }

    @staticmethod
    def normalize(title: str, extension: str = ".html") -> str:
        if not title:
            return f"unnamed{extension}"
        text: str = LanguageTransliterator.transliterate_smart(title)
        text = text.lower()
        text = text.strip()
        text = FilenameNormalizer.PUNCTUATION_PATTERN.sub(" ", text)
        text = FilenameNormalizer.SPACE_PATTERN.sub(" ", text)
        text = text.replace(" ", "_")
        text = FilenameNormalizer.UNDERSCORE_PATTERN.sub("_", text)
        text = FilenameNormalizer.DASH_PATTERN.sub("-", text)
        text = FilenameNormalizer.LEADING_TRAILING_PATTERN.sub("", text)
        text = re.sub(r"[-_]{2,}", "_", text)
        if text.lower() in FilenameNormalizer.RESERVED_NAMES:
            text = f"_{text}"
        max_name_length: int = MAX_FILENAME_LENGTH - len(extension)
        if len(text) > max_name_length:
            text = text[:max_name_length].rstrip("_-")
        if not text:
            text = "page"
        return f"{text}{extension}"

    @staticmethod
    def ensure_unique(filename: Path, existing_files: set[Path]) -> Path:
        if filename not in existing_files:
            return filename
        stem: str = filename.stem
        suffix: str = filename.suffix
        counter: int = 1
        while True:
            new_filename: Path = filename.parent / f"{stem}_{counter}{suffix}"
            if new_filename not in existing_files:
                return new_filename
            counter += 1


class HtmlFileProcessor:
    def __init__(self) -> None:
        self.title_extractor: HtmlTitleExtractor = HtmlTitleExtractor()
        self.existing_names: set[str] = set()

    def process_file(self, path: Path) -> ProcessingResult:
        start_time: datetime = datetime.now()
        original_name: str = path.name
        try:
            if not path.exists():
                return ProcessingResult(
                    path=path,
                    original_name=original_name,
                    new_name=None,
                    title=None,
                    success=False,
                    error=f"File not found: {path}",
                    duration=(datetime.now() - start_time).total_seconds(),
                )
            if not path.is_file():
                return ProcessingResult(
                    path=path,
                    original_name=original_name,
                    new_name=None,
                    title=None,
                    success=False,
                    error=f"Not a file: {path}",
                    duration=(datetime.now() - start_time).total_seconds(),
                )
            if path.stat().st_size > MAX_FILE_SIZE:
                return ProcessingResult(
                    path=path,
                    original_name=original_name,
                    new_name=None,
                    title=None,
                    success=False,
                    error=f"File too large: {path.stat().st_size} bytes",
                    duration=(datetime.now() - start_time).total_seconds(),
                )
            title: str | None = self.title_extractor.extract_title(path)
            if not title:
                return ProcessingResult(
                    path=path,
                    original_name=original_name,
                    new_name=None,
                    title=None,
                    success=False,
                    error="No valid title found in HTML",
                    duration=(datetime.now() - start_time).total_seconds(),
                )
            new_name: str = FilenameNormalizer.normalize(title, path.suffix)
            if new_name == original_name:
                return ProcessingResult(
                    path=path,
                    original_name=original_name,
                    new_name=new_name,
                    title=title,
                    success=True,
                    error="Filename already matches title",
                    duration=(datetime.now() - start_time).total_seconds(),
                )
            new_path: Path = path.parent / new_name
            new_path = FilenameNormalizer.ensure_unique(new_path, self.existing_names)
            try:
                path.rename(new_path)
                self.existing_names.add(str(new_path))
                logger.debug(f"Renamed: {original_name} -> {new_path.name}")
                return ProcessingResult(
                    path=new_path,
                    original_name=original_name,
                    new_name=new_path.name,
                    title=title,
                    success=True,
                    duration=(datetime.now() - start_time).total_seconds(),
                )
            except Exception as e:
                return ProcessingResult(
                    path=path,
                    original_name=original_name,
                    new_name=new_name,
                    title=title,
                    success=False,
                    error=f"Rename failed: {e}",
                    duration=(datetime.now() - start_time).total_seconds(),
                )
        except Exception as e:
            logger.error(f"Unexpected error processing {path}: {e}")
            return ProcessingResult(
                path=path,
                original_name=original_name,
                new_name=None,
                title=None,
                success=False,
                error=str(e),
                duration=(datetime.now() - start_time).total_seconds(),
            )


class FileDiscovery:
    @staticmethod
    def discover_files(paths: list[str]) -> list[Path]:
        discovered: list[Path] = []
        seen: set[Path] = set()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for path_str in paths:
                path: Path = Path(path_str).resolve()
                if not path.exists():
                    logger.warning(f"Path not found: {path}")
                    continue
                if path.is_file():
                    if path.suffix.lower() in SUPPORTED_EXT:
                        discovered.append(path)
                else:
                    futures.append(executor.submit(FileDiscovery._discover_in_directory, path))
            for future in futures:
                try:
                    discovered.extend(future.result())
                except Exception as e:
                    logger.error(f"Error during file discovery: {e}")
        unique_files: list[Path] = []
        for f in discovered:
            if f not in seen:
                unique_files.append(f)
                seen.add(f)
        return sorted(unique_files)

    @staticmethod
    def _discover_in_directory(directory: Path) -> list[Path]:
        files: list[Path] = []
        try:
            for item in directory.rglob("*"):
                if any(part in FileDiscovery.SKIP_DIRS for part in item.parts):
                    continue
                if item.is_symlink():
                    continue
                if item.is_file() and item.suffix.lower() in SUPPORTED_EXT:
                    files.append(item)
        except Exception as e:
            logger.error(f"Error discovering files in {directory}: {e}")
        return files


def process_file_task(path: Path) -> ProcessingResult:
    processor: HtmlFileProcessor = HtmlFileProcessor()
    return processor.process_file(path)


class HtmlRenamerApp:
    def __init__(self, paths: list[str] | None = None) -> None:
        self.paths: list[str] = paths or ["."]
        self.results: list[ProcessingResult] = []

    def run(self) -> None:
        print("=" * 70)
        print("HTML File Renamer (by Title Tag)")
        print("=" * 70)
        print(f"Discovering HTML files in: {', '.join(self.paths)}")
        files: list[Path] = FileDiscovery.discover_files(self.paths)
        if not files:
            logger.warning("No HTML files found")
            return
        print(f"Found {len(files):,} HTML files")
        print(f"Processing with {WORKERS} workers...")
        with Pool(WORKERS) as pool:
            async_results = []
            for path in files:
                result = pool.apply_async(process_file_task, (path,))
                async_results.append(result)
            for i, async_result in enumerate(async_results, 1):
                try:
                    result: ProcessingResult = async_result.get(timeout=60)
                    self.results.append(result)
                    if i % 10 == 0 or i == len(async_results):
                        print(f"Progress: {i}/{len(async_results)} files")
                except Exception as e:
                    logger.error(f"Error retrieving result: {e}")
        self._print_summary()

    def _print_summary(self) -> None:
        print("=" * 70)
        print("SUMMARY")
        print("=" * 70)
        successful: int = sum(1 for r in self.results if r.success)
        failed: int = len(self.results) - successful
        renamed: int = sum(1 for r in self.results if r.new_name and r.new_name != r.original_name)
        skipped: int = successful - renamed
        total_duration: float = sum(r.duration for r in self.results)
        print(f"Total files processed: {len(self.results)}")
        print(f"  ✓ Successful: {successful}")
        print(f"    - Renamed: {renamed}")
        print(f"    - Skipped: {skipped}")
        print(f"  ✗ Failed: {failed}")
        print(f"Total processing time: {total_duration:.2f}s")
        if total_duration > 0:
            print(f"Average time per file: {total_duration / len(self.results):.3f}s")
        if renamed > 0:
            print(f"\n✓ Successfully renamed files ({renamed}):")
            for result in sorted(self.results, key=lambda r: r.duration, reverse=True)[:10]:
                if result.new_name and result.new_name != result.original_name:
                    print(f"  '{result.original_name:40}' -> '{result.new_name}'")
                    print(f"    Title: {result.title}")
        if failed > 0:
            print(f"\n✗ Failed files ({failed}):")
            for result in self.results:
                if not result.success:
                    print(f"  {result.original_name}: {result.error}")
        print("=" * 70)


def main() -> None:
    paths: list[str] = sys.argv[1:] if len(sys.argv) > 1 else ["."]
    app: HtmlRenamerApp = HtmlRenamerApp(paths)
    app.run()


if __name__ == "__main__":
    main()
