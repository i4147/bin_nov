#!/data/data/com.termux/files/usr/bin/env python
"""Find the Python scripts of ``~/bin`` that are most similar to a given file.
Compares the target with every script in the folder using a normalized AST hash, a line-level difflib score and an ssdeep fuzzy score, prints the best ``--top`` matches per method and can report each comparison live with ``-v``.
Usage: most_similar FILE [--bin-dir DIR] [--top N] [-v] Positional arguments: file Target Python file to compare Options: --bin-dir Directory to search (default: ~/bin) --top Number of results per method (default: 3) -v, --verbose Show each comparison result live as it's computed Examples: $ most_similar tool.py --top 5 Command-line script: run ``python most_similar.py`` or the ``most_similar`` command.
The module is importable as well."""

import argparse
import ast
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sys
from typing import Iterator

from dh import SKIP_DIRS
import ssdeep

BOLD_BLUE: str = "\033[1;34m"
RESET: str = "\033[0m"


class _Normalizer(ast.NodeTransformer):
    @staticmethod
    def _strip_docstring(node: ast.AST) -> ast.AST:
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    @staticmethod
    def _clear_arg_annotations(args: ast.arguments) -> None:
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            a.annotation = None
        if args.vararg:
            args.vararg.annotation = None
        if args.kwarg:
            args.kwarg.annotation = None

    def visit_Module(self, node: ast.Module) -> ast.Module:
        self.generic_visit(node)
        return self._strip_docstring(node)

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        self._clear_arg_annotations(node.args)
        return self._strip_docstring(node)

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self.generic_visit(node)
        return self._strip_docstring(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        self.generic_visit(node)
        if node.value is not None:
            return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)
        return ast.copy_location(ast.Pass(), node)


def normalize_python(source: str) -> str:
    lines: list[str] = source.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    src: str = "\n".join(lines)
    try:
        tree: ast.Module = ast.parse(src)
    except SyntaxError:
        return src
    tree = _Normalizer().visit(tree)
    ast.fix_missing_locations(tree)
    try:
        return ast.unparse(tree)
    except Exception:
        return src


def iter_py_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield dirpath / name


def sha256_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def line_similarity(target_lines: set[str], candidate_lines: set[str]) -> float:
    if not target_lines or not candidate_lines:
        return 0.0
    intersection: set[str] = target_lines & candidate_lines
    union: set[str] = target_lines | candidate_lines
    return len(intersection) / len(union) * 100


def fmt_score(score: float) -> str:
    return f"{BOLD_BLUE}{score:.1f}{RESET}"


@dataclass(slots=True)
class MatchResult:
    path: Path
    method: str
    score: float
    detail: str = ""


@dataclass(slots=True)
class AnalysisResult:
    target_path: Path
    sha256_matches: list[MatchResult] = field(default_factory=list)
    line_matches: list[MatchResult] = field(default_factory=list)
    ssdeep_matches: list[MatchResult] = field(default_factory=list)

    def all_results(self) -> list[MatchResult]:
        return self.sha256_matches + self.line_matches + self.ssdeep_matches


