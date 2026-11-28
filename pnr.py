#!/data/data/com.termux/files/usr/bin/env python
"""Rename files and directories by removing a substring from their names.

Typer CLI wrapping pathlib-based recursive renames; supports --dry-run and --recursive, and skips symlinks and common ignore directories.
"""

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from dh import SKIP_DIRS, unique_path

app = typer.Typer(
    help="Rename files and directories using pathlib.",
    add_completion=False,
)


def should_skip(path: Path) -> bool:
    path = Path(path)
    if path.is_symlink():
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def remove_string_from_names(
    string_to_remove: str,
    dry_run: bool = False,
    recursive: bool = False,
    current_path: Optional[Path] = None,
) -> int:
    if current_path is None:
        current_path = Path.cwd()

    renamed_count: int = 0
    try:
        items: list[Path] = list(current_path.iterdir())
    except PermissionError:
        typer.echo(f"Permission denied: {current_path}")
        return renamed_count

    for item in items:
        if should_skip(item):
            continue

        if (item.is_file() or item.is_dir()) and string_to_remove in item.name:
            new_name: str = item.name.replace(string_to_remove, "")
            if not new_name.strip():
                typer.echo(
                    f"Warning: Removing '{string_to_remove}' would make name empty for '{item.name}'"
                )
                continue

            new_path: Path = current_path / new_name
            if new_path.exists():
                new_path = unique_path(new_path)

            if dry_run:
                typer.echo(f"[DRY RUN] Would rename: {item.name} -> {new_name}")
            else:
                try:
                    item.rename(new_path)
                    typer.echo(f"{item.name} -> {new_name}")
                    renamed_count += 1
                    item = new_path  # Update reference so recursive traversal uses the renamed path
                except OSError as e:
                    typer.echo(f"Error renaming '{item.name}': {e}")

        if recursive and item.is_dir():
            renamed_count += remove_string_from_names(
                string_to_remove, dry_run, recursive, item
            )

    return renamed_count


def replace_string_in_names(
    str1: str,
    str2: str,
    dry_run: bool = False,
    recursive: bool = False,
    current_path: Optional[Path] = None,
) -> int:
    if current_path is None:
        current_path = Path.cwd()

    renamed_count: int = 0
    try:
        items: list[Path] = list(current_path.iterdir())
    except PermissionError:
        typer.echo(f"Permission denied: {current_path}")
        return renamed_count

    for item in items:
        if should_skip(item):
            continue

        if (item.is_file() or item.is_dir()) and str1 in item.name:
            new_name: str = item.name.replace(str1, str2)
            if not new_name.strip():
                typer.echo(
                    f"Warning: Replacing '{str1}' with '{str2}' would make name empty for '{item.name}'"
                )
                continue

            new_path: Path = current_path / new_name
            if new_path.exists():
                new_path = unique_path(new_path)

            if dry_run:
                typer.echo(f"[DRY RUN] Would rename: {item.name} -> {new_name}")
            else:
                try:
                    item.rename(new_path)
                    typer.echo(f"{item.name} -> {new_name}")
                    renamed_count += 1
                    item = new_path  # Update reference so recursive traversal uses the renamed path
                except OSError as e:
                    typer.echo(f"Error renaming '{item.name}': {e}")

        if recursive and item.is_dir():
            renamed_count += replace_string_in_names(
                str1, str2, dry_run, recursive, item
            )

    return renamed_count


def rename_by_template(
    template: str,
    dry_run: bool = False,
    recursive: bool = False,
    current_path: Optional[Path] = None,
) -> int:
    if current_path is None:
        current_path = Path.cwd()

    renamed_count: int = 0
    try:
        files: list[Path] = [
            f for f in current_path.iterdir() if f.is_file() and (not should_skip(f))
        ]
        script_name: str = Path(__file__).name
        files = [f for f in files if f.name != script_name]

        if files:
            file_count: int = len(files)
            if file_count < 10:
                padding: int = 1
            elif file_count < 100:
                padding = 2
            elif file_count < 1000:
                padding = 3
            else:
                padding = 4

            for i, path in enumerate(sorted(files), 1):
                ext: str = path.suffix
                number_str: str = str(i).zfill(padding)
                new_name: str = f"{template}{number_str}{ext}"
                if new_name == path.name:
                    continue

                new_path: Path = current_path / new_name
                if new_path.exists():
                    new_path = unique_path(new_path)

                if dry_run:
                    typer.echo(f"[DRY RUN] Would rename: {path.name} -> {new_name}")
                else:
                    try:
                        path.rename(new_path)
                        typer.echo(f"{path.name} -> {new_name}")
                        renamed_count += 1
                    except OSError as e:
                        typer.echo(f"Error renaming '{path.name}': {e}")
    except PermissionError:
        typer.echo(f"Permission denied: {current_path}")
        return renamed_count

    if recursive:
        try:
            for item in current_path.iterdir():
                if item.is_dir() and (not should_skip(item)):
                    renamed_count += rename_by_template(
                        template, dry_run, recursive, item
                    )
        except PermissionError:
            typer.echo(f"Permission denied accessing subdirectory in {current_path}")

    return renamed_count


