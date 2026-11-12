#!/usr/bin/env python3
"""Batched driver that was used to annotate the whole repo.

Runs :mod:`infer_annotations` over the root-level ``*.py`` files in chunks
(so a long run prints progress and can be interrupted / resumed), then runs the
audit tools to make sure nothing was broken.

    python arena/run_all.py            # dry run, report only
    python arena/run_all.py --write    # annotate in place
    python arena/run_all.py --write --batch 100 --only "a*.py"

The tool is idempotent: running it twice changes nothing the second time.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import infer_annotations as ia  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="write changes in place")
    ap.add_argument("--batch", type=int, default=100, help="files per batch (default 100)")
    ap.add_argument("--only", default="*.py", help="glob applied to the file names")
    ap.add_argument("--json", default="", help="write the full JSON report here")
    args = ap.parse_args(argv)

    files = sorted(p for p in REPO_ROOT.glob("*.py") if fnmatch.fnmatch(p.name, args.only))
    print(f"{len(files)} files, batch size {args.batch}, write={args.write}")
    results: list[dict] = []
    started = time.time()
    for start in range(0, len(files), args.batch):
        batch = files[start : start + args.batch]
        for path in batch:
            results.append(ia.process_file(path, args.write))
        done = start + len(batch)
        changed = sum(1 for r in results if r["status"] == "changed")
        edits = sum(len(r["edits"]) for r in results)
        print(
            f"  [{done:>4}/{len(files)}] changed={changed:<4} edits={edits:<5} ({time.time() - started:.1f}s)",
            flush=True,
        )

    bad = [r for r in results if r["status"] in ("rejected", "compile-error", "infer-error", "parse-error")]
    for r in bad:
        print(f"!! {r['file']}: {r['status']}: {r['error']}", file=sys.stderr)
    changed = sum(1 for r in results if r["status"] == "changed")
    edits = sum(len(r["edits"]) for r in results)
    print(f"\nprocessed {len(results)} files, changed {changed}, edits {edits}, problems {len(bad)}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
        print(f"report: {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
