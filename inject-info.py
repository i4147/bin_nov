#!/data/data/com.termux/files/usr/bin/env python
"""Inject personal metadata from ``~/.myinfo`` into Python project files.

Two engines are bundled:

* ``structured`` — edits ``pyproject.toml``/``setup.cfg``/``setup.py`` in a
  single project using format-aware parsers (``tomlkit``/``ast``).
* ``scan`` — walks a directory tree and rewrites ``setup.py``, ``setup.cfg``,
  ``pyproject.toml``, ``MANIFEST.in``, ``tox.ini`` and ``Pipfile`` with
  regular-expression based transformations.
"""

from __future__ import annotations
import argparse
import ast
from collections.abc import Callable, Iterable, Iterator, Sequence
import difflib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, NamedTuple, Sequence, TypedDict

from dh import SKIP_DIRS, atomic_write


try:
    import tomlkit
except ImportError:  # pragma: no cover
    sys.exit("This script needs 'tomlkit'. Install with: pip install tomlkit")

__version__: str = "1.0.0"

INFO_PATH = Path.home() / ".myinfo"
DEFAULT_PYPROJECT: Path = Path("pyproject.toml")
DEFAULT_SETUPCFG: Path = Path("setup.cfg")
DEFAULT_SETUPPY: Path = Path("setup.py")

REQUIRED_KEYS: tuple[str, ...] = ("name", "email", "github_username")
TARGET_FILENAMES: frozenset[str] = frozenset({
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "MANIFEST.in",
    "tox.ini",
    "Pipfile",
})
GITHUB_USER_RE = re.compile(r"[A-Za-z0-9_.-]+")
_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_\-]+)\s*=")

Edit = tuple[int, int, str]
Transform = tuple[re.Pattern[str], Callable[[re.Match[str]], str]]


class Info(TypedDict):
    """Validated contents of ``~/.myinfo``."""

    name: str
    email: str
    github_username: str


class Plan(NamedTuple):
    """A pending change for one file."""

    original: str
    new: str


# ---------------------------------------------------------------------------
# Shared: info loading
# ---------------------------------------------------------------------------


def load_info(path: Path) -> Info:
    """Read and validate the ``~/.myinfo`` JSON file."""
    if not path.is_file():
        sys.exit(f"Missing info file: {path}")
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"Invalid JSON in {path}: {exc}")
    if not isinstance(raw, dict):
        sys.exit(f"{path} must contain a JSON object")
    missing = [key for key in REQUIRED_KEYS if not raw.get(key)]
    if missing:
        sys.exit(f"Missing/empty keys in {path}: {missing}")
    info: Info = {
        "name": str(raw["name"]),
        "email": str(raw["email"]),
        "github_username": str(raw["github_username"]),
    }
    if not GITHUB_USER_RE.fullmatch(info["github_username"]):
        sys.exit(f"Invalid github_username: {info['github_username']!r}")
    return info


# ---------------------------------------------------------------------------
# Shared: emit / preview
# ---------------------------------------------------------------------------


def _emit(
    path: Path,
    plan: Plan,
    *,
    dry_run: bool,
    show_diff: bool,
    check: bool,
    quiet: bool,
) -> bool:
    """Write ``plan`` or preview it. Returns ``True`` when a change is pending."""
    if plan.new == plan.original:
        return False
    if show_diff:
        sys.stdout.writelines(
            difflib.unified_diff(
                plan.original.splitlines(keepends=True),
                plan.new.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"{path} (new)",
            )
        )
        print()
    if dry_run or check:
        if not quiet and not show_diff:
            print(f"would update: {path}")
        return True
    atomic_write(path, plan.new, encoding="utf-8")
    if not quiet:
        print(f"updated: {path}")
    return True


# ---------------------------------------------------------------------------
# Engine 1: structured edits (tomlkit / ast / ini)
# ---------------------------------------------------------------------------


def _pyproject_slug(doc: Any) -> str:
    try:
        name = doc["project"]["name"]
        if name:
            return str(name)
    except (KeyError, TypeError):
        pass
    return Path.cwd().name


