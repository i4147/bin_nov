#!/data/data/com.termux/files/usr/bin/env python
"""Batch-clean filenames recursively by stripping numbering patterns, with dry-run and undo."""

import argparse
import contextlib
from dataclasses import dataclass, field
import json
from multiprocessing import Pool
import os
from pathlib import Path
import re
import sys
import time
from typing import Optional
import unicodedata
from uuid import uuid4

from dh import MAX_WORKERS


DEFAULT_RULES: list[tuple[str, str]] = [
    (r"_\d+", ""),
    (r"\(\d+\)", ""),
    (r" +", "_"),
]
DEFAULT_EXCLUDES: set[str] = {".git", "node_modules", "__pycache__", ".venv", ".idea", ".vscode"}
_SEP_COLLAPSE: re.Pattern[str] = re.compile(r"_+")
_DASH_COLLAPSE: re.Pattern[str] = re.compile(r"-+")
_SANITIZE: re.Pattern[str] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(slots=True)
class CleanConfig:
    rules: list[tuple[str, str]] = field(default_factory=lambda: list(DEFAULT_RULES))
    trailing_only: bool = False
    collapse_sep: bool = True
    strip_sep: bool = True
    case: str = "preserve"
    ascii_fold: bool = False
    sanitize: bool = False
    empty_fallback: str = "unnamed"
    fix_double_ext: bool = True
    extensions: Optional[set[str]] = None
    exclude_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDES))
    include_hidden: bool = False
    recursive: bool = True
    max_depth: Optional[int] = None
    on_conflict: str = "suffix"
    workers: Optional[int] = None
    dry_run: bool = False
    log_path: Optional[Path] = None


@dataclass(slots=True)
class RenameResult:
    source: Path
    target: Optional[Path]
    status: str
    reason: str = ""


class ConflictError(Exception):
    pass


def _clean_stem(stem: str, cfg: CleanConfig) -> str:
    for pattern, repl in cfg.rules:
        if cfg.trailing_only:
            stem = re.sub(f"(?:{pattern})$", repl, stem)
        else:
            stem = re.sub(pattern, repl, stem)
    if cfg.collapse_sep:
        stem = _SEP_COLLAPSE.sub("_", stem)
        stem = _DASH_COLLAPSE.sub("-", stem)
    if cfg.strip_sep:
        stem = stem.strip("_-.")
    if cfg.ascii_fold:
        stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    if cfg.case == "lower":
        stem = stem.lower()
    elif cfg.case == "upper":
        stem = stem.upper()
    if cfg.sanitize:
        stem = _SANITIZE.sub("_", stem)
        stem = stem.rstrip(". ")
    if not stem:
        stem = cfg.empty_fallback
    return stem


def _compute_target(path: Path, cfg: CleanConfig) -> Path:
    stem = _clean_stem(path.stem, cfg)
    suffix = path.suffix
    if cfg.fix_double_ext and suffix and stem.lower().endswith(suffix.lower()) and len(stem) > len(suffix):
        stem = stem[: -len(suffix)]
    return path.with_name(f"{stem}{suffix}")


def _matches(p: Path, cfg: CleanConfig) -> bool:
    if not cfg.include_hidden and p.name.startswith("."):
        return False
    return not (cfg.extensions is not None and p.suffix.lower() not in cfg.extensions)


def walk_files(root: Path, cfg: CleanConfig) -> list[Path]:
    root = root.resolve()
    result: list[Path] = []

    def _walk(dir_: Path, depth: int) -> None:
        if cfg.max_depth is not None and depth > cfg.max_depth:
            return
        try:
            entries = list(os.scandir(dir_))
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name in cfg.exclude_dirs:
                    continue
                if cfg.recursive:
                    _walk(Path(entry.path), depth + 1)
            elif entry.is_file(follow_symlinks=False):
                p = Path(entry.path)
                if _matches(p, cfg):
                    result.append(p)

    _walk(root, 0)
    return result


