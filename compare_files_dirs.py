#!/data/data/com.termux/files/usr/bin/env python
"""File and directory comparison utilities in one CLI.
Subcommands: ``a-b`` prints the differing lines of two files, ``compare-dirs``/``compare-dirz`` report the hashes unique to each tree, ``fcmp``/``same-file`` compare two paths, ``pdif`` diffs the Python-aware content, ``pycommon`` finds the shared lines and ``compare-move`` moves or copies the common files.
Usage: compare_files_dirs {a-b,compare-move,compare-dirs,...} [options] Positional arguments: first second source target dir1 dir2 directory file1 file2 Subcommands: a-b compare-move compare-dirs compare-dirz fcmp pdif pycommon same-file udiffer Options: --encoding --common-dir --yes --common-file --hash-chunk-size --dir1-output --common-output --only-in-dir1-output --base Examples: $ compare_files_dirs compare-dirs old/ new/ Command-line script: run ``python compare_files_dirs.py`` or the ``compare_files_dirs`` command.
The module is importable as well."""

import argparse
import difflib
import filecmp
import hashlib
import os
from pathlib import Path
import shutil
from string import Template
import sys
from typing import Any, Optional

try:
    from dh import cprint
except ImportError:

    def cprint(*args: Any, **kwargs: str) -> None:
        print(*args, **kwargs)


def _resolve(p: str) -> Path:
    return Path(Template(p).substitute(os.environ)).expanduser().resolve()


