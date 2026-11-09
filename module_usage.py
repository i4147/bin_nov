#!/data/data/com.termux/files/usr/bin/env python
"""Report how the local scripts use a Python package.
Counts the imports and the attribute calls of the package (``dh`` by default) across a directory of scripts, writes ``~/dh_usage.txt`` and the ``charts`` subcommand adds the matplotlib bar charts of the most used names.
Usage: module_usage {report,charts} [--dir DIR] [--package dh] [--top-n N] Subcommands: report Text report only (original module_usage.py).
charts Text report + matplotlib charts (original module_usage_with_charts.py).
Options: --dir --output --package --chart-dir --top-n --style Examples: $ module_usage charts --package dh --top-n 15 Command-line script: run ``python module_usage.py`` or the ``module_usage`` command.
The module is importable as well."""

import argparse
import ast
from collections import Counter, defaultdict
import contextlib
from datetime import datetime
import importlib
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence


DEFAULT_PACKAGE: str = "dh"
DEFAULT_DIR = Path.home() / "bin"
DEFAULT_OUTPUT = Path.home() / "dh_usage.txt"
DEFAULT_CHART_DIR = Path.home()
DEFAULT_CHART_STYLE: str = "seaborn-v0_8-darkgrid"
DEFAULT_TOP_N: int = 10
STDLIB_FALLBACK: set[str] = {
    "os",
    "sys",
    "re",
    "json",
    "math",
    "time",
    "datetime",
    "pathlib",
    "collections",
    "itertools",
    "functools",
    "typing",
    "argparse",
    "logging",
    "subprocess",
    "shutil",
    "tempfile",
    "hashlib",
    "base64",
    "uuid",
    "csv",
    "io",
    "textwrap",
    "string",
    "random",
    "statistics",
    "decimal",
    "fractions",
    "enum",
    "dataclasses",
    "abc",
    "copy",
    "pprint",
    "traceback",
    "warnings",
    "contextlib",
    "threading",
    "multiprocessing",
    "socket",
    "http",
    "urllib",
    "email",
    "xml",
    "html",
    "configparser",
    "ast",
    "inspect",
    "dis",
    "tokenize",
    "compileall",
    "zipfile",
    "tarfile",
    "gzip",
    "bz2",
    "lzma",
    "pickle",
    "shelve",
    "dbm",
    "sqlite3",
    "unittest",
    "doctest",
    "pdb",
    "profile",
    "cProfile",
    "webbrowser",
    "tkinter",
    "turtle",
}


def collect_stdlib_modules() -> set[str]:
    modules: set[str] = set()
    for info in importlib.machinery.all_suffixes():
        _ = info
    try:
        for mod in importlib.machinery.PathFinder().iter_modules() if False else ():
            _ = mod
    except Exception:
        pass
    import pkgutil

    for info in pkgutil.iter_modules():
        name = info.name
        if name.startswith("_"):
            continue
        modules.add(name)
    modules.update(STDLIB_FALLBACK)
    return modules


