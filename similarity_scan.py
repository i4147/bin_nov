#!/data/data/com.termux/files/usr/bin/env python
"""Read-only similarity scan over root-level .py files (Phase 2 helper).

Method
------
1. For each root *.py file, produce a normalized token stream with the
   stdlib `tokenize` module: COMMENT/NL/NEWLINE/INDENT/DEDENT/ENCODING/
   ENDMARKER dropped; string literals -> STR, numbers -> NUM, other
   NAME/OP tokens kept verbatim. This measures *structural* similarity
   rather than literal/string similarity.
2. Build 6-gram shingles of the token stream, hash them, and index them
   inverted (shingle -> files). Candidate pairs are those sharing >= 12
   shingles (cheap pre-filter; the ~267k-pair space is mostly noise).
3. For each candidate pair compute exact Jaccard |A&B|/|A|B| over shingle
   sets, plus containment |A&B|/min(|A|,|B|). Shingle Jaccard is an
   estimate of token-level similarity, not a difflib ratio; we say so in
   the report.
4. Near-exact duplicates: identical SHA256 of the byte content (exact)
   and identical sha256 of the normalized token stream (structural twins).
5. Names are clustered by shared alphabetic prefix families purely for
   presentation; the numbers always come from the shingle comparison.

Excluded: no recursion needed (repo root is flat; only root *.py files).
No vendored/build/cache dirs exist at root. .git excluded by construction.
"""

from __future__ import annotations
import collections
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tokenize


ROOT = Path.cwd().resolve()
SHINGLE = 6
MIN_SHARED_SHINGLES = 12

tokenized: dict[str, list[str]] = {}
contents: dict[str, str] = {}
errors: list[str] = []

for f in sorted(ROOT.glob("*.py")):
    name = f.name
    try:
        raw = f.read_bytes()
        contents[name] = hashlib.sha256(raw).hexdigest()
        toks: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(raw.decode("utf-8", errors="replace")).readline):
            ttype, tstr = tok.type, tok.string
            if ttype in (
                tokenize.COMMENT,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
                tokenize.ENDMARKER,
            ):
                continue
            if ttype == tokenize.STRING:
                toks.append("STR")
            elif ttype == tokenize.NUMBER:
                toks.append("NUM")
            else:
                toks.append(tstr)
        tokenized[name] = toks
    except Exception as e:  # noqa: BLE001 - report and continue
        errors.append(f"{name}: {e}")

print(f"files tokenized: {len(tokenized)}  errors: {len(errors)}")
for e in errors:
    print("ERR", e)

# exact byte duplicates
by_hash: dict[str, list[str]] = collections.defaultdict(list)
for name, h in contents.items():
    by_hash[h].append(name)
exact_dupes = {h: sorted(v) for h, v in by_hash.items() if len(v) > 1}

# structural twins (identical normalized token stream)
by_tok: dict[str, list[str]] = collections.defaultdict(list)
for name, toks in tokenized.items():
    by_tok[hashlib.sha256(" ".join(toks).encode()).hexdigest()].append(name)
struct_twins = {h: sorted(v) for h, v in by_tok.items() if len(v) > 1}

# shingle sets
shingle_sets: dict[str, set[int]] = {}
for name, toks in tokenized.items():
    if len(toks) < SHINGLE:
        shingle_sets[name] = {hash(tuple(toks))} if toks else set()
    else:
        shingle_sets[name] = {hash(tuple(toks[i : i + SHINGLE])) for i in range(len(toks) - SHINGLE + 1)}

inv: dict[int, list[str]] = collections.defaultdict(list)
for name, sh in shingle_sets.items():
    for s in sh:
        inv[s].append(name)

pair_hits: collections.Counter[tuple[str, str]] = collections.Counter()
for s, names in inv.items():
    if len(names) > 40:  # pathologically common shingle (e.g. tiny files); still fine to count, but skip to save memory
        continue
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            key = (names[i], names[j]) if names[i] < names[j] else (names[j], names[i])
            pair_hits[key] += 1

pairs = []
for (a, b), shared in pair_hits.items():
    if shared < MIN_SHARED_SHINGLES:
        continue
    A, B = shingle_sets[a], shingle_sets[b]
    inter = len(A & B)
    union = len(A | B)
    jaccard = inter / union if union else 0.0
    containment = inter / min(len(A), len(B)) if min(len(A), len(B)) else 0.0
    pairs.append((jaccard, containment, a, b, inter))

pairs.sort(key=lambda p: (-p[0], -p[1]))
print(f"candidate pairs: {len(pairs)}")
print("EXACT_BYTE_DUPLICATES")
for h, names in exact_dupes.items():
    print(" ", ", ".join(names))
print("STRUCTURAL_TWINS")
for h, names in struct_twins.items():
    print(" ", ", ".join(names))
print("TOP_PAIRS jaccard containment a b shared")
for jaccard, containment, a, b, inter in pairs[:40]:
    print(f"{jaccard:.3f} {containment:.3f} {a} {b} {inter}")

with open(ROOT / "arena" / "similarity_raw.json", "w", encoding="utf-8") as fh:
    json.dump(
        {
            "exact_byte_duplicates": list(exact_dupes.values()),
            "structural_twins": list(struct_twins.values()),
            "pairs": [
                {
                    "jaccard": round(j, 4),
                    "containment": round(c, 4),
                    "a": a,
                    "b": b,
                    "shared": s,
                }
                for j, c, a, b, s in pairs
            ],
            "token_counts": {n: len(t) for n, t in tokenized.items()},
            "errors": errors,
        },
        fh,
        indent=2,
    )
print("wrote arena/similarity_raw.json")