def analyze_similarity(
    target: Path,
    bin_dir: Path,
    top_n: int = 3,
    verbose: bool = False,
) -> AnalysisResult:
    target_src: str = target.read_text(encoding="utf-8", errors="replace")
    target_normalized: str = normalize_python(target_src)
    target_sha: str = sha256_hash(target_normalized)
    target_lines: set[str] = set(target_normalized.splitlines())
    target_ssdeep: str = ssdeep.hash(target_normalized)
    result: AnalysisResult = AnalysisResult(target_path=target)
    sha_matches: list[MatchResult] = []
    line_matches: list[MatchResult] = []
    ssdeep_matches: list[MatchResult] = []
    for candidate in iter_py_files(bin_dir):
        if candidate.resolve() == target.resolve():
            continue
        try:
            candidate_src: str = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            if verbose:
                print(f"  [skip] {candidate.name}: unreadable", file=sys.stderr)
            continue
        candidate_normalized: str = normalize_python(candidate_src)
        candidate_sha: str = sha256_hash(candidate_normalized)
        sha_match: bool = candidate_sha == target_sha
        if verbose:
            print(f"  [sha256] {candidate.name}: {'MATCH' if sha_match else 'no match'}")
        if sha_match:
            sha_matches.append(
                MatchResult(
                    path=candidate,
                    method="sha256",
                    score=100.0,
                    detail="exact match after normalization",
                )
            )
            continue
        candidate_lines: set[str] = set(candidate_normalized.splitlines())
        line_score: float = line_similarity(target_lines, candidate_lines)
        if verbose:
            print(
                f"  [line]   {candidate.name}: {line_score:.1f}% ({len(target_lines & candidate_lines)} common lines)"
            )
        if line_score > 0:
            line_matches.append(
                MatchResult(
                    path=candidate,
                    method="line",
                    score=line_score,
                    detail=f"{len(target_lines & candidate_lines)} common lines",
                )
            )
        try:
            candidate_ssdeep: str = ssdeep.hash(candidate_normalized)
            fuzzy_score: int = ssdeep.compare(target_ssdeep, candidate_ssdeep)
            if verbose:
                print(f"  [ssdeep] {candidate.name}: {fuzzy_score}/100")
            if fuzzy_score > 0:
                ssdeep_matches.append(
                    MatchResult(
                        path=candidate,
                        method="ssdeep",
                        score=float(fuzzy_score),
                        detail=f"fuzzy score {fuzzy_score}/100",
                    )
                )
        except Exception as e:
            if verbose:
                print(f"  [ssdeep] {candidate.name}: error ({e})", file=sys.stderr)
            continue
    result.sha256_matches = sorted(sha_matches, key=lambda m: -m.score)[:top_n]
    result.line_matches = sorted(line_matches, key=lambda m: -m.score)[:top_n]
    result.ssdeep_matches = sorted(ssdeep_matches, key=lambda m: -m.score)[:top_n]
    return result


def print_results(result: AnalysisResult) -> None:
    print(f"\nTarget: {result.target_path.name}")
    print("=" * 60)
    if result.sha256_matches:
        print("\n[1] SHA-256 Exact Matches:")
        for m in result.sha256_matches:
            print(f"  {fmt_score(m.score)}%  {m.path.name}")
    if result.line_matches:
        print("\n[2] Line Similarity:")
        for m in result.line_matches:
            print(f"  {fmt_score(m.score)}%  {m.path.name}")
    if result.ssdeep_matches:
        print("\n[3] ssdeep Fuzzy Hash Similarity:")
        for m in result.ssdeep_matches:
            print(f"  {fmt_score(m.score)}%  {m.path.name}")
    if not result.all_results():
        print("\nNo similar files found.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog=Path(argv[0]).name,
        description="Find similar .py files in ~/bin using multiple methods.",
    )
    ap.add_argument("file", type=Path, help="target Python file to compare")
    ap.add_argument(
        "--bin-dir",
        type=Path,
        default=Path.home() / "bin",
        help="directory to search (default: ~/bin)",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=3,
        help="number of results per method (default: 3)",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show each comparison result live as it's computed",
    )
    return ap.parse_args(argv[1:])


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    args = parse_args(argv)
    target: Path = args.file.expanduser().resolve()
    if not target.is_file():
        print(f"error: not a file: {target}", file=sys.stderr)
        return 1
    bin_dir: Path = args.bin_dir.expanduser().resolve()
    if not bin_dir.is_dir():
        print(f"error: no such directory: {bin_dir}", file=sys.stderr)
        return 1
    if args.verbose:
        print(f"Comparing {target.name} against files in {bin_dir}...")
    result: AnalysisResult = analyze_similarity(target, bin_dir, args.top, verbose=args.verbose)
    print_results(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
