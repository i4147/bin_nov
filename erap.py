#!/data/data/com.termux/files/usr/bin/env python
"""Run the Python type checkers and either append their output to the sources or write JSON reports.
The ``append`` subcommand runs ty, pyright, pylint, pyrefly and mypy on each file and appends the diagnostics as comments, ``report`` writes one JSON report per file under ``report/`` (``-a`` selects all the tools), and the ``dh`` unresolved-import noise is filtered out.
Usage: erap {append,report} [-a] [-g] [-p] [-t] [-m] [paths ...] Positional arguments: paths ...
Python files or directories to process (default: .
recursively) Subcommands: append Run checkers and append their output to Python files.
report Run checkers on every .py in CWD and write JSON reports per file.
Options: -a, --all Run all tools:ty,pyright,pylint,pyrefly,mypy -g, --pyright Run pyright -p, --pylint Run pylint -r, --pyrefly Run pyrefly -t, --ty Run ty -m, --mypy Run mypy -o, --output Examples: $ erap report -a src/ Command-line script: run ``python erap.py`` or the ``erap`` command.
The module is importable as well."""

import argparse
import json
import multiprocessing
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable, Iterator

from dh import MAX_WORKERS


APPEND_TOOLS: dict[str, list[str]] = {
    "ty": ["ty", "check"],
    "pyright": ["pyright"],
    "pylint": ["pylint", "-E"],
    "pyrefly": ["pyrefly", "check"],
    "mypy": ["mypy"],
}
REPORT_TOOLS: dict[str, list[str]] = {
    "mypy": ["mypy", "--ignore-missing-imports"],
    "pylyzer": ["pylyzer"],
    "ty": ["ty", "check"],
    "pyrefly": ["pyrefly", "check"],
    "pylint": ["pylint", "--errors-only"],
}
DEFAULT_APPEND_TOOLS: list[str] = ["ty", "pyright", "pyrefly"]
DEFAULT_REPORT_DIR: str = "report"
_DH_IMPORT_PATTERN: str = "error[unresolved-import]:cannot resolve imported module `dh`"


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def _run_tool(name: str, base_cmd: list[str], target: Path) -> tuple[str, str | None]:
    try:
        _rc, out = _run(base_cmd + [str(target)])
    except FileNotFoundError:
        return name, f"ERROR:{name} not found in PATH"
    except Exception as exc:
        return name, f"ERROR:{exc!s}"
    return name, out if out.strip() else None


def _iter_py_files(paths: Iterable[str]) -> Iterator[Path]:
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            yield from p.rglob("*.py")


def _append_outputs(target: Path, results: dict[str, str | None]) -> None:
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n")
        for name, out in results.items():
            fh.write(f"#====={name} output=====\n")
            if out:
                for line in out.split("\n"):
                    if line.strip():
                        fh.write(f"# {line}\n")
            else:
                fh.write("# (no issues)\n")


def _process_append(target: Path, tools: list[str]) -> str:
    results: dict[str, str | None] = {}
    for name in tools:
        key, value = _run_tool(name, APPEND_TOOLS[name], target)
        results[key] = value
    ty_out = (results.get("ty") or "").lower()
    skip = (tools == ["ty"] and "all checks passed" in ty_out) or (_DH_IMPORT_PATTERN in ty_out)
    if skip:
        return f"✓ Skipped (ty:all checks passed):{target}"
    _append_outputs(target, results)
    return f"✓ Updated:{target}"


def _process_report(target: Path, out_dir: Path) -> None:
    print(f"Processing:{target.name}")
    entry: dict = {
        "filename": target.name,
        "filepath": str(target.resolve()),
        "tools": {},
    }
    for name, base in REPORT_TOOLS.items():
        exe = base[0]
        if shutil.which(exe) is None:
            entry["tools"][name] = {
                "status": "skipped",
                "error": f"Executable '{exe}' not found in PATH.",
            }
            continue
        try:
            rc, out = _run(base + [str(target)])
            out = out.strip()
            entry["tools"][name] = {
                "exit_code": rc,
                "has_errors": rc != 0,
                "output": out or "No output.",
            }
        except Exception as exc:
            entry["tools"][name] = {"status": "error", "error": str(exc)}
    report_path = out_dir / f"{target.stem}.json"
    report_path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    print(f"->Report saved:{report_path}")


def _cmd_append(args: argparse.Namespace) -> int:
    paths = args.paths or ["."]
    if args.all:
        tools = ["ty", "pyright", "pylint", "pyrefly", "mypy"]
    else:
        tools = []
        for enabled, name in (
            (args.ty, "ty"),
            (args.pyright, "pyright"),
            (args.pylint, "pylint"),
            (args.pyrefly, "pyrefly"),
            (args.mypy, "mypy"),
        ):
            if enabled:
                tools.append(name)
        if not tools:
            tools = list(DEFAULT_APPEND_TOOLS)
    files = list(_iter_py_files(paths))
    if not files:
        print("No .py files found.")
        return 0
    with multiprocessing.Pool(processes=MAX_WORKERS) as pool:
        futures = [pool.apply_async(_process_append, args=(f, tools)) for f in files]
        for fut in futures:
            print(fut.get())
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    out_dir = Path(args.output) if args.output else cwd / DEFAULT_REPORT_DIR
    out_dir.mkdir(exist_ok=True)
    self_name = Path(__file__).name
    files = sorted(f for f in cwd.iterdir() if f.is_file() and f.suffix == ".py" and f.name != self_name)
    if not files:
        print("No .py files found in current directory.")
        return 0
    print(f"Found {len(files)} Python file(s). Generating reports in '{out_dir.name}/'...\n")
    for f in files:
        _process_report(f, out_dir)
    print("\nCompleted analysis for all files.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run code checkers and either append outputs to Python files or generate JSON reports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python merged.py append file.py\n"
            "  python merged.py append . -a\n"
            "  python merged.py append . -g -p\n"
            "  python merged.py append dir/ -r\n"
            "  python merged.py append file1.py file2.py -t -p\n"
            "  python merged.py report\n"
            "  python merged.py report -o reports/\n"
        ),
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    ap = sub.add_parser(
        "append",
        help="Run checkers and append their output to Python files.",
    )
    ap.add_argument(
        "paths",
        nargs="*",
        help="Python files or directories to process (default: . recursively)",
    )
    ap.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Run all tools:ty,pyright,pylint,pyrefly,mypy",
    )
    ap.add_argument("-g", "--pyright", action="store_true", help="Run pyright")
    ap.add_argument("-p", "--pylint", action="store_true", help="Run pylint")
    ap.add_argument("-r", "--pyrefly", action="store_true", help="Run pyrefly")
    ap.add_argument("-t", "--ty", action="store_true", help="Run ty")
    ap.add_argument("-m", "--mypy", action="store_true", help="Run mypy")
    ap.set_defaults(func=_cmd_append)
    rp = sub.add_parser(
        "report",
        help="Run checkers on every .py in CWD and write JSON reports per file.",
    )
    rp.add_argument(
        "-o",
        "--output",
        default=None,
        help=f"Output directory for reports (default:'{DEFAULT_REPORT_DIR}')",
    )
    rp.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
