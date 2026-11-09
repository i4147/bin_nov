#!/data/data/com.termux/files/usr/bin/env python
"""Stage everything, commit with a timestamp message and push.
Stages the changes (warning about the empty files), creates the ``YYYY-MM-DD HH:MM`` message or the ``-m`` one, commits and pushes to the remote/branch, with ``--force-with-lease``, ``--allow-empty``, ``--dry-run`` and ``-y`` for the confirmations.
Usage: gp [repo] [-m MESSAGE] [-r REMOTE] [--branch BRANCH] [--dry-run] Positional arguments: repo Path to the Git repository (default: current directory) Options: -b, --backend -r, --remote Remote name to push to (default: origin) --branch Branch to push (default: current branch / HEAD) -m, --message Override the automatic datetime commit message --allow-empty Create a commit even when there are no changes --force-with-lease Use --force-with-lease when pushing (safer than --force) --yes, -y Assume 'yes' for all confirmation prompts --dry-run Show what would be done without actually committing or pushing --no-push Commit only; do not push to the remote --skip-empty-check Do not warn about empty (0-byte) files Examples: $ gp -m 'fix docs' Command-line script: run ``python gp.py`` or the ``gp`` command.
The module is importable as well."""

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys
from typing import Any, Literal, Protocol, Sequence


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
WHITE_LIST: set[str] = {"py.typed", "__init__.py"}
AVAILABLE_BACKENDS: list[BackendName] = ["subprocess"]
if HAS_GITPYTHON:
    AVAILABLE_BACKENDS.append("gitpython")
if HAS_PYGIT2:
    AVAILABLE_BACKENDS.append("pygit2")
if HAS_DULWICH:
    AVAILABLE_BACKENDS.append("dulwich")


def format_commit_message(when: datetime | None = None) -> str:
    dt = when or datetime.now().astimezone()
    day_name = dt.strftime("%A")
    day = str(dt.day)
    month = dt.strftime("%B")
    year = dt.strftime("%Y")
    hour = dt.strftime("%I").lstrip("0") or "0"
    minute = dt.strftime("%M")
    ampm = dt.strftime("%p")
    return f"{day_name}, {day} {month} {year},  {hour}:{minute}{ampm}"