def _normalize_leading_dashes(argv: list[str]) -> list[str]:
    """Format option values starting with '-' so Click/Typer parses them as values."""
    fixed: list[str] = []
    i: int = 0
    while i < len(argv):
        arg: str = argv[i]
        if arg in ("-r", "-t") and i + 1 < len(argv):
            fixed.append(f"{arg}{argv[i + 1]}")
            i += 2
        elif arg in ("--remove", "--template") and i + 1 < len(argv):
            fixed.append(f"{arg}={argv[i + 1]}")
            i += 2
        elif arg == "-s" and i + 2 < len(argv):
            val1: str = argv[i + 1]
            val2: str = argv[i + 2]
            if val1.startswith("-"):
                fixed.extend([f"{arg}{val1}", val2])
            else:
                fixed.extend([arg, val1, val2])
            i += 3
        elif arg == "--replace" and i + 2 < len(argv):
            val1 = argv[i + 1]
            val2 = argv[i + 2]
            if val1.startswith("-"):
                fixed.extend([f"{arg}={val1}", val2])
            else:
                fixed.extend([arg, val1, val2])
            i += 3
        else:
            fixed.append(arg)
            i += 1
    return fixed


@app.command(
    epilog='Examples:\n  python pnr.py -r "old_string"\n  python pnr.py -r "-master" --dry-run'
)
def main(
    remove: Annotated[
        Optional[str],
        typer.Option(
            "-r",
            "--remove",
            metavar="STRING",
            help="Remove specified string from file and directory names.",
        ),
    ] = None,
    replace: Annotated[
        Optional[tuple[str, str]],
        typer.Option(
            "-s",
            "--replace",
            metavar="STR1 STR2",
            help="Replace STR1 with STR2 in file and directory names.",
        ),
    ] = None,
    template: Annotated[
        Optional[str],
        typer.Option(
            "-t",
            "--template",
            metavar="NAME",
            help="Rename files using template with sequential numbering.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would be renamed without actually doing it.",
        ),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive",
            help="Process directories recursively.",
        ),
    ] = False,
) -> None:
    modes_selected: int = sum(opt is not None for opt in (remove, replace, template))
    if modes_selected == 0:
        typer.echo(
            "Error: You must specify one of -r/--remove, -s/--replace, or -t/--template.",
            err=True,
        )
        raise typer.Exit(code=2)
    if modes_selected > 1:
        typer.echo(
            "Error: Options -r/--remove, -s/--replace, and -t/--template are mutually exclusive.",
            err=True,
        )
        raise typer.Exit(code=2)

    cwd: Path = Path.cwd()
    typer.echo(f"Working in directory: {cwd}")
    if recursive:
        typer.echo("Recursive mode enabled")
    if dry_run:
        typer.echo("DRY RUN MODE - No actual changes will be made\n")

    try:
        if remove is not None:
            typer.echo(f"Removing '{remove}' from names...")
            count: int = remove_string_from_names(remove, dry_run, recursive)
            typer.echo(f"\nOperation completed. {count} items processed.")
        elif replace is not None:
            str1, str2 = replace
            typer.echo(f"Replacing '{str1}' with '{str2}' in names...")
            count = replace_string_in_names(str1, str2, dry_run, recursive)
            typer.echo(f"\nOperation completed. {count} items processed.")
        elif template is not None:
            typer.echo(f"Renaming files using template '{template}'...")
            count = rename_by_template(template, dry_run, recursive)
            typer.echo(f"\nOperation completed. {count} items processed.")
    except KeyboardInterrupt:
        typer.echo("\nOperation cancelled by user.", err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"An error occurred: {e}", err=True)
        raise typer.Exit(code=1)


def cli() -> None:
    sys.argv[1:] = _normalize_leading_dashes(sys.argv[1:])
    app()


if __name__ == "__main__":
    cli()
