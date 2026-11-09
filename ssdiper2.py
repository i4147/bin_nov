#!/data/data/com.termux/files/usr/bin/env python
"""Group similar files by ssdeep hash with minified function names.
Computes the ssdeep digests, keeps the pairs above the similarity threshold in ``simz.json`` and optionally moves each related cluster into a ``groupNNN`` subdirectory (``-g``).
The internal helpers carry single-letter names from an earlier size-optimisation pass.
Usage: ssdiper2 [-g] Options: -g, --group-similar Move similar files into group001, group002, ...
subdirs based on simz.json Examples: $ ssdiper2 -g Command-line script: run ``python ssdiper2.py`` or the ``ssdiper2`` command.
The module is importable as well."""

import argparse
from collections import defaultdict
import json
import operator
from pathlib import Path
from typing import Any, Iterable, TextIO

from dh import get_files
import ssdeep


def X(h: str, M=1):
    try:
        if h.stat().st_size < M:
            return None
        with h.open("rb") as d:
            s = d.read()
            if len(s) < M:
                return None
            return ssdeep.hash(s)
    except FileNotFoundError:
        print(f"Error: File not found at {h}")
        return None
    except OSError as c:
        print(f"OS error accessing {h}: {c}")
        return None
    except Exception as c:
        print(f"An unexpected error occurred for {h}: {c}")
        return None


def Z(ag: Iterable[Any], ai=70):
    t = {}
    for h in ag:
        I = X(h)
        if I:
            t[str(h)] = I
    D = []
    cwd = Path.cwd()
    w = list(t.keys())
    for L in range(len(w)):
        for ad in range(L + 1, len(w)):
            u = w[L]
            v = w[ad]
            aa = t[u]
            ab = t[v]
            try:
                R = ssdeep.compare(aa, ab)
                if ai <= R:
                    D.append({
                        "file1": str(Path(u).relative_to(cwd)),
                        "file2": str(Path(v).relative_to(cwd)),
                        "similarity_score": R,
                    })
            except ssdeep.error as c:
                print(f"Error comparing hashes for {u} and {v}: {c}")
            except Exception as c:
                print(f"An unexpected error occurred during comparison for {u} and {v}: {c}")
    D.sort(key=operator.itemgetter("similarity_score"), reverse=True)
    return D


def ah(s: TextIO, J="simz.json") -> None:
    try:
        with Path(J).open("w", encoding="utf-8") as d:
            json.dump(s, d, indent=2, ensure_ascii=False)
    except Exception as c:
        print(f"Error saving data to JSON file '{J}': {c}")


def U(name: str):
    C = Path(name)
    if C.stem.endswith("_1"):
        return C.stem[:-2] + C.suffix
    return name


def V(k: Any, l: Any) -> bool:
    return U(k) == l or U(l) == k


def group_similar(ae="simz.json"):
    cwd = Path.cwd()
    y = cwd / ae
    if not y.exists():
        print(f"{y} not found")
        return
    try:
        Q = json.loads(y.read_text(encoding="utf-8"))
    except Exception as c:
        print(f"Error reading {y}: {c}")
        return
    m = set()
    for q in Q:
        m.add(q["file1"])
        m.add(q["file2"])
    o = {d: d for d in m}

    def B(r: int):
        n = r
        while o[n] != n:
            n = o[n]
        while o[r] != n:
            o[r], r = (n, o[r])
        return n

    def aj(k: Any, l: Any) -> None:
        O, P = (B(k), B(l))
        if O != P:
            o[O] = P

    for q in Q:
        k, l = (q["file1"], q["file2"])
        if V(k, l):
            aj(k, l)
    H = defaultdict(list)
    for d in m:
        H[B(d)].append(d)
    z = [sorted(K) for K in H.values() if len(K) > 1]
    z.sort()
    for ac, Y in enumerate(z, start=1):
        E = cwd / f"group{ac:03d}"
        E.mkdir(exist_ok=True)
        for name in Y:
            T = cwd / name
            A = E / name
            if not T.exists():
                print(f"[skip] {name}: not found in {cwd}")
                continue
            if A.exists():
                print(f"[skip] {name}: already exists at {A}")
                continue
            T.rename(A)
            print(f"[move] {name} -> {E.name}/")
    print(f"\nDone. Created {len(z)} group(s).")


def af() -> None:
    N = argparse.ArgumentParser()
    N.add_argument(
        "-g",
        "--group-similar",
        action="store_true",
        help="Move similar files into group001, group002, ... subdirs based on simz.json",
    )
    W = N.parse_args()
    cwd = Path.cwd()
    F = 0
    G = "simz.json"
    m = get_files(cwd)
    if not m:
        print("No files found matching the criteria in the specified directory.")
    else:
        S = Z(m, F)
        if S:
            ah(S, G)
        else:
            print(f"\nNo files found with similarity >= {F}%.")
    if W.group_similar:
        group_similar(G)


if __name__ == "__main__":
    af()