def confirm(prompt: str, *, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


def find_empty_files(repo: Path, paths: Sequence[str]) -> list[str]:
    empty: list[str] = []
    for rel in paths:
        full = repo / rel
        try:
            if full.is_file() and full.stat().st_size == 0:
                if full.name in WHITE_LIST:
                    continue
                empty.append(rel)
        except OSError:
            continue
    return sorted(empty)


class GitBackend(Protocol):
    def current_branch(self) -> str | None: ...

    def has_changes(self) -> bool: ...

    def list_relevant_files(self) -> list[str]: ...

    def stage_all(self) -> None: ...

    def commit(self, message: str, *, allow_empty: bool = False) -> str: ...

    def push(
        self,
        remote: str,
        branch: str | None,
        *,
        force_with_lease: bool = False,
    ) -> None: ...


class SubprocessBackend:
    def __init__(self, repo: Path) -> None:
        self._repo = repo.resolve()

    def _run(
        self,
        *args: str,
        check: bool = True,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self._repo,
            capture_output=capture,
            text=True,
            check=check,
        )

    def current_branch(self) -> str | None:
        result = self._run("branch", "--show-current", check=False)
        name = result.stdout.strip()
        return name or None

    def has_changes(self) -> bool:
        result = self._run("status", "--porcelain")
        return bool(result.stdout.strip())

    def list_relevant_files(self) -> list[str]:
        tracked = self._run("ls-files", "-z").stdout.split("\0")
        untracked = self._run("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
        files = [p for p in tracked + untracked if p]
        return files

    def stage_all(self) -> None:
        self._run("add", "-A")

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        cmd = ["commit", "-m", message]
        if allow_empty:
            cmd.append("--allow-empty")
        self._run(*cmd)
        return self._run("rev-parse", "HEAD").stdout.strip()

    def push(
        self,
        remote: str,
        branch: str | None,
        *,
        force_with_lease: bool = False,
    ) -> None:
        cmd = ["push"]
        if force_with_lease:
            cmd.append("--force-with-lease")
        cmd.append(remote)
        cmd.append(branch or "HEAD")
        cmd.insert(1, "-u")
        self._run(*cmd)


class GitPythonBackend:
    def __init__(self, repo: Path) -> None:
        if not HAS_GITPYTHON:
            msg = "GitPython is not installed"
            raise RuntimeError(msg)
        self._repo = gitpython.Repo(str(repo.resolve()))

    def current_branch(self) -> str | None:
        try:
            return self._repo.active_branch.name
        except TypeError:
            return None

    def has_changes(self) -> bool:
        return self._repo.is_dirty(untracked_files=True)

    def list_relevant_files(self) -> list[str]:
        tracked = [item.path for item in self._repo.index.entries.values()]
        untracked = list(self._repo.untracked_files)
        return sorted(set(tracked) | set(untracked))

    def stage_all(self) -> None:
        self._repo.git.add(A=True)

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        if allow_empty:
            commit = self._repo.index.commit(message, skip_hooks=False)
        else:
            commit = self._repo.index.commit(message)
        return commit.hexsha

    def push(
        self,
        remote: str,
        branch: str | None,
        *,
        force_with_lease: bool = False,
    ) -> None:
        origin = self._repo.remote(name=remote)
        refspec = f"{branch}:{branch}" if branch else None
        kwargs: dict[str, object] = {"set_upstream": True}
        if force_with_lease:
            kwargs["force_with_lease"] = True
        if refspec:
            origin.push(refspec=refspec, **kwargs)
        else:
            origin.push(**kwargs)


class Pygit2Backend:
    def __init__(self, repo: Path) -> None:
        if not HAS_PYGIT2:
            msg = "pygit2 is not installed"
            raise RuntimeError(msg)
        self._repo = pygit2.Repository(str(repo.resolve()))

    def current_branch(self) -> str | None:
        if self._repo.head_is_detached or self._repo.head_is_unborn:
            return None
        return self._repo.head.shorthand

    def has_changes(self) -> bool:
        return bool(self._repo.status())

    def list_relevant_files(self) -> list[str]:
        status = self._repo.status()
        files = list(status.keys())
        try:
            for entry in self._repo.index:
                files.append(entry.path)
        except Exception:
            pass
        return sorted(set(files))

    def stage_all(self) -> None:
        self._repo.index.add_all()
        self._repo.index.write()

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        config = self._repo.config
        try:
            name = config["user.name"]
            email = config["user.email"]
        except KeyError:
            name, email = "Unknown", "unknown@example.com"

        author = pygit2.Signature(name, email)
        parents = []
        if not self._repo.head_is_unborn:
            parents = [self._repo.head.target]

        tree = self._repo.index.write_tree()
        sha = self._repo.create_commit("HEAD", author, author, message, tree, parents)
        return str(sha)

    def push(
        self,
        remote: str,
        branch: str | None,
        *,
        force_with_lease: bool = False,
    ) -> None:
        remote_obj = self._repo.remotes[remote]
        if branch is None:
            if self._repo.head_is_unborn:
                msg = "Cannot push an unborn HEAD"
                raise RuntimeError(msg)
            branch = self._repo.head.shorthand
        remote_obj.push([f"refs/heads/{branch}:refs/heads/{branch}"])


class DulwichBackend:
    def __init__(self, repo: Path) -> None:
        if not HAS_DULWICH:
            msg = "dulwich is not installed"
            raise RuntimeError(msg)
        self._repo = DulwichRepo(str(repo.resolve()))

    def current_branch(self) -> str | None:
        try:
            ref = self._repo.refs.read_ref(b"HEAD")
            if ref.startswith(b"ref: "):
                return ref[5:].decode().removeprefix("refs/heads/")
        except KeyError:
            pass
        return None

    def has_changes(self) -> bool:
        status = dulwich_porcelain.status(self._repo)
        return bool(
            status.staged["add"]
            or status.staged["delete"]
            or status.staged["modify"]
            or status.unstaged
            or status.untracked
        )

    def list_relevant_files(self) -> list[str]:
        status = dulwich_porcelain.status(self._repo)
        files: set[str] = set()
        for collection in (
            status.staged["add"],
            status.staged["delete"],
            status.staged["modify"],
            status.unstaged,
            status.untracked,
        ):
            for p in collection:
                if isinstance(p, bytes):
                    files.add(p.decode("utf-8", errors="replace"))
                else:
                    files.add(str(p))
        for entry in self._repo.open_index():
            files.add(entry.path.decode("utf-8", errors="replace"))
        return sorted(files)

    def stage_all(self) -> None:
        dulwich_porcelain.add(self._repo, paths=None)

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        config = self._repo.get_config()
        try:
            name = config.get((b"user",), b"name").decode()
            email = config.get((b"user",), b"email").decode()
        except KeyError:
            name, email = "Unknown", "unknown@example.com"

        sha = dulwich_porcelain.commit(
            self._repo,
            message=message.encode("utf-8"),
            author=f"{name} <{email}>".encode("utf-8"),
        )
        return sha.decode("ascii")

    def push(
        self,
        remote: str,
        branch: str | None,
        *,
        force_with_lease: bool = False,
    ) -> None:
        if branch is None:
            branch = self.current_branch()
            if branch is None:
                msg = "detached HEAD – supply --branch"
                raise RuntimeError(msg)
        dulwich_porcelain.push(
            self._repo,
            remote_location=remote,
            refspecs=[f"refs/heads/{branch}:refs/heads/{branch}"],
        )


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
        description="Stage all changes, commit with a datetime message, and push.",
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
        "-r",
        "--remote",
        default="origin",
        help="Remote name to push to (default: origin)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Branch to push (default: current branch / HEAD)",
    )
    parser.add_argument(
        "-m",
        "--message",
        default=None,
        help="Override the automatic datetime commit message",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Create a commit even when there are no changes",
    )
    parser.add_argument(
        "--force-with-lease",
        action="store_true",
        help="Use --force-with-lease when pushing (safer than --force)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Assume 'yes' for all confirmation prompts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually committing or pushing",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Commit only; do not push to the remote",
    )
    parser.add_argument(
        "--skip-empty-check",
        action="store_true",
        help="Do not warn about empty (0-byte) files",
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

    message = args.message or format_commit_message()
    branch = backend.current_branch()

    print(f"Repository : {repo}")
    print(f"Backend    : {args.backend}")
    print(f"Branch     : {branch or '(detached HEAD)'}")
    print(f"Message    : {message}")
    print(f"Remote     : {args.remote}")
    print("-" * 60)

    if branch is not None and branch != "main":
        print(f"WARNING: You are on branch '{branch}', not 'main'.")
        if not args.yes and not args.dry_run:
            if not confirm("Continue with commit & push on this branch?", default=False):
                print("Aborted by user.")
                return 1
        elif args.dry_run:
            print("[dry-run] would ask for confirmation to continue on non-main branch")

    if not args.skip_empty_check:
        relevant = backend.list_relevant_files()
        empty_files = find_empty_files(repo, relevant)
        if empty_files:
            print("WARNING: The following empty (0-byte) files were found:")
            for p in empty_files:
                print(f"    • {p}")
            if not args.yes and not args.dry_run:
                if not confirm("Continue anyway?", default=False):
                    print("Aborted by user.")
                    return 1
            elif args.dry_run:
                print("[dry-run] would ask for confirmation because of empty files")

    if args.dry_run:
        dirty = backend.has_changes()
        print(f"[dry-run] working tree dirty          : {dirty}")
        print(f"[dry-run] would stage all changes")
        print(f"[dry-run] would commit with message   : {message!r}")
        if not args.no_push:
            print(f"[dry-run] would push to {args.remote}" + (f" (force-with-lease)" if args.force_with_lease else ""))
        else:
            print("[dry-run] --no-push specified; would not push")
        return 0

    try:
        if not backend.has_changes() and not args.allow_empty:
            print("Nothing to commit – working tree is clean.")
            return 0

        print("Staging all changes …")
        backend.stage_all()

        print("Creating commit …")
        sha = backend.commit(message, allow_empty=args.allow_empty)
        print(f"Created commit {sha[:12]}")

        if args.no_push:
            print("Skipping push (--no-push).")
        else:
            print(f"Pushing to {args.remote} …")
            backend.push(
                args.remote,
                args.branch or branch,
                force_with_lease=args.force_with_lease,
            )
            print("Push completed successfully.")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
