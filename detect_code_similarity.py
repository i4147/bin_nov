#!/data/data/com.termux/files/usr/bin/env python
"""Detect the identical and similar Python files of a tree.
Normalizes each file (comments and formatting dropped), hashes it, compares the pairs with difflib and optionally ssdeep/tlsh, and writes a JSON report of the exact duplicates and of the pairs above the similarity threshold (70 % by default).
Usage: detect_code_similarity [--dir DIR] [--threshold N] [--output report.json] Options: --dir Root directory to scan (default: current folder) --threshold Similarity threshold percentage (default: 70) --mp-method Parallel processing method to use --output Path for JSON output report --exclude-dir Directories to exclude Examples: $ detect_code_similarity --dir src --threshold 85 Command-line script: run ``python detect_code_similarity.py`` or the ``detect_code_similarity`` command.
The module is importable as well."""

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import contextlib
import difflib
import hashlib
import io
import json
import multiprocessing as mp
from pathlib import Path
import sys
import time
from tokenize import COMMENT, ENDMARKER, NEWLINE, NL, generate_tokens
from typing import Any, NamedTuple

from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table


console = Console()
try:
    import ssdeep

    HAS_SSDEEP = True
except ImportError:
    HAS_SSDEEP = False
try:
    import tlsh

    HAS_TLSH = True
except ImportError:
    HAS_TLSH = False


class FileRecord(NamedTuple):
    path: str
    size: int
    raw_code: str
    normalized_code: str
    exact_hash: str
    tokens: list[str]
    ssdeep_hash: str | None
    tlsh_hash: str | None


class CodeNormalizer(ast.NodeTransformer):
    def visit_Module(self, node: ast.Module) -> ast.Module:
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
        node.returns = None
        if node.args:
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                arg.annotation = None
            if node.args.vararg:
                node.args.vararg.annotation = None
            if node.args.kwarg:
                node.args.kwarg.annotation = None
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
        node.returns = None
        if node.args:
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                arg.annotation = None
            if node.args.vararg:
                node.args.vararg.annotation = None
            if node.args.kwarg:
                node.args.kwarg.annotation = None
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
        self.generic_visit(node)
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if node.value is not None:
            return ast.Assign(targets=[node.target], value=node.value)
        return None


def normalize_source(code_text: str) -> tuple[str, list[str]]:
    lines = code_text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    clean_code = "\n".join(lines)
    tokens: list[str] = []
    try:
        for tok in generate_tokens(io.StringIO(clean_code).readline):
            if tok.type not in (COMMENT, NL, NEWLINE, ENDMARKER):
                tokens.append(tok.string)
    except Exception as e:
        logger.debug(f"Tokenization warning: {e}")
        tokens = clean_code.split()
    try:
        parsed_ast = ast.parse(clean_code)
        normalizer = CodeNormalizer()
        normalized_ast = normalizer.visit(parsed_ast)
        normalized_text = ast.unparse(normalized_ast)
    except SyntaxError as se:
        logger.warning(f"Syntax error during AST normalization (falling back to raw stripped): {se}")
        normalized_text = clean_code
    except Exception as exc:
        logger.error(f"Unexpected normalization error: {exc}")
        normalized_text = clean_code
    return normalized_text, tokens


def process_single_file(file_path: Path) -> FileRecord | None:
    try:
        if not file_path.is_file():
            return None
        try:
            raw_code = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw_code = file_path.read_text(encoding="latin-1", errors="ignore")
        size = file_path.stat().st_size
        normalized_code, tokens = normalize_source(raw_code)
        exact_hash = hashlib.sha256(normalized_code.encode("utf-8", errors="ignore")).hexdigest()
        ssdeep_hash = ssdeep.hash(normalized_code) if HAS_SSDEEP else None
        tlsh_hash = (
            tlsh.hash(normalized_code.encode("utf-8", errors="ignore"))
            if HAS_TLSH and len(normalized_code) > 50
            else None
        )
        return FileRecord(
            path=str(file_path.resolve()),
            size=size,
            raw_code=raw_code,
            normalized_code=normalized_code,
            exact_hash=exact_hash,
            tokens=tokens,
            ssdeep_hash=ssdeep_hash,
            tlsh_hash=tlsh_hash,
        )
    except Exception as e:
        logger.error(f"Failed processing file {file_path}: {e}")
        return None