def _sha256_file(path: Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_hashes(root: Path, chunk_size: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for v in root.rglob("*"):
        if v.is_file():
            z = v.relative_to(root)
            result[str(z)] = _sha256_file(v, chunk_size)
    return result


def _read_lines_udiffer(path: str, encoding: Optional[str] = None) -> list[str]:
    if encoding:
        with open(path, encoding=encoding) as f:
            return f.readlines()
    try:
        with open(path) as f:
            return f.readlines()
    except UnicodeDecodeError:
        with open(path, encoding="utf_16") as f:
            return f.readlines()


def cmd_a_b(args: argparse.Namespace) -> int:
    e = Path(args.first)
    f = Path(args.second)
    d = {line.rstrip("\n") for line in f.read_text(encoding=args.encoding).splitlines()}
    c = e.read_text(encoding=args.encoding).splitlines(keepends=True)
    a = [line for line in c if line.rstrip("\n") not in d]
    b = e.with_suffix(e.suffix + ".tmp")
    b.write_text("".join(a), encoding=args.encoding)
    b.replace(e)
    return 0


def cmd_compare_move(args: argparse.Namespace) -> int:
    A = _resolve(args.source)
    B = _resolve(args.target)
    print(f"Source directory (first):{A}")
    print(f"Target directory (second):{B}")
    print("-" * 40)
    if not A.exists():
        print(f"Error:Source directory '{args.source}' (expanded to '{A}') does not exist.")
        return 1
    if not B.exists():
        print(f"Error:Target directory '{args.target}' (expanded to '{B}') does not exist.")
        return 1
    o = Path.cwd() / args.common_dir
    o.mkdir(exist_ok=True)
    print(f"Created/verified directory:{o}")
    g = {f.name for f in A.iterdir() if f.is_file()}
    h = {f.name for f in B.iterdir() if f.is_file()}
    c = g & h
    if not c:
        print("\nNo files found that exist in both directories.")
        return 0
    print(f"\nFound {len(c)} file(s) that exist in BOTH directories:")
    for u in sorted(c):
        l = (A / u).stat().st_size
        n = (B / u).stat().st_size
        p = "✓" if l == n else "⚠"
        print(f"  {p} {u} (source:{l} bytes,target:{n} bytes)")
    print("\n" + "=" * 40)
    if not args.yes:
        x = input(f"Move these {len(c)} common file(s) from source to '{o}'? (y/n):").lower()
        if x != "y":
            print("Operation cancelled.")
            return 0
    j = 0
    d: list[str] = []
    b: list[str] = []
    for u in sorted(c):
        k = A / u
        s = o / u
        m = B / u
        if k.stat().st_size != m.stat().st_size:
            b.append(u)
        if s.exists():
            D = s.stem
            F = s.suffix
            y = 1
            while s.exists():
                v = f"{D}_common{y}{F}"
                s = o / v
                y += 1
            print(f"\n  Note:'{u}' will be renamed to '{s.name}' to avoid conflict")
        try:
            shutil.move(str(k), str(s))
            print(f"  ✓ Moved:{u}->{s.name}")
            j += 1
        except Exception as e:
            print(f"  ✗ Error moving {u}:{e}")
            d.append(u)
    print("\n" + "=" * 40)
    print(f"Summary:Successfully moved {j} of {len(c)} common file(s)")
    if b:
        print(f"\n⚠ Warning:{len(b)} file(s) had different sizes in source vs target:")
        for u in b:
            print(f"-{u}")
        print("  (Files were still moved,but verify they are correct versions)")
    if d:
        print(f"\nFailed to move {len(d)} file(s):")
        for u in d:
            print(f"-{u}")
    if j > 0:
        print(f"\nMoved common files are located in:{o}")
        print(f"Note:These files still exist in the target directory:{B}")
    return 0


def cmd_compare_dirs(args: argparse.Namespace) -> int:
    K = Path.cwd()
    E = args.source.strip()
    F = args.target.strip()
    B = Path(E).expanduser() if "~" in E else Path(E)
    y = Path(F).expanduser() if "~" in F else Path(F)
    u = [p.name for p in B.glob("*") if p.is_file()]
    w = [p.name for p in y.glob("*") if p.exists() and p.is_file()]
    r = [B.resolve() / p for p in u if p in w]
    s = {str(B.resolve() / p): str(y.resolve() / p) for p in u if p in w}
    if r:
        for k in r:
            print(f"-{k}")
    else:
        print("no common files")
        return 1
    a = [p for p in u if p not in w]
    g = K / args.common_file
    g.write_text("\n".join([str(p) for p in r]))
    if not args.yes:
        J = input(f"delete from {E}  ? ")
    else:
        J = "y"
    if J == "y":
        for k, v in s.items():
            if _sha256_file(Path(k), args.hash_chunk_size) == _sha256_file(Path(v), args.hash_chunk_size):
                print(f"the files are identical \n{k}\n{v}")
                Path(k).unlink()
            else:
                print(f"similar name filed:\n{k}\n{v}\n")
    cprint("only in first")
    for p in a:
        print(p)
    return 0


def cmd_compare_dirz(args: argparse.Namespace) -> int:
    t = Path(args.dir1)
    u = Path(args.dir2)
    w = _dir_hashes(t, args.hash_chunk_size)
    x = _dir_hashes(u, args.hash_chunk_size)
    m: list[str] = []
    o: list[str] = []
    c: list[str] = []
    for l, B in w.items():
        if l not in x:
            c.append(l)
        elif x[l] == B:
            o.append(l)
        else:
            m.append(l)
    Path(args.dir1_output).write_text("\n".join(m), encoding="utf-8")
    Path(args.common_output).write_text("\n".join(o), encoding="utf-8")
    Path(args.only_in_dir1_output).write_text("\n".join(c), encoding="utf-8")
    print("Comparison complete. See dir1.txt,common.txt,only_in_dir1.txt")
    return 0


def cmd_fcmp(args: argparse.Namespace) -> int:
    base = Path(args.base).resolve() if args.base else Path.cwd()
    target = Path(args.directory)
    c = filecmp.dircmp(base, target)
    print(c.report_full_closure())
    return 0


def cmd_pdif(args: argparse.Namespace) -> int:
    h = args.file1
    j = args.file2
    try:
        with Path(h).open("r", encoding=args.encoding) as n, Path(j).open("r", encoding=args.encoding) as o:
            f = n.readlines()
            g = o.readlines()
    except FileNotFoundError as e:
        print(f"Error:{e}")
        return 1
    f = [l.rstrip("\n") for l in f]
    g = [l.rstrip("\n") for l in g]
    c: list[int] = []
    d: list[int] = []
    b = 0
    for i, l in enumerate(f):
        if l not in g:
            c.append(i + 1)
    for i, l in enumerate(g):
        if l not in f:
            d.append(i + 1)
    for l in f:
        if l in g:
            b += 1
    print(f"{h} :{len(f)}")
    print(f"{j} :{len(g)}")
    print(f"common:{b}")
    print(f"Number of different lines in File 1:{len(c)}")
    if c:
        print(f"Line numbers:{c}")
    print(f"Number of different lines in File 2:{len(d)}")
    if d:
        print(f"Line numbers:{d}")
    return 0


def cmd_pycommon(args: argparse.Namespace) -> int:
    g = Path(args.file1)
    h = Path(args.file2)
    if not g.exists() or not h.exists():
        print("Error:One or both files do not exist.")
        return 1
    with g.open("r", encoding=args.encoding) as m:
        f = {j.strip("\n") for j in m}
    e: list[str] = []
    k: set[str] = set()
    with h.open("r", encoding=args.encoding) as n:
        for j in n:
            b = j.strip("\n")
            if b in f and b not in k:
                e.append(b)
                k.add(b)
                print(b)
    return 0


def cmd_same_file(args: argparse.Namespace) -> int:
    try:
        result = Path(args.file1).samefile(args.file2)
    except FileNotFoundError:
        result = False
    except OSError as e:
        print(f"error:{e}", file=sys.stderr)
        result = False
    print(result)
    return 0 if result else 1


def cmd_udiffer(args: argparse.Namespace) -> int:
    j = _read_lines_udiffer(args.first, args.encoding)
    h = _read_lines_udiffer(args.second, args.encoding)
    i = list(difflib.unified_diff(j, h, fromfile=args.first, tofile=args.second))
    if i:
        sys.stdout.writelines(i)
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Merged file utilities.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("a-b")
    p.add_argument("first")
    p.add_argument("second")
    p.add_argument("--encoding", default="utf-8")
    p.set_defaults(func=cmd_a_b)
    p = sub.add_parser("compare-move")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--common-dir", default="common")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_compare_move)
    p = sub.add_parser("compare-dirs")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--common-file", default="common.txt")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--hash-chunk-size", type=int, default=32768)
    p.set_defaults(func=cmd_compare_dirs)
    p = sub.add_parser("compare-dirz")
    p.add_argument("dir1")
    p.add_argument("dir2")
    p.add_argument("--dir1-output", default="dir1.txt")
    p.add_argument("--common-output", default="common.txt")
    p.add_argument("--only-in-dir1-output", default="only_in_dir1.txt")
    p.add_argument("--hash-chunk-size", type=int, default=65536)
    p.set_defaults(func=cmd_compare_dirz)
    p = sub.add_parser("fcmp")
    p.add_argument("directory")
    p.add_argument("--base", default=None)
    p.set_defaults(func=cmd_fcmp)
    p = sub.add_parser("pdif")
    p.add_argument("file1")
    p.add_argument("file2")
    p.add_argument("--encoding", default="utf-8")
    p.set_defaults(func=cmd_pdif)
    p = sub.add_parser("pycommon")
    p.add_argument("file1")
    p.add_argument("file2")
    p.add_argument("--encoding", default="utf-8")
    p.set_defaults(func=cmd_pycommon)
    p = sub.add_parser("same-file")
    p.add_argument("file1")
    p.add_argument("file2")
    p.set_defaults(func=cmd_same_file)
    p = sub.add_parser("udiffer")
    p.add_argument("first")
    p.add_argument("second")
    p.add_argument("--encoding", default=None)
    p.set_defaults(func=cmd_udiffer)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
