#!/data/data/com.termux/files/usr/bin/env python
"""Unified shebang inspection and editing tool for source files."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import contextlib
import os
from pathlib import Path
import re
import sys
from typing import Optional, Sequence

from dh import MAX_WORKERS


TERMUX_PREFIX = "/data/data/com.termux/files/usr"
DEFAULT_SHEBANG_FIX = "#!/data/data/com.termux/usr"
DEFAULT_SHEBANG_ADD = f"#!{TERMUX_PREFIX}/bin/env python"
DEFAULT_SHEBANG_SH = f"#!{TERMUX_PREFIX}/bin/bash"
_TERMUX_PY_SHEBANGS = {
    f"#!{TERMUX_PREFIX}/bin/python",
    f"#!{TERMUX_PREFIX}/bin/python3.14",
    f"#!{TERMUX_PREFIX}/bin/python3.13",
    f"#!{TERMUX_PREFIX}/bin/python3.14",
    f"#!{TERMUX_PREFIX}/bin/python3",
    f"#!{TERMUX_PREFIX}/bin/env python",
    f"#!{TERMUX_PREFIX}/bin/env python3",
}
_SHEBANG_TO_EXT = {
    "python": ".py",
    "python3": ".py",
    "python2": ".py",
    "bash": ".sh",
    "sh": ".sh",
    "zsh": ".sh",
    "ksh": ".sh",
    "dash": ".sh",
}
_RENAME_PATTERNS = [
    (r"#!/data/data/com.termux/files/usr/bin/python3?", ".py"),
    (r"#!/data/data/com.termux/files/usr/bin/python3.14?", ".py"),
    (r"#!/data/data/com.termux/files/usr/bin/python3.14?", ".py"),
    (r"#!/data/data/com.termux/files/usr/bin/env python3?", ".py"),
    (r"#!/usr/bin/env python", ".py"),
    (r"#!/usr/bin/python", ".py"),
    (r"#!/data/data/com.termux/files/usr/bin/env sh", ".sh"),
    (r"#!/data/data/com.termux/files/usr/bin/bash", ".sh"),
    (r"#!/data/data/com.termux/files/usr/bin/sh", ".sh"),
    (r"#!/data/data/com.termux/files/usr/bin/env bash", ".sh"),
    (r"#!/usr/bin/env bash", ".sh"),
    (r"#!/bin/bash", ".sh"),
    (r"#!/bin/sh", ".sh"),
    (r"#!/data/data/com.termux/files/usr/bin/node", ".js"),
    (r"#!/data/data/com.termux/files/usr/bin/env node", ".js"),
    (r"#!/usr/bin/env node", ".js"),
    (r"#!/usr/bin/node", ".js"),
    (r"#!/data/data/com.termux/files/usr/bin/ruby", ".rb"),
    (r"#!/data/data/com.termux/files/usr/bin/env ruby", ".rb"),
    (r"#!/usr/bin/env ruby", ".rb"),
    (r"#!/usr/bin/ruby", ".rb"),
    (r"#!/data/data/com.termux/files/usr/bin/perl", ".pl"),
    (r"#!/data/data/com.termux/files/usr/bin/env perl", ".pl"),
    (r"#!/usr/bin/env perl", ".pl"),
    (r"#!/usr/bin/perl", ".pl"),
    (r"#!/data/data/com.termux/files/usr/bin/lua", ".lua"),
    (r"#!/data/data/com.termux/files/usr/bin/env lua", ".lua"),
    (r"#!/usr/bin/env lua", ".lua"),
    (r"#!/usr/bin/lua", ".lua"),
    (r"#!/data/data/com.termux/files/usr/bin/php", ".php"),
    (r"#!/data/data/com.termux/files/usr/bin/env php", ".php"),
    (r"#!/usr/bin/env php", ".php"),
    (r"#!/usr/bin/php", ".php"),
    (r"#!/data/data/com.termux/files/usr/bin/Rscript", ".r"),
    (r"#!/usr/bin/env Rscript", ".r"),
    (r"#!/usr/bin/Rscript", ".r"),
    (r"#!/data/data/com.termux/files/usr/bin/fish", ".fish"),
    (r"#!/data/data/com.termux/files/usr/bin/env fish", ".fish"),
    (r"#!/usr/bin/env fish", ".fish"),
    (r"#!/usr/bin/fish", ".fish"),
    (r"#!/usr/bin/awk", ".awk"),
    (r"#!/usr/bin/env awk", ".awk"),
    (r"#!/usr/bin/sed", ".sed"),
]
_PY_SKIP_EXT = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\.(md|txt|rst|json|yaml|yml|toml|ini|cfg|conf|log|lock|gitignore|dockerignore)$",
        r"\.(css|html|js|ts|jsx|tsx|vue|svelte)$",
        r"\.(jpg|jpeg|png|gif|svg|ico|webp)$",
        r"\.(mp4|mp3|avi|mkv|mov)$",
        r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx)$",
        r"\.(zip|tar|gz|rar|7z)$",
        r"\.(so|dll|dylib|exe|o|a|lib)$",
        r"\.(pyc|pyo|pyd)$",
    )
]
_PY_SPECIAL_STEMS = {
    "setup",
    "manage",
    "app",
    "wsgi",
    "asgi",
    "test",
    "conftest",
    "requirements",
    "main",
    "cli",
    "run",
}
_PY_INDICATOR = [
    re.compile(p, re.MULTILINE)
    for p in (
        r"^(from|import)\s+",
        r"^def\s+\w+\s*\(",
        r"^class\s+\w+[:\(]",
        r"^if\s+__name__\s*==\s*['\"]__main__['\"]",
        r"^#!.*python",
    )
]


def _gather(paths: Sequence[str], exts: Optional[Sequence[str]] = None) -> list[Path]:
    ext_set = None
    if exts:
        ext_set = {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            found.append(p)
        elif p.is_dir():
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                if ext_set is None or f.suffix.lower() in ext_set:
                    found.append(f)
        else:
            print(f"Not found: {p}", file=sys.stderr)
    return sorted(set(found))


def _count_shebang_lines(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("#!"))


def _read_first_line(path: Path) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.readline().strip()
    except OSError:
        return None


def _looks_like_python(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return False
    if head.startswith(b"#!"):
        first = head.split(b"\n", 1)[0]
        if b"python" in first.lower():
            return True
    text = head.decode("utf-8", errors="ignore")
    return any(p.search(text) for p in _PY_INDICATOR)


def _iter_python_files(root: Path):
    for path in root.rglob("*"):
        if any(part.startswith(".") and part != "." for part in path.parts) and ".git" in path.parts:
            continue
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        if path.suffix == ".py":
            yield path
            continue
        if any(p.search(str(path)) for p in _PY_SKIP_EXT):
            continue
        if path.stem in _PY_SPECIAL_STEMS:
            if _looks_like_python(path):
                yield path
            continue
        if "." not in path.name and _looks_like_python(path):
            yield path


def _unique_target(src: Path, new_ext: str) -> Optional[Path]:
    parent = src.parent
    stem = src.stem if src.suffix else src.name
    candidate = parent / f"{stem}{new_ext}"
    i = 1
    while candidate.exists() and candidate != src:
        candidate = parent / f"{stem}_{i}{new_ext}"
        i += 1
    if candidate == src:
        return None
    return candidate


def _do_rename(src: Path, new_ext: str, verbose: bool) -> Optional[Path]:
    target = _unique_target(src, new_ext)
    if target is None:
        return None
    try:
        os.rename(src, target)
    except OSError as e:
        print(f"Error renaming {src} to {target}:{e}")
        return None
    if verbose:
        print(f"  RENAMED:{src.name}->{target.name}")
    return target


def _detect_ext_simple(path: Path) -> Optional[str]:
    first = _read_first_line(path)
    if first is None:
        return None
    if not first.startswith("#!"):
        return None
    cmd = first[2:].strip()
    if "/env " in cmd:
        cmd = cmd.split("/env ")[-1]
    else:
        cmd = Path(cmd).name
    lower = cmd.lower()
    for name, ext in _SHEBANG_TO_EXT.items():
        if name in lower:
            return ext
    return None


def _match_shebang_broad(line: str) -> Optional[str]:
    for pattern, ext in _RENAME_PATTERNS:
        if re.match(pattern, line):
            return ext
    return None


def _worker_fix_sh(path_str: str, shebang: str) -> str:
    path = Path(path_str)
    try:
        with path.open("r+", encoding="utf-8") as f:
            lines = f.readlines()
            if not lines:
                return f"Skipped: {path.name}"
            if lines[0].startswith("#!"):
                lines[0] = shebang + "\n"
                if len(lines) > 1 and lines[1].strip() != "":
                    lines.insert(1, "\n")
            else:
                lines.insert(0, shebang + "\n")
                if len(lines) > 1 and lines[1].strip() != "":
                    lines.insert(1, "\n")
            f.seek(0)
            f.writelines(lines)
            f.truncate()
    except OSError as e:
        return f"Error: {path.name}:{e}"
    if "bin" in path.parts:
        with contextlib.suppress(OSError):
            path.chmod(0o755)
    return f"{path.name} updated"


def _worker_fix_python(path_str: str, shebang: str, cv2_shebang: str) -> tuple[str, bool, Optional[str], str]:
    path = Path(path_str)
    if path.is_symlink():
        return (path_str, False, "Symlink skipped", "skipped")
    try:
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(?:import\s+cv2\b|from\s+cv2\b)", text, re.MULTILINE):
            target = cv2_shebang
        else:
            target = shebang
        has_shebang = text.startswith("#!")
        if not has_shebang:
            path.write_text(f"{target}\n{text}", encoding="utf-8")
            return (path_str, True, None, "added")
        first = text.split("\n", 1)[0]
        if "python" not in first.lower():
            return (path_str, False, "Not a Python shebang", "skipped")
        if first.strip() == target:
            return (path_str, False, "Already correct", "unchanged")
        lines = text.split("\n")
        lines[0] = target
        path.write_text("\n".join(lines), encoding="utf-8")
        return (path_str, True, None, "updated")
    except Exception as e:
        return (path_str, False, str(e), "error")


def _worker_rm(path_str: str) -> str:
    path = Path(path_str)
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines or not lines[0].startswith("#!/"):
            return ""
        path.write_text("\n".join(lines[1:]), encoding="utf-8")
        return f"{path.name} updated."
    except Exception:
        return ""


def _should_add_python(path: Path) -> bool:
    try:
        if path.stat().st_size == 0 or path.name == "__init__.py":
            return False
    except OSError:
        return False
    if path.suffix == ".py":
        return True
    try:
        with path.open(encoding="utf-8") as f:
            first = f.readline().strip()
            if first.startswith("#!") and "python" in first:
                return True
            f.seek(0)
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#"):
                    return line.startswith(("import ", "from ", "class ", "def "))
    except (OSError, UnicodeDecodeError):
        return False
    return False


def _worker_add_python(path_str: str, shebang: str) -> str:
    path = Path(path_str)
    if path.is_symlink():
        return ""
    if not _should_add_python(path):
        return ""
    try:
        with path.open("r+", encoding="utf-8") as f:
            lines = f.readlines()
            if not lines:
                return ""
            if lines[0].startswith("#!"):
                lines[0] = shebang + "\n"
                if len(lines) > 1 and lines[1].strip():
                    lines.insert(1, "\n")
            else:
                has_code = any(l.strip().startswith(("import ", "from ", "def ", "class ")) for l in lines)
                if has_code:
                    lines.insert(0, shebang + "\n")
                    lines.insert(1, "\n")
            f.seek(0)
            f.writelines(lines)
            f.truncate()
    except OSError:
        return ""
    if "bin" in path.parts:
        with contextlib.suppress(OSError):
            path.chmod(0o755)
    return f"{path.name} updated."


def _convert_to_cloud(path_str: str) -> tuple[str, bool, Optional[str]]:
    path = Path(path_str)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return (path_str, False, str(e))
    if not text.startswith("#!"):
        return (path_str, False, "No shebang found")
    lines = text.split("\n")
    first = lines[0]
    if first not in _TERMUX_PY_SHEBANGS:
        return (path_str, False, "Not a Termux shebang")
    new_line = "#!/usr/bin/env python3" if "python3" in first else "#!/usr/bin/env python"
    lines[0] = new_line
    path.write_text("\n".join(lines), encoding="utf-8")
    return (path_str, True, None)


def _cmd_check_double(args: argparse.Namespace) -> int:
    paths = args.paths or [os.getcwd()]
    files = _gather(paths, [".py"])
    for f in files:
        if f.is_symlink():
            continue
        if _count_shebang_lines(f) > 1:
            print(f.name)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    paths = args.paths or [os.getcwd()]
    files = _gather(paths, [".py"])
    count = 0
    for f in files:
        if _count_shebang_lines(f) > 1:
            count += 1
            print(f"{f} has 2 shebang")
    print(f"Done. Updated {count} files.")
    return 0


def _cmd_fix_sh(args: argparse.Namespace) -> int:
    paths = args.paths or [os.getcwd()]
    files = _gather(paths, [".sh"])
    if not files:
        print("No .sh files found.")
        return 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for msg in ex.map(_worker_fix_sh, [str(f) for f in files], [args.shebang] * len(files)):
            if msg:
                print(msg)
    return 0


def _cmd_fix_ext(args: argparse.Namespace) -> int:
    paths = args.paths or [os.getcwd()]
    files = [f for f in _gather(paths) if f.is_file()]
    renamed = 0
    skipped = 0
    for f in files:
        ext = _detect_ext_simple(f)
        if ext is None:
            if args.verbose:
                print(f"  SKIP:{f.name} (no recognized shebang)")
            continue
        if f.suffix.lower() == ext:
            if args.verbose:
                print(f"  SKIP:{f.name} (already has correct extension)")
            skipped += 1
            continue
        if args.dry_run:
            target_name = (f.stem or f.name) + ext
            print(f"  WOULD RENAME:{f.name}->{target_name}")
            renamed += 1
        else:
            result = _do_rename(f, ext, verbose=True)
            if result is not None:
                renamed += 1
            else:
                skipped += 1
    print("\nSummary:")
    print(f"  {'Would rename' if args.dry_run else 'Renamed'}:{renamed} files")
    print(f"  Skipped:{skipped} files")
    return 0


def _cmd_fix(args: argparse.Namespace) -> int:
    root = Path(args.paths[0]) if args.paths else Path.cwd()
    if not root.is_dir():
        print(f"Error:{root} is not a valid directory", file=sys.stderr)
        return 1
    files = list(_iter_python_files(root))
    if not files:
        print("No Python files found.")
        return 0
    print(f"Found {len(files)} Python files to check.")
    updated: list[str] = []
    added: list[str] = []
    skipped_links = 0
    skipped_non_py = 0
    unchanged = 0
    errors: list[tuple[str, str]] = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(_worker_fix_python, str(f), args.shebang, args.cv2_shebang) for f in files]
        for fut in futures:
            path_str, changed, reason, status = fut.result()
            if reason:
                if "Symlink" in reason:
                    skipped_links += 1
                elif "Not a Python shebang" in reason:
                    skipped_non_py += 1
                elif "Already correct" in reason:
                    unchanged += 1
                else:
                    errors.append((path_str, reason))
            elif changed and status == "updated":
                updated.append(path_str)
            elif changed and status == "added":
                added.append(path_str)
            else:
                skipped_links += 1
    if updated:
        print(f"\nUpdated existing shebangs in {len(updated)} files:")
        for p in updated:
            print(f"  {p}")
    if added:
        print(f"\nAdded new shebang to {len(added)} files:")
        for p in added:
            print(f"  {p}")
    if not updated and not added:
        print("\nNo files needed updating.")
    print("\n" + "=" * 40)
    print("Summary:")
    print(f"  Updated existing shebangs:{len(updated)} files")
    print(f"  Added new shebangs:{len(added)} files")
    print(f"  Skipped (symlinks):{skipped_links} files")
    print(f"  Not Python shebang:{skipped_non_py} files")
    print(f"  Already correct:{unchanged} files")
    print(f"  Total processed:{len(files)} files")
    if errors:
        print(f"  Errors:{len(errors)} files")
        for p, reason in errors:
            print(f"-{p}:{reason}")
        return 1
    return 0


def _cmd_rename(args: argparse.Namespace) -> int:
    root = Path(args.paths[0]) if args.paths else Path.cwd()
    files = [f for f in root.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        print("No files found in directory.")
        return 0
    renamed = 0
    already_correct = 0
    unknown = 0
    for f in files:
        first = _read_first_line(f)
        if first is None or not first.startswith("#!"):
            continue
        ext = _match_shebang_broad(first)
        if ext is None:
            display = first if len(first) <= 50 else first[:50] + "..."
            print(f"Unknown shebang in:{f.name}")
            print(f"   Shebang:{display}")
            unknown += 1
            continue
        if f.suffix == ext:
            already_correct += 1
            continue
        if args.dry_run:
            print(f"  Would rename:{f.name}->{f.stem}{ext}")
            renamed += 1
        elif _do_rename(f, ext, verbose=True) is not None:
            renamed += 1
    print(f"\n{'=' * 40}")
    print("Summary:")
    print(f"   Renamed:{renamed} file(s)")
    print(f"   Skipped (already correct):{already_correct} file(s)")
    if unknown:
        print(f"   Unknown shebangs:{unknown} file(s)")
    print(f"{'=' * 40}")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    paths = args.paths or [os.getcwd()]
    files = _gather(paths, [".py"])
    if not files:
        print("No .py files found.")
        return 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for msg in ex.map(_worker_rm, [str(f) for f in files]):
            if msg:
                print(msg)
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    root = Path(args.paths[0]) if args.paths else Path.cwd()
    files = [f for f in root.rglob("*") if f.is_file() and not f.is_symlink()]
    if not files:
        print("No files found.")
        return 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for msg in ex.map(_worker_add_python, [str(f) for f in files], [args.shebang] * len(files)):
            if msg:
                print(msg)
    return 0


def _cmd_to_cloud(args: argparse.Namespace) -> int:
    root = Path(args.paths[0]) if args.paths else Path()
    if not root.exists() or not root.is_dir():
        print(f"Error:{root} is not a valid directory", file=sys.stderr)
        return 1
    print(f"Searching for Python files in:{root.absolute()}")
    files = list(root.rglob("*.py"))
    if not files:
        print("No Python files found.")
        return 0
    print(f"Found {len(files)} Python files")
    print("Converting Termux shebangs...")
    print("-" * 40)
    successes = 0
    skipped = 0
    failed = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for path_str, ok, reason in ex.map(_convert_to_cloud, [str(f) for f in files]):
            if ok:
                successes += 1
                print(f"  {path_str}")
            elif reason in ("No shebang found", "Not a Termux shebang"):
                skipped += 1
                print(f"  {path_str} - {reason}")
            else:
                failed += 1
                print(f"  {path_str} - {reason}")
    print("-" * 40)
    print("\nSummary:")
    print(f"  Successfully converted:{successes}")
    print(f"  Skipped:{skipped}")
    print(f"  Failed:{failed}")
    print(f"  Total files processed:{len(files)}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shebang_tool",
        description="Unified shebang inspection and editing tool.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("check-double", help="print files with >1 shebang")
    sp.add_argument("paths", nargs="*")
    sp.set_defaults(func=_cmd_check_double)
    sp = sub.add_parser("check", help="count and report files with >1 shebang")
    sp.add_argument("paths", nargs="*")
    sp.set_defaults(func=_cmd_check)
    sp = sub.add_parser("fix-sh", help="rewrite .sh shebangs")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--shebang", default=DEFAULT_SHEBANG_SH)
    sp.set_defaults(func=_cmd_fix_sh)
    sp = sub.add_parser("fix-ext", help="rename files to match shebang extension")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=_cmd_fix_ext)
    sp = sub.add_parser("fix", help="add/update Termux python shebangs")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--shebang", default=DEFAULT_SHEBANG_FIX)
    sp.add_argument("--cv2-shebang", default=DEFAULT_SHEBANG_FIX)
    sp.set_defaults(func=_cmd_fix)
    sp = sub.add_parser("rename", help="rename by broad shebang map")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=_cmd_rename)
    sp = sub.add_parser("rm", help="remove shebang from .py files")
    sp.add_argument("paths", nargs="*")
    sp.set_defaults(func=_cmd_rm)
    sp = sub.add_parser("add", help="add Termux python shebang")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--shebang", default=DEFAULT_SHEBANG_ADD)
    sp.set_defaults(func=_cmd_add)
    sp = sub.add_parser("to-cloud", help="convert Termux shebang -> env")
    sp.add_argument("paths", nargs="*")
    sp.set_defaults(func=_cmd_to_cloud)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
