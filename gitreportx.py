#!/data/data/com.termux/files/usr/bin/env python
"""Report the files added or removed in every commit of a repository.
Walks the history through subprocess, GitPython, pygit2 or Dulwich (``-b``), prints each commit with the added and removed paths, can skip the commits without changes and limit the walk with ``--max-commits``.
Usage: gitreportx [repo] [-b BACKEND] [--only-with-changes] [--max-commits N] Positional arguments: repo Path to the Git repository (default: current directory) Options: -b, --backend --only-with-changes Skip commits that neither added nor removed any files --max-commits Stop after processing N commits Examples: $ gitreportx .
--only-with-changes --max-commits 50 Command-line script: run ``python gitreportx.py`` or the ``gitreportx`` command.
The module is importable as well."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Iterator, Literal, Protocol, Sequence


try:
    import git as gitpython

    HAS_GITPYTHON = True
except ImportError:
    HAS_GITPYTHON = False
try:
    import pygit2

    HAS_PYGIT2 = True
except ImportError:
    HAS_PYGIT2 = False
try:
    from dulwich import porcelain as dulwich_porcelain
    from dulwich.repo import Repo as DulwichRepo

    HAS_DULWICH = True
except ImportError:
    HAS_DULWICH = False
BackendName = Literal["subprocess", "gitpython", "pygit2", "dulwich"]
AVAILABLE_BACKENDS: list[BackendName] = ["subprocess"]
if HAS_GITPYTHON:
    AVAILABLE_BACKENDS.append("gitpython")
if HAS_PYGIT2:
    AVAILABLE_BACKENDS.append("pygit2")
if HAS_DULWICH:
    AVAILABLE_BACKENDS.append("dulwich")


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    status: Literal["A", "D"]


@dataclass(frozen=True, slots=True)
class CommitReport:
    sha: str
    short_sha: str
    summary: str
    author: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


class GitBackend(Protocol):
    def iter_commits(self) -> Iterator[CommitReport]: ...


class SubprocessBackend:
    def __init__(self, repo: Path) -> None:
        self._repo = repo.resolve()

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self._repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def iter_commits(self) -> Iterator[CommitReport]:
        log = self._run("log", "--pretty=format:%H")
        shas = [line.strip() for line in log.splitlines() if line.strip()]
        for sha in shas:
            meta = self._run(
                "log",
                "-1",
                "--pretty=format:%h%n%s%n%an <%ae>",
                sha,
            ).splitlines()
            short_sha = meta[0] if meta else sha[:7]
            summary = meta[1] if len(meta) > 1 else ""
            author = meta[2] if len(meta) > 2 else ""
            try:
                name_status = self._run(
                    "diff-tree",
                    "--no-commit-id",
                    "--name-status",
                    "-r",
                    "--diff-filter=AD",
                    "-z",
                    sha,
                )
            except subprocess.CalledProcessError:
                name_status = self._run(
                    "show",
                    "--pretty=format:",
                    "--name-status",
                    "--diff-filter=AD",
                    "-z",
                    sha,
                )
            added: list[str] = []
            removed: list[str] = []
            parts = name_status.split("\0")
            i = 0
            while i + 1 < len(parts):
                status = parts[i]
                path = parts[i + 1]
                if not status or not path:
                    i += 2
                    continue
                if status.startswith("A"):
                    added.append(path)
                elif status.startswith("D"):
                    removed.append(path)
                i += 2
            yield CommitReport(
                sha=sha,
                short_sha=short_sha,
                summary=summary,
                author=author,
                added=tuple(sorted(added)),
                removed=tuple(sorted(removed)),
            )


class GitPythonBackend:
    def __init__(self, repo: Path) -> None:
        if not HAS_GITPYTHON:
            msg = "GitPython is not installed"
            raise RuntimeError(msg)
        self._repo = gitpython.Repo(str(repo.resolve()))

    def iter_commits(self) -> Iterator[CommitReport]:
        for commit in self._repo.iter_commits():
            added: list[str] = []
            removed: list[str] = []
            if not commit.parents:
                for item in commit.tree.traverse():
                    if item.type == "blob":
                        added.append(item.path)
            else:
                parent = commit.parents[0]
                for diff in parent.diff(commit, create_patch=False):
                    if diff.change_type == "A":
                        added.append(diff.b_path or diff.a_path)
                    elif diff.change_type == "D":
                        removed.append(diff.a_path or diff.b_path)
            yield CommitReport(
                sha=commit.hexsha,
                short_sha=commit.hexsha[:7],
                summary=commit.summary,
                author=f"{commit.author.name} <{commit.author.email}>",
                added=tuple(sorted(p for p in added if p)),
                removed=tuple(sorted(p for p in removed if p)),
            )


class Pygit2Backend:
    def __init__(self, repo: Path) -> None:
        if not HAS_PYGIT2:
            msg = "pygit2 is not installed"
            raise RuntimeError(msg)
        self._repo = pygit2.Repository(str(repo.resolve()))

    def iter_commits(self) -> Iterator[CommitReport]:
        walker = self._repo.walk(
            self._repo.head.target,
            pygit2.GIT_SORT_TIME | pygit2.GIT_SORT_TOPOLOGICAL,
        )
        for commit in walker:
            added: list[str] = []
            removed: list[str] = []
            if len(commit.parents) == 0:
                for entry in commit.tree:
                    self._collect_blobs(entry, "", added)
            else:
                parent = commit.parents[0]
                diff = self._repo.diff(parent, commit)
                for patch in diff:
                    delta = patch.delta
                    status = delta.status
                    if status == pygit2.GIT_DELTA_ADDED:
                        added.append(delta.new_file.path)
                    elif status == pygit2.GIT_DELTA_DELETED:
                        removed.append(delta.old_file.path)
            author = commit.author
            yield CommitReport(
                sha=str(commit.id),
                short_sha=str(commit.id)[:7],
                summary=commit.message.splitlines()[0] if commit.message else "",
                author=f"{author.name} <{author.email}>",
                added=tuple(sorted(added)),
                removed=tuple(sorted(removed)),
            )

    def _collect_blobs(
        self,
        entry: pygit2.Object,
        prefix: str,
        out: list[str],
    ) -> None:
        name = f"{prefix}/{entry.name}" if prefix else entry.name
        if entry.type == pygit2.GIT_OBJ_BLOB:
            out.append(name)
        elif entry.type == pygit2.GIT_OBJ_TREE:
            for child in entry:
                self._collect_blobs(child, name, out)


class DulwichBackend:
    def __init__(self, repo: Path) -> None:
        if not HAS_DULWICH:
            msg = "dulwich is not installed"
            raise RuntimeError(msg)
        self._repo = DulwichRepo(str(repo.resolve()))

    def iter_commits(self) -> Iterator[CommitReport]:
        head = self._repo.head()
        for entry in self._repo.get_walker(include=[head]):
            commit = entry.commit
            sha = commit.id.decode("ascii")
            short_sha = sha[:7]
            message = commit.message.decode("utf-8", errors="replace")
            summary = message.splitlines()[0] if message else ""
            author = commit.author.decode("utf-8", errors="replace")
            added: list[str] = []
            removed: list[str] = []
            parents = commit.parents
            if not parents:
                tree = self._repo[commit.tree]
                self._walk_tree(tree, b"", added)
            else:
                parent_commit = self._repo[parents[0]]
                changes = dulwich_porcelain.diff_tree(
                    self._repo,
                    parent_commit.tree,
                    commit.tree,
                )
                for change in changes:
                    if change.type == "add":
                        path = change.new.path.decode("utf-8", errors="replace")
                        added.append(path)
                    elif change.type == "delete":
                        path = change.old.path.decode("utf-8", errors="replace")
                        removed.append(path)
            yield CommitReport(
                sha=sha,
                short_sha=short_sha,
                summary=summary,
                author=author,
                added=tuple(sorted(added)),
                removed=tuple(sorted(removed)),
            )

    def _walk_tree(self, tree: object, prefix: bytes, out: list[str]) -> None:
        for entry in tree.items():
            name = entry.path
            full = prefix + b"/" + name if prefix else name
            if entry.mode & 0o170000 == 0o040000:
                child = self._repo[entry.sha]
                self._walk_tree(child, full, out)
            else:
                out.append(full.decode("utf-8", errors="replace"))


def create_backend(name: BackendName, repo: Path) -> GitBackend:
    if name == "subprocess":
        return SubprocessBackend(repo)
    if name == "gitpython":
        return GitPythonBackend(repo)
    if name == "pygit2":
        return Pygit2Backend(repo)
    if name == "dulwich":
        return DulwichBackend(repo)
    msg = f"Unknown backend: {name}"
    raise ValueError(msg)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report files added or removed in every commit of a Git repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "repo",
        type=Path,
        nargs="?",
        default=Path(),
        help="Path to the Git repository (default: current directory)",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=AVAILABLE_BACKENDS,
        default="subprocess",
        help=(f"Git access backend to use. Available: {', '.join(AVAILABLE_BACKENDS)} (default: subprocess)"),
    )
    parser.add_argument(
        "--only-with-changes",
        action="store_true",
        help="Skip commits that neither added nor removed any files",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        default=None,
        metavar="N",
        help="Stop after processing N commits",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo: Path = args.repo.resolve()
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        print(f"error: {repo} does not look like a Git repository", file=sys.stderr)
        return 1
    try:
        backend = create_backend(args.backend, repo)
    except Exception as exc:
        print(f"error: failed to initialise backend '{args.backend}': {exc}", file=sys.stderr)
        return 1
    print(f"Repository : {repo}")
    print(f"Backend    : {args.backend}")
    print("-" * 72)
    count = 0
    for report in backend.iter_commits():
        if args.only_with_changes and not report.added and not report.removed:
            continue
        print(f"commit {report.short_sha}  {report.summary}")
        print(f"Author: {report.author}")
        if report.added:
            print("  Added:")
            for p in report.added:
                print(f"    + {p}")
        if report.removed:
            print("  Removed:")
            for p in report.removed:
                print(f"    - {p}")
        if not report.added and not report.removed:
            print("  (no files added or removed)")
        print()
        count += 1
        if args.max_commits is not None and count >= args.max_commits:
            break
    print(f"Processed {count} commit(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