def plan_pyproject(info: Info, path: Path) -> Plan | None:
    """Plan author/urls updates for ``pyproject.toml``."""
    original = path.read_text(encoding="utf-8")
    doc: Any = tomlkit.parse(original)
    if "project" not in doc:
        doc["project"] = tomlkit.table()
    project: Any = doc["project"]
    author = tomlkit.table()
    author["name"] = info["name"]
    author["email"] = info["email"]
    authors = tomlkit.aot()
    authors.append(author)
    project["authors"] = authors
    base = f"https://github.com/{info['github_username']}/{_pyproject_slug(doc)}"
    if "urls" not in project:
        project["urls"] = tomlkit.table()
    urls: Any = project["urls"]
    urls["Homepage"] = base
    urls["Repository"] = base
    urls["Issues"] = f"{base}/issues"
    return Plan(original=original, new=tomlkit.dumps(doc))


def _update_ini_section(text: str, section: str, updates: dict[str, str]) -> str:
    """Rewrite keys inside ``[section]`` of an INI file, appending when absent."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_section = False
    found_section = False
    written: set[str] = set()

    def flush_missing() -> None:
        for key, value in updates.items():
            if key not in written:
                out.append(f"{key} = {value}\n")
                written.add(key)

    for line in lines:
        header = _HEADER_RE.match(line.rstrip("\n"))
        if header:
            if in_section:
                flush_missing()
            in_section = header.group(1).strip() == section
            found_section = found_section or in_section
            out.append(line)
            continue
        if in_section:
            key_match = _KEY_RE.match(line)
            if key_match and key_match.group(1) in updates:
                out.append(f"{key_match.group(1)} = {updates[key_match.group(1)]}\n")
                written.add(key_match.group(1))
                continue
        out.append(line)
    if in_section:
        flush_missing()
    if not found_section:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        if out and out[-1].strip():
            out.append("\n")
        out.append(f"[{section}]\n")
        out.extend(f"{key} = {value}\n" for key, value in updates.items())
    return "".join(out)


def plan_setupcfg(info: Info, path: Path) -> Plan | None:
    """Plan ``[metadata]`` updates for ``setup.cfg``."""
    original = path.read_text(encoding="utf-8")
    updates = {
        "author": info["name"],
        "author_email": info["email"],
        "url": f"https://github.com/{info['github_username']}",
    }
    return Plan(original=original, new=_update_ini_section(original, "metadata", updates))


def _offset(src: str, lineno: int, col: int) -> int:
    """Convert an AST ``(lineno, col_offset)`` pair into an absolute char offset."""
    if lineno <= 1:
        return col
    lines = src.splitlines(keepends=True)
    return sum(len(line) for line in lines[: lineno - 1]) + col


def _node_end_offset(src: str, node: ast.expr) -> int:
    """Absolute end offset of an AST expression node."""
    if node.end_lineno is None or node.end_col_offset is None:
        msg = "AST node is missing end position information"
        raise ValueError(msg)
    return _offset(src, node.end_lineno, node.end_col_offset)


def _find_setup_call(src: str) -> tuple[ast.Call | None, str | None]:
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return None, f"syntax error: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "setup") or (
                isinstance(func, ast.Attribute) and func.attr == "setup"
            ):
                return node, None
    return None, "no setup() call found"


def _guess_indent(src: str, call: ast.Call) -> str:
    """Best-effort indentation used by the first arg/keyword of ``call``."""
    if call.keywords:
        kw = call.keywords[0]
        arg = kw.arg
        if arg is None:
            return "    "
        value_start = _offset(src, kw.value.lineno, kw.value.col_offset)
        eq_pos = src.rfind("=", 0, value_start)
        if eq_pos < 0:
            return "    "
        pos = eq_pos - 1
        while pos >= 0 and src[pos] in " \t":
            pos -= 1
        arg_start = pos - len(arg) + 1
        if arg_start < 0 or src[arg_start : arg_start + len(arg)] != arg:
            return "    "
        line_start = src.rfind("\n", 0, arg_start) + 1
        indent = src[line_start:arg_start]
        return indent if not indent.strip() else "    "
    if call.args:
        first = call.args[0]
        arg_start = _offset(src, first.lineno, first.col_offset)
        line_start = src.rfind("\n", 0, arg_start) + 1
        indent = src[line_start:arg_start]
        return indent if not indent.strip() else "    "
    return "    "


def _setup_slug(src: str, call: ast.Call) -> str:
    for kw in call.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return Path.cwd().name


def _apply_edits(src: str, edits: list[Edit]) -> str:
    """Apply edits from highest offset to lowest so earlier offsets stay valid."""
    for start, end, repl in sorted(edits, key=lambda edit: edit[0], reverse=True):
        src = src[:start] + repl + src[end:]
    return src


def plan_setuppy(info: Info, path: Path) -> Plan | None:
    """Plan ``setup()`` keyword updates for ``setup.py``."""
    original = path.read_text(encoding="utf-8")
    call, err = _find_setup_call(original)
    if call is None:
        print(f"Warning: {path}: {err}; skipping", file=sys.stderr)
        return None
    if any(kw.arg is None for kw in call.keywords):
        print(
            f"Warning: {path}: setup(**kwargs) present; skipping to avoid conflict",
            file=sys.stderr,
        )
        return None

    new_values = {
        "author": info["name"],
        "author_email": info["email"],
        "url": f"https://github.com/{info['github_username']}/{_setup_slug(original, call)}",
    }
    existing: dict[str, ast.keyword] = {}
    for kw in call.keywords:
        if kw.arg is not None:
            existing[kw.arg] = kw

    edits: list[Edit] = []
    for key, value in new_values.items():
        kw = existing.get(key)
        if kw is not None:
            start = _offset(original, kw.value.lineno, kw.value.col_offset)
            edits.append((start, _node_end_offset(original, kw.value), repr(value)))

    missing = [(key, value) for key, value in new_values.items() if key not in existing]
    if missing:
        close = _node_end_offset(original, call) - 1
        if close < 0 or original[close] != ")":
            print(
                f"Warning: {path}: could not locate closing paren; skipping",
                file=sys.stderr,
            )
            return None
        if call.lineno == call.end_lineno:
            pos = close - 1
            while pos >= 0 and original[pos] in " \t":
                pos -= 1
            if pos >= 0 and original[pos] == "(":
                sep = ""
            elif pos >= 0 and original[pos] == ",":
                sep = " "
            else:
                sep = ", "
            insert = sep + ", ".join(f"{key}={value!r}" for key, value in missing)
            edits.append((close, close, insert))
        else:
            line_start = original.rfind("\n", 0, close) + 1
            leading = original[line_start:close]
            indent = _guess_indent(original, call)
            if not leading.strip():
                pos = line_start - 1
                while pos >= 0 and original[pos] in " \t":
                    pos -= 1
                if pos >= 0 and original[pos] == "\n":
                    pos -= 1
                    while pos >= 0 and original[pos] in " \t":
                        pos -= 1
                if pos >= 0 and original[pos] not in "(,":
                    edits.append((pos + 1, pos + 1, ","))
                insert = "".join(f"{indent}{key}={value!r},\n" for key, value in missing)
                edits.append((line_start, line_start, insert))
            else:
                pos = close - 1
                while pos >= 0 and original[pos] in " \t\n\r":
                    pos -= 1
                prefix = "" if pos < 0 or original[pos] in "(," else ","
                insert = prefix + "".join(f"\n{indent}{key}={value!r}," for key, value in missing)
                edits.append((close, close, insert))
    return Plan(original=original, new=_apply_edits(original, edits))


def run_structured(args: argparse.Namespace) -> int:
    """Sub-command: format-aware edits for one project."""
    info = load_info(args.info)
    changed = 0
    for path, planner in (
        (args.pyproject, plan_pyproject),
        (args.setupcfg, plan_setupcfg),
    ):
        if not path.exists():
            print(f"Skipping {path} (not found)", file=sys.stderr)
            continue
        plan = planner(info, path)
        if plan is not None and _emit(
            path,
            plan,
            dry_run=args.dry_run,
            show_diff=args.diff,
            check=args.check,
            quiet=args.quiet,
        ):
            changed += 1

    if args.pyproject.exists() and not args.force_setuppy:
        print(
            f"Skipping {args.setuppy} (pyproject.toml present; pass --force-setuppy to override)",
            file=sys.stderr,
        )
    elif not args.setuppy.exists():
        print(f"Skipping {args.setuppy} (not found)", file=sys.stderr)
    else:
        plan = plan_setuppy(info, args.setuppy)
        if plan is not None and _emit(
            args.setuppy,
            plan,
            dry_run=args.dry_run,
            show_diff=args.diff,
            check=args.check,
            quiet=args.quiet,
        ):
            changed += 1

    suffix = " (dry-run)" if (args.dry_run or args.check) else ""
    print(f"{changed} file(s) changed{suffix}")
    return 1 if (args.check and changed) else 0


# ---------------------------------------------------------------------------
# Engine 2: regex-based tree scanner
# ---------------------------------------------------------------------------


def build_transformations(info: Info) -> dict[str, list[Transform]]:
    """Return suffix -> ordered list of regex transformations."""
    name = info["name"]
    email = info["email"]
    gh_user = info["github_username"]

    def quote_literal(value: str) -> Callable[[re.Match[str]], str]:
        def _replace(m: re.Match[str]) -> str:
            return f"{m.group(1)}{m.group(2)}{value}{m.group(2)}"

        return _replace

    def bare_value(value: str) -> Callable[[re.Match[str]], str]:
        def _replace(m: re.Match[str]) -> str:
            return f"{m.group(1)}{value}"

        return _replace

    def _gh_user(m: re.Match[str]) -> str:
        return m.group(0) if m.group(2) == gh_user else f"{m.group(1)}{gh_user}/"

    def _gh_io(m: re.Match[str]) -> str:
        return m.group(0) if m.group(1) == gh_user else f"{gh_user}.github.io"

    def _poetry_author(m: re.Match[str]) -> str:
        inner = m.group(2)
        if not inner.strip() or inner.lstrip().startswith("{"):
            return m.group(0)
        return f'{m.group(1)}"{name} <{email}>"{m.group(3)}'

    common: list[Transform] = [
        (
            re.compile(r"(?<!git\+)(https?://(?:www\.)?github\.com/)([A-Za-z0-9_.-]+)/"),
            _gh_user,
        ),
        (re.compile(r"(?<!git\+)(\bgithub\.com/)([A-Za-z0-9_.-]+)/"), _gh_user),
        (re.compile(r"\b([A-Za-z0-9_.-]+)\.github\.io\b"), _gh_io),
    ]
    py: list[Transform] = [
        (
            re.compile(r'(?<![.\w])(author[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(name),
        ),
        (
            re.compile(r'(?<![.\w])(author_email[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(email),
        ),
        (
            re.compile(r'(?<![.\w])(maintainer[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(name),
        ),
        (
            re.compile(r'(?<![.\w])(maintainer_email[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(email),
        ),
    ]
    cfg: list[Transform] = [
        (
            re.compile(r'(?m)^([ \t]*author[ \t]*=[ \t]*)(?![ "\']).*$'),
            bare_value(name),
        ),
        (
            re.compile(r'(?m)^([ \t]*author_email[ \t]*=[ \t]*)(?![ "\']).*$'),
            bare_value(email),
        ),
        (
            re.compile(r'(?m)^([ \t]*maintainer[ \t]*=[ \t]*)(?![ "\']).*$'),
            bare_value(name),
        ),
        (
            re.compile(r'(?m)^([ \t]*maintainer_email[ \t]*=[ \t]*)(?![ "\']).*$'),
            bare_value(email),
        ),
    ]
    toml: list[Transform] = [
        (
            re.compile(r'(?<![.\w])(author[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(name),
        ),
        (
            re.compile(r'(?<![.\w])(author_email[ \t]*=[ \t]*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(email),
        ),
        (
            re.compile(r'(\b(?:authors|maintainers)\s*=\s*\[\s*\{\s*name\s*=\s*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(name),
        ),
        (
            re.compile(r'(\b(?:authors|maintainers)\s*=\s*\[\s*\{[^}]*?\bemail\s*=\s*)(["\'])(?:[^"\'\\]|\\.)*\2'),
            quote_literal(email),
        ),
        (
            re.compile(r"(\b(?:authors|maintainers)\s*=\s*\[)([^\]]*)(\])"),
            _poetry_author,
        ),
    ]
    return {
        ".py": py + common,
        ".cfg": cfg + common,
        ".ini": cfg + common,
        ".toml": toml + common,
    }


def find_target_files(root: Path, extra: Iterable[str] = ()) -> Iterator[Path]:
    """Yield all target files under ``root``, pruning ``SKIP_DIRS``."""
    wanted = TARGET_FILENAMES | frozenset(extra)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fname in sorted(filenames):
            if fname in wanted:
                yield Path(dirpath) / fname


def plan_scan_file(path: Path, transforms: Iterable[Transform]) -> Plan | None:
    """Apply ``transforms`` to ``path`` and return a plan if anything changed."""
    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        print(f"  skipped {path}: {exc}", file=sys.stderr)
        return None
    new = original
    for pattern, repl in transforms:
        new = pattern.sub(repl, new)
    if new == original:
        return None
    return Plan(original=original, new=new)


def run_scan(args: argparse.Namespace) -> int:
    """Sub-command: regex rewrite of every target file under a project tree."""
    if not args.project_dir.is_dir():
        sys.exit(f"Not a directory: {args.project_dir}")
    info = load_info(args.info)
    table = build_transformations(info)
    changed = 0
    scanned = 0
    for path in find_target_files(args.project_dir, args.include):
        scanned += 1
        transforms = table.get(path.suffix.lower())
        if transforms is None:
            continue
        plan = plan_scan_file(path, transforms)
        if plan is not None and _emit(
            path,
            plan,
            dry_run=args.dry_run,
            show_diff=args.diff,
            check=args.check,
            quiet=args.quiet,
        ):
            changed += 1
    print()
    suffix = " (dry-run)" if args.dry_run else ""
    print(f"Scanned {scanned} file(s), {changed} changed{suffix}")
    return 1 if (args.check and changed) else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_DESCRIPTION: str = (__doc__ or "Inject personal metadata into project files.").strip().splitlines()[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``myinfo`` CLI."""
    parser = argparse.ArgumentParser(prog="myinfo", description=_DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--info",
        type=Path,
        default=INFO_PATH,
        help=f"Path to info file (default: {INFO_PATH})",
    )
    common.add_argument("--dry-run", action="store_true", help="Do not write any files, just report")
    common.add_argument("--diff", action="store_true", help="Print a unified diff of every change")
    common.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any file would change (implies --dry-run)",
    )
    common.add_argument("--quiet", action="store_true", help="Suppress per-file status lines")

    structured = sub.add_parser("structured", parents=[common], help="Format-aware edits in a single project")
    structured.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help="Path to pyproject.toml",
    )
    structured.add_argument("--setupcfg", type=Path, default=DEFAULT_SETUPCFG, help="Path to setup.cfg")
    structured.add_argument("--setuppy", type=Path, default=DEFAULT_SETUPPY, help="Path to setup.py")
    structured.add_argument(
        "--force-setuppy",
        action="store_true",
        help="Inject into setup.py even when pyproject.toml exists",
    )
    structured.set_defaults(func=run_structured)

    scan = sub.add_parser("scan", parents=[common], help="Regex-based rewrite of a project tree")
    scan.add_argument("project_dir", type=Path, help="Project directory to update")
    scan.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="NAME",
        help="Extra filename to scan (repeatable)",
    )
    scan.set_defaults(func=run_scan)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
