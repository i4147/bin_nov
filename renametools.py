#!/data/data/com.termux/files/usr/bin/env python
"""Renaming and normalisation toolkit.
Subcommands: ``lowercase`` lowercases the names, the extensions or the content, ``images`` appends the ``WxH`` dimensions, ``clean-names`` strips the common prefixes and the junk patterns (release tags, quality markers) from the media files, ``jscss`` normalises the ``.js``/``.css`` names (and the HTML references), ``suggest-names`` proposes names for the ``.py`` files from their docstrings and ``pnr`` does the remove/replace/template renames.
Usage: renametools {lowercase,images,clean-names,jscss,suggest-names,pnr} [options] Positional arguments: paths ...
Files or directories (default: current directory).
Subcommands: lowercase Lowercase names, extensions, or file content.
images Rename image files to include WxH dimensions.
clean-names Strip common prefix/suffix and junk patterns from media files.
jscss Normalize .js/.css filenames and optionally HTML content.
suggest-names Suggest meaningful names for .py files from their docstrings.
pnr Remove / replace / template-number names.
Options: -v, --verbose Enable debug logging.
--target What to lowercase (default: name).
-r, --recursive Recurse into subdirectories (default).
--no-recursive Only operate on top-level entries.
--dry-run Preview only.
--root Root directory to scan (default: current).
--separator Separator between name and dimensions (default: _).
--apply Actually rename (default: dry-run).
--pattern Regex of junk to strip (repeatable; overrides defaults).
--query-style 'any' strips everything after ?
or # (default); 'param' strips only ?key=value patterns.
...
plus 6 more; run ``renametools --help`` for the full list.
Examples: $ renametools images --dry-run ~/photos $ renametools clean-names ~/media --apply Command-line script: run ``python renametools.py`` or the ``renametools`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Iterator, Optional, Sequence

from dh import MAX_WORKERS
from loguru import logger


log = logger


def iter_paths(
    paths: Sequence[Path],
    *,
    recursive: bool = True,
    include_dirs: bool = False,
    suffixes: Optional[set[str]] = None,
) -> Iterator[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for root in paths:
        root = Path(root)
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = list(root.rglob("*") if recursive else root.glob("*"))
        else:
            continue
        for p in candidates:
            if p in seen:
                continue
            if p.is_symlink():
                continue
            if p.is_file():
                if suffixes is not None and p.suffix.lower() not in suffixes:
                    continue
            elif p.is_dir():
                if not include_dirs:
                    continue
            else:
                continue
            seen.add(p)
            out.append(p)
    return iter(sorted(out))


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def safe_rename(src: Path, dst: Path, *, dry_run: bool, verbose: bool = True) -> bool:
    if dst.exists() and dst != src:
        dst = unique_path(dst)
    if dry_run:
        if verbose:
            print(f"[DRY RUN] {src} -> {dst}")
        return True
    try:
        src.rename(dst)
    except OSError as e:
        print(f"✗ Failed to rename {src}: {e}", file=sys.stderr)
        return False
    if verbose:
        print(f"✓ {src.name} -> {dst.name}")
    return True


def _common_prefix(strings: Sequence[str]) -> str:
    if not strings:
        return ""
    common = Path(strings[0])
    for value in strings[1:]:
        path = Path(value)
        while common != path and common not in path.parents:
            if common.parent == common:
                return ""
            common = common.parent
    return str(common)


def _common_suffix(strings: Sequence[str]) -> str:
    return _common_prefix([s[::-1] for s in strings])[::-1]


def _lowercase_name(paths: Sequence[Path], *, recursive: bool, dry_run: bool, verbose: bool) -> int:
    items = list(iter_paths(paths, recursive=recursive, include_dirs=True))
    items.sort(key=lambda p: len(p.parts), reverse=True)
    count = 0
    for p in items:
        lower = p.name.lower()
        if lower == p.name:
            if verbose:
                print(f"Skipping {p.name}: already lowercase.")
            continue
        target = p.with_name(lower)
        if target.exists() and target != p:
            target = unique_path(target)
        if safe_rename(p, target, dry_run=dry_run, verbose=verbose):
            count += 1
    return count


def _lowercase_ext(paths: Sequence[Path], *, recursive: bool, dry_run: bool, verbose: bool) -> int:
    count = 0
    for p in iter_paths(paths, recursive=recursive):
        ext = p.suffix[1:]
        if not ext or ext == ext.lower():
            continue
        new_suffix = "." + ext.lower()
        target = p.with_suffix(new_suffix)
        if safe_rename(p, target, dry_run=dry_run, verbose=verbose):
            count += 1
    return count


def _lowercase_content(paths: Sequence[Path], *, dry_run: bool, verbose: bool) -> int:
    count = 0
    for p in paths:
        p = Path(p)
        if not p.is_file():
            print(f"⚠️  {p} is not a file; skipping.", file=sys.stderr)
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            print(f"✗ {p}: {e}", file=sys.stderr)
            continue
        lowered = text.lower()
        if lowered == text:
            if verbose:
                print(f"Skipping {p}: already lowercase.")
            continue
        if dry_run:
            print(f"[DRY RUN] Would lowercase content: {p}")
        else:
            p.write_text(lowered, encoding="utf-8")
            if verbose:
                print(f"✓ Lowercased content: {p}")
        count += 1
    return count


def cmd_lowercase(args: argparse.Namespace) -> int:
    if args.target == "content":
        n = _lowercase_content(args.paths, dry_run=args.dry_run, verbose=True)
    elif args.target == "ext":
        n = _lowercase_ext(
            args.paths,
            recursive=args.recursive,
            dry_run=args.dry_run,
            verbose=True,
        )
    else:
        n = _lowercase_name(
            args.paths,
            recursive=args.recursive,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    verb = "Would rename" if args.dry_run else "Renamed"
    print(f"\n{verb} {n} item(s).")
    return 0


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".gif", ".webp"}
DIM_IN_NAME = re.compile(r"\d+[xX]\d+")


def _image_dimensions(path: Path) -> Optional[tuple[int, int]]:
    try:
        import cv2
    except ImportError:
        msg = "opencv-python (cv2) is required for the 'images' subcommand."
        raise RuntimeError(msg)
    img = cv2.imread(str(path))
    if img is None:
        return None
    h, w = img.shape[:2]
    return w, h


def _rename_image(args: tuple[Path, str, bool]) -> tuple[Path, bool, str]:
    path, separator, dry_run = args
    if DIM_IN_NAME.search(path.stem):
        return (path, False, "Already has dimensions in name")
    try:
        dims = _image_dimensions(path)
    except RuntimeError as e:
        return (path, False, str(e))
    if dims is None:
        return (path, False, "Failed to read image")
    w, h = dims
    new_name = f"{path.stem}{separator}{w}x{h}{path.suffix}"
    target = path.with_name(new_name)
    if target.exists() and target != path:
        return (path, False, f"Target filename already exists: {new_name}")
    if dry_run:
        return (path, True, f"[DRY RUN] {path.name} -> {new_name}")
    try:
        path.rename(target)
    except OSError as e:
        return (path, False, f"Rename failed: {e}")
    return (target, True, f"{path.name} -> {new_name} ({w}x{h})")


def cmd_images(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    files = list(iter_paths([root], recursive=True, suffixes=IMAGE_SUFFIXES))
    if not files:
        print("[WARN] No images found.")
        return 0
    print(f"[SCAN] Found {len(files)} image file(s) under {root}")
    tasks = [(f, args.separator, args.dry_run) for f in files]
    renamed = skipped = failed = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for path, ok, msg in pool.map(_rename_image, tasks):
            if ok:
                renamed += 1
                print(f"✓ {msg}")
            elif "Already has dimensions" in msg:
                skipped += 1
                print(f"⊘ SKIP {path.name}: {msg}")
            else:
                failed += 1
                print(f"✗ FAIL {path.name}: {msg}", file=sys.stderr)
    print("-" * 40)
    print(f"[SUMMARY] Renamed: {renamed} | Skipped: {skipped} | Failed: {failed} | Total: {len(files)}")
    return 0 if failed == 0 else 1


DEFAULT_JUNK_PATTERNS = (
    r"\bOutcast\b",
    r"\bS\d{2}\b",
    r"\b720p\b",
    r"\b1080p\b",
    r"\bBluRay\b",
    r"\bx264\b",
    "-REWARD_HI",
)
DEFAULT_MEDIA_SUFFIXES = {".srt", ".mkv", ".mp4", ".avi"}


def _strip_junk(name: str, patterns: Sequence[re.Pattern[str]]) -> str:
    for pat in patterns:
        name = pat.sub("", name)
    return re.sub(r"\.+", ".", name).strip(". ")


def cmd_clean_names(args: argparse.Namespace) -> int:
    patterns = [re.compile(p, re.IGNORECASE) for p in args.pattern]
    files = list(iter_paths(args.paths, recursive=args.recursive, suffixes=DEFAULT_MEDIA_SUFFIXES))
    if not files:
        print("No matching files found.")
        return 1
    names = [f.name for f in files]
    prefix = _common_prefix(names)
    suffix = _common_suffix(names)
    if args.apply:
        print("Preview & APPLY:")
    else:
        print("Preview (dry-run; use --apply to rename):")
    changed = 0
    for f in files:
        original = f.name
        middle = original[len(prefix) : len(original) - len(suffix)] if suffix else original[len(prefix) :]
        middle = _strip_junk(middle, patterns)
        new_name = f"{f.stem.split('.')[0]}.{middle}{f.suffix}"
        new_name = re.sub(r"\.+", ".", new_name)
        if new_name == original:
            continue
        print(f"OLD: {original}   ->   NEW: {new_name}")
        changed += 1
        if args.apply:
            target = f.with_name(new_name)
            if target.exists():
                print(f"  SKIPPED (exists): {new_name}")
                continue
            safe_rename(f, target, dry_run=False, verbose=False)
    if not args.apply:
        print("\nDry-run only. Use --apply to rename.")
    else:
        print(f"\nRenamed up to {changed} file(s).")
    return 0


_QUERY_ANY_RE = re.compile(r"(\.(?:js|css))([?#].*)?$", re.IGNORECASE)
_QUERY_PARAM_RE = re.compile(r"\?[a-zA-Z0-9_-]+=[^\"'\s>]+", re.IGNORECASE)
_JS_CSS_REF_RE = re.compile(r"\b([^\s<>\"']*?\.(?:js|css))([?#][^\s<>\"']*)?\b", re.IGNORECASE)


def _strip_query(name: str, style: str) -> str:
    if style == "param":
        return _QUERY_PARAM_RE.sub("", name)
    return _QUERY_ANY_RE.sub(r"\1", name)


def _strip_query_in_html(text: str) -> str:
    return _JS_CSS_REF_RE.sub(lambda m: m.group(1), text)


def cmd_jscss(args: argparse.Namespace) -> int:
    renamed = 0
    for path in iter_paths(args.paths, recursive=True):
        if path.suffix.lower() in {".js", ".css"}:
            new_name = _strip_query(path.name, args.query_style)
            if new_name != path.name:
                target = path.with_name(new_name)
                if target.exists():
                    target = unique_path(target)
                if safe_rename(path, target, dry_run=args.dry_run):
                    renamed += 1
        if args.also_html and path.suffix.lower() in {".html", ".htm", ".js"}:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            new_text = _strip_query_in_html(text)
            if new_text == text:
                continue
            if args.dry_run:
                print(f"[DRY RUN] Would update HTML: {path}")
            else:
                path.write_text(new_text, encoding="utf-8")
                print(f"✓ Updated HTML: {path}")
    print(f"\nProcessed {renamed} filename(s).")
    return 0


_MEANINGLESS_STEMS = {"main", "run", "test", "script", "app"}
_STOP_WORDS = {
    "this",
    "that",
    "from",
    "with",
    "for",
    "the",
    "and",
    "or",
    "are",
    "is",
}
_EPILOG_RE = re.compile(r"epilog\s*=\s*[\\'\"]([^\\'\"]+)[\\'\"]", re.IGNORECASE)


@dataclass
class NameSuggestion:
    path: Path
    current_name: str
    has_meaning: bool = False
    suggestion: Optional[str] = None
    error: Optional[str] = None
    renamed: bool = False


class SourceAnalyzer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tree: Optional[ast.Module] = None
        self.content = ""
        try:
            self.content = path.read_text(encoding="utf-8", errors="ignore")
            self.tree = ast.parse(self.content)
        except SyntaxError:
            self.tree = None
        except OSError as e:
            msg = f"Failed to read {path}: {e}"
            raise RuntimeError(msg) from e

    def module_docstring(self) -> Optional[str]:
        return ast.get_docstring(self.tree) if self.tree else None

    def argparse_epilog(self) -> Optional[str]:
        m = _EPILOG_RE.search(self.content)
        return m.group(1) if m else None

    def main_docstring(self) -> Optional[str]:
        if not self.tree:
            return None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                return ast.get_docstring(node)
        return None

    def purpose(self) -> Optional[str]:
        return self.module_docstring() or self.argparse_epilog() or self.main_docstring()

    def is_meaningful_name(self) -> bool:
        stem = self.path.stem
        if len(stem) < 3 or stem in _MEANINGLESS_STEMS:
            return False
        return not re.match(r"^[a-z0-9]{1,2}$", stem)

    def suggest_name(self) -> Optional[str]:
        purpose = self.purpose()
        if not purpose:
            return None
        words = re.findall(r"\b[a-z][a-z0-9]*\b", purpose.lower())
        if not words:
            return None
        filtered = [w for w in words if len(w) > 3 and w not in _STOP_WORDS]
        if filtered:
            return "_".join(filtered[:3])
        return "_".join(words[:2]) if len(words) >= 2 else None


def _analyze(path: Path) -> NameSuggestion:
    s = NameSuggestion(path=path, current_name=path.stem)
    try:
        analyzer = SourceAnalyzer(path)
        s.has_meaning = analyzer.is_meaningful_name()
        if not s.has_meaning:
            s.suggestion = analyzer.suggest_name()
    except RuntimeError as e:
        s.error = str(e)
    return s


def cmd_suggest_names(args: argparse.Namespace) -> int:
    py_files = list(iter_paths(args.paths, recursive=True, suffixes={".py"}))
    if not py_files:
        print("No Python files found.")
        return 1
    cwd = Path.cwd()
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_analyze, py_files))
    if args.apply:
        for s in results:
            if s.has_meaning or not s.suggestion:
                continue
            target = s.path.parent / f"{s.suggestion}.py"
            if target == s.path:
                continue
            if target.exists():
                s.error = f"Target already exists: {target.name}"
                continue
            try:
                s.path.rename(target)
                s.renamed = True
            except OSError as e:
                s.error = f"Rename failed: {e}"
    meaningful = sum(1 for s in results if s.has_meaning)
    unnamed = sum(1 for s in results if not s.has_meaning)
    errors = sum(1 for s in results if s.error)
    renames = sum(1 for s in results if s.renamed)
    print("=" * 78)
    print(f"  Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"  Total files: {len(results)} | Meaningful: {meaningful} | Unnamed: {unnamed}")
    print(f"  Errors: {errors} | Renamed: {renames}")
    print("=" * 78)
    if unnamed:
        print("\nUNNAMED FILES:")
        for s in results:
            if s.has_meaning:
                continue
            try:
                rel = s.path.relative_to(cwd)
            except ValueError:
                rel = s.path
            print(f"  📄 {rel}")
            print(f"     Current: {s.current_name}")
            print(f"     Suggest: {s.suggestion or '(none)'}")
            if s.error:
                print(f"     Error:   {s.error}")
            elif s.renamed:
                print(f"     ✓ Renamed to: {s.suggestion}")
    if errors:
        print("\nFILES WITH ERRORS:")
        for s in results:
            if s.error:
                print(f"  ❌ {s.path}: {s.error}")
    return 0


SKIP_PARTS = {".git"}


def _is_skippable(p: Path) -> bool:
    if p.is_symlink():
        return True
    return any(part in SKIP_PARTS for part in p.parts)


def _pnr_remove(root: Path, needle: str, *, dry_run: bool, recursive: bool) -> int:
    count = 0
    try:
        entries = list(root.iterdir())
    except PermissionError:
        print(f"Permission denied: {root}", file=sys.stderr)
        return 0
    for entry in entries:
        if _is_skippable(entry):
            continue
        if needle in entry.name:
            new_name = entry.name.replace(needle, "")
            if not new_name.strip():
                print(f"Warning: removing '{needle}' makes '{entry.name}' empty")
            else:
                target = root / new_name
                if target.exists():
                    target = unique_path(target)
                if safe_rename(entry, target, dry_run=dry_run):
                    count += 1
        if recursive and entry.is_dir():
            count += _pnr_remove(entry, needle, dry_run=dry_run, recursive=True)
    return count


def _pnr_replace(root: Path, old: str, new: str, *, dry_run: bool, recursive: bool) -> int:
    count = 0
    try:
        entries = list(root.iterdir())
    except PermissionError:
        print(f"Permission denied: {root}", file=sys.stderr)
        return 0
    for entry in entries:
        if _is_skippable(entry):
            continue
        if old in entry.name:
            new_name = entry.name.replace(old, new)
            if not new_name.strip():
                print(f"Warning: replacement makes '{entry.name}' empty")
            else:
                target = root / new_name
                if target.exists():
                    target = unique_path(target)
                if safe_rename(entry, target, dry_run=dry_run):
                    count += 1
        if recursive and entry.is_dir():
            count += _pnr_replace(entry, old, new, dry_run=dry_run, recursive=True)
    return count


def _pnr_template(root: Path, prefix: str, *, dry_run: bool, recursive: bool) -> int:
    count = 0
    try:
        files = [p for p in root.iterdir() if p.is_file() and not _is_skippable(p) and p.name != Path(__file__).name]
    except PermissionError:
        print(f"Permission denied: {root}", file=sys.stderr)
        return 0
    if files:
        n = len(files)
        width = 1 if n < 10 else 2 if n < 100 else 3 if n < 1000 else 4
        for i, f in enumerate(sorted(files), 1):
            new_name = f"{prefix}{str(i).zfill(width)}{f.suffix}"
            if new_name == f.name:
                continue
            target = root / new_name
            if target.exists():
                target = unique_path(target)
            if safe_rename(f, target, dry_run=dry_run):
                count += 1
    if recursive:
        try:
            subdirs = [d for d in root.iterdir() if d.is_dir() and not _is_skippable(d)]
        except PermissionError:
            return count
        for d in subdirs:
            count += _pnr_template(d, prefix, dry_run=dry_run, recursive=True)
    return count


def cmd_pnr(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    if args.dry_run:
        print("DRY RUN MODE — no changes will be made.\n")
    if args.remove is not None:
        n = _pnr_remove(root, args.remove, dry_run=args.dry_run, recursive=args.recursive)
    elif args.replace is not None:
        old, new = args.replace
        n = _pnr_replace(root, old, new, dry_run=args.dry_run, recursive=args.recursive)
    else:
        n = _pnr_template(root, args.template, dry_run=args.dry_run, recursive=args.recursive)
    print(f"\nOperation complete. {n} item(s) affected.")
    return 0


def _add_paths(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path.cwd()],
        help="Files or directories (default: current directory).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renametools",
        description=("Unified renamer / normalizer: lowercase, images, clean-names, jscss, suggest-names, pnr."),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)
    lc = sub.add_parser(
        "lowercase",
        help="Lowercase names, extensions, or file content.",
    )
    _add_paths(lc)
    lc.add_argument(
        "--target",
        choices=["name", "ext", "content"],
        default="name",
        help="What to lowercase (default: name).",
    )
    lc.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        default=True,
        help="Recurse into subdirectories (default).",
    )
    lc.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="Only operate on top-level entries.",
    )
    lc.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only.",
    )
    im = sub.add_parser(
        "images",
        help="Rename image files to include WxH dimensions.",
    )
    _add_paths(im)
    im.add_argument(
        "--root",
        type=Path,
        default=Path(),
        help="Root directory to scan (default: current).",
    )
    im.add_argument(
        "--separator",
        default="_",
        help="Separator between name and dimensions (default: _).",
    )
    im.add_argument("--dry-run", action="store_true", help="Preview only.")
    cn = sub.add_parser(
        "clean-names",
        help="Strip common prefix/suffix and junk patterns from media files.",
    )
    _add_paths(cn)
    cn.add_argument("-r", "--recursive", action="store_true", help="Recurse into subdirectories.")
    cn.add_argument("--apply", action="store_true", help="Actually rename (default: dry-run).")
    cn.add_argument(
        "--pattern",
        action="append",
        default=list(DEFAULT_JUNK_PATTERNS),
        help="Regex of junk to strip (repeatable; overrides defaults).",
    )
    js = sub.add_parser(
        "jscss",
        help="Normalize .js/.css filenames and optionally HTML content.",
    )
    _add_paths(js)
    js.add_argument(
        "--query-style",
        choices=["any", "param"],
        default="any",
        help=("'any' strips everything after ? or # (default); 'param' strips only ?key=value patterns."),
    )
    js.add_argument("--also-html", action="store_true", help="Also rewrite .html/.js content.")
    js.add_argument("--dry-run", action="store_true", help="Preview only.")
    sn = sub.add_parser(
        "suggest-names",
        help="Suggest meaningful names for .py files from their docstrings.",
    )
    _add_paths(sn)
    sn.add_argument("-a", "--apply", action="store_true", help="Rename files in place.")
    pnr = sub.add_parser("pnr", help="Remove / replace / template-number names.")
    group = pnr.add_mutually_exclusive_group(required=True)
    group.add_argument("-r", "--remove", metavar="STR", help="Remove STR from names.")
    group.add_argument(
        "-s",
        "--replace",
        nargs=2,
        metavar=("A", "B"),
        help="Replace A with B in names.",
    )
    group.add_argument(
        "-t",
        "--template",
        metavar="NAME",
        help="Rename files to NAME + sequential number.",
    )
    pnr.add_argument("--dry-run", action="store_true", help="Preview only.")
    pnr.add_argument("--recursive", action="store_true", help="Recurse into subdirectories.")
    pnr.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Directory to operate on (default: cwd).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "lowercase": cmd_lowercase,
        "images": cmd_images,
        "clean-names": cmd_clean_names,
        "jscss": cmd_jscss,
        "suggest-names": cmd_suggest_names,
        "pnr": cmd_pnr,
    }
    try:
        return dispatch[args.command](args)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.", file=sys.stderr)
        return 130
    except Exception as e:
        log.exception("Unexpected error: {}", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