def compute_similarity(f1: FileRecord, f2: FileRecord) -> dict[str, Any]:
    exact_match = f1.exact_hash == f2.exact_hash
    seq_matcher = difflib.SequenceMatcher(None, f1.normalized_code, f2.normalized_code)
    sequence_score = round(seq_matcher.ratio() * 100.0, 2)
    set1, set2 = set(f1.tokens), set(f2.tokens)
    union_len = len(set1 | set2)
    jaccard_score = round((len(set1 & set2) / union_len * 100.0) if union_len > 0 else 0.0, 2)
    ssdeep_score = 0.0
    if HAS_SSDEEP and f1.ssdeep_hash and f2.ssdeep_hash:
        with contextlib.suppress(Exception):
            ssdeep_score = float(ssdeep.compare(f1.ssdeep_hash, f2.ssdeep_hash))
    tlsh_score = 0.0
    if HAS_TLSH and f1.tlsh_hash and f2.tlsh_hash:
        try:
            diff = tlsh.diff(f1.tlsh_hash, f2.tlsh_hash)
            tlsh_score = round(max(0.0, 100.0 - (diff / 2.0)), 2)
        except Exception:
            pass
    scores = [sequence_score, jaccard_score]
    if HAS_SSDEEP and ssdeep_score > 0:
        scores.append(ssdeep_score)
    if HAS_TLSH and tlsh_score > 0:
        scores.append(tlsh_score)
    composite_score = round(sum(scores) / len(scores), 2)
    return {
        "file_a": f1.path,
        "file_b": f2.path,
        "exact_match": exact_match,
        "composite_similarity": 100.0 if exact_match else composite_score,
        "metrics": {
            "sequence_matcher_similarity": sequence_score,
            "token_jaccard_similarity": jaccard_score,
            "ssdeep_similarity": ssdeep_score if HAS_SSDEEP else None,
            "tlsh_similarity": tlsh_score if HAS_TLSH else None,
        },
    }