def _clean_worker(args: tuple[Path, CleanConfig]) -> tuple[Path, Path]:
    path, cfg = args
    return (path, _compute_target(path, cfg))


def build_plan(files: list[Path], cfg: CleanConfig) -> list[tuple[Path, Path]]:
    if not files:
        return []
    if cfg.workers == 1 or len(files) < 100:
        proposals: dict[Path, Path] = {f: _compute_target(f, cfg) for f in files}
    else:
        with Pool(cfg.workers) as pool:
            proposals = dict(pool.map(_clean_worker, [(f, cfg) for f in files]))
    moving = {s: t for s, t in proposals.items() if s != t}
    moving_sources = set(moving)
    result: list[tuple[Path, Path]] = []
    assigned: set[Path] = set()
    for source in sorted(moving):
        proposed = moving[source]
        final: Optional[Path] = proposed
        counter = 1
        while (final.exists() and final not in moving_sources) or final in assigned:
            if cfg.on_conflict == "skip":
                final = None
                break
            if cfg.on_conflict == "error":
                msg = f"target exists: {final}"
                raise ConflictError(msg)
            final = proposed.with_name(f"{proposed.stem}_{counter}{proposed.suffix}")
            counter += 1
        if final is None:
            continue
        result.append((source, final))
        assigned.add(final)
    return result


def execute_plan(plan: list[tuple[Path, Path]], cfg: CleanConfig) -> list[RenameResult]:
    if cfg.dry_run:
        return [RenameResult(s, t, "planned", "dry-run") for s, t in plan]
    results: list[RenameResult] = []
    all_sources = {s for s, t in plan}
    pending: list[tuple[Path, Path, Path]] = []
    rename_log: list[dict[str, str]] = []

    def _sort_key(item: tuple[Path, Path]) -> tuple[int, str]:
        s, t = item
        return (0 if t not in all_sources else 1, str(s))

    for source, target in sorted(plan, key=_sort_key):
        try:
            if not target.exists():
                os.rename(source, target)
                results.append(RenameResult(source, target, "renamed"))
                rename_log.append({"old": str(source), "new": str(target)})
            else:
                temp = source.parent / f".__cleantmp_{uuid4().hex}{source.suffix}"
                os.rename(source, temp)
                pending.append((temp, target, source))
        except OSError as e:
            results.append(RenameResult(source, target, "error", str(e)))
    for temp, target, source in pending:
        try:
            os.rename(temp, target)
            results.append(RenameResult(source, target, "renamed"))
            rename_log.append({"old": str(source), "new": str(target)})
        except OSError as e:
            with contextlib.suppress(OSError):
                os.rename(temp, source)
            results.append(RenameResult(source, target, "error", str(e)))
    if cfg.log_path and rename_log:
        try:
            data: list[dict] = []
            if cfg.log_path.exists():
                data = json.loads(cfg.log_path.read_text())
            data.append({"timestamp": time.time(), "renames": rename_log})
            cfg.log_path.write_text(json.dumps(data, indent=2))
        except (OSError, json.JSONDecodeError):
            pass
    return results


def plan_renames(root: Path, cfg: CleanConfig) -> list[tuple[Path, Path]]:
    files = walk_files(root, cfg)
    return build_plan(files, cfg)


def clean_filenames(root: Optional[Path] = None, cfg: Optional[CleanConfig] = None) -> list[RenameResult]:
    cfg = cfg or CleanConfig()
    root = root or Path.cwd()
    plan = plan_renames(root, cfg)
    return execute_plan(plan, cfg)


def undo_renames(log_path: Path) -> list[RenameResult]:
    if not log_path.exists():
        return []
    try:
        data = json.loads(log_path.read_text())
    except json.JSONDecodeError:
        return []
    results: list[RenameResult] = []
    for entry in reversed(data):
        for rename in reversed(entry.get("renames", [])):
            old = Path(rename["old"])
            new = Path(rename["new"])
            if new.exists() and not old.exists():
                try:
                    os.rename(new, old)
                    results.append(RenameResult(new, old, "renamed", "undo"))
                except OSError as e:
                    results.append(RenameResult(new, old, "error", str(e)))
            else:
                results.append(RenameResult(new, old, "skipped", "not-undoable"))
    return results