def extract_imports(path: Path) -> dict[str, list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"   ⚠️  Skipping {path.name}: {exc}")
        return {}
    imports: dict[str, list[str]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name not in imports:
                    imports[name] = []
                if alias.asname:
                    imports[name].append(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            for alias in node.names:
                name = alias.name if alias.asname is None else alias.asname
                imports[module].append(name)
    return dict(imports)


def _collect_package_aliases(
    tree: ast.AST,
    package: str,
) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(package + "."):
                    aliases.add(alias.asname or alias.name)
    return aliases


def _record_package_attribute_calls(
    tree: ast.AST,
    aliases: set[str],
    imports: dict[str, list[str]],
    package: str,
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in aliases:
                imports[package].append(func.attr)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
            inner = func.value
            while isinstance(inner, ast.Attribute):
                inner = inner.value
            if isinstance(inner, ast.Name) and inner.id in aliases:
                imports[package].append(func.attr)


def count_calls(
    path: Path,
    imports: dict[str, list[str]],
    package: str,
) -> dict[str, Counter[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return {}
    lookup: dict[str, tuple[str, str]] = {}
    for module, names in imports.items():
        for name in names:
            lookup[name] = (module, name)
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in lookup:
            module, name = lookup[func.id]
            counts[module][name] += 1
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = func.value.id
            for module in imports:
                if module == base or module.endswith("." + base):
                    counts[module][func.attr] += 1
    return dict(counts.items())


def _extract_imports_full(
    path: Path,
    package: str,
) -> dict[str, list[str]]:
    imports = extract_imports(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return imports
    aliases = _collect_package_aliases(tree, package)
    if aliases:
        imports.setdefault(package, [])
        _record_package_attribute_calls(tree, aliases, imports, package)
    return imports


def analyze_directory(
    directory: Path,
    package: str,
    stdlib: set[str],
) -> tuple[
    list[tuple[str, dict[str, Counter[str]]]],
    dict[str, int],
    dict[str, int],
    dict[str, int],
]:
    files = sorted(directory.glob("*.py"))
    if not files:
        msg = f"No .py files found in {directory}"
        raise FileNotFoundError(msg)
    per_file: list[tuple[str, dict[str, Counter[str]]]] = []
    for f in files:
        imports = _extract_imports_full(f, package)
        if not imports:
            continue
        counts = count_calls(f, imports, package)
        if counts:
            per_file.append((f.name, counts))
    stdlib_totals: Counter[str] = Counter()
    thirdparty_totals: Counter[str] = Counter()
    package_totals: Counter[str] = Counter()
    stdlib_files: dict[str, set[str]] = defaultdict(set)
    thirdparty_files: dict[str, set[str]] = defaultdict(set)
    package_files: dict[str, set[str]] = defaultdict(set)
    for filename, counts in per_file:
        for module, attrs in counts.items():
            total = sum(attrs.values())
            top = module.split(".")[0]
            if top == package:
                package_totals[module] += total
                for attr in attrs:
                    package_files[attr].add(filename)
            elif top in stdlib:
                stdlib_totals[module] += total
                for attr in attrs:
                    stdlib_files[attr].add(filename)
            else:
                thirdparty_totals[module] += total
                for attr in attrs:
                    thirdparty_files[attr].add(filename)
    stdlib_filecounts = {k: len(v) for k, v in stdlib_files.items()}
    thirdparty_filecounts = {k: len(v) for k, v in thirdparty_files.items()}
    package_filecounts = {k: len(v) for k, v in package_files.items()}
    return (
        per_file,
        stdlib_totals,
        thirdparty_totals,
        package_totals,
        stdlib_filecounts,
        thirdparty_filecounts,
        package_filecounts,
    )


def build_report(
    per_file: list[tuple[str, dict[str, Counter[str]]]],
    stdlib_totals: Counter[str],
    thirdparty_totals: Counter[str],
    package_totals: Counter[str],
    stdlib_files: dict[str, int],
    thirdparty_files: dict[str, int],
    package_files: dict[str, int],
    directory: Path,
    package: str,
) -> str:
    lines: list[str] = []
    now = datetime.now()
    lines.append("=" * 40)
    lines.append(f"  IMPORT USAGE REPORT — {now:%Y-%m-%d %H:%M}")
    lines.append("=" * 40)
    lines.append(f"  Scanned directory: {directory}")
    lines.append(f"  Files scanned: {len(per_file)}")
    lines.append("")
    lines.append("─" * 40)
    lines.append("  SECTION 1: STANDARD LIBRARY MODULES")
    lines.append("─" * 40)
    if stdlib_totals:
        lines.append(f"\n  {'Module':<35} {'Total Calls':<15} {'Files':<10}")
        lines.append("  " + "-" * 40)
        for module in sorted(stdlib_totals, key=lambda m: -stdlib_totals[m]):
            count = stdlib_files.get(module, 0)
            lines.append(f"  {module:<35} {stdlib_totals[module]:<15} {count:<10}")
        lines.append(f"\n  Total stdlib modules used: {len(stdlib_totals)}")
    else:
        lines.append("  (none)")
    lines.append(f"\n{'─' * 40}")
    lines.append("  SECTION 2: THIRD-PARTY PACKAGES")
    lines.append("─" * 40)
    if thirdparty_totals:
        lines.append(f"\n  {'Package':<35} {'Total Calls':<15} {'Files':<10}")
        lines.append("  " + "-" * 40)
        for module in sorted(thirdparty_totals, key=lambda m: -thirdparty_totals[m]):
            count = thirdparty_files.get(module, 0)
            lines.append(f"  {module:<35} {thirdparty_totals[module]:<15} {count:<10}")
        lines.append(f"\n  Total third-party packages used: {len(thirdparty_totals)}")
    else:
        lines.append("  (none)")
    lines.append(f"\n{'─' * 40}")
    lines.append(f"  SECTION 3: CUSTOM '{package}' PACKAGE")
    lines.append("─" * 40)
    if package_totals:
        lines.append(f"\n  {'Function':<35} {'Total Calls':<15} {'Files':<10}")
        lines.append("  " + "-" * 40)
        for func in sorted(package_totals, key=lambda f: -package_totals[f]):
            count = package_files.get(func, 0)
            lines.append(f"  {func:<35} {package_totals[func]:<15} {count:<10}")
        lines.append(f"\n  Total {package} functions used: {len(package_totals)}")
    else:
        lines.append("  (none)")
    lines.append(f"\n{'─' * 40}")
    lines.append("  SECTION 4: PER-FILE BREAKDOWN")
    lines.append("─" * 40)
    sorted_files = sorted(
        per_file,
        key=lambda x: -sum(sum(c.values()) for c in x[1].values()),
    )
    for filename, counts in sorted_files:
        total = sum(sum(c.values()) for c in counts.values())
        lines.append(f"\n  📄 {filename}  ({total} total calls)")
        stdlib_block: dict[str, Counter[str]] = {}
        thirdparty_block: dict[str, Counter[str]] = {}
        package_block: dict[str, Counter[str]] = {}
        for module, attrs in counts.items():
            top = module.split(".")[0]
            if top == package:
                package_block[module] = attrs
            elif top in stdlib_totals or top in STDLIB_FALLBACK:
                stdlib_block[module] = attrs
            else:
                thirdparty_block[module] = attrs

        def _emit(block: dict[str, Counter[str]], title: str) -> None:
            if not block:
                return
            lines.append(f"    [{title}]")
            for module in sorted(block):
                attrs = block[module]
                subtotal = sum(attrs.values())
                lines.append(f"      {module:<30} {subtotal} call(s)")
                for attr, c in sorted(attrs.items(), key=lambda x: -x[1]):
                    if c > 0:
                        lines.append(f"        {attr:<28} {c} time(s)")

        _emit(stdlib_block, "stdlib")
        _emit(thirdparty_block, "third-party")
        _emit(package_block, package)
    lines.append("")
    lines.append("=" * 40)
    lines.append("  END OF REPORT")
    lines.append("=" * 40)
    return "\n".join(lines)


def _load_matplotlib():
    try:
        import matplotlib as mpl

        mpl.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except ImportError as exc:
        msg = "matplotlib is required for the 'charts' subcommand. Install it with: pip install matplotlib"
        raise SystemExit(msg) from exc


def generate_charts(
    stdlib_totals: Counter[str],
    thirdparty_totals: Counter[str],
    package_totals: Counter[str],
    chart_dir: Path,
    top_n: int,
    style: str,
    package: str,
) -> None:
    if not stdlib_totals and not thirdparty_totals and not package_totals:
        print("⚠️  No data to chart.")
        return
    plt = _load_matplotlib()
    with contextlib.suppress(OSError):
        plt.style.use(style)
    chart_dir.mkdir(parents=True, exist_ok=True)
    print("\n📊 Generating matplotlib charts...")
    fig, ax = plt.subplots(figsize=(12, 6))
    top = dict(sorted(stdlib_totals.items(), key=lambda x: -x[1])[:top_n])
    if top:
        ax.barh(list(top.keys()), list(top.values()), color="#3498db")
        ax.set_xlabel("Total Calls")
        ax.set_title(
            f"Top {top_n} Standard Library Modules by Usage",
            fontsize=14,
            fontweight="bold",
        )
        ax.invert_yaxis()
        plt.tight_layout()
        out = chart_dir / "01_stdlib_top10.png"
        plt.savefig(out, dpi=100, bbox_inches="tight")
        print(f"   ✅ Saved: {out.name}")
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 8))
    distribution = {
        "Standard Library": sum(stdlib_totals.values()),
        "Third-Party": sum(thirdparty_totals.values()),
        f"Custom ({package})": sum(package_totals.values()),
    }
    distribution = {k: v for k, v in distribution.items() if v > 0}
    if distribution:
        colors = ["#3498db", "#e74c3c", "#2ecc71"][: len(distribution)]
        _, _, autotexts = ax.pie(
            distribution.values(),
            labels=distribution.keys(),
            autopct="%1.1f%%",
            colors=colors,
            startangle=90,
        )
        for t in autotexts:
            t.set_color("white")
            t.set_fontweight("bold")
        ax.set_title(
            "Import Usage Distribution by Category",
            fontsize=14,
            fontweight="bold",
        )
        plt.tight_layout()
        out = chart_dir / "02_category_distribution.png"
        plt.savefig(out, dpi=100, bbox_inches="tight")
        print(f"   ✅ Saved: {out.name}")
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(12, 6))
    top = dict(sorted(thirdparty_totals.items(), key=lambda x: -x[1])[:top_n])
    if top:
        ax.barh(list(top.keys()), list(top.values()), color="#e74c3c")
        ax.set_xlabel("Total Calls")
        ax.set_title(
            f"Top {top_n} Third-Party Packages by Usage",
            fontsize=14,
            fontweight="bold",
        )
        ax.invert_yaxis()
        plt.tight_layout()
        out = chart_dir / "03_thirdparty_top10.png"
        plt.savefig(out, dpi=100, bbox_inches="tight")
        print(f"   ✅ Saved: {out.name}")
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(12, 6))
    top = dict(sorted(package_totals.items(), key=lambda x: -x[1])[:top_n])
    if top:
        ax.bar(list(top.keys()), list(top.values()), color="#2ecc71")
        ax.set_ylabel("Total Calls")
        ax.set_title(
            f"Top {top_n} {package} Functions by Usage",
            fontsize=14,
            fontweight="bold",
        )
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        out = chart_dir / "04_dh_functions.png"
        plt.savefig(out, dpi=100, bbox_inches="tight")
        print(f"   ✅ Saved: {out.name}")
    plt.close(fig)


def _run_analysis(args: argparse.Namespace) -> tuple[Any, str]:
    directory: Path = Path(args.dir).expanduser()
    if not directory.is_dir():
        print(f"❌ {directory} does not exist or is not a directory.")
        sys.exit(1)
    print(f"🔍 Scanning Python files in {directory} ...")
    print("   Building stdlib list (this may take a moment)...")
    stdlib = collect_stdlib_modules()
    print(f"   Detected {len(stdlib)} stdlib modules\n")
    try:
        (
            per_file,
            stdlib_totals,
            thirdparty_totals,
            package_totals,
            stdlib_files,
            thirdparty_files,
            package_files,
        ) = analyze_directory(directory, args.package, stdlib)
    except FileNotFoundError as exc:
        print(f"⚠️  {exc}")
        output_path = Path(args.output).expanduser()
        output_path.write_text(f"{exc}\n", encoding="utf-8")
        sys.exit(0)
    if not per_file:
        print("✅ No imports found in any script.")
        output_path = Path(args.output).expanduser()
        output_path.write_text(f"No imports found in {directory}.\n", encoding="utf-8")
        sys.exit(0)
    report = build_report(
        per_file,
        stdlib_totals,
        thirdparty_totals,
        package_totals,
        stdlib_files,
        thirdparty_files,
        package_files,
        directory,
        args.package,
    )
    return (
        (stdlib_totals, thirdparty_totals, package_totals),
        report,
    )


def cmd_report(args: argparse.Namespace) -> int:
    _, report = _run_analysis(args)
    output_path = Path(args.output).expanduser()
    output_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n✅ Report saved to {output_path}")
    return 0


def cmd_charts(args: argparse.Namespace) -> int:
    data, report = _run_analysis(args)
    stdlib_totals, thirdparty_totals, package_totals = data
    output_path = Path(args.output).expanduser()
    output_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n✅ Report saved to {output_path}")
    generate_charts(
        stdlib_totals,
        thirdparty_totals,
        package_totals,
        Path(args.chart_dir).expanduser(),
        args.top_n,
        args.style,
        args.package,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="module_usage.py",
        description="Analyze import usage in a directory of Python scripts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dir",
        default=str(DEFAULT_DIR),
        help=f"Directory of .py files to scan. Default: {DEFAULT_DIR}",
    )
    common.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Path of the text report file. Default: {DEFAULT_OUTPUT}",
    )
    common.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help=f"Name of the custom package to track separately. Default: {DEFAULT_PACKAGE}",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_report = sub.add_parser(
        "report",
        parents=[common],
        help="Text report only (original module_usage.py).",
    )
    p_report.set_defaults(func=cmd_report)
    p_charts = sub.add_parser(
        "charts",
        parents=[common],
        help="Text report + matplotlib charts (original module_usage_with_charts.py).",
    )
    p_charts.add_argument(
        "--chart-dir",
        default=str(DEFAULT_CHART_DIR),
        help=f"Directory for chart PNGs. Default: {DEFAULT_CHART_DIR}",
    )
    p_charts.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"Number of items in the top-N charts. Default: {DEFAULT_TOP_N}",
    )
    p_charts.add_argument(
        "--style",
        default=DEFAULT_CHART_STYLE,
        help=f"Matplotlib style name. Default: {DEFAULT_CHART_STYLE}",
    )
    p_charts.set_defaults(func=cmd_charts)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
