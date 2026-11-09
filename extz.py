#!/data/data/com.termux/files/usr/bin/env python
"""Report the file extensions used below the current directory.
Counts the files per extension with their sizes, prints a coloured table of the most common ones, and ``-s`` switches to the total size per extension while ``-p`` writes a Matplotlib pie chart (the top 30 are charted).
Usage: extz [-s] [-p] [--no-color] Options: -s, --size Show total size per extension instead of example files -p, --pie-chart --no-color Disable color output Examples: $ extz -s -p Command-line script: run ``python extz.py`` or the ``extz`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import contextlib
from pathlib import Path
import shutil
import sys

from dh import SKIP_DIRS


TOP_N_PIE = 30


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    MAGENTA = "\033[35m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"
    HEADER_FG = BOLD + CYAN
    EXT_FG = BOLD + MAGENTA
    COUNT_FG = BOLD + GREEN
    DATA_FG = YELLOW


def format_size(bytes_val: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"


def should_skip(path: Path) -> bool:
    if path.is_symlink():
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def scan_extensions(show_size: bool = False) -> dict[str, tuple[list[Path], int]]:
    ext_map: dict[str, tuple[list[Path], int]] = defaultdict(lambda: ([], 0))
    cwd = Path.cwd()
    for file_path in cwd.rglob("*"):
        try:
            if should_skip(file_path):
                continue
            if file_path.is_file():
                ext = file_path.suffix or "<no-ext>"
                paths, total_size = ext_map[ext]
                file_size = file_path.stat().st_size if show_size else 0
                ext_map[ext] = (paths + [file_path], total_size + file_size)
        except OSError:
            continue
    return ext_map


def generate_report(
    ext_map: dict[str, tuple[list[Path], int]],
    show_size: bool = False,
    max_examples: int = 3,
) -> list[tuple[str, int, str]]:
    report_data = []
    for ext in sorted(ext_map.keys(), key=lambda x: -len(ext_map[x][0])):
        paths, total_size = ext_map[ext]
        count = len(paths)
        if show_size:
            info = format_size(total_size)
        else:
            examples = ", ".join(str(p.relative_to(Path.cwd())) for p in paths[:max_examples])
            info = examples
        report_data.append((ext, count, info))
    return report_data


def format_table(
    report_data: list[tuple[str, int, str]],
    term_width: int,
    show_size: bool = False,
    use_color: bool = True,
) -> str:
    col1_width = max(10, int(term_width * 0.15))
    col2_width = 12
    col3_width = max(25, term_width - col1_width - col2_width - 5)
    lines = []
    col3_label = "Total Size" if show_size else "Example Files"
    if use_color:
        header = (
            f"{Colors.HEADER_FG}{'Extension':<{col1_width}}{Colors.RESET} | "
            f"{Colors.HEADER_FG}{'Count':>{col2_width}}{Colors.RESET} | "
            f"{Colors.HEADER_FG}{col3_label:<{col3_width}}{Colors.RESET}"
        )
    else:
        header = f"{'Extension':<{col1_width}} | {'Count':>{col2_width}} | {col3_label:<{col3_width}}"
    lines.append(header)
    sep = "-" * len(header)
    lines.append(f"{Colors.DIM}{sep}{Colors.RESET}" if use_color else sep)
    for ext, count, info in report_data:
        if len(info) > col3_width:
            info = info[: col3_width - 3] + "..."
        if use_color:
            row = (
                f"{Colors.EXT_FG}{ext:<{col1_width}}{Colors.RESET} | "
                f"{Colors.COUNT_FG}{count:>{col2_width}}{Colors.RESET} | "
                f"{Colors.DATA_FG}{info:<{col3_width}}{Colors.RESET}"
            )
        else:
            row = f"{ext:<{col1_width}} | {count:>{col2_width}} | {info:<{col3_width}}"
        lines.append(row)
    return "\n".join(lines)


def write_pie_chart(
    ext_map: dict[str, tuple[list[Path], int]],
    out_path: Path = Path("extinfo.png"),
    top_n: int = TOP_N_PIE,
) -> None:
    try:
        import matplotlib as mpl

        mpl.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            "Error: --pie-chart requires matplotlib (pip install matplotlib)",
            file=sys.stderr,
        )
        sys.exit(2)
    if not ext_map:
        print("No files found — nothing to plot.", file=sys.stderr)
        return
    sizes: dict[str, int] = {}
    for ext, (paths, cached_size) in ext_map.items():
        if cached_size:
            sizes[ext] = cached_size
        else:
            total = 0
            for p in paths:
                with contextlib.suppress(OSError):
                    total += p.stat().st_size
            sizes[ext] = total
    total_files = sum(len(paths) for paths, _ in ext_map.values())
    total_exts = len(sizes)
    ordered = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    if 0 < top_n < total_exts:
        head = ordered[:top_n]
        tail_size = sum(s for _, s in ordered[top_n:])
        ordered = [*head, (f"other ({total_exts - top_n} exts)", tail_size)]
        shown = top_n
    else:
        shown = total_exts
    values = [s for _, s in ordered]
    total_size = sum(values) or 1
    legend_labels = [f"{ext}  ({size / total_size * 100:.1f}%, {format_size(size)})" for ext, size in ordered]
    fig, ax = plt.subplots(figsize=(11, 8))
    wedges, _ = ax.pie(
        values,
        startangle=90,
        counterclock=False,
        wedgeprops={"linewidth": 0.6, "edgecolor": "white"},
    )
    ax.axis("equal")
    ax.legend(
        wedges,
        legend_labels,
        title=f"Top {shown} of {total_exts} exts",
        loc="center left",
        bbox_to_anchor=(1.0, 0.5),
        fontsize=9,
    )
    ax.set_title(
        f"Disk usage by extension\n"
        f"{total_files:,} files · {format_size(total_size)} total · "
        f"{total_exts:,} distinct exts",
        fontsize=13,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nWrote pie chart: {out_path} ({out_path.stat().st_size:,} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Report file extensions in current directory recursively.")
    parser.add_argument(
        "-s",
        "--size",
        action="store_true",
        help="Show total size per extension instead of example files",
    )
    parser.add_argument(
        "-p",
        "--pie-chart",
        action="store_true",
        help=f"Also write a matplotlib pie chart to extinfo.png (top {TOP_N_PIE} exts)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    args = parser.parse_args()
    try:
        term_width = shutil.get_terminal_size(fallback=(80, 24)).columns
        use_color = not args.no_color and sys.stdout.isatty()
        mode_str = "size mode" if args.size else "example files mode"
        cwd_str = str(Path.cwd())
        need_sizes = args.size or args.pie_chart
        ext_map = scan_extensions(show_size=need_sizes)
        if not ext_map:
            print("No files found.")
            sys.exit(0)
        report_data = generate_report(ext_map, show_size=args.size)
        table = format_table(report_data, term_width, show_size=args.size, use_color=use_color)
        print(table)
        total_files = sum(count for _, count, _ in report_data)
        total_exts = len(report_data)
        if args.size:
            total_bytes = sum(ext_map[ext][1] for ext in ext_map)
            lines = [
                f"✓ Total unique extensions: {total_exts}",
                f"✓ Total files: {total_files}",
                f"✓ Total size: {format_size(total_bytes)}",
            ]
        else:
            lines = [
                f"✓ Total unique extensions: {total_exts}",
                f"✓ Total files: {total_files}",
            ]
        print()
        for line in lines:
            print(f"{Colors.GRAY}{line}{Colors.RESET}" if use_color else line)
        if args.pie_chart:
            write_pie_chart(ext_map, out_path=Path.cwd() / "extinfo.png")
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
