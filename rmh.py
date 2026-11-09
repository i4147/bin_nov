#!/data/data/com.termux/files/usr/bin/env python
"""Remove comments from C/C++ files recursively."""


from dataclasses import dataclass
from multiprocessing import Pool, cpu_count
from pathlib import Path
import re
import sys

from dh import MAX_WORKERS
from loguru import logger
from tqdm import tqdm


@dataclass
class ProcessResult:
    path: Path
    success: bool
    original_lines: int
    final_lines: int
    comments_removed: int
    original_size: int
    final_size: int
    backup_size: int
    error: str | None = None

    @property
    def space_freed(self) -> int:
        return max(0, self.original_size - self.final_size)

    @property
    def total_space_used(self) -> int:
        return self.final_size + self.backup_size


class CommentRemover:
    STRING_PATTERN = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    RAW_STRING_PATTERN = r'(?:u8|u|U|L)?R"(?P<raw_delimiter>[^()\s\\]{0,16})\(.*?\)(?P=raw_delimiter)"'
    SINGLE_COMMENT_PATTERN = r"//(?:\\\r?\n|[^\n])*"
    MULTI_COMMENT_PATTERN = r"/\*.*?\*/"

    def __init__(self):
        self.string_regex = re.compile(self.STRING_PATTERN)
        self.single_comment_regex = re.compile(self.SINGLE_COMMENT_PATTERN)
        self.multi_comment_regex = re.compile(self.MULTI_COMMENT_PATTERN, re.DOTALL)
        self.comment_token_regex = re.compile(
            f"(?P<string>{self.RAW_STRING_PATTERN}|{self.STRING_PATTERN})|"
            f"(?P<single>{self.SINGLE_COMMENT_PATTERN})|"
            f"(?P<multi>{self.MULTI_COMMENT_PATTERN})",
            re.DOTALL,
        )

    def _protect_strings(self, text: str) -> tuple[str, dict]:
        protected_strings = {}
        placeholder_counter = [0]

        def replace_string(match):
            placeholder = f"__STRING_PLACEHOLDER_{placeholder_counter[0]}__"
            protected_strings[placeholder] = match.group(0)
            placeholder_counter[0] += 1
            return placeholder

        protected_text = self.string_regex.sub(replace_string, text)
        return (protected_text, protected_strings)

    def _restore_strings(self, text: str, protected_strings: dict) -> str:
        for placeholder, original in protected_strings.items():
            text = text.replace(placeholder, original)
        return text

    def remove_comments(self, text: str) -> tuple[str, int]:
        total_comments = 0

        def remove_comment(match: re.Match) -> str:
            nonlocal total_comments
            if match.group("string") is not None:
                return match.group(0)
            total_comments += 1
            newlines = "".join(re.findall(r"\r\n|\r|\n", match.group(0)))
            return " " + newlines

        cleaned_text = self.comment_token_regex.sub(remove_comment, text)
        return (cleaned_text, total_comments)

    def process_file(self, path: Path) -> ProcessResult:
        try:
            path = path.resolve()
            original_bytes = path.read_bytes()
            original_content = original_bytes.decode("utf-8")
            original_lines = len(original_content.split("\n"))
            original_size = len(original_bytes)
            cleaned_content, comments_removed = self.remove_comments(original_content)
            final_lines = len(cleaned_content.split("\n"))
            final_size = len(cleaned_content.encode("utf-8"))
            backup_path = path.with_suffix(path.suffix + ".bak")
            with backup_path.open("xb") as backup:
                backup.write(original_bytes)
            backup_size = len(original_bytes)
            path.write_bytes(cleaned_content.encode("utf-8"))
            return ProcessResult(
                path=path,
                success=True,
                original_lines=original_lines,
                final_lines=final_lines,
                comments_removed=comments_removed,
                original_size=original_size,
                final_size=final_size,
                backup_size=backup_size,
            )
        except UnicodeDecodeError as e:
            return ProcessResult(
                path=path,
                success=False,
                original_lines=0,
                final_lines=0,
                comments_removed=0,
                original_size=0,
                final_size=0,
                backup_size=0,
                error=f"Encoding error: {e}",
            )
        except Exception as e:
            return ProcessResult(
                path=path,
                success=False,
                original_lines=0,
                final_lines=0,
                comments_removed=0,
                original_size=0,
                final_size=0,
                backup_size=0,
                error=f"Processing error: {e}",
            )


def find_source_files(root_dir: Path) -> list[Path]:
    extensions = {".h", ".hpp", ".c", ".cpp", ".cc", ".cxx", ".hxx"}
    files = []
    if root_dir.is_file():
        if root_dir.suffix in extensions:
            files.append(root_dir)
    else:
        for ext in extensions:
            files.extend(root_dir.rglob(f"*{ext}"))
    return sorted(files)


