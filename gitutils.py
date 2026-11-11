#!/data/data/com.termux/files/usr/bin/env python
"""List the added, modified and deleted files of every commit across several git backends.
Walks the history through Dulwich, pygit2, GitPython or the ``git`` CLI, applies the date/author/path/mode filters and writes the records as JSON, JSONL, CSV, YAML or Markdown (``-f``), with ``--first-parent`` and ``--max-count`` support.
Usage: gitutils [repo_path] [-b BACKEND] [-f FORMAT] [--since DATE] [--path GLOB] Positional arguments: repo_path Path to the git repository (default: .) output_path Output file (- for stdout) Options: -b, --backend Git backend to use -f, --format Output format --branch Ref/branch to walk (default: HEAD) --since Only commits after this date (ISO 8601 or git-style) --until Only commits before this date --author Filter by author name or email (substring) --max-count Limit number of commits emitted --first-parent Follow only first parents --path Only include paths matching this glob (fnmatch) --exclude Exclude paths matching this glob (fnmatch) ...
plus 6 more; run ``gitutils --help`` for the full list.
Examples: $ gitutils .
-f csv -o history.csv --since 2025-01-01 Command-line script: run ``python gitutils.py`` or the ``gitutils`` command.
The module is importable as well."""

import argparse
import contextlib
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any, Callable, Iterator

from loguru import logger


LOG = logger
EXIT_OK: int = 0
EXIT_NO_COMMITS: int = 1
EXIT_USAGE: int = 2
EXIT_REPO: int = 3
BACKEND_CAPS: dict[str, dict[str, bool]] = {
    "dulwich": {"rename": False, "stats": False},
    "pygit2": {"rename": True, "stats": True},
    "gitpython": {"rename": True, "stats": True},
    "gitcli": {"rename": True, "stats": True},
}
CSV_FIELDS: list[str] = [
    "sha",
    "short",
    "author_name",
    "author_email",
    "committer_name",
    "committer_email",
    "authored_at",
    "committed_at",
    "message",
    "parents",
    "added",
    "modified",
    "deleted",
    "renamed",
    "files",
    "insertions",
    "deletions",
]


@dataclass(slots=True)
class Options:
    repo_path: str = "."
    branch: str = "HEAD"
    since: str | None = None
    until: str | None = None
    author: str | None = None
    max_count: int | None = None
    first_parent: bool = False
    path: str | None = None
    exclude: str | None = None
    mode: str = "changed"
    rename: bool = False


