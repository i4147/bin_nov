#!/usr/bin/env python3
"""Annotation coverage report for the root scripts.

    python arena/annotation_stats.py                 # summary table
    python arena/annotation_stats.py --top 20        # files with the most gaps
    python arena/annotation_stats.py --json s.json    # machine readable

``self`` / ``cls`` are excluded from the "missing" counts on purpose: PEP 484
and PEP 673 say instance and class method receivers stay unannotated, so they
would otherwise dominate the numbers.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF_NAMES = {"self", "cls", "mcs"}


def function_stats(tree: ast.Module) -> dict:
    out = {"functions": 0, "params": 0, "params_annotated": 0, "returns_annotated": 0}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        out["functions"] += 1
        args = node.args
        positional = args.posonlyargs + args.args
        all_args = (
            args.posonlyargs
            + args.args
            + args.kwonlyargs
            + ([args.vararg] if args.vararg else [])
            + ([args.kwarg] if args.kwarg else [])
        )
        for i, arg in enumerate(all_args):
            receiver = arg in positional and positional and positional[0] is arg and arg.arg in SELF_NAMES
            if receiver:
                continue
            out["params"] += 1
            if arg.annotation is not None:
                out["params_annotated"] += 1
        if node.returns is not None:
            out["returns_annotated"] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--top", type=int, default=0, help="list the N files with the most gaps")
    ap.add_argument("--json", default="", help="write the report as JSON")
    args = ap.parse_args(argv)

    totals = {
        "files": 0,
        "functions": 0,
        "params": 0,
        "params_annotated": 0,
        "returns_annotated": 0,
        "files_with_gaps": 0,
    }
    per_file = []
    for path in sorted(REPO_ROOT.glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            print(f"!! {path.name}: {e}")
            continue
        st = function_stats(tree)
        totals["files"] += 1
        for k in ("functions", "params", "params_annotated", "returns_annotated"):
            totals[k] += st[k]
        gaps = (st["params"] - st["params_annotated"]) + (st["functions"] - st["returns_annotated"])
        if gaps:
            totals["files_with_gaps"] += 1
            per_file.append({"file": path.name, "gaps": gaps, **st})
    per_file.sort(key=lambda r: -r["gaps"])

    p = totals["params"] or 1
    f = totals["functions"] or 1
    print(f"files                     {totals['files']}")
    print(f"functions                 {totals['functions']}")
    print(f"parameters                {totals['params']}")
    print(f"  annotated               {totals['params_annotated']} ({totals['params_annotated'] / p:.1%})")
    print(f"  missing                 {totals['params'] - totals['params_annotated']}")
    print(f"return annotations        {totals['returns_annotated']} ({totals['returns_annotated'] / f:.1%})")
    print(f"files with remaining gaps {totals['files_with_gaps']}")

    if args.top:
        print(f"\n{'gaps':>5}  {'funcs':>5}  {'params':>6}  file")
        for row in per_file[: args.top]:
            print(f"{row['gaps']:>5}  {row['functions']:>5}  {row['params']:>6}  {row['file']}")
    if args.json:
        Path(args.json).write_text(json.dumps({"totals": totals, "files": per_file}, indent=1))
        print(f"\nreport: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