def _normalize_exts(raw: str) -> set[str]:
    out: set[str] = set()
    for token in raw.split(","):
        token = token.strip().lower()
        if not token:
            continue
        out.add(token if token.startswith(".") else "." + token)
    return out


def _print_summary(results: list[RenameResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("  ".join(f"{k}: {v}" for k, v in counts.items()))
    for r in results:
        if r.status == "error":
            tgt = r.target.name if r.target else "?"
            print(f"  ERROR: {r.source.name} -> {tgt}  ({r.reason})", file=sys.stderr)


def cli(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="cleaner", description="Clean filenames recursively.")
    p.add_argument("root", nargs="?", default=".", help="root directory")
    p.add_argument("--dry-run", action="store_true", help="plan only, no changes")
    p.add_argument("--no-recursive", action="store_false", dest="recursive", help="only top-level files")
    p.add_argument("--max-depth", type=int, default=None, help="max directory depth")
    p.add_argument("--ext", default=None, help="comma-separated extensions, e.g. .jpg,.png")
    p.add_argument("--exclude", action="append", default=None, help="exclude directory name (repeatable)")
    p.add_argument("--include-hidden", action="store_true", help="also process dotfiles")
    p.add_argument("--on-conflict", choices=["suffix", "skip", "error"], default="suffix")
    p.add_argument("--lower", action="store_true", help="lowercase stems")
    p.add_argument("--upper", action="store_true", help="uppercase stems")
    p.add_argument("--collapse", action="store_true", help="collapse repeated separators")
    p.add_argument("--no-strip", action="store_false", dest="strip_sep", help="don't trim separators")
    p.add_argument("--sanitize", action="store_true", help="remove illegal characters")
    p.add_argument("--ascii", action="store_true", help="fold unicode to ascii")
    p.add_argument("--trailing-only", action="store_true", help="only strip trailing _N/(N)")
    p.add_argument("--no-double-ext-fix", action="store_false", dest="fix_double_ext")
    p.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    p.add_argument("--log", default=None, help="path to rename log json (enables undo)")
    p.add_argument("--undo", default=None, help="undo renames from log file")
    args = p.parse_args(argv)
    if args.undo:
        results = undo_renames(Path(args.undo))
        _print_summary(results)
        return 0
    root = Path(args.root)
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    cfg = CleanConfig(
        trailing_only=args.trailing_only,
        collapse_sep=args.collapse,
        strip_sep=args.strip_sep,
        case="lower" if args.lower else "upper" if args.upper else "preserve",
        ascii_fold=args.ascii,
        sanitize=args.sanitize,
        fix_double_ext=args.fix_double_ext,
        extensions=_normalize_exts(args.ext) if args.ext else None,
        include_hidden=args.include_hidden,
        recursive=args.recursive,
        max_depth=args.max_depth,
        on_conflict=args.on_conflict,
        workers=MAX_WORKERS,
        dry_run=args.dry_run,
        log_path=Path(args.log) if args.log else None,
    )
    if args.exclude:
        cfg.exclude_dirs = set(args.exclude)
    try:
        plan = plan_renames(root, cfg)
    except ConflictError as e:
        print(f"Conflict: {e}", file=sys.stderr)
        return 2
    if not plan:
        print("No files to rename.")
        return 0
    if cfg.dry_run:
        for s, t in plan:
            print(f"  {s.name} -> {t.name}")
        print(f"\n[dry-run] {len(plan)} file(s) would be renamed.")
        return 0
    if not args.yes:
        for s, t in plan:
            print(f"  {s.name} -> {t.name}")
        try:
            resp = input(f"\nRename {len(plan)} file(s)? [y/N] ")
        except EOFError:
            resp = ""
        if resp.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    results = execute_plan(plan, cfg)
    _print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
