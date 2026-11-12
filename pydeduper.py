#!/data/data/com.termux/files/usr/bin/env python
"""Consolidate the duplicate top-level definitions of a tree into ``utils/``.
Extracts the functions, classes and constants with ast (archives and compressed files included), groups the identical ones and the ``report``/``copy``/``move`` subcommands show the groups, copy one representative into ``utils/`` or patch the originals with the imports.
Usage: pydeduper {report,copy,move} [--dir DIR] [--min-occurs N] Subcommands: report Show duplicate groups without modifying anything copy Copy one representative of each duplicate group into utils/ move Copy into utils/ and patch the originals with imports Options: --dir Root directory to scan (default: current directory) --min-occurs Minimum occurrences to count as duplicate (default: 2) --match-mode How to decide duplicates (default: content) --const-mode Which top-level Assign nodes count as constants (default: all) --no-archives Do not scan zip/tar archives or compressed files --utils-dir Output directory (default: <root>/utils) --func-file File name for functions inside utils/ (default: funcs.py) --class-file File name for classes inside utils/ (default: classes.py) --const-file File name for constants inside utils/ (default: const.py) --log-level Logging level (default: INFO) ...
plus 2 more; run ``pydeduper --help`` for the full list.
Examples: $ pydeduper report --dir src $ pydeduper move --min-occurs 3 Command-line script: run ``python pydeduper.py`` or the ``pydeduper`` command.
The module is importable as well."""

import argparse
import ast
import bz2
from collections import defaultdict
from dataclasses import dataclass, field
import gzip
import hashlib
import lzma
from multiprocessing import Pool
from pathlib import Path
import sys
import tarfile
from typing import Any, Iterable, Iterator, Optional
import zipfile

from dh import MAX_WORKERS, SKIP_DIRS
from loguru import logger

try:
    from loguru import logger

    _HAS_LOGURU = True
except ImportError:
    _HAS_LOGURU = False
try:
    import zstandard as zstd

    _HAS_ZSTD = True
except ImportError:
    zstd = None
    _HAS_ZSTD = False
try:
    import brotli

    _HAS_BROTLI = True
except ImportError:
    try:
        import brotlicffi as brotli

        _HAS_BROTLI = True
    except ImportError:
        brotli = None
        _HAS_BROTLI = False
ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".zip",
    ".whl",
    ".tar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz2",
    ".txz",
)
COMPRESSED_SUFFIXES: tuple[str, ...] = (".gz", ".bz2", ".xz", ".lzma", ".zst", ".br")
PYTHON_SUFFIX: str = ".py"
KIND_ORDER: tuple[str, str, str] = ("func", "class", "const")
KIND_TO_FILE_DEFAULT: dict[str, str] = {"func": "funcs.py", "class": "classes.py", "const": "const.py"}
TYPEVAR_NAMES: set[str] = {"TypeVar", "NewType", "ParamSpec", "TypeVarTuple"}


@dataclass(slots=True)
class Source:
    origin: str
    text: str
    path: Optional[Path] = None


@dataclass(slots=True)
class Definition:
    kind: str
    name: str
    source: str
    content_hash: str
    origin: str
    lineno: int
    end_lineno: int
    imports: list[str] = field(default_factory=list)
    path: Optional[Path] = None


def _success(msg: str, *args: Any) -> None:
    if _HAS_LOGURU:
        logger.success(msg, *args)
    else:
        logger.info(msg, *args)


def _setup_logging(level: str, verbose: bool) -> None:
    lvl = "DEBUG" if verbose else level.upper()
    if _HAS_LOGURU:
        logger.remove()
        logger.add(
            sys.stderr,
            level=lvl,
            format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
            colorize=True,
        )
        logger.add(
            "/data/data/com.termux/files/home/tmp/apps/pydedup.log",
            level="DEBUG",
            rotation="5 MB",
            retention=3,
            encoding="utf-8",
        )
    else:
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def _safe_read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except OSError as exc:
            logger.error(f"cannot read {path}: {exc}")
            return None
    except OSError as exc:
        logger.error(f"cannot read {path}: {exc}")
        return None


