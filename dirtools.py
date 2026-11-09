#!/data/data/com.termux/files/usr/bin/env python
"""Unified directory analysis and folderization toolkit.
Subcommands: ``info``/``subdirs`` print the size report and the charts, ``list``/``tree`` show the entries, and ``split-range``/``split-count``/``split-greedy``/``split-even`` move the files into folders by size range, by count or max size, by greedy bin packing or by an even split.
Usage: dirtools {info,subdirs,list,tree,split-range,split-count,split-greedy,split-even} [options] Subcommands: info Directory report + optional bar chart by extension subdirs Chart top-level directory size distribution list Du-style listing of current directory entries tree Tree view of a directory split-range Folderize by size ranges (foldesiz.py) split-count Folderize by fixed count or max-MB per folder (foldesize.py) split-greedy Greedy bin-pack into ~N folders (foldsize.py) split-even Even count split into N folders (foldsize2.py) Options: -d, --directory Directory to scan (default: .) --save-report Save the text report to PATH (skipped if it exists) --chart Save a bar chart of extension sizes to PATH --out Output image path (default: dirinfo.png) --top-n Show only top N entries; 0 = all (default: 25) --min-kb Ignore entries smaller than this many KB (default: 100) --dpi Image DPI (default: 300) --include-hidden Include dotfiles/dot-directories -s, --sizes Show sizes -H, --human-readable Human-readable sizes (used with --sizes) ...
plus 4 more; run ``dirtools --help`` for the full list.
Examples: $ dirtools split-count --max-mb 100 ~/photos Command-line script: run ``python dirtools.py`` or the ``dirtools`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import contextlib
import math
from pathlib import Path
import shutil
import sys
from typing import Iterable, Optional, Sequence


def human_size(n: int) -> str:
    if n < 0:
        return "-" + human_size(-n)
    if n < 1_000:
        return f"{n}B"
    if n < 1_000_000:
        return f"{n // 1000}k"
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n < 1_000_000_000_000:
        return f"{n / 1_000_000_000:.1f}G"
    return f"{n / 1_000_000_000_000:.1f}T"


def iter_files(
    root: Path,
    recursive: bool = True,
    include_hidden: bool = False,
    exclude_names: Iterable[str] = (),
) -> list[Path]:
    exclude = set(exclude_names)
    it = root.rglob("*") if recursive else root.glob("*")
    out: list[Path] = []
    for p in it:
        if not p.is_file() or p.is_symlink():
            continue
        if not include_hidden and p.name.startswith("."):
            continue
        if p.name in exclude:
            continue
        out.append(p)
    return out


def dir_size(path: Path, include_hidden: bool = False) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if not p.is_file() or p.is_symlink():
                continue
            if not include_hidden and p.name.startswith("."):
                continue
            with contextlib.suppress(OSError):
                total += p.stat().st_size
    except OSError:
        pass
    return total


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.parent / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def _safe_folder_name(name: str) -> str:
    return "".join(c for c in name if c not in '<>:"/\\|?*')


def cmd_info(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    total_size = 0
    n_files = 0
    n_dirs = 0
    ext_sizes: dict[str, int] = defaultdict(int)
    for p in root.rglob("*"):
        if p.is_dir():
            n_dirs += 1
        elif p.is_file():
            n_files += 1
            try:
                sz = p.stat().st_size
            except OSError:
                sz = 0
            total_size += sz
            ext = p.suffix.lower() if p.suffix else "(no extension)"
            ext_sizes[ext] += sz
    lines: list[str] = []
    lines.append(f"Total size: {human_size(total_size)}")
    lines.append("")
    lines.append("File extensions:")
    for ext in sorted(ext_sizes.keys()):
        lines.append(f"- {ext}")
    lines.append("")
    lines.append(f"Number of files: {n_files}")
    lines.append(f"Number of folders: {n_dirs}")
    lines.append("Size by extension:")
    for ext, sz in sorted(ext_sizes.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"  {ext}: {human_size(sz)}")
    report = "\n".join(lines)
    if args.save_report:
        out = Path(args.save_report)
        if out.exists():
            print(f"{out} exists")
            return 1
        try:
            out.write_text(report, encoding="utf-8")
            print(f"Summary saved to {out}")
        except OSError as e:
            print(f"Error saving summary to {out}: {e}", file=sys.stderr)
            return 1
    else:
        print(report)
    if args.chart:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            print(
                "matplotlib is required for --chart (pip install matplotlib)",
                file=sys.stderr,
            )
            return 2
        items = sorted(
            ((e, s) for e, s in ext_sizes.items() if s > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        if not items:
            print("No data to plot.", file=sys.stderr)
            return 0
        labels, values = zip(*items)
        fig, ax = plt.subplots(figsize=(12, 7))
        ax.bar(labels, values, color="skyblue")
        ax.set_title("Size by File Extension")
        ax.set_xlabel("File Extension")
        ax.set_ylabel("Size (bytes)")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        try:
            plt.savefig(args.chart)
            print(f"Bar chart saved to {args.chart}")
        except Exception as e:
            print(f"Error saving chart: {e}", file=sys.stderr)
            return 1
        finally:
            plt.close(fig)
    return 0


def cmd_subdirs(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    sizes: dict[str, int] = {}
    try:
        for entry in root.iterdir():
            if entry.is_symlink():
                continue
            if not args.include_hidden and entry.name.startswith("."):
                continue
            if entry.is_dir():
                sizes[entry.name] = dir_size(entry, include_hidden=args.include_hidden)
            elif entry.is_file():
                try:
                    sizes[entry.name] = entry.stat().st_size
                except OSError:
                    continue
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    min_bytes = int(args.min_kb * 1024)
    sizes = {k: v for k, v in sizes.items() if v >= min_bytes}
    if not sizes:
        print("No subdirectories meeting criteria.")
        return 0
    total = sum(sizes.values())
    ordered = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    if args.top_n > 0 and len(ordered) > args.top_n:
        top = dict(ordered[: args.top_n])
        other = sum(s for _, s in ordered[args.top_n :])
    else:
        top = dict(ordered)
        other = 0
    if other > 0:
        top["Other"] = other
    labels = list(top.keys())
    values = list(top.values())
    for name, sz in zip(labels, values):
        pct = sz / total * 100 if total else 0.0
        print(f"  {name}: {human_size(sz)} ({pct:.1f}%)")
    print(f"  TOTAL: {human_size(total)}")
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            "matplotlib is required for chart output (pip install matplotlib)",
            file=sys.stderr,
        )
        return 2
    fig, ax = plt.subplots(figsize=(10, 7))
    if args.chart == "bar":
        ax.bar(labels, values, color="skyblue")
        ax.set_ylabel("Size (bytes)")
        ax.set_title("Directory Size Distribution")
        plt.xticks(rotation=45, ha="right")
    elif args.chart == "pie":
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=140)
        ax.set_title("Directory Size Distribution")
        ax.axis("equal")
    elif args.chart == "circle":
        ax.pie(
            values,
            labels=labels,
            autopct="%1.1f%%",
            startangle=140,
            wedgeprops={"width": 0.4},
        )
        ax.set_title("Directory Size Distribution")
        ax.axis("equal")
    plt.tight_layout()
    try:
        plt.savefig(args.out, dpi=args.dpi)
        print(f"Chart saved to {args.out}")
    except Exception as e:
        print(f"Error saving chart: {e}", file=sys.stderr)
        return 1
    finally:
        plt.close(fig)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    entries: list[tuple[int, Path]] = []
    for p in root.iterdir():
        if p.is_symlink():
            continue
        try:
            sz = dir_size(p) if p.is_dir() else p.stat().st_size
        except OSError:
            continue
        entries.append((sz, p))
    total = sum(sz for sz, _ in entries)
    for sz, p in sorted(entries, key=lambda x: x[0]):
        name_color = "\x1b[5;94m" if p.is_dir() else "\x1b[5;92m"
        size_color = "\x1b[5;96m" if sz > 1_048_576 else ""
        reset = "\x1b[0m"
        print(f"{name_color}{p.name:25}{reset}  {size_color}{human_size(sz):>10}{reset}")
    print(f"total size : \x1b[5;94m{human_size(total)}\x1b[0m")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        return 1

    def visible(p: Path) -> bool:
        if not args.include_hidden and p.name.startswith("."):
            return False
        if ".git" in p.parts:
            return False
        return not (args.dirs_only and not p.is_dir())

    def walk(d: Path, prefix: str = "") -> None:
        entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        entries = [e for e in entries if visible(e)]
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            connector = "└── " if last else "├── "
            if args.sizes:
                if e.is_dir():
                    sz = dir_size(e, include_hidden=args.include_hidden)
                else:
                    try:
                        sz = e.stat().st_size
                    except OSError:
                        sz = 0
                shown = human_size(sz) if args.human_readable else f"{sz}"
                size_str = f" [{shown}]"
            else:
                size_str = ""
            print(f"{prefix}{connector}{e.name}{size_str}")
            if e.is_dir():
                ext = "    " if last else "│   "
                walk(e, prefix + ext)

    print(root.name)
    walk(root)
    return 0


def cmd_split_range(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    files: list[tuple[Path, int]] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if not args.include_hidden and p.name.startswith("."):
            continue
        try:
            files.append((p, p.stat().st_size))
        except OSError:
            continue
    if not files:
        print("No files found.")
        return 0
    files.sort(key=lambda x: x[1])
    if args.dirs and args.dirs > 0:
        n_dirs = args.dirs
    else:
        sizes = [s for _, s in files]
        if len(sizes) < 2:
            n_dirs = 1
        else:
            span = max(sizes) - min(sizes)
            if span <= 0:
                n_dirs = 1
            else:
                n_dirs = max(1, min(100, len(sizes)))
    print(f"{n_dirs} dirs will be created")
    all_sizes = sorted(s for _, s in files)
    buckets: list[tuple[int, int, str]] = []
    per = len(all_sizes) // n_dirs
    rem = len(all_sizes) % n_dirs
    idx = 0
    for i in range(n_dirs):
        end = idx + per + (1 if i < rem else 0)
        chunk = all_sizes[idx:end]
        if chunk:
            mn, mx = min(chunk), max(chunk)
            name = _safe_folder_name(f"{human_size(mn)}-{human_size(mx)}")
            buckets.append((mn, mx, name))
            if not args.dry_run:
                (root / name).mkdir(exist_ok=True, parents=True)
        idx = end
    moved = 0
    for p, sz in files:
        matched = False
        for mn, mx, name in buckets:
            if mn <= sz <= mx:
                dst = unique_path(root / name / p.name)
                try:
                    if args.dry_run:
                        print(f"[DRY RUN] Would move {p.name} -> {name}/")
                    else:
                        shutil.move(str(p), str(dst))
                    moved += 1
                except Exception as e:
                    print(f"Failed to move {p}: {e}")
                matched = True
                break
        if not matched:
            print(f"No folder match for {p.name} ({sz:,} bytes)")
    print(f"Folderization complete! Moved {moved} file(s).")
    return 0


_COUNT_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (100, 10),
    (500, 25),
    (1000, 50),
    (5000, 100),
)


def _files_per_folder(count: int) -> int:
    for limit, per in _COUNT_THRESHOLDS:
        if count <= limit:
            return per
    return 200


def cmd_split_count(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    files: list[dict] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if not args.include_hidden and p.name.startswith("."):
            continue
        try:
            files.append({"path": p, "name": p.name, "size": p.stat().st_size})
        except OSError:
            continue
    if not files:
        print("No files found!")
        return 0
    total = sum(f["size"] for f in files)
    print(f"Total: {len(files)} files, {human_size(total)}")
    files.sort(key=lambda f: f["size"])
    if args.max_mb:
        max_bytes = int(args.max_mb * 1024 * 1024)
        chunks: list[list[dict]] = []
        cur: list[dict] = []
        cur_size = 0
        for f in files:
            if cur_size + f["size"] > max_bytes and cur:
                chunks.append(cur)
                cur = []
                cur_size = 0
            cur.append(f)
            cur_size += f["size"]
        if cur:
            chunks.append(cur)
    else:
        if args.dirs and args.dirs > 0:
            per_folder = math.ceil(len(files) / args.dirs)
        else:
            per_folder = _files_per_folder(len(files))
        n_dirs = math.ceil(len(files) / per_folder) if per_folder else 1
        chunks = [files[i * per_folder : (i + 1) * per_folder] for i in range(n_dirs)]
    for i, chunk in enumerate(chunks, 1):
        if not chunk:
            continue
        mn = chunk[0]["size"]
        mx = chunk[-1]["size"]
        name = _safe_folder_name(f"{human_size(mn)}-{human_size(mx)}")
        dst_dir = root / name
        try:
            if not args.dry_run:
                dst_dir.mkdir(exist_ok=True, parents=True)
            print(
                f"  Folder {i}/{len(chunks)}: {name} ({len(chunk)} files, {human_size(sum(f['size'] for f in chunk))})"
            )
            for f in chunk:
                target = dst_dir / f["name"]
                k = 1
                stem, suf = (str(Path(f["name"]).with_suffix("")), Path(f["name"]).suffix)
                while target.exists():
                    target = dst_dir / f"{stem}_{k}{suf}"
                    k += 1
                try:
                    if not args.dry_run:
                        shutil.move(str(f["path"]), str(target))
                except Exception as e:
                    print(f"      Error moving {f['name']}: {e}")
        except Exception as e:
            print(f"  Error creating folder {name}: {e}")
    print("✓ Organization complete!")
    return 0


def cmd_split_greedy(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    files = iter_files(
        root,
        recursive=True,
        include_hidden=args.include_hidden,
        exclude_names={"folderize.py"},
    )
    if not files:
        print("No files found to process.")
        return 0
    sizes_by_path = {p: p.stat().st_size for p in files}
    total = sum(sizes_by_path.values())
    count = len(files)
    print(f"Found {count:,} files ({total:,} bytes)")
    max_files_per = max(1000, count // 10)
    max_bytes_per = max(1_000_000, total // 10)
    est = max(math.ceil(count / max_files_per), math.ceil(total / max_bytes_per))
    n_dirs = max(2, min(100, est))
    print(f"Targeting ~{n_dirs} directories")
    sorted_files = sorted(files, key=lambda p: sizes_by_path[p], reverse=True)
    bins: list[dict] = [{"files": [], "size": 0} for _ in range(n_dirs)]
    for f in sorted_files:
        sz = sizes_by_path[f]
        i = min(range(n_dirs), key=lambda j: bins[j]["size"])
        bins[i]["files"].append(f)
        bins[i]["size"] += sz
    existing = {p.name for p in root.iterdir() if p.is_dir()}
    created: list[tuple[str, int, int]] = []
    for b in bins:
        if not b["files"]:
            continue
        bin_sizes = [sizes_by_path[f] for f in b["files"]]
        mn, mx = min(bin_sizes), max(bin_sizes)
        base = _safe_folder_name(f"{human_size(mn)}-{human_size(mx)}")
        name = base
        k = 1
        while name in existing:
            name = f"{base}_{k}"
            k += 1
        target = root / name
        if not args.dry_run:
            target.mkdir(exist_ok=True)
        existing.add(name)
        created.append((name, len(b["files"]), b["size"]))
        print(f"Created dir '{name}' → {len(b['files'])} files, {b['size']:,} bytes")
        for f in b["files"]:
            dst = unique_path(target / f.name)
            try:
                if not args.dry_run:
                    shutil.move(str(f), str(dst))
            except Exception as e:
                print(f"  Failed to move {f}: {e}")
    print("-" * 40)
    print("✅ Folderization complete")
    print(f"   Files processed: {count:,}")
    print(f"   Directories created: {len(created)}")
    print("-" * 40)
    print(f"\n{'Dir Name':<20} {'Files':>8} {'Size (bytes)':>14}")
    print("-" * 40)
    for name, n, sz in sorted(created, key=lambda x: x[2]):
        print(f"{name:<20} {n:>8} {sz:>14,}")
    return 0


def cmd_split_even(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    files = iter_files(
        root,
        recursive=False,
        include_hidden=args.include_hidden,
        exclude_names={"folderize.py"},
    )
    if not files:
        print("No files found to process.")
        return 0
    n = len(files)
    print(f"Found {n:,} files")
    if args.dirs and args.dirs > 0:
        n_dirs = args.dirs
    else:
        max_per = max(1000, n // 10)
        n_dirs = max(2, min(100, math.ceil(n / max_per)))
    print(f"Creating {n_dirs} directories")
    per = n // n_dirs
    rem = n % n_dirs
    existing = {p.name for p in root.iterdir() if p.is_dir()}
    plan: list[tuple[str, int, int]] = []
    idx = 0
    for i in range(n_dirs):
        start = idx
        end = idx + per + (1 if i < rem else 0)
        base = f"{start}_{end - 1}"
        name = base
        k = 1
        while name in existing:
            name = f"{base}_{k}"
            k += 1
        target = root / name
        if not args.dry_run:
            target.mkdir(exist_ok=True)
        existing.add(name)
        plan.append((name, start, end))
        print(f"Created dir '{name}' for files [{start},{end})")
        idx = end
    for name, start, end in plan:
        target = root / name
        for i in range(start, min(end, n)):
            f = files[i]
            dst = unique_path(target / f.name)
            try:
                if not args.dry_run:
                    shutil.move(str(f), str(dst))
            except Exception as e:
                print(f"  Failed to move {f}: {e}")
    print("-" * 40)
    print("✅ Folderization complete")
    print(f"   Files processed: {n:,}")
    print(f"   Directories created: {len(plan)}")
    print("-" * 40)
    print(f"\n{'Dir Name':<20} {'Files':>8}")
    print("-" * 40)
    for name, start, end in plan:
        print(f"{name:<20} {end - start:>8}")
    print(f"\nTotal directories: {len(plan)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dir_tools.py",
        description="Unified directory analysis & folderization toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original-script mapping:\n"
            "  dirinfo.py     ->  info\n"
            "  dirinfo2.py    ->  subdirs --chart bar\n"
            "  visdir.py      ->  subdirs --chart pie --top-n 0 --min-kb 0\n"
            "  pddd.py        ->  list\n"
            "  pytree.py      ->  tree\n"
            "  foldesiz.py    ->  split-range\n"
            "  foldesize.py   ->  split-count\n"
            "  foldsize.py    ->  split-greedy\n"
            "  foldsize2.py   ->  split-even\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("info", help="Directory report + optional bar chart by extension")
    p.add_argument("-d", "--directory", default=".", help="Directory to scan (default: .)")
    p.add_argument(
        "--save-report",
        metavar="PATH",
        help="Save the text report to PATH (skipped if it exists)",
    )
    p.add_argument("--chart", metavar="PATH", help="Save a bar chart of extension sizes to PATH")
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("subdirs", help="Chart top-level directory size distribution")
    p.add_argument("-d", "--directory", default=".", help="Directory to scan (default: .)")
    p.add_argument(
        "--chart",
        choices=("bar", "pie", "circle"),
        default="bar",
        help="Chart type (default: bar)",
    )
    p.add_argument("--out", default="dirinfo.png", help="Output image path (default: dirinfo.png)")
    p.add_argument(
        "--top-n",
        type=int,
        default=25,
        help="Show only top N entries; 0 = all (default: 25)",
    )
    p.add_argument(
        "--min-kb",
        type=float,
        default=100.0,
        help="Ignore entries smaller than this many KB (default: 100)",
    )
    p.add_argument("--dpi", type=int, default=300, help="Image DPI (default: 300)")
    p.add_argument("--include-hidden", action="store_true", help="Include dotfiles/dot-directories")
    p.set_defaults(func=cmd_subdirs)
    p = sub.add_parser("list", help="du-style listing of current directory entries")
    p.add_argument("-d", "--directory", default=".", help="Directory (default: .)")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("tree", help="Tree view of a directory")
    p.add_argument("-d", "--directory", default=".", help="Directory (default: .)")
    p.add_argument("-s", "--sizes", action="store_true", help="Show sizes")
    p.add_argument(
        "-H",
        "--human-readable",
        action="store_true",
        help="Human-readable sizes (used with --sizes)",
    )
    p.add_argument("--dirs-only", action="store_true", help="List directories only")
    p.add_argument("--include-hidden", action="store_true", help="Include dotfiles/dot-directories")
    p.set_defaults(func=cmd_tree)
    p = sub.add_parser("split-range", help="Folderize by size ranges (foldesiz.py)")
    p.add_argument("-d", "--directory", default=".", help="Source directory (default: .)")
    p.add_argument(
        "--dirs",
        type=int,
        default=None,
        help="Number of folders; omit for auto heuristic",
    )
    p.add_argument("--include-hidden", action="store_true", help="Include dotfiles/dot-directories")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.set_defaults(func=cmd_split_range)
    p = sub.add_parser(
        "split-count",
        help="Folderize by fixed count or max-MB per folder (foldesize.py)",
    )
    p.add_argument("-d", "--directory", default=".", help="Source directory (default: .)")
    p.add_argument(
        "--dirs",
        type=int,
        default=4,
        help="Number of folders when --max-mb is absent (default: 4)",
    )
    p.add_argument(
        "--max-mb",
        type=float,
        default=None,
        help="Maximum MB per folder; overrides --dirs if set",
    )
    p.add_argument("--include-hidden", action="store_true", help="Include dotfiles/dot-directories")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.set_defaults(func=cmd_split_count)
    p = sub.add_parser("split-greedy", help="Greedy bin-pack into ~N folders (foldsize.py)")
    p.add_argument("-d", "--directory", default=".", help="Source directory (default: .)")
    p.add_argument("--include-hidden", action="store_true", help="Include dotfiles/dot-directories")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.set_defaults(func=cmd_split_greedy)
    p = sub.add_parser("split-even", help="Even count split into N folders (foldsize2.py)")
    p.add_argument("-d", "--directory", default=".", help="Source directory (default: .)")
    p.add_argument(
        "--dirs",
        type=int,
        default=None,
        help="Number of folders; omit for auto heuristic",
    )
    p.add_argument("--include-hidden", action="store_true", help="Include dotfiles/dot-directories")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.set_defaults(func=cmd_split_even)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