def parallel_map(method: str, func: Any, items: list[Any], max_workers: int | None = None) -> list[Any]:
    if method == "thread-pool":
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(func, items))
    elif method == "process-pool":
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(func, items))
    elif method == "mp-map":
        with mp.Pool(processes=max_workers) as pool:
            return pool.map(func, items)
    elif method == "mp-imap":
        with mp.Pool(processes=max_workers) as pool:
            return list(pool.imap_unordered(func, items))
    else:
        msg = f"Unknown multiprocessing method: {method}"
        raise ValueError(msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect identical and similar Python files recursively.")
    parser.add_argument("--dir", type=str, default=".", help="Root directory to scan (default: current folder)")
    parser.add_argument("--threshold", type=float, default=70.0, help="Similarity threshold percentage (default: 70)")
    parser.add_argument(
        "--mp-method",
        type=str,
        default="mp-map",
        choices=["mp-map", "mp-imap", "process-pool", "thread-pool"],
        help="Parallel processing method to use",
    )
    parser.add_argument("--output", type=str, default="code_similarity_report.json", help="Path for JSON output report")
    parser.add_argument(
        "--exclude-dir",
        nargs="*",
        default=[".git", "__pycache__", ".venv", "venv", "build", "dist"],
        help="Directories to exclude",
    )
    args = parser.parse_args()
    root_dir = Path(args.dir).resolve()
    if not root_dir.exists():
        console.print(f"[bold red]Error:[/] Directory '{root_dir}' does not exist.", style="red")
        sys.exit(1)
    console.print(f"[bold cyan]Scanning directory:[/] {root_dir} recursively...")
    py_files: list[Path] = []
    for p in root_dir.rglob("*.py"):
        if any(exc in p.parts for exc in args.exclude_dir):
            continue
        py_files.append(p)
    if not py_files:
        console.print("[yellow]No Python files found matching criteria.[/yellow]")
        sys.exit(0)
    console.print(f"[green]Found {len(py_files)} Python files. Processing in parallel ({args.mp_method})...[/green]")
    start_time = time.time()
    records: list[FileRecord | None] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Normalizing and hashing files...", total=len(py_files))
        results = parallel_map(args.mp_method, process_single_file, py_files)
        records = [r for r in results if r is not None]
        progress.update(task, completed=len(py_files))
    console.print(f"[green]Successfully processed {len(records)} files in {time.time() - start_time:.2f}s.[/green]")
    exact_clusters: dict[str, list[str]] = {}
    for rec in records:
        exact_clusters.setdefault(rec.exact_hash, []).append(rec.path)
    identical_groups = {h: paths for h, paths in exact_clusters.items() if len(paths) > 1}
    console.print("[cyan]Performing pairwise fuzzy similarity analysis...[/cyan]")
    similar_pairs: list[dict[str, Any]] = []
    pairs = [(records[i], records[j]) for i in range(len(records)) for j in range(i + 1, len(records))]
    for f1, f2 in pairs:
        res = compute_similarity(f1, f2)
        if res["composite_similarity"] >= args.threshold:
            similar_pairs.append(res)
    similar_pairs.sort(key=lambda x: x["composite_similarity"], reverse=True)
    report_data = {
        "scan_metadata": {
            "root_directory": str(root_dir),
            "total_files_scanned": len(py_files),
            "total_files_processed": len(records),
            "similarity_threshold": args.threshold,
            "multiprocessing_method": args.mp_method,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "features": {
                "ssdeep_available": HAS_SSDEEP,
                "tlsh_available": HAS_TLSH,
            },
        },
        "identical_groups": [{"sha256": h, "files": paths} for h, paths in identical_groups.items()],
        "similar_pairs": similar_pairs,
    }
    output_path = Path(args.output)
    output_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    console.print(f"[bold green]JSON report successfully saved to:[/] {output_path.resolve()}")
    console.print("\n")
    console.print(f"[bold underline]Summary Report (Threshold >= {args.threshold}%)[/bold underline]")
    if identical_groups:
        console.print(f"\n[bold yellow]Found {len(identical_groups)} Identical File Group(s):[/bold yellow]")
        for idx, (h, paths) in enumerate(identical_groups.items(), 1):
            console.print(f"  [cyan]Group {idx} (SHA: {h[:12]}...):[/cyan]")
            for p in paths:
                console.print(
                    f"    - {Path(p).relative_to(root_dir) if root_dir in Path(p).parents or p == str(root_dir) else p}"
                )
    else:
        console.print("\n[green]No completely identical files found.[/green]")
    if similar_pairs:
        table = Table(title=f"Similar File Pairs (>= {args.threshold}%)", show_header=True, header_style="bold magenta")
        table.add_column("File A", style="dim")
        table.add_column("File B", style="dim")
        table.add_column("Exact", justify="center")
        table.add_column("Similarity", justify="right", style="bold green")
        for pair in similar_pairs[:20]:
            rel_a = Path(pair["file_a"]).name
            rel_b = Path(pair["file_b"]).name
            table.add_row(
                rel_a,
                rel_b,
                "Yes" if pair["exact_match"] else "No",
                f"{pair['composite_similarity']}%",
            )
        console.print(table)
        if len(similar_pairs) > 20:
            console.print(
                f"[italic dim]...and {len(similar_pairs) - 20} more pairs saved in the JSON report.[/italic dim]"
            )
    else:
        console.print(f"[green]No similar file pairs found above {args.threshold}% threshold.[/green]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[bold red]Operation cancelled by user.[/bold red]")
        sys.exit(1)
    except Exception as exc:
        logger.exception(f"Unhandled fatal error: {exc}")
        sys.exit(1)