def _decompress(path: Path) -> Optional[bytes]:
    suffix = path.suffix.lower()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.error(f"cannot read {path}: {exc}")
        return None
    try:
        if suffix == ".gz":
            return gzip.decompress(raw)
        if suffix == ".bz2":
            return bz2.decompress(raw)
        if suffix in (".xz", ".lzma"):
            return lzma.decompress(raw)
        if suffix == ".zst":
            if not _HAS_ZSTD:
                logger.warning(f"zstandard not installed; skipping {path}")
                return None
            return zstd.ZstdDecompressor().decompress(raw)
        if suffix == ".br":
            if not _HAS_BROTLI:
                logger.warning(f"brotli not installed; skipping {path}")
                return None
            return brotli.decompress(raw)
    except Exception as exc:
        logger.error(f"decompression failed for {path}: {exc}")
    return None


def _iter_zip(path: Path) -> Iterator[Source]:
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(PYTHON_SUFFIX):
                    continue
                try:
                    raw = zf.read(info)
                except Exception as exc:
                    logger.error(f"cannot read {path}::{info.filename}: {exc}")
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                yield Source(origin=f"{path}::{info.filename}", text=text, path=None)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.error(f"cannot open zip {path}: {exc}")


def _iter_tar(path: Path) -> Iterator[Source]:
    try:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.lower().endswith(PYTHON_SUFFIX):
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                try:
                    raw = fh.read()
                finally:
                    fh.close()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                yield Source(origin=f"{path}::{member.name}", text=text, path=None)
    except (tarfile.TarError, OSError) as exc:
        logger.error(f"cannot open tar {path}: {exc}")


def _iter_compressed(path: Path) -> Iterator[Source]:
    raw = _decompress(path)
    if raw is None:
        return
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    if ".py" in path.name or "def " in text or "class " in text:
        yield Source(origin=str(path), text=text, path=None)


def iter_sources(root: Path, include_archives: bool = True) -> Iterator[Source]:
    root = root.resolve()
    utils_dir = (root / "utils").resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(utils_dir)
            continue
        except ValueError:
            pass
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        name_lower = path.name.lower()
        if name_lower.endswith(PYTHON_SUFFIX):
            text = _safe_read_text(path)
            if text is not None:
                yield Source(origin=str(path), text=text, path=path)
            continue
        if not include_archives:
            continue
        full_suffixes = "".join(path.suffixes).lower()
        if full_suffixes.endswith((".zip", ".whl")):
            yield from _iter_zip(path)
        elif full_suffixes.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz")):
            yield from _iter_tar(path)
        elif full_suffixes.endswith(COMPRESSED_SUFFIXES):
            yield from _iter_compressed(path)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collect_imports(tree: ast.Module, node: ast.AST) -> list[str]:
    used: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            used.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            cur = sub
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                used.add(cur.id)
    result: list[str] = []
    seen: set[str] = set()
    for top in tree.body:
        if isinstance(top, ast.Import):
            if any((a.asname or a.name.split(".")[0]) in used for a in top.names):
                stmt = ast.unparse(top)
                if stmt not in seen:
                    seen.add(stmt)
                    result.append(stmt)
        elif isinstance(top, ast.ImportFrom) and any((a.asname or a.name) in used for a in top.names):
            stmt = ast.unparse(top)
            if stmt not in seen:
                seen.add(stmt)
                result.append(stmt)
    return result


def _is_literal_value(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal_value(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_literal_value(k) and _is_literal_value(v) for k, v in zip(node.keys, node.values))
    return False


def _is_typevar_call(node: ast.Assign) -> bool:
    value = node.value
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Name):
        return func.id in TYPEVAR_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in TYPEVAR_NAMES
    return False


def _const_names(node: ast.AST, mode: str) -> list[str]:
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names = [node.target.id]
    elif isinstance(node, ast.Assign):
        names = []
        for t in node.targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Tuple):
                names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
    else:
        return []
    if not names:
        return []
    if mode == "uppercase":
        if not all(n.isupper() for n in names):
            if not (isinstance(node, ast.Assign) and _is_typevar_call(node)):
                return []
    elif mode == "literal" and not (isinstance(node, ast.Assign) and _is_literal_value(node.value)):
        return []
    return names


