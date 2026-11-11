#!/data/data/com.termux/files/usr/bin/env python
import re
from concurrent.futures import InterpreterPoolExecutor
from pathlib import Path

from loguru import logger


VERSION_SUFFIX: re.Pattern[str] = re.compile(
    r"[-_]v?\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?$"
)


def rename_readme(source: Path) -> tuple[Path, Path, str]:
    folder_name: str = VERSION_SUFFIX.sub("", source.parent.name)
    if not folder_name:
        folder_name = source.parent.name

    destination: Path = source.parent / f"{folder_name}{source.suffix}"

    if source == destination:
        return source, destination, "unchanged"

    if destination.exists() or destination.is_symlink():
        return source, destination, "exists"

    try:
        source.rename(destination)
        return source, destination, "renamed"
    except OSError as error:
        return source, destination, f"error: {error}"


def main() -> None:
    files: list[Path] = [
        path
        for path in Path(".").rglob("*")
        if path.is_file() and path.name in {"README.md", "README.rst"}
    ]
    files.sort(key=lambda path: path.as_posix())

    if not files:
        logger.info("No README.md or README.rst files found.")
        return

    with InterpreterPoolExecutor(max_workers=4) as executor:
        results: list[tuple[Path, Path, str]] = list(
            executor.map(rename_readme, files)
        )

    for source, destination, status in results:
        if status == "renamed":
            logger.info("Renamed {} -> {}", source, destination)
        elif status == "exists":
            logger.warning(
                "Skipped {}: destination already exists ({})",
                source,
                destination,
            )
        elif status == "unchanged":
            logger.info("Unchanged: {}", source)
        else:
            logger.error("Could not rename {}: {}", source, status)


if __name__ == "__main__":
    main()