@dataclass(slots=True)
class CommitRecord:
    sha: str
    short: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    authored_at: str
    committed_at: str
    message: str
    parents: list[str]
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[dict[str, str]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def _parse_dt(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _passes_filters(rec: CommitRecord, opts: Options) -> bool:
    if opts.author:
        needle = opts.author.lower()
        hay = (rec.author_email + " " + rec.author_name).lower()
        if needle not in hay:
            return False
    if opts.since and _parse_dt(rec.authored_at) < _parse_dt(opts.since):
        return False
    return not (opts.until and _parse_dt(rec.authored_at) > _parse_dt(opts.until))


def _path_ok(path: str, opts: Options) -> bool:
    if opts.path and not fnmatch.fnmatch(path, opts.path):
        return False
    return not (opts.exclude and fnmatch.fnmatch(path, opts.exclude))


def _apply_mode(rec: CommitRecord, mode: str) -> CommitRecord:
    if mode == "added":
        rec.modified = []
        rec.deleted = []
        rec.renamed = []
    elif mode == "modified":
        rec.added = []
        rec.deleted = []
        rec.renamed = []
    elif mode == "deleted":
        rec.added = []
        rec.modified = []
        rec.renamed = []
    elif mode == "changed":
        rec.renamed = []
    return rec


def _sig_iso(t: int, offset_min: int) -> str:
    tz = timezone(timedelta(minutes=offset_min))
    return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(tz).isoformat()


def _parse_actor(raw: bytes | str) -> tuple[str, str]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if "<" in raw and ">" in raw:
        name, rest = raw.rsplit("<", 1)
        return name.strip(), rest.rstrip(">").strip()
    return raw.strip(), ""


def _iter_dulwich(opts: Options) -> Iterator[CommitRecord]:
    from dulwich.repo import Repo

    repo = Repo(opts.repo_path)
    try:
        ref = opts.branch
        try:
            head_sha = repo.refs[ref.encode()]
        except KeyError:
            try:
                head_sha = repo.refs[f"refs/heads/{ref}".encode()]
            except KeyError:
                head_sha = ref.encode()
        blob_cache: dict[bytes, dict[str, bytes]] = {}

        def tree_blobs(tree_sha: bytes) -> dict[str, bytes]:
            if tree_sha in blob_cache:
                return blob_cache[tree_sha]
            result: dict[str, bytes] = {}
            stack = [("", repo[tree_sha])]
            while stack:
                prefix, t = stack.pop()
                for item in t.iteritems():
                    name = item.path.decode() if isinstance(item.path, bytes) else item.path
                    full = prefix + name
                    if item.mode & 0o170000 == 0o040000:
                        stack.append((full + "/", repo[item.sha]))
                    else:
                        result[full] = item.sha
            blob_cache[tree_sha] = result
            return result

        emitted = 0

        def _build(commit: Any) -> CommitRecord:
            sha = commit.id.decode()
            an, ae = _parse_actor(commit.author)
            cn, ce = _parse_actor(commit.committer)
            parents = [p.decode() for p in commit.parents]
            parents_used = parents[:1] if (opts.first_parent and parents) else parents
            current = tree_blobs(commit.tree)
            parent_map: dict[str, bytes] = {}
            for p in parents_used:
                pc = repo[p.encode()]
                parent_map.update(tree_blobs(pc.tree))
            added, modified, deleted = [], [], []
            for path, sh in current.items():
                if not _path_ok(path, opts):
                    continue
                if path not in parent_map:
                    added.append(path)
                elif parent_map[path] != sh:
                    modified.append(path)
            for path in parent_map:
                if not _path_ok(path, opts):
                    continue
                if path not in current:
                    deleted.append(path)
            return CommitRecord(
                sha=sha,
                short=sha[:8],
                author_name=an,
                author_email=ae,
                committer_name=cn,
                committer_email=ce,
                authored_at=_sig_iso(commit.author_time, commit.author_timezone),
                committed_at=_sig_iso(commit.commit_time, commit.commit_timezone),
                message=commit.message.decode("utf-8", errors="replace").strip(),
                parents=parents,
                added=sorted(added),
                modified=sorted(modified),
                deleted=sorted(deleted),
                renamed=[],
                stats={"files": len(added) + len(modified) + len(deleted)},
            )

        if opts.first_parent:
            cur = head_sha.decode() if isinstance(head_sha, bytes) else head_sha
            seen: set[str] = set()
            while cur and cur not in seen:
                seen.add(cur)
                commit = repo[cur.encode()]
                rec = _build(commit)
                if _passes_filters(rec, opts):
                    yield _apply_mode(rec, opts.mode)
                    emitted += 1
                    if opts.max_count is not None and emitted >= opts.max_count:
                        break
                cur = commit.parents[0].decode() if commit.parents else None
        else:
            walker = repo.get_walker(include=[head_sha])
            for entry in walker:
                rec = _build(entry.commit)
                if _passes_filters(rec, opts):
                    yield _apply_mode(rec, opts.mode)
                    emitted += 1
                    if opts.max_count is not None and emitted >= opts.max_count:
                        break
    finally:
        repo.close()


def _iter_pygit2(opts: Options) -> Iterator[CommitRecord]:
    import pygit2

    repo = pygit2.Repository(opts.repo_path)
    try:
        ref = repo.revparse_single(opts.branch)
    except (KeyError, ValueError):
        ref = repo.revparse_single("HEAD")
    head_id = (
        ref.id if hasattr(ref, "id") and isinstance(ref.id, pygit2.Oid) else getattr(ref, "target", None) or ref.id
    )

    def _build(commit: Any) -> CommitRecord:
        parents = list(commit.parents)
        parents_used = parents[:1] if (opts.first_parent and parents) else parents
        if parents_used:
            diff = repo.diff(parents_used[0], commit, context_lines=0)
        else:
            diff = commit.tree.diff_to_tree(context_lines=0)
        if opts.rename:
            with contextlib.suppress(Exception):
                diff.find_similar()
        added, modified, deleted, renamed = [], [], [], []
        for patch in diff:
            d = patch.delta
            old_path = d.old_file.path
            new_path = d.new_file.path
            st = d.status
            if st == pygit2.GIT_DELTA_ADDED:
                if _path_ok(new_path, opts):
                    added.append(new_path)
            elif st == pygit2.GIT_DELTA_DELETED:
                if _path_ok(old_path, opts):
                    deleted.append(old_path)
            elif st == pygit2.GIT_DELTA_RENAMED:
                if _path_ok(new_path, opts):
                    renamed.append({"from": old_path, "to": new_path})
            elif st in (pygit2.GIT_DELTA_MODIFIED, pygit2.GIT_DELTA_COPIED) and _path_ok(new_path, opts):
                modified.append(new_path)
        try:
            s = diff.stats
            stats = {
                "files": s.files_changed,
                "insertions": s.insertions,
                "deletions": s.deletions,
            }
        except Exception:
            stats = {}
        return CommitRecord(
            sha=str(commit.id),
            short=str(commit.id)[:8],
            author_name=commit.author.name,
            author_email=commit.author.email,
            committer_name=commit.committer.name,
            committer_email=commit.committer.email,
            authored_at=_sig_iso(commit.author.time, commit.author.offset),
            committed_at=_sig_iso(commit.committer.time, commit.committer.offset),
            message=commit.message.strip(),
            parents=[str(p.id) for p in parents],
            added=sorted(added),
            modified=sorted(modified),
            deleted=sorted(deleted),
            renamed=renamed,
            stats=stats,
        )

    emitted = 0
    if opts.first_parent:
        cur = head_id
        seen: set[str] = set()
        while cur is not None:
            sid = str(cur)
            if sid in seen:
                break
            seen.add(sid)
            commit = repo[cur]
            rec = _build(commit)
            if _passes_filters(rec, opts):
                yield _apply_mode(rec, opts.mode)
                emitted += 1
                if opts.max_count is not None and emitted >= opts.max_count:
                    break
            cur = commit.parents[0].id if commit.parents else None
    else:
        for commit in repo.walk(head_id, pygit2.GIT_SORT_TIME):
            rec = _build(commit)
            if _passes_filters(rec, opts):
                yield _apply_mode(rec, opts.mode)
                emitted += 1
                if opts.max_count is not None and emitted >= opts.max_count:
                    break


def _iter_gitpython(opts: Options) -> Iterator[CommitRecord]:
    from git import Repo as GitRepo
    from git.objects.tree import NULL_TREE

    repo = GitRepo(opts.repo_path)
    kwargs: dict[str, Any] = {}
    if opts.first_parent:
        kwargs["first_parent"] = True
    try:
        commits = repo.iter_commits(opts.branch, **kwargs)
    except Exception:
        commits = iter(())
    emitted = 0
    for commit in commits:
        parents = list(commit.parents)
        parents_used = parents[:1] if (opts.first_parent and parents) else parents
        if parents_used:
            diffs = commit.diff(parents_used[0], create_patch=False, R=opts.rename)
        else:
            diffs = commit.diff(NULL_TREE, create_patch=False, R=opts.rename)
        added, modified, deleted, renamed = [], [], [], []
        for d in diffs:
            ct = d.change_type
            a = d.a_path
            b = d.b_path
            if ct == "A":
                if _path_ok(b, opts):
                    added.append(b)
            elif ct == "D":
                if _path_ok(a, opts):
                    deleted.append(a)
            elif ct == "R":
                if _path_ok(b, opts):
                    renamed.append({"from": a, "to": b})
            elif ct in ("M", "C", "T") and _path_ok(b, opts):
                modified.append(b)
        try:
            total = commit.stats.total
            stats = {
                "files": int(total.get("files", 0)),
                "insertions": int(total.get("insertions", 0)),
                "deletions": int(total.get("deletions", 0)),
            }
        except Exception:
            stats = {}
        rec = CommitRecord(
            sha=commit.hexsha,
            short=commit.hexsha[:8],
            author_name=commit.author.name,
            author_email=commit.author.email,
            committer_name=commit.committer.name,
            committer_email=commit.committer.email,
            authored_at=commit.authored_datetime.isoformat(),
            committed_at=commit.committed_datetime.isoformat(),
            message=commit.message.strip(),
            parents=[p.hexsha for p in parents],
            added=sorted(added),
            modified=sorted(modified),
            deleted=sorted(deleted),
            renamed=renamed,
            stats=stats,
        )
        if _passes_filters(rec, opts):
            yield _apply_mode(rec, opts.mode)
            emitted += 1
            if opts.max_count is not None and emitted >= opts.max_count:
                break


def _iter_gitcli(opts: Options) -> Iterator[CommitRecord]:
    args = [
        "git",
        "-C",
        opts.repo_path,
        "log",
        "--format=%x1e%H%x1f%h%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%aI%x1f%cI%x1f%s%x1f%P",
        "--name-status",
        "-z",
    ]
    args.append("-M" if opts.rename else "--no-renames")
    if opts.first_parent:
        args.append("--first-parent")
    if opts.since:
        args.append(f"--since={opts.since}")
    if opts.until:
        args.append(f"--until={opts.until}")
    if opts.author:
        args.append(f"--author={opts.author}")
    args.append(opts.branch)
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return
    emitted = 0
    for chunk in out.split("\x1e")[1:]:
        nl = chunk.find("\n")
        if nl < 0:
            continue
        header = chunk[:nl].split("\x1f")
        if len(header) < 10:
            continue
        sha, short, an, ae, cn, ce, ai, ci, msg, parents_str = header[:10]
        parents = parents_str.split() if parents_str.strip() else []
        tokens = chunk[nl + 1 :].split("\0")
        added, modified, deleted, renamed = [], [], [], []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if not tok:
                i += 1
                continue
            code = tok[0]
            if code in ("R", "C"):
                if i + 2 >= len(tokens):
                    break
                old = tokens[i + 1]
                new = tokens[i + 2]
                i += 3
                if code == "R":
                    if _path_ok(new, opts):
                        renamed.append({"from": old, "to": new})
                elif _path_ok(new, opts):
                    added.append(new)
            else:
                if i + 1 >= len(tokens):
                    break
                path = tokens[i + 1]
                i += 2
                if code == "A":
                    if _path_ok(path, opts):
                        added.append(path)
                elif code == "D":
                    if _path_ok(path, opts):
                        deleted.append(path)
                elif code in ("M", "T") and _path_ok(path, opts):
                    modified.append(path)
        rec = CommitRecord(
            sha=sha,
            short=short,
            author_name=an,
            author_email=ae,
            committer_name=cn,
            committer_email=ce,
            authored_at=ai,
            committed_at=ci,
            message=msg.strip(),
            parents=parents,
            added=sorted(added),
            modified=sorted(modified),
            deleted=sorted(deleted),
            renamed=renamed,
            stats={},
        )
        if _passes_filters(rec, opts):
            yield _apply_mode(rec, opts.mode)
            emitted += 1
            if opts.max_count is not None and emitted >= opts.max_count:
                break


BACKENDS: dict[str, Callable[[Options], Iterator[CommitRecord]]] = {
    "dulwich": _iter_dulwich,
    "pygit2": _iter_pygit2,
    "gitpython": _iter_gitpython,
    "gitcli": _iter_gitcli,
}


def iter_commits(repo_path: str = ".", backend: str = "dulwich", **kwargs: Any) -> Iterator[CommitRecord]:
    opts = Options(repo_path=repo_path, **kwargs)
    if backend not in BACKENDS:
        msg = f"unknown backend: {backend}"
        raise ValueError(msg)
    return BACKENDS[backend](opts)


def get_added_files_per_commit(repo_path: str = ".", backend: str = "dulwich", **kwargs: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for rec in iter_commits(repo_path=repo_path, backend=backend, **kwargs):
        result[rec.short] = sorted({p.split("/")[-1] for p in rec.added})
    return result


def _record_to_row(rec: CommitRecord) -> dict[str, Any]:
    return {
        "sha": rec.sha,
        "short": rec.short,
        "author_name": rec.author_name,
        "author_email": rec.author_email,
        "committer_name": rec.committer_name,
        "committer_email": rec.committer_email,
        "authored_at": rec.authored_at,
        "committed_at": rec.committed_at,
        "message": rec.message,
        "parents": ";".join(rec.parents),
        "added": ";".join(rec.added),
        "modified": ";".join(rec.modified),
        "deleted": ";".join(rec.deleted),
        "renamed": ";".join(f"{r['from']}->{r['to']}" for r in rec.renamed),
        "files": rec.stats.get("files", 0),
        "insertions": rec.stats.get("insertions", 0),
        "deletions": rec.stats.get("deletions", 0),
    }


def _write_json(records: Iterator[CommitRecord], out: Any) -> int:
    data = [asdict(r) for r in records]
    json.dump(data, out, indent=2, default=str)
    out.write("\n")
    return len(data)


def _write_jsonl(records: Iterator[CommitRecord], out: Any) -> int:
    n = 0
    for r in records:
        out.write(json.dumps(asdict(r), default=str) + "\n")
        n += 1
    return n


def _write_csv(records: Iterator[CommitRecord], out: Any) -> int:
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    n = 0
    for r in records:
        writer.writerow(_record_to_row(r))
        n += 1
    return n


def _write_yaml(records: Iterator[CommitRecord], out: Any) -> int:
    try:
        import yaml
    except ImportError as e:
        msg = f"yaml output requires pyyaml: {e}"
        raise SystemExit(msg)
    data = [asdict(r) for r in records]
    yaml.safe_dump(data, out, sort_keys=False, default_flow_style=False)
    return len(data)


def _write_markdown(records: Iterator[CommitRecord], out: Any) -> int:
    cols = [
        "short",
        "author_name",
        "authored_at",
        "added",
        "modified",
        "deleted",
        "renamed",
    ]
    out.write("| " + " | ".join(cols) + " |\n")
    out.write("| " + " | ".join("---" for _ in cols) + " |\n")
    n = 0
    for r in records:
        row = _record_to_row(r)
        cells = [str(row[c]).replace("|", "\\|") for c in cols]
        out.write("| " + " | ".join(cells) + " |\n")
        n += 1
    return n


def _write_table(records: Iterator[CommitRecord], out: Any) -> int:
    rows = [_record_to_row(r) for r in records]
    if not rows:
        return 0
    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(show_header=True, header_style="bold")
        for c in CSV_FIELDS:
            table.add_column(c)
        for row in rows:
            table.add_row(*[str(row[c]) for c in CSV_FIELDS])
        Console(file=out, force_terminal=False).print(table)
    except ImportError:
        widths = {c: max(len(c), max((len(str(r[c])) for r in rows), default=0)) for c in CSV_FIELDS}
        out.write(" | ".join(c.ljust(widths[c]) for c in CSV_FIELDS) + "\n")
        out.write("-+-".join("-" * widths[c] for c in CSV_FIELDS) + "\n")
        for r in rows:
            out.write(" | ".join(str(r[c]).ljust(widths[c]) for c in CSV_FIELDS) + "\n")
    return len(rows)


WRITERS: dict[str, Callable[[Iterator[CommitRecord], Any], int]] = {
    "json": _write_json,
    "jsonl": _write_jsonl,
    "csv": _write_csv,
    "yaml": _write_yaml,
    "md": _write_markdown,
    "markdown": _write_markdown,
    "table": _write_table,
}


def _load_config_file(path: Path | None) -> dict[str, Any]:
    candidates: list[Path] = []
    if path:
        candidates.append(path)
    else:
        candidates.append(Path(".gitaddedfiles.toml"))
        candidates.append(Path("pyproject.toml"))
    for p in candidates:
        if not p.is_file():
            continue
        try:
            with p.open("rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            LOG.warning("could not read config {}: {}", p, e)
            continue
        if p.name == "pyproject.toml":
            return data.get("tool", {}).get("gitaddedfiles", {})
        return data
    return {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git-added-files",
        description="List added/modified/deleted files per commit across multiple Git backends.",
    )
    p.add_argument(
        "repo_path",
        nargs="?",
        default=None,
        help="Path to the git repository (default: .)",
    )
    p.add_argument("output_path", nargs="?", default=None, help="Output file (- for stdout)")
    p.add_argument("-b", "--backend", choices=list(BACKENDS), help="Git backend to use")
    p.add_argument("-f", "--format", dest="fmt", choices=list(WRITERS), help="Output format")
    p.add_argument("--branch", help="Ref/branch to walk (default: HEAD)")
    p.add_argument("--since", help="Only commits after this date (ISO 8601 or git-style)")
    p.add_argument("--until", help="Only commits before this date")
    p.add_argument("--author", help="Filter by author name or email (substring)")
    p.add_argument("--max-count", type=int, help="Limit number of commits emitted")
    p.add_argument(
        "--first-parent",
        action="store_true",
        default=None,
        help="Follow only first parents",
    )
    p.add_argument("--path", help="Only include paths matching this glob (fnmatch)")
    p.add_argument("--exclude", help="Exclude paths matching this glob (fnmatch)")
    p.add_argument(
        "--mode",
        choices=["added", "modified", "deleted", "changed", "all"],
        help="Which change types to include",
    )
    p.add_argument(
        "--rename",
        action="store_true",
        default=None,
        help="Enable rename detection (backend-dependent)",
    )
    p.add_argument(
        "--config",
        help="Explicit config TOML path (default: .gitaddedfiles.toml or pyproject.toml)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and count but do not write output",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    p.add_argument("-q", "--quiet", action="store_true", help="Quiet logging (errors only)")
    return p


def _resolve(cli_val: Any, key: str, env_key: str, config: dict[str, Any], default: Any) -> Any:
    if cli_val is not None:
        return cli_val
    if key in config:
        return config[key]
    if env_key in os.environ:
        return os.environ[env_key]
    return default


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = "ERROR" if args.quiet else ("DEBUG" if args.verbose else "INFO")
    config = _load_config_file(Path(args.config) if args.config else None)
    repo_path = _resolve(args.repo_path, "repo_path", "GAF_REPO_PATH", config, ".")
    output_path = _resolve(args.output_path, "output_path", "GAF_OUTPUT_PATH", config, None)
    backend = _resolve(args.backend, "backend", "GAF_BACKEND", config, "dulwich")
    fmt = _resolve(args.fmt, "format", "GAF_FORMAT", config, "json")
    branch = _resolve(args.branch, "branch", "GAF_BRANCH", config, "HEAD")
    since = _resolve(args.since, "since", "GAF_SINCE", config, None)
    until = _resolve(args.until, "until", "GAF_UNTIL", config, None)
    author = _resolve(args.author, "author", "GAF_AUTHOR", config, None)
    max_count = _resolve(args.max_count, "max_count", "GAF_MAX_COUNT", config, None)
    first_parent = _resolve(args.first_parent, "first_parent", "GAF_FIRST_PARENT", config, False)
    path_glob = _resolve(args.path, "path", "GAF_PATH", config, None)
    exclude = _resolve(args.exclude, "exclude", "GAF_EXCLUDE", config, None)
    mode = _resolve(args.mode, "mode", "GAF_MODE", config, "changed")
    rename = _resolve(args.rename, "rename", "GAF_RENAME", config, False)
    if max_count is not None:
        try:
            max_count = int(max_count)
        except (TypeError, ValueError):
            LOG.error("invalid max_count: {}", max_count)
            return EXIT_USAGE
    if backend not in BACKENDS:
        LOG.error("unknown backend: {} (choose from {})", backend, ", ".join(BACKENDS))
        return EXIT_USAGE
    if fmt not in WRITERS:
        LOG.error("unknown format: {} (choose from {})", fmt, ", ".join(WRITERS))
        return EXIT_USAGE
    if rename and not BACKEND_CAPS[backend]["rename"]:
        LOG.warning("backend {} does not support rename detection; ignoring --rename", backend)
        rename = False
    opts = Options(
        repo_path=str(repo_path),
        branch=branch,
        since=since,
        until=until,
        author=author,
        max_count=max_count,
        first_parent=bool(first_parent),
        path=path_glob,
        exclude=exclude,
        mode=mode,
        rename=bool(rename),
    )
    try:
        records = BACKENDS[backend](opts)
    except ImportError as e:
        LOG.error("missing dependency for backend {}: {}", backend, e)
        return EXIT_REPO
    except Exception as e:
        LOG.error("failed to open repository: {}", e)
        if args.verbose:
            LOG.exception("traceback")
        return EXIT_REPO
    if args.dry_run:
        n = sum(1 for _ in records)
        LOG.info("dry run: would write {} commits (backend={}, format={})", n, backend, fmt)
        return EXIT_OK if n else EXIT_NO_COMMITS
    try:
        if output_path in (None, "-"):
            n = WRITERS[fmt](records, sys.stdout)
        else:
            with open(output_path, "w", newline="") as f:
                n = WRITERS[fmt](records, f)
    except Exception as e:
        LOG.error("output error: {}", e)
        if args.verbose:
            LOG.exception("traceback")
        return EXIT_REPO
    LOG.info("wrote {} commits (backend={}, format={})", n, backend, fmt)
    return EXIT_OK if n else EXIT_NO_COMMITS


if __name__ == "__main__":
    sys.exit(main())
