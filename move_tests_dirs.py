#!/data/data/com.termux/files/usr/bin/env python
"""Move the ``tests`` folders of the installed packages into ``~/tmp/tests_dirs``.
Scans the Termux ``site-packages`` (skipping numpy, pandas, scipy and numba) for ``tests``/``test`` directories, moves each one to the destination with its package prefix and reports the results; ``-d`` only lists what would move.
Usage: move_tests_dirs [-d] Examples: $ move_tests_dirs -d $ move_tests_dirs Runs as a script (``python move_tests_dirs.py``) in the directory it should act on; the module can also be imported."""

from multiprocessing.pool import Pool
from pathlib import Path
import shutil
import sys
from typing import Final, NamedTuple

from dh import MAX_WORKERS
from loguru import logger


SRC: Final[Path] = Path("/data/data/com.termux/files/usr/lib/python3.14/site-packages")
DEST: Final[Path] = Path.home() / "tmp" / "tests_dirs"
EXCLUDED: Final[tuple[str, ...]] = ("numpy", "pandas", "scipy", "numba")
DRY_RUN: Final[bool] = "-d" in sys.argv


class MoveResult(NamedTuple):
    path: Path
    success: bool
    message: str


def _is_excluded(path: Path) -> bool:
    parts: tuple[str, ...] = path.parts
    return any(name in parts for name in EXCLUDED)


def move_tests_folder(
    tests_path: Path,
    base_src: Path,
    base_dst: Path,
    dry_run: bool = DRY_RUN,
) -> MoveResult:
    if _is_excluded(tests_path):
        return MoveResult(tests_path, False, f"excluded path: {tests_path}")
    try:
        relative_path: Path = tests_path.relative_to(base_src)
        dst_path: Path = base_dst / relative_path.parent / tests_path.name
        if dry_run:
            return MoveResult(tests_path, True, f"will move: {tests_path} -> {dst_path}")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tests_path), str(dst_path))
        return MoveResult(tests_path, True, f"moved: {tests_path} -> {dst_path}")
    except Exception as e:
        return MoveResult(tests_path, False, f"error moving {tests_path}: {e}")


def move_tests_recursive(
    source_dir: Path = SRC,
    destination_dir: Path = DEST,
    dry_run: bool = DRY_RUN,
) -> int:
    source: Path = source_dir.resolve()
    destination: Path = destination_dir
    tests_folders: list[Path] = [p for p in source.rglob("tests") if p.is_dir() and not _is_excluded(p)]
    if not tests_folders:
        logger.info("No 'tests' folders found.")
        return 0
    logger.info("Found {} 'tests' folder(s) to move", len(tests_folders))
    logger.info("Source: {}", source)
    logger.info("Destination: {}", destination)
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[Path, Path, Path, bool]] = [
        (tests_path, source, destination, dry_run) for tests_path in tests_folders
    ]
    moved_count: int = 0
    with Pool(processes=MAX_WORKERS) as pool:
        for result in pool.starmap(move_tests_folder, jobs):
            if result.success:
                logger.info("{}", result.message)
                moved_count += 1
            else:
                logger.warning("{}", result.message)
    logger.info(
        "✓ Successfully moved {}/{} directories",
        moved_count,
        len(tests_folders),
    )
    return moved_count


def main() -> None:
    move_tests_recursive()


if __name__ == "__main__":
    raise SystemExit(main())