def extract_definitions(
    text: str,
    origin: str,
    path: Optional[Path],
    const_mode: str = "all",
) -> list[Definition]:
    try:
        tree = ast.parse(text, filename=origin)
    except SyntaxError as exc:
        logger.warning(f"syntax error in {origin}: {exc}")
        return []
    out: list[Definition] = []
    for node in tree.body:
        kind: Optional[str] = None
        name = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind, name = "func", node.name
        elif isinstance(node, ast.ClassDef):
            kind, name = "class", node.name
        else:
            names = _const_names(node, const_mode)
            if names:
                kind, name = "const", names[0]
        if kind is None:
            continue
        try:
            src = ast.unparse(node)
        except Exception as exc:
            logger.error(f"unparse failed in {origin} for {name}: {exc}")
            continue
        out.append(
            Definition(
                kind=kind,
                name=name,
                source=src,
                content_hash=_sha256(src),
                origin=origin,
                lineno=node.lineno,
                end_lineno=node.end_lineno or node.lineno,
                imports=_collect_imports(tree, node),
                path=path,
            )
        )
    return out


def _worker(payload: tuple[str, str, Optional[Path], str]) -> list[Definition]:
    text, origin, path, const_mode = payload
    return extract_definitions(text, origin, path, const_mode)


def _collect_all(
    sources: list[Source],
    workers: int,
    const_mode: str,
) -> list[Definition]:
    payloads = [(s.text, s.origin, s.path, const_mode) for s in sources]
    if workers <= 1:
        out: list[Definition] = []
        for p in payloads:
            out.extend(_worker(p))
        return out
    with Pool(processes=workers) as pool:
        results = pool.map(_worker, payloads, chunksize=4)
    out = []
    for r in results:
        out.extend(r)
    return out


def group_duplicates(
    defs: Iterable[Definition],
    min_occurs: int = 2,
    match_mode: str = "content",
) -> dict[str, list[Definition]]:
    groups: dict[str, list[Definition]] = defaultdict(list)
    for d in defs:
        key = f"{d.kind}::{d.name}" if match_mode == "name" else d.content_hash
        groups[key].append(d)
    return {k: v for k, v in groups.items() if len(v) >= min_occurs}


def _read_existing_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"cannot parse {path}: {exc}")
        return set()
    hashes: set[str] = set()
    for node in tree.body:
        try:
            hashes.add(_sha256(ast.unparse(node)))
        except Exception:
            continue
    return hashes


def write_utils(
    groups: dict[str, list[Definition]],
    utils_dir: Path,
    func_file: str,
    class_file: str,
    const_file: str,
    dry_run: bool = False,
) -> dict[str, Path]:
    utils_dir.mkdir(parents=True, exist_ok=True)
    file_map = {"func": func_file, "class": class_file, "const": const_file}
    by_kind: dict[str, list[Definition]] = defaultdict(list)
    for group in groups.values():
        by_kind[group[0].kind].append(group[0])
    written: dict[str, Path] = {}
    for kind in KIND_ORDER:
        reps = by_kind.get(kind, [])
        if not reps:
            continue
        target = utils_dir / file_map[kind]
        existing = _read_existing_hashes(target)
        new_reps = [r for r in reps if r.content_hash not in existing]
        if not new_reps:
            logger.info(f"no new objects for {target}")
            continue
        deduped: list[Definition] = []
        seen_names: set[str] = set()
        for r in new_reps:
            if r.name in seen_names:
                logger.warning(f"name collision in {target.name}: '{r.name}' — skipping")
                continue
            seen_names.add(r.name)
            deduped.append(r)
        if not deduped:
            continue
        if not (target.exists() and target.stat().st_size > 0):
            imports: list[str] = []
            for r in deduped:
                imports.extend(r.imports)
            seen_imports: set[str] = set()
            uniq_imports: list[str] = []
            for imp in imports:
                if imp not in seen_imports:
                    seen_imports.add(imp)
                    uniq_imports.append(imp)
        if target.exists() and target.stat().st_size > 0:
            base = target.read_text(encoding="utf-8").rstrip() + "\n\n"
        else:
            base = '"""Auto-generated by pydedup."""\n\n'
            if uniq_imports:
                base += "\n".join(uniq_imports) + "\n\n"
        body = "\n\n".join(r.source for r in deduped) + "\n"
        text = base + body
        try:
            ast.parse(text)
        except SyntaxError as exc:
            logger.error(f"generated {target} has syntax errors — skipping: {exc}")
            continue
        if dry_run:
            logger.info(f"[dry-run] would write {len(deduped)} object(s) to {target}")
        else:
            target.write_text(text, encoding="utf-8")
            _success(f"wrote {len(deduped)} object(s) to {target}")
        written[kind] = target
    return written


