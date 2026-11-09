#!/data/data/com.termux/files/usr/bin/env python
"""Draw an SVG dependency graph of the installed Python packages.
Reads the installed distributions and their requirements, builds the graph (restricted by ``--root`` to the reachable packages), lays it out in topological levels and writes the SVG to ``-o``.
Usage: mk_deptree_svg [-o graph.svg] [--root PKG ...] Options: -o, --output Output SVG path.
--root Only show dependencies reachable from these packages.
Examples: $ mk_deptree_svg -o deps.svg --root mypkg Command-line script: run ``python mk_deptree_svg.py`` or the ``mk_deptree_svg`` command.
The module is importable as well."""

import argparse
from collections import defaultdict, deque
import html
from importlib import metadata
from pathlib import Path
import re

from dh import MAX_WORKERS
from joblib import Parallel, delayed


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def get_installed_packages() -> dict[str, metadata.Distribution]:
    packages = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            packages[normalize_name(name)] = distribution
    return packages


def package_dependencies(
    item: tuple[str, metadata.Distribution],
) -> tuple[str, list[str]]:
    package_name, distribution = item
    print(f"procesding ... {package_name}")
    dependencies = []
    for requirement in distribution.requires or []:
        dependency_name = requirement.split(";", 1)[0]
        dependency_name = re.split(r"[<>=!~\[\s]", dependency_name, maxsplit=1)[0]
        if dependency_name:
            dependencies.append(normalize_name(dependency_name))
    return package_name, sorted(set(dependencies))


def build_dependency_graph(
    packages: dict[str, metadata.Distribution],
) -> dict[str, list[str]]:
    results = Parallel(n_jobs=MAX_WORKERS, prefer="threads")(
        delayed(package_dependencies)(item) for item in packages.items()
    )
    installed_names = set(packages)
    graph = {}
    for package_name, dependencies in results:
        graph[package_name] = [dependency for dependency in dependencies if dependency in installed_names]
    return graph


def reachable_graph(
    graph: dict[str, list[str]],
    roots: list[str] | None,
) -> tuple[dict[str, list[str]], list[str]]:
    if not roots:
        selected_roots = sorted(graph)
    else:
        selected_roots = [normalize_name(root) for root in roots if normalize_name(root) in graph]
    if not selected_roots:
        msg = "None of the requested root packages are installed."
        raise ValueError(msg)
    included = set()
    queue = deque(selected_roots)
    while queue:
        package = queue.popleft()
        if package in included:
            continue
        included.add(package)
        for dependency in graph.get(package, []):
            if dependency not in included:
                queue.append(dependency)
    limited_graph = {
        package: [dependency for dependency in graph.get(package, []) if dependency in included]
        for package in sorted(included)
    }
    return limited_graph, selected_roots


def topological_levels(
    graph: dict[str, list[str]],
    roots: list[str],
) -> dict[str, int]:
    levels = dict.fromkeys(roots, 0)
    queue = deque(roots)
    while queue:
        package = queue.popleft()
        current_level = levels[package]
        for dependency in graph.get(package, []):
            proposed_level = current_level + 1
            if proposed_level > levels.get(dependency, -1):
                levels[dependency] = proposed_level
                queue.append(dependency)
    for package in graph:
        levels.setdefault(package, 0)
    return levels


def svg_text(
    x: int,
    y: int,
    text: str,
    *,
    font_size: int = 14,
    fill: str = "#202124",
    anchor: str = "middle",
    weight: str = "normal",
) -> str:
    escaped = html.escape(text)
    return (
        f'<text x="{x}" y="{y}" font-size="{font_size}" '
        f'fill="{fill}" text-anchor="{anchor}" '
        f'font-family="Arial, sans-serif" font-weight="{weight}">'
        f"{escaped}</text>"
    )


def create_svg(
    graph: dict[str, list[str]],
    roots: list[str],
    output: Path,
) -> None:
    levels = topological_levels(graph, roots)
    by_level = defaultdict(list)
    for package, level in levels.items():
        by_level[level].append(package)
    for level in by_level:
        by_level[level].sort()
    node_width = 180
    node_height = 42
    horizontal_gap = 50
    vertical_gap = 100
    margin = 60
    max_nodes_on_level = max(len(nodes) for nodes in by_level.values())
    width = max(
        900,
        margin * 2 + max_nodes_on_level * node_width + max(0, max_nodes_on_level - 1) * horizontal_gap,
    )
    height = margin * 2 + (max(by_level) + 1) * node_height + max(0, *by_level) * vertical_gap
    positions: dict[str, tuple[int, int]] = {}
    for level, packages in by_level.items():
        total_width = len(packages) * node_width + max(0, len(packages) - 1) * horizontal_gap
        start_x = max(margin, (width - total_width) // 2)
        y = margin + level * (node_height + vertical_gap)
        for index, package in enumerate(packages):
            x = start_x + index * (node_width + horizontal_gap)
            positions[package] = (x, y)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'),
        "<defs>",
        """
        <marker id="arrow" markerWidth="10" markerHeight="10"
                refX="9" refY="3" orient="auto"
                markerUnits="strokeWidth">
            <path d="M0,0 L0,6 L9,3 z" fill="#8a8f98"/>
        </marker>
        """,
        """
        <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="2" stdDeviation="2"
                          flood-color="#000000" flood-opacity="0.18"/>
        </filter>
        """,
        "</defs>",
        '<rect width="100%" height="100%" fill="#f7f8fa"/>',
        svg_text(
            width // 2,
            30,
            "Installed Python Package Dependencies",
            font_size=20,
            weight="bold",
        ),
    ]
    for package, dependencies in graph.items():
        if package not in positions:
            continue
        package_x, package_y = positions[package]
        package_center_x = package_x + node_width // 2
        package_bottom_y = package_y + node_height
        for dependency in dependencies:
            if dependency not in positions:
                continue
            dependency_x, dependency_y = positions[dependency]
            dependency_center_x = dependency_x + node_width // 2
            parts.append(
                f'<line x1="{package_center_x}" y1="{package_bottom_y}" '
                f'x2="{dependency_center_x}" y2="{dependency_y}" '
                f'stroke="#8a8f98" stroke-width="1.5" '
                f'marker-end="url(#arrow)"/>'
            )
    for package, (x, y) in positions.items():
        is_root = package in roots
        fill = "#dbeafe" if is_root else "#ffffff"
        stroke = "#2563eb" if is_root else "#9aa0a6"
        parts.extend([
            (
                f'<rect x="{x}" y="{y}" width="{node_width}" '
                f'height="{node_height}" rx="8" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="1.5" '
                f'filter="url(#shadow)"/>'
            ),
            svg_text(
                x + node_width // 2,
                y + 26,
                package,
                font_size=13,
                weight="bold" if is_root else "normal",
            ),
        ])
    parts.append("</svg>")
    output.write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an SVG dependency graph for installed Python packages.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("installed-dependencies.svg"),
        help="Output SVG path.",
    )
    parser.add_argument(
        "--root",
        nargs="+",
        help="Only show dependencies reachable from these packages.",
    )
    args = parser.parse_args()
    packages = get_installed_packages()
    if not packages:
        msg = "No installed Python packages were found."
        raise SystemExit(msg)
    packages = dict(sorted(packages.items(), key=lambda item: item[0]))
    graph = build_dependency_graph(packages)
    try:
        graph, roots = reachable_graph(graph, args.root)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    args.output.parent.mkdir(parents=True, exist_ok=True)
    create_svg(graph, roots, args.output)
    print(f"Wrote {len(graph)} packages to {args.output}")


if __name__ == "__main__":
    raise SystemExit(main())
