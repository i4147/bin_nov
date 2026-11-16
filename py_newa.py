#!/data/data/com.termux/files/usr/bin/env python
"""CLI tool to generate a new boilerplate Python script."""

import argparse
from datetime import datetime
from pathlib import Path
import stat
import sys

TEMPLATE_CONTENT = '''#!/usr/bin/env python3
"""
{description}

Created on: {date}
"""

import argparse
import logging
import sys
from pathlib import Path


def setup_logging(verbose: bool = False) -> None:
    """Configure basic logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    args = parse_args(argv)
    setup_logging(args.verbose)

    logging.info("Script started successfully.")
    # TODO: Add your script logic here

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def create_script(
    filepath: Path, description: str, force: bool = False
) -> None:
    """Write the template content to the target file."""
    if not filepath.suffix:
        filepath = filepath.with_suffix(".py")

    if filepath.exists() and not force:
        print(
            f"Error: '{filepath}' already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    content = TEMPLATE_CONTENT.format(
        description=description,
        date=datetime.now().strftime("%Y-%m-%d"),
    )

    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")

    # Make the file executable on Unix-like systems (macOS/Linux)
    current_permissions = filepath.stat().st_mode
    filepath.chmod(
        current_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    print(f"Created template script: {filepath}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a new Python script from a template."
    )
    parser.add_argument("filename", type=Path, help="Name or path of the new script")
    parser.add_argument(
        "-d",
        "--description",
        default="A brief description of what this script does.",
        help="Docstring description for the new script",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite the file if it already exists",
    )

    args = parser.parse_args()
    create_script(args.filename, args.description, args.force)


if __name__ == "__main__":
    main()
