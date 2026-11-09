#!/data/data/com.termux/files/usr/bin/env python
"""Replace symlinks with copies of the files they point to.
Walks the given paths, copies the target content over each symlink (preserving metadata), logs the replacements to ``replaced.txt`` and the failures to ``errors.txt``, and processes the tree in parallel.
Usage: replace_symlink_with_orig [paths ...] Examples: $ replace_symlink_with_orig ./bin Runs as a script (``python replace_symlink_with_orig.py``) in the directory it should act on; the module can also be imported."""

from multiprocessing.pool import Pool
from pathlib import Path
import shutil
from typing import Final, NamedTuple

from dh import MAX_WORKERS
from loguru import logger


REPLACED_LOG_NAME: Final[str] = "replaced.txt"
ERRORS_LOG_NAME: Final[str] = "errors.txt"


class ProcessResult(NamedTuple):
    status: str
    msg: str


def process_symlink(symlink_path: Path) -> ProcessResult | None:
    try:
        raw_target: Path = symlink_path.readlink()
        target_path: Path = raw_target if raw_target.is_absolute() else (symlink_path.parent / raw_target).resolve()
        if symlink_path.parent.name == "bin" and target_path.parent == symlink_path.parent:
            return None
        if target_path.suffix == ".so":
            return None
        if not target_path.exists():
            return ProcessResult(
                "error",
                f"Target does not exist: {symlink_path} -> {target_path}",
            )
        symlink_path.unlink()
        if target_path.is_dir():
            shutil.copytree(target_path, symlink_path)
        else:
            shutil.copy2(target_path, symlink_path)
        return ProcessResult(
            "replaced",
            f"Replaced: {symlink_path} -> {target_path}",
        )
    except Exception as e:
        return ProcessResult(
            "error",
            f"Failed to process {symlink_path}: {e!s}",
        )


def main() -> None:
    current_dir: Path = Path.cwd()
    replaced_log: Path = current_dir / REPLACED_LOG_NAME
    errors_log: Path = current_dir / ERRORS_LOG_NAME
    logger.info("Scanning for symlinks...")
    symlinks: list[Path] = [p for p in current_dir.rglob("*") if p.is_symlink()]
    if not symlinks:
        logger.info("No symlinks found.")
        return
    logger.info("Found {} symlinks. Processing in parallel...", len(symlinks))
    replaced_list: list[str] = []
    errors_list: list[str] = []
    with Pool(processes=MAX_WORKERS) as pool:
        for result in pool.imap_unordered(process_symlink, symlinks):
            if result is None:
                continue
            if result.status == "replaced":
                replaced_list.append(result.msg)
            elif result.status == "error":
                errors_list.append(result.msg)
    if replaced_list:
        replaced_log.write_text("\n".join(replaced_list) + "\n", encoding="utf-8")
        logger.info(
            "Successfully replaced {} symlinks. Logged to {}",
            len(replaced_list),
            REPLACED_LOG_NAME,
        )
    if errors_list:
        errors_log.write_text("\n".join(errors_list) + "\n", encoding="utf-8")
        logger.error(
            "Encountered {} errors. Logged to {}",
            len(errors_list),
            ERRORS_LOG_NAME,
        )


if __name__ == "__main__":
    raise SystemExit(main())
