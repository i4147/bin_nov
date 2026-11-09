#!/usr/bin/env python3
"""Accuracy harness: does the inferencer recover the annotations already in a file?

Note that the annotations in this repository were themselves produced by this
tool, so treat the numbers as "how stable is the inference", not as a blind
benchmark against an independent annotator.  For every file in the sample:

1. parse it and record the annotations that are already there (the "truth");
2. strip every annotation and ``ast.unparse`` the result, giving a clean,
   annotation-free version of the same program in a temp directory;
3. run the annotator over that copy;
4. compare what it produced with the truth, per parameter and per return.

Because step 2 goes through ``ast.unparse`` the numbers are a lower bound:
formatting, comments and multi-line signatures are lost, and some hints that a
human could infer from context simply disappear.

    python arena/measure.py --sample 60
    python arena/measure.py --files addel.py valhtml.py --show-misses
"""

from __future__ import annotations

import argparse
import ast
import copy
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import infer_annotations as ia  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def collect_truth(tree: ast.Module) -> dict[str, str]:
    """Map ``qualname`` (params) / ``qualname()`` (return) to the annotation text."""
    truth: dict[str, str] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{node.name}"
                args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                args += [a for a in (node.args.vararg, node.args.kwarg) if a]
                for arg in args:
                    if arg.arg in ("self", "cls") or arg.annotation is None:
                        continue
                    truth[f"{qual}:{arg.arg}"] = ast.unparse(arg.annotation)
                if node.returns is not None:
                    truth[f"{qual}:return"] = ast.unparse(node.returns)
                visit(node.body, f"{qual}.")
            elif isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    return truth


def strip_source(tree: ast.Module) -> str:
    tree = copy.deepcopy(tree)
    for n in ast.walk(tree):
        if hasattr(n, "annotation"):
            n.annotation = None
        if hasattr(n, "returns"):
            n.returns = None
    for parent in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(parent, attr, None)
            if isinstance(body, list) and body:
                out = []
                for stmt in body:
                    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                        out.append(ast.Assign(targets=[stmt.target], value=stmt.value))
                    elif isinstance(stmt, ast.AnnAssign):
                        continue
                    else:
                        out.append(stmt)
                if not out:  # e.g. a dataclass whose only body was Declarations
                    out.append(ast.Pass())
                setattr(parent, attr, out)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def norm(text: str) -> str:
    text = text.replace("typing.", "").replace(" ", "")
    text = text.replace("Optional[", "|None[")  # crude, only for stable comparison
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=50, help="random files (default 50)")
    ap.add_argument("--files", nargs="*", default=[], help="explicit files to measure")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args(argv)

    if args.files:
        paths = [REPO_ROOT / f for f in args.files]
    else:
        pool = sorted(REPO_ROOT.glob("*.py"))
        random.Random(args.seed).shuffle(pool)
        paths = pool[: args.sample]

    n_truth = n_hit = n_extra = n_wrong = 0
    misses: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="measure-"))
    try:
        for path in paths:
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            truth = collect_truth(tree)
            if not truth:
                continue
            stripped = tmp / path.name
            stripped.write_text(strip_source(tree))
            ia.process_file(stripped, write=True)
            got = collect_truth(ast.parse(stripped.read_text()))
            n_truth += len(truth)
            for key, want in truth.items():
                if key not in got:
                    misses.append(f"MISS  {path.name}: {key} -> {want}")
                    continue
                have = got[key]
                if norm(have) == norm(want):
                    n_hit += 1
                else:
                    n_wrong += 1
                    misses.append(f"DIFF  {path.name}: {key} want {want!r} got {have!r}")
            n_extra += len(set(got) - set(truth))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    pct = (n_hit / n_truth * 100) if n_truth else 0.0
    any_pct = ((n_hit + n_wrong) / n_truth * 100) if n_truth else 0.0
    print(f"files                    {len(paths)}")
    print(f"annotations in truth     {n_truth}")
    print(f"  annotated at all       {n_hit + n_wrong} ({any_pct:.1f}%)")
    print(f"  recovered exactly      {n_hit} ({pct:.1f}%)")
    print(f"  recovered differently  {n_wrong}")
    print(f"  not inferred           {n_truth - n_hit - n_wrong}")
    print(f"annotations beyond truth {n_extra}")
    if args.show_misses:
        for line in misses[:80]:
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