def _insert_imports(lines: list[str], imports: list[str]) -> list[str]:
    if not imports:
        return lines
    text = "".join(lines)
    insert_at = 0
    try:
        tree = ast.parse(text)
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            insert_at = tree.body[0].end_lineno or tree.body[0].lineno
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                insert_at = max(insert_at, node.end_lineno or node.lineno)
    except SyntaxError:
        return imports + lines
    return lines[:insert_at] + imports + lines[insert_at:]


def _patch_file(
    path: Path,
    defs: list[Definition],
    utils_rel: Path,
    file_map: dict[str, str],
    dry_run: bool,
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error(f"cannot read {path}: {exc}")
        return
    lines = text.splitlines(keepends=True)
    for d in sorted(defs, key=lambda x: x.lineno, reverse=True):
        start = d.lineno - 1
        end = d.end_lineno
        if start < 0 or end > len(lines) or start >= end:
            logger.warning(f"invalid line range for '{d.name}' in {path}")
            continue
        del lines[start:end]
    names_by_kind: dict[str, list[str]] = defaultdict(list)
    for d in defs:
        names_by_kind[d.kind].append(d.name)
    import_lines: list[str] = []
    for kind in KIND_ORDER:
        names = names_by_kind.get(kind)
        if not names:
            continue
        module = file_map[kind].removesuffix(".py")
        full_module = ".".join(list(utils_rel.parts) + [module])
        names_sorted = ", ".join(sorted(set(names)))
        import_lines.append(f"from {full_module} import {names_sorted}\n")
    patched = _insert_imports(lines, import_lines)
    new_text = "".join(patched)
    try:
        ast.parse(new_text)
    except SyntaxError as exc:
        logger.error(f"patched {path} has syntax errors — original preserved: {exc}")
        return
    if dry_run:
        logger.info(f"[dry-run] would patch {path}: -{len(defs)} definition(s), +imports")
    else:
        path.write_text(new_text, encoding="utf-8")
        _success(f"patched {path}: -{len(defs)} definition(s)")


def patch_originals(
    groups: dict[str, list[Definition]],
    utils_dir: Path,
    root: Path,
    func_file: str,
    class_file: str,
    const_file: str,
    dry_run: bool = False,
) -> None:
    try:
        utils_rel = utils_dir.relative_to(root)
    except ValueError:
        utils_rel = Path("utils")
    file_map = {"func": func_file, "class": class_file, "const": const_file}
    by_file: dict[Path, list[Definition]] = defaultdict(list)
    for group in groups.values():
        for d in group:
            if d.path is None:
                logger.warning(f"cannot patch archive/compressed member {d.origin}")
                continue
            by_file[d.path].append(d)
    for path, defs in by_file.items():
        _patch_file(path, defs, utils_rel, file_map, dry_run)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dir",
        type=Path,
        default=Path(),
        help="root directory to scan (default: current directory)",
    )
    p.add_argument(
        "--min-occurs",
        type=int,
        default=2,
        help="minimum occurrences to count as duplicate (default: 2)",
    )
    p.add_argument(
        "--match-mode",
        choices=("content", "name"),
        default="content",
        help="how to decide duplicates (default: content)",
    )
    p.add_argument(
        "--const-mode",
        choices=("all", "uppercase", "literal"),
        default="all",
        help="which top-level Assign nodes count as constants (default: all)",
    )
    p.add_argument(
        "--no-archives",
        action="store_true",
        help="do not scan zip/tar archives or compressed files",
    )
    p.add_argument(
        "--utils-dir",
        type=Path,
        default=None,
        help="output directory (default: <root>/utils)",
    )
    p.add_argument(
        "--func-file",
        default="funcs.py",
        help="file name for functions inside utils/ (default: funcs.py)",
    )
    p.add_argument(
        "--class-file",
        default="classes.py",
        help="file name for classes inside utils/ (default: classes.py)",
    )
    p.add_argument(
        "--const-file",
        default="const.py",
        help="file name for constants inside utils/ (default: const.py)",
    )
    p.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default="INFO",
        help="logging level (default: INFO)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="shortcut for --log-level DEBUG",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would happen without writing anything",
    )


def _resolve_utils_dir(args: argparse.Namespace) -> Path:
    if args.utils_dir is not None:
        return args.utils_dir.resolve()
    return (args.dir / "utils").resolve()


