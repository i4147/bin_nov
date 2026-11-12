#!/data/data/com.termux/files/usr/bin/env python
"""Advanced file renamer with regex, filters and an undo log.
Extends :doc:`pnr` with ``--regex``, case-insensitive matching, include/exclude globs, extension, file/dir and hidden filters, a depth limit and a JSON log of the performed renames.
Usage: pnr2 [paths ...] [-r STR | -s OLD NEW | -t TEMPLATE] [-E] [--dry-run] Positional arguments: paths ...
Target paths Options: -r, --remove Remove string -s, --replace Replace STR1 with STR2 -t, --template Rename using template -E, --regex Treat patterns as regex -i, --ignore-case Case-insensitive matching --include Include glob pattern --exclude Exclude glob pattern --exclude-dir Exclude directory pattern --ext File extension filters --files-only Process files only ...
plus 40 more; run ``pnr2 --help`` for the full list.
Examples: $ pnr2 ~/photos -E -s '^IMG_(\\d+)' 'pic_\1' Command-line script: run ``python pnr2.py`` or the ``pnr2`` command.
The module is importable as well."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import random
import re
from typing import Any, Generator
import unicodedata
import uuid
from dh import unique_path


class PathRenamer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.paths = [Path(p) for p in args.paths] if hasattr(args, "paths") and args.paths else [Path.cwd()]
        self.log_entries: list[dict] = []

    def should_skip(self, path: Path, root: Path) -> bool:
        if not self.args.follow_symlinks and path.is_symlink():
            return True
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        parts = rel.parts
        if not self.args.hidden and any(p.startswith(".") and p not in {".", ".."} for p in parts):
            return True
        if self.args.exclude_dir:
            for part in parts[:-1]:
                if any(Path(part).match(ed) for ed in self.args.exclude_dir):
                    return True
        name = path.name
        if name == Path(__file__).name:
            return True
        if self.args.include:
            matched_inc = any(Path(name).match(inc) for inc in self.args.include)
            if not matched_inc and path.is_file():
                return True
        if self.args.exclude and any(Path(name).match(exc) for exc in self.args.exclude):
            return True
        if self.args.ext and path.is_file():
            exts = [e.lstrip(".").lower() for e in self.args.ext]
            if path.suffix.lstrip(".").lower() not in exts:
                return True
        return False

    def walk_files(self) -> Generator[Path, None, None]:
        max_depth = self.args.max_depth if hasattr(self.args, "max_depth") and self.args.max_depth is not None else None
        for root_path in self.paths:
            if not root_path.exists():
                continue
            if root_path.is_file():
                if not self.should_skip(root_path, root_path.parent):
                    yield root_path
                continue
            for base, dirs, files in os.walk(root_path, followlinks=self.args.follow_symlinks):
                base_p = Path(base)
                try:
                    rel_depth = len(base_p.relative_to(root_path).parts)
                except ValueError:
                    rel_depth = 0
                if max_depth is not None and rel_depth > max_depth:
                    dirs.clear()
                    continue
                if self.args.exclude_dir:
                    dirs[:] = [d for d in dirs if not any(Path(d).match(ed) for ed in self.args.exclude_dir)]
                if not self.args.hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                if not self.args.dirs_only:
                    for f in files:
                        f_path = base_p / f
                        if not self.should_skip(f_path, root_path):
                            yield f_path
                if self.args.dirs_only or (hasattr(self.args, "template") and self.args.template):
                    for d in dirs:
                        d_path = base_p / d
                        if not self.should_skip(d_path, root_path):
                            yield d_path

    def clean_unique_stem(self, stem: str) -> str:
        return re.sub(r"(_\d+)+$", "", stem)

    def transliterate_text(self, text: str) -> str:
        norm = unicodedata.normalize("NFKD", text)
        ascii_text = norm.encode("ascii", "ignore").decode("ascii")
        if not ascii_text.strip():
            return text
        return ascii_text

    def transform_name(self, name: str, original_path: Path, index: int = 0) -> str:
        stem, ext = Path(name).stem, Path(name).suffix
        if self.args.remove:
            if self.args.regex:
                flags = re.IGNORECASE if self.args.ignore_case else 0
                stem = re.sub(self.args.remove, "", stem, flags=flags)
            elif self.args.ignore_case:
                pattern = re.compile(re.escape(self.args.remove), re.IGNORECASE)
                stem = pattern.sub("", stem)
            else:
                stem = stem.replace(self.args.remove, "")
        if self.args.replace:
            s1, s2 = self.args.replace
            if self.args.regex:
                flags = re.IGNORECASE if self.args.ignore_case else 0
                stem = re.sub(s1, s2, stem, flags=flags)
            elif self.args.ignore_case:
                pattern = re.compile(re.escape(s1), re.IGNORECASE)
                stem = pattern.sub(s2, stem)
            else:
                stem = stem.replace(s1, s2)
        if hasattr(self.args, "template") and self.args.template:
            start = self.args.start if hasattr(self.args, "start") else 1
            step = self.args.step if hasattr(self.args, "step") else 1
            num = start + (index * step)
            padding = self.args.padding if hasattr(self.args, "padding") and self.args.padding else 3
            num_str = str(num).zfill(padding)
            if hasattr(self.args, "format") and self.args.format:
                fmt = self.args.format
                stem = fmt.format(template=self.args.template, number=num, number_str=num_str, original=stem, ext=ext)
            else:
                stem = f"{self.args.template}{num_str}"
        if hasattr(self.args, "prefix") and self.args.prefix:
            stem = f"{self.args.prefix}{stem}"
        if hasattr(self.args, "suffix") and self.args.suffix:
            stem = f"{stem}{self.args.suffix}"
        if hasattr(self.args, "lower") and self.args.lower:
            stem = stem.lower()
        if hasattr(self.args, "upper") and self.args.upper:
            stem = stem.upper()
        if hasattr(self.args, "title") and self.args.title:
            stem = stem.title()
        if hasattr(self.args, "snake") and self.args.snake:
            stem = re.sub(r"[\s\-]+", "_", stem).lower()
        if hasattr(self.args, "kebab") and self.args.kebab:
            stem = re.sub(r"[\s_]+", "-", stem).lower()
        if hasattr(self.args, "camel") and self.args.camel:
            words = re.split(r"[\s\-_]+", stem)
            stem = words[0].lower() + "".join(w.capitalize() for w in words[1:])
        if hasattr(self.args, "trim") and self.args.trim:
            stem = stem.strip()
        if hasattr(self.args, "collapse_spaces") and self.args.collapse_spaces:
            stem = re.sub(r"\s+", " ", stem)
        if hasattr(self.args, "spaces_to_underscores") and self.args.spaces_to_underscores:
            stem = stem.replace(" ", "_")
        if hasattr(self.args, "normalize_unicode") and self.args.normalize_unicode:
            stem = unicodedata.normalize("NFC", stem)
        if hasattr(self.args, "remove_diacritics") and self.args.remove_diacritics:
            stem = "".join(c for c in unicodedata.normalize("NFD", stem) if unicodedata.category(c) != "Mn")
        if hasattr(self.args, "ascii") and self.args.ascii:
            stem = self.transliterate_text(stem)
        if hasattr(self.args, "transliterate") and self.args.transliterate:
            stem = self.transliterate_text(stem)
        if hasattr(self.args, "sanitize") and self.args.sanitize:
            stem = re.sub(r'[<>:"/\\|?*]', "_", stem)
        if hasattr(self.args, "random") and self.args.random:
            stem = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))
        if hasattr(self.args, "uuid") and self.args.uuid:
            stem = str(uuid.uuid4())
        if hasattr(self.args, "rename_by_mtime") and self.args.rename_by_mtime and original_path.exists():
            mtime = original_path.stat().st_mtime
            dt = datetime.fromtimestamp(mtime)
            fmt = (
                self.args.date_format
                if hasattr(self.args, "date_format") and self.args.date_format
                else "%Y-%m-%d_%H-%M-%S"
            )
            stem = dt.strftime(fmt)
        if hasattr(self.args, "max_length") and self.args.max_length and len(stem) > self.args.max_length:
            stem = stem[: self.args.max_length]
        if not stem.strip():
            stem = "file"
        return f"{stem}{ext if original_path.is_file() else ''}"

    def run(self) -> int:
        if hasattr(self.args, "undo") and self.args.undo:
            return self.perform_undo()
        files = list(self.walk_files())
        if not files:
            if not self.args.quiet:
                print("No files matched criteria.")
            return 0
        sort_by = getattr(self.args, "sort", "name")
        reverse = getattr(self.args, "reverse", False)
        if sort_by == "mtime":
            files.sort(key=lambda p: p.stat().st_mtime, reverse=reverse)
        elif sort_by == "size":
            files.sort(key=lambda p: p.stat().st_size if p.is_file() else 0, reverse=reverse)
        elif sort_by == "random":
            random.shuffle(files)
        elif sort_by == "natural":

            def natural_key(path: Path) -> list[Any]:
                return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", path.name)]

            files.sort(key=natural_key, reverse=reverse)
        else:
            files.sort(key=lambda p: p.name.lower(), reverse=reverse)
        renamed_count = 0
        success_ops = []
        for idx, path in enumerate(files):
            new_name = self.transform_name(path.name, path, idx)
            if new_name == path.name:
                continue
            new_path = path.parent / new_name
            if new_path.exists():
                if getattr(self.args, "overwrite", False):
                    pass
                elif getattr(self.args, "backup", False):
                    backup_path = path.parent / f"{new_path.stem}.bak{new_path.suffix}"
                    if backup_path.exists():
                        backup_path = unique_path(backup_path)
                    if not self.args.dry_run:
                        new_path.rename(backup_path)
                elif getattr(self.args, "interactive", False):
                    resp = input(f"Target {new_path} exists. Overwrite? [y/N]: ").strip().lower()
                    if resp != "y":
                        continue
                else:
                    new_path = unique_path(new_path)
            if self.args.dry_run:
                if not self.args.quiet:
                    print(f"[DRY RUN] {path} -> {new_path.name}")
                renamed_count += 1
            else:
                try:
                    path.rename(new_path)
                    if not self.args.quiet:
                        print(f"{path} -> {new_path.name}")
                    success_ops.append({"old": str(path), "new": str(new_path)})
                    renamed_count += 1
                except OSError as e:
                    if not self.args.quiet:
                        print(f"Error renaming {path}: {e}")
        if success_ops and hasattr(self.args, "log") and self.args.log:
            try:
                with open(self.args.log, "w", encoding="utf-8") as f:
                    json.dump(success_ops, f, indent=2)
            except Exception as e:
                if not self.args.quiet:
                    print(f"Failed to write log: {e}")
        if not self.args.quiet:
            print(f"\nOperation completed. {renamed_count} items processed.")
        return 0

    def perform_undo(self) -> int:
        undo_file = self.args.undo
        if not Path(undo_file).exists():
            print(f"Undo log file not found: {undo_file}")
            return 1
        try:
            with open(undo_file, "r", encoding="utf-8") as f:
                ops = json.load(f)
        except Exception as e:
            print(f"Failed to read undo log: {e}")
            return 1
        restored_count = 0
        for op in reversed(ops):
            old_p = Path(op["old"])
            new_p = Path(op["new"])
            if new_p.exists():
                try:
                    new_p.rename(old_p)
                    if not self.args.quiet:
                        print(f"Restored: {new_p} -> {old_p.name}")
                    restored_count += 1
                except OSError as e:
                    print(f"Failed to restore {new_p}: {e}")
        print(f"Undo completed. Restored {restored_count} items.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Advanced File Renamer")
    parser.add_argument("paths", nargs="*", default=["."], help="Target paths")
    parser.add_argument("-r", "--remove", metavar="STRING", help="Remove string")
    parser.add_argument("-s", "--replace", nargs=2, metavar=("STR1", "STR2"), help="Replace STR1 with STR2")
    parser.add_argument("-t", "--template", metavar="NAME", help="Rename using template")
    parser.add_argument("-E", "--regex", action="store_true", help="Treat patterns as regex")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="Case-insensitive matching")
    parser.add_argument("--include", action="append", help="Include glob pattern")
    parser.add_argument("--exclude", action="append", help="Exclude glob pattern")
    parser.add_argument("--exclude-dir", action="append", help="Exclude directory pattern")
    parser.add_argument("--ext", nargs="+", help="File extension filters")
    parser.add_argument("--files-only", action="store_true", help="Process files only")
    parser.add_argument("--dirs-only", action="store_true", help="Process directories only")
    parser.add_argument("--hidden", action="store_true", default=True, help="Include hidden files")
    parser.add_argument("--no-hidden", dest="hidden", action="store_false", help="Exclude hidden files")
    parser.add_argument("--max-depth", type=int, help="Maximum recursion depth")
    parser.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    parser.add_argument("--backup", action="store_true", help="Backup existing files")
    parser.add_argument("--interactive", action="store_true", help="Interactive overwrite confirmation")
    parser.add_argument("--log", help="Log renames to JSON file")
    parser.add_argument("--undo", help="Undo renames from JSON log")
    parser.add_argument("--prefix", help="Add prefix")
    parser.add_argument("--suffix", help="Add suffix")
    parser.add_argument("--lower", action="store_true", help="Convert to lowercase")
    parser.add_argument("--upper", action="store_true", help="Convert to uppercase")
    parser.add_argument("--title", action="store_true", help="Convert to title case")
    parser.add_argument("--snake", action="store_true", help="Convert to snake case")
    parser.add_argument("--kebab", action="store_true", help="Convert to kebab case")
    parser.add_argument("--camel", action="store_true", help="Convert to camel case")
    parser.add_argument("--trim", action="store_true", help="Trim whitespace")
    parser.add_argument("--collapse-spaces", action="store_true", help="Collapse multiple spaces")
    parser.add_argument("--spaces-to-underscores", action="store_true", help="Convert spaces to underscores")
    parser.add_argument("--normalize-unicode", action="store_true", help="Normalize unicode")
    parser.add_argument("--remove-diacritics", action="store_true", help="Remove diacritics")
    parser.add_argument("--ascii", action="store_true", help="Convert to ASCII")
    parser.add_argument("--transliterate", action="store_true", help="Transliterate non-English names")
    parser.add_argument("--sanitize", action="store_true", help="Sanitize illegal characters")
    parser.add_argument("--max-length", type=int, help="Truncate long names")
    parser.add_argument("--random", action="store_true", help="Rename to random string")
    parser.add_argument("--uuid", action="store_true", help="Rename to UUID")
    parser.add_argument("--date-format", help="Date format string")
    parser.add_argument("--rename-by-mtime", action="store_true", help="Rename by modification time")
    parser.add_argument("--start", type=int, default=1, help="Start number for template")
    parser.add_argument("--step", type=int, default=1, help="Step for template")
    parser.add_argument("--padding", type=int, default=3, help="Number padding")
    parser.add_argument("--format", help="Custom format string")
    parser.add_argument(
        "--sort", choices=["name", "mtime", "size", "random", "natural"], default="name", help="Sort order"
    )
    parser.add_argument("--reverse", action="store_true", help="Reverse sort order")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Quiet output")
    args = parser.parse_args()
    renamer = PathRenamer(args)
    try:
        return renamer.run()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