def collect_source_files(targets: list[str]) -> list[Path]:
    all_files = set()
    for target in targets:
        path = Path(target).resolve()
        if not path.exists():
            logger.warning(f"Path not found: {target}")
            continue
        found_files = find_source_files(path)
        all_files.update(found_files)
    return sorted(all_files)


def process_files_parallel(paths: list[Path], num_workers: int | None = None) -> list[ProcessResult]:
    num_workers = num_workers or cpu_count()
    remover = CommentRemover()
    with Pool(num_workers) as pool:
        results = list(
            tqdm(
                pool.imap_unordered(remover.process_file, paths),
                total=len(paths),
                desc="Processing files",
                unit="file",
            )
        )
    return results


def _format_bytes(bytes_val: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.2f} TB"


def print_summary(results: list[ProcessResult], targets: list[Path]) -> None:
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    total_comments = sum(r.comments_removed for r in successful)
    total_lines_removed = sum(r.original_lines - r.final_lines for r in successful)
    total_space_freed = sum(r.space_freed for r in successful)
    total_final_size = sum(r.final_size for r in successful)
    total_backup_size = sum(r.backup_size for r in successful)
    total_space_used = total_final_size + total_backup_size
    print("=" * 40)
    print("Processing Summary:")
    print(f"  Files processed: {len(results)}")
    print(f"  Successful: {len(successful)}")
    print(f"  Failed: {len(failed)}")
    print(f"  Total comments removed: {total_comments}")
    print(f"  Total lines removed: {total_lines_removed}")
    print("=" * 40)
    print("Disk Space Summary:")
    print(f"  Space freed: {_format_bytes(total_space_freed)}")
    print(f"  Final file size: {_format_bytes(total_final_size)}")
    print(f"  Backup files size: {_format_bytes(total_backup_size)}")
    print(f"  Total space used: {_format_bytes(total_space_used)}")
    print("=" * 40)
    if successful:
        print("Successful files:")
        for result in successful:
            print(
                f"  {result.path.name}: {result.comments_removed} comments, "
                f"{result.original_lines - result.final_lines} lines removed, "
                f"freed {_format_bytes(result.space_freed)}"
            )
    if failed:
        logger.warning("Failed files:")
        for result in failed:
            logger.warning(f"  {result.path.name}: {result.error}")


def main(
    targets: list[str] | None = None,
    num_workers: int | None = None,
    keep_backups: bool = True,
    dry_run: bool = False,
) -> int:
    if not targets:
        targets = ["."]
    target_paths = []
    for target in targets:
        path = Path(target).resolve()
        if not path.exists():
            logger.error(f"Path not found: {target}")
            return 1
        target_paths.append(path)
    print("Scanning for C/C++ files...")
    print(f"Targets: {', '.join(str(p) for p in target_paths)}")
    source_files = collect_source_files(targets)
    if not source_files:
        logger.warning("No C/C++ source files found.")
        return 0
    print(f"Found {len(source_files)} source files")
    print("File types: .h, .hpp, .c, .cpp, .cc, .cxx, .hxx")
    if dry_run:
        print("DRY RUN MODE: No files will be modified")
        remover = CommentRemover()
        total_preview_freed = 0
        for path in source_files[:5]:
            try:
                content = path.read_text(encoding="utf-8")
                cleaned, comments = remover.remove_comments(content)
                original_size = len(content.encode("utf-8"))
                final_size = len(cleaned.encode("utf-8"))
                freed = original_size - final_size
                total_preview_freed += freed
                print(f"  {path.name}: {comments} comments, would free {_format_bytes(freed)}")
            except Exception as e:
                logger.error(f"  {path.name}: {e}")
        if len(source_files) > 5:
            print(f"  ... and {len(source_files) - 5} more files")
            print(f"Preview: Would free approximately {_format_bytes(total_preview_freed)} (for {5} processed files)")
        return 0
    print(f"Using {num_workers or cpu_count()} workers for parallel processing")
    results = process_files_parallel(source_files, num_workers)
    print_summary(results, target_paths)
    if not keep_backups:
        print("Removing backup files...")
        backup_count = 0
        for result in results:
            if result.success:
                backup_path = result.path.with_suffix(result.path.suffix + ".bak")
                if backup_path.exists():
                    backup_path.unlink()
                    backup_count += 1
        print(f"Removed {backup_count} backup files")
    failed = [r for r in results if not r.success]
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Remove comments from C/C++ files recursively",
        epilog="Examples:\n  %(prog)s                           # Process current directory\n  %(prog)s src/ include/             # Process multiple directories\n  %(prog)s file.cpp                  # Process single file\n  %(prog)s src/ file.h               # Process directory and file\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Files or directories to process (default: current directory)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Remove .bak backup files after processing",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")
    args = parser.parse_args()
    targets = args.targets or None
    exit_code = main(
        targets=targets,
        num_workers=MAX_WORKERS,
        keep_backups=not args.no_backup,
        dry_run=args.dry_run,
    )
    sys.exit(exit_code)