def _load_sources(args: argparse.Namespace) -> list[Source]:
    root = args.dir.resolve()
    if not root.exists():
        logger.error(f"directory does not exist: {root}")
        return []
    if not root.is_dir():
        logger.error(f"not a directory: {root}")
        return []
    include_archives = not args.no_archives
    sources = list(iter_sources(root, include_archives=include_archives))
    logger.info(f"scanned {root}: {len(sources)} source unit(s)")
    return sources


def _extract_and_group(
    args: argparse.Namespace,
) -> tuple[list[Definition], dict[str, list[Definition]]]:
    sources = _load_sources(args)
    if not sources:
        return [], {}
    defs = _collect_all(sources, MAX_WORKERS, args.const_mode)
    logger.info(f"extracted {len(defs)} top-level definition(s)")
    groups = group_duplicates(defs, args.min_occurs, args.match_mode)
    logger.info(f"found {len(groups)} duplicate group(s) (min-occurs={args.min_occurs}, match-mode={args.match_mode})")
    return defs, groups


def cmd_report(args: argparse.Namespace) -> int:
    _, groups = _extract_and_group(args)
    if not groups:
        _success("no duplicates found")
        return 0
    total = sum(len(g) for g in groups.values())
    _success(f"{len(groups)} duplicate group(s) covering {total} definition(s)")
    for key, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[1][0].kind, kv[1][0].name)):
        rep = group[0]
        header = f"{rep.kind} '{rep.name}' — {len(group)} occurrence(s)"
        print()
        print("=" * len(header))
        print(header)
        print("=" * len(header))
        for d in group:
            loc = f"{d.origin}:{d.lineno}-{d.end_lineno}"
            print(f"  [{d.content_hash[:12]}] {loc}")
        if len({d.content_hash for d in group}) > 1:
            print(f"  note: grouped by {args.match_mode}, contents differ")
    return 0


def cmd_copy(args: argparse.Namespace) -> int:
    _, groups = _extract_and_group(args)
    if not groups:
        _success("no duplicates to copy")
        return 0
    utils_dir = _resolve_utils_dir(args)
    written = write_utils(
        groups,
        utils_dir,
        args.func_file,
        args.class_file,
        args.const_file,
        dry_run=args.dry_run,
    )
    if not written:
        logger.info("nothing new to write")
        return 0
    verb = "would write" if args.dry_run else "wrote"
    _success(f"{verb} {len(written)} file(s) into {utils_dir}")
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    _, groups = _extract_and_group(args)
    if not groups:
        _success("no duplicates to move")
        return 0
    utils_dir = _resolve_utils_dir(args)
    written = write_utils(
        groups,
        utils_dir,
        args.func_file,
        args.class_file,
        args.const_file,
        dry_run=args.dry_run,
    )
    patchable: dict[str, list[Definition]] = {key: group for key, group in groups.items() if group[0].kind in written}
    if not patchable:
        logger.warning("nothing was written; skipping originals patching")
        return 0
    patch_originals(
        patchable,
        utils_dir,
        args.dir.resolve(),
        args.func_file,
        args.class_file,
        args.const_file,
        dry_run=args.dry_run,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pydedup",
        description=(
            "Find duplicate top-level Python definitions (functions, classes, "
            "constants) across a tree and consolidate them into utils/."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    p_report = subparsers.add_parser(
        "report",
        help="show duplicate groups without modifying anything",
        description="Scan and print duplicate definitions. No files are written.",
    )
    _add_common_args(p_report)
    p_report.set_defaults(func=cmd_report)
    p_copy = subparsers.add_parser(
        "copy",
        help="copy one representative of each duplicate group into utils/",
        description=(
            "Scan, group duplicates, and append one representative per group "
            "into utils/funcs.py, utils/classes.py and/or utils/const.py. "
            "Originals are left untouched."
        ),
    )
    _add_common_args(p_copy)
    p_copy.set_defaults(func=cmd_copy)
    p_move = subparsers.add_parser(
        "move",
        help="copy into utils/ and patch the originals with imports",
        description=(
            "Like `copy`, but also removes the moved definitions from each "
            "origin file and inserts `from utils.<mod> import <name>` at the "
            "top. Files that fail to re-parse are left untouched."
        ),
    )
    _add_common_args(p_move)
    p_move.set_defaults(func=cmd_move)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level, args.verbose)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        logger.warning("interrupted by user")
        return 130
    except BrokenPipeError:
        return 0
    except Exception as exc:
        if _HAS_LOGURU:
            logger.exception(f"fatal: {exc}")
        else:
            logger.exception(f"fatal: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
