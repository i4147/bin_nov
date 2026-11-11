#!/data/data/com.termux/files/usr/bin/env python
"""Git maintenance commands with pluggable backends.
Subcommands: ``checkout-previous`` returns to HEAD^ in the repositories whose subject matches a prefix, ``rm-commits``/``cut``/``del-remote-commits`` drop the commits older than N days (with a backup branch, and a force-push for the remote variant), ``list-added`` lists the files of the last commits and ``restore-deleted`` brings back everything ever deleted.
Usage: gitools {checkout-previous,rm-commits,cut,del-remote-commits,list-added,restore-deleted} [options] Positional arguments: mode "clean" encrypts; "smudge" decrypts Subcommands: checkout-previous Checkout HEAD^ in repos whose HEAD subject matches a prefix rm-commits Reset branch to drop commits older than N days (creates a backup branch) cut Rewrite branch dropping commits older than N days del-remote-commits Reset branch and force-push (deletes commits on remote) list-added List files added in the last N commits restore-deleted Restore every file ever deleted in history stage-deleted Stage pending deletions that match history, then commit squash-deletions Amend last commit with pending historical deletions age-filter Age-based git clean/smudge filter (reads/writes stdin/stdout) Options: -b, --backend Git backend (default: subprocess; other backends fall back to subprocess for unsupported operations) -C, --repo Directory containing repositories (default: .) --prefix Latest commit subject must start with this prefix (default: 2026-08-29) -y, --yes Skip the interactive confirmation --days Delete commits older than N days (default: 30) --method Rewrite method (default: squash) --branch Branch to clean (default: master; falls back to main) --no-push Do not force-push; only reset locally -n Number of commits to look back (default: 10) -m, --message Commit message for the restore commit (default: "removed files") ...
plus 3 more; run ``gitools --help`` for the full list.
Examples: $ gitools cut --days 30 --branch main --no-push The commit-rewriting and force-push subcommands change the history; run them on a branch with a backup."""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Optional, Sequence


@dataclass(slots=True)
class CommitInfo:
    sha: str
    subject: str
    timestamp: int
    parents: list[str] = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.sha[:8]

    @property
    def date(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[ERR] {msg}", file=sys.stderr)


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _parse_commit_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class GitBackend:
    name = "subprocess"

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo).resolve()

    def _run(self, *args: str, check: bool = True, input: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            input=input,
            check=check,
        )

    def _git(self, *args: str) -> str:
        return self._run(*args).stdout.strip()

    def is_repo(self) -> bool:
        r = self._run("rev-parse", "--is-inside-work-tree", check=False)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def rev_parse(self, *args: str) -> str:
        return self._git("rev-parse", *args)

    def head_sha(self) -> str:
        return self.rev_parse("HEAD")

    def current_branch(self) -> Optional[str]:
        r = self._run("symbolic-ref", "--short", "-q", "HEAD", check=False)
        return r.stdout.strip() or None

    def is_dirty(self, untracked: bool = True) -> bool:
        args = ["status", "--porcelain"]
        if not untracked:
            args.append("--untracked-files=no")
        return bool(self._run(*args, check=False).stdout.strip())

    def status_porcelain(self) -> list[str]:
        return self._run("status", "--porcelain", check=False).stdout.splitlines()

    def log(self, *, ref: str = "HEAD", n: Optional[int] = None, reverse: bool = False) -> list[CommitInfo]:
        args = ["log", "--format=%H%x00%s%x00%ct%x00%P"]
        if n:
            args.append(f"-n{n}")
        if reverse:
            args.append("--reverse")
        args.append(ref)
        out = self._run(*args, check=False).stdout
        commits: list[CommitInfo] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x00")
            if len(parts) < 4:
                continue
            sha, subj, ts, parents = parts[0], parts[1], parts[2], parts[3]
            commits.append(
                CommitInfo(
                    sha=sha,
                    subject=subj,
                    timestamp=int(ts),
                    parents=parents.split() if parents else [],
                )
            )
        return commits

    def checkout(self, ref: str) -> None:
        self._git("checkout", ref)

    def reset(self, ref: str, mode: str = "hard") -> None:
        self._git("reset", f"--{mode}", ref)

    def cherry_pick(self, sha: str, allow_empty: bool = True) -> None:
        args = ["cherry-pick"]
        if allow_empty:
            args.append("--allow-empty")
        args.append(sha)
        self._git(*args)

    def create_branch(self, name: str, start: Optional[str] = None) -> None:
        args = ["branch", name]
        if start:
            args.append(start)
        self._git(*args)

    def delete_branch(self, name: str, force: bool = True) -> None:
        self._git("branch", "-D" if force else "-d", name)

    def add(self, *files: str) -> None:
        self._git("add", "--", *files)

    def commit(self, message: str) -> str:
        return self._git("commit", "-m", message)

    def amend_last_commit(self) -> str:
        return self._git("commit", "--amend", "--no-edit")

    def push(self, remote: str, refspec: str, force: bool = False) -> None:
        args = ["push"]
        if force:
            args.append("--force")
        args.extend([remote, refspec])
        self._git(*args)

    def deleted_files_in_history(self) -> dict[str, str]:
        out = self._run(
            "log",
            "--diff-filter=D",
            "--pretty=format:%H",
            "--name-only",
            check=False,
        ).stdout
        deletions: dict[str, str] = {}
        current_sha: Optional[str] = None
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if len(line) == 40 and " " not in line:
                current_sha = line
            elif current_sha and line not in deletions:
                deletions[line] = current_sha
        return deletions

    def files_added_in_last(self, n: int) -> list[Path]:
        out = self._run(
            "log",
            "-n",
            str(n),
            "--pretty=format:",
            "--name-status",
            "--diff-filter=A",
            check=False,
        ).stdout
        added: list[Path] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                p = self.repo / parts[1]
                if p.is_symlink():
                    continue
                added.append(p)
        return added


class GitPythonBackend(GitBackend):
    name = "gitpython"

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        try:
            from git import Repo

            self._r = Repo(str(self.repo))
        except Exception:
            self._r = None

    def is_repo(self) -> bool:
        if self._r is not None:
            return True
        return super().is_repo()

    def head_sha(self) -> str:
        if self._r is not None:
            return self._r.head.commit.hexsha
        return super().head_sha()

    def current_branch(self) -> Optional[str]:
        if self._r is not None and not self._r.head.is_detached:
            return self._r.active_branch.name
        return super().current_branch()

    def is_dirty(self, untracked: bool = True) -> bool:
        if self._r is not None:
            return self._r.is_dirty(untracked_files=untracked)
        return super().is_dirty(untracked)

    def log(self, *, ref: str = "HEAD", n: Optional[int] = None, reverse: bool = False) -> list[CommitInfo]:
        if self._r is None:
            return super().log(ref=ref, n=n, reverse=reverse)
        try:
            commits = list(self._r.iter_commits(ref, max_count=n, reverse=reverse))
        except Exception:
            return super().log(ref=ref, n=n, reverse=reverse)
        return [
            CommitInfo(
                sha=c.hexsha,
                subject=c.summary,
                timestamp=c.committed_date,
                parents=[p.hexsha for p in c.parents],
            )
            for c in commits
        ]

    def reset(self, ref: str, mode: str = "hard") -> None:
        if self._r is None:
            return super().reset(ref, mode)
        self._r.git.reset(f"--{mode}", ref)
        return None

    def checkout(self, ref: str) -> None:
        if self._r is None:
            return super().checkout(ref)
        self._r.git.checkout(ref)
        return None

    def cherry_pick(self, sha: str, allow_empty: bool = True) -> None:
        if self._r is None:
            return super().cherry_pick(sha, allow_empty)
        args = ["--allow-empty"] if allow_empty else []
        self._r.git.cherry_pick(sha, *args)
        return None

    def create_branch(self, name: str, start: Optional[str] = None) -> None:
        if self._r is None:
            return super().create_branch(name, start)
        self._r.create_head(name, start) if start else self._r.create_head(name)
        return None

    def add(self, *files: str) -> None:
        if self._r is None:
            return super().add(*files)
        self._r.index.add(list(files))
        return None

    def commit(self, message: str) -> str:
        if self._r is None:
            return super().commit(message)
        return self._r.index.commit(message).hexsha

    def amend_last_commit(self) -> str:
        if self._r is None:
            return super().amend_last_commit()
        self._r.git.commit("--amend", "--no-edit")
        return self._r.head.commit.hexsha

    def push(self, remote: str, refspec: str, force: bool = False) -> None:
        if self._r is None:
            return super().push(remote, refspec, force)
        args = ["--force"] if force else []
        self._r.git.push(remote, refspec, *args)
        return None


class Pygit2Backend(GitBackend):
    name = "libgit2"

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        try:
            import pygit2

            self._repo = pygit2.Repository(str(self.repo))
        except Exception:
            self._repo = None

    def is_repo(self) -> bool:
        if self._repo is not None:
            return True
        return super().is_repo()

    def head_sha(self) -> str:
        if self._repo is not None:
            try:
                return str(self._repo.head.target)
            except Exception:
                pass
        return super().head_sha()

    def rev_parse(self, *args: str) -> str:
        if self._repo is None or len(args) != 1:
            return super().rev_parse(*args)
        try:
            obj = self._repo.revparse_single(args[0])
            return str(obj.id)
        except Exception:
            return super().rev_parse(*args)


class DulwichBackend(GitBackend):
    name = "dulwich"

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        try:
            from dulwich.repo import Repo as DRepo

            self._d = DRepo(str(self.repo))
        except Exception:
            self._d = None

    def is_repo(self) -> bool:
        if self._d is not None:
            return True
        return super().is_repo()

    def head_sha(self) -> str:
        if self._d is None:
            return super().head_sha()
        try:
            return self._d.head().decode()
        except Exception:
            return super().head_sha()


class PyGithubBackend(GitBackend):
    name = "pygithub"

    def __init__(self, repo: Path) -> None:
        super().__init__(repo)
        self._gh = None
        try:
            import github

            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if token:
                self._gh = github.Github(token)
        except Exception:
            self._gh = None


class GhBackend(GitBackend):
    name = "gh"


def make_backend(name: str, repo: Path) -> GitBackend:
    if name == "subprocess":
        return GitBackend(repo)
    if name == "gitpython":
        return GitPythonBackend(repo)
    if name == "libgit2":
        return Pygit2Backend(repo)
    if name == "dulwich":
        return DulwichBackend(repo)
    if name == "pygithub":
        return PyGithubBackend(repo)
    if name == "gh":
        return GhBackend(repo)
    if name == "typer":
        return GitBackend(repo)
    msg = f"Unknown backend: {name!r}"
    raise ValueError(msg)


BACKEND_CHOICES: tuple[str, ...] = (
    "subprocess",
    "gitpython",
    "libgit2",
    "dulwich",
    "pygithub",
    "gh",
    "typer",
)


def cmd_checkout_previous(args: argparse.Namespace) -> int:
    root = Path(args.repo).resolve()
    if not root.is_dir():
        err(f"Not a directory: {root}")
        return 1
    candidates = [root] + [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    found = False
    for candidate in candidates:
        be = make_backend(args.backend, candidate)
        if not be.is_repo():
            continue
        found = True
        try:
            head_list = be.log(ref="HEAD", n=1)
        except Exception as e:
            err(f"{candidate.name}: {e}")
            continue
        if not head_list:
            continue
        head = head_list[0]
        if not head.subject.startswith(args.prefix):
            print(f"[skip] {candidate.name}: latest commit does not start with {args.prefix!r}")
            continue
        if not head.parents:
            print(f"[skip] {candidate.name}: no parent commit")
            continue
        target = head.parents[0]
        print(f"\n[match] Repository: {candidate.name}")
        print(f"        Path: {candidate}")
        print(f"        Current commit: {head.short}")
        print(f"        Latest message: {head.subject}")
        print(f"        Target commit: {target[:8]}")
        if not args.yes:
            ans = input(f"Checkout {target[:8]} in {candidate.name}? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print(f"[skip] {candidate.name}: cancelled")
                continue
        be.checkout(target)
        print(f"[done] {candidate.name}: now at {target[:8]}")
    if not found:
        print("No git repositories found.")
    return 0


def cmd_rm_commits(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    be = make_backend(args.backend, repo)
    if not be.is_repo():
        err(f"{repo} is not a git repository")
        return 1
    branch = be.current_branch()
    if branch is None:
        err("HEAD is detached — checkout a branch first")
        return 1
    print(f"Current branch: {branch}")
    cutoff = _cutoff(args.days)
    print(f"Cutoff: {cutoff:%Y-%m-%d %H:%M:%S UTC}")
    if be.is_dirty(untracked=True):
        err("Working directory is not clean — commit or stash first")
        return 1
    commits = be.log(ref=branch)
    if not commits:
        print("No commits found.")
        return 0
    keep = [c for c in commits if _parse_commit_ts(c.timestamp) > cutoff]
    old = [c for c in commits if _parse_commit_ts(c.timestamp) <= cutoff]
    if not old:
        print(f"No commits older than {args.days} days.")
        return 0
    if not keep:
        err("All commits would be deleted — aborting")
        return 1
    print(f"\nFound {len(old)} commit(s) to delete (older than {args.days} days)")
    print(f"Keeping {len(keep)} commit(s)")
    print("\nOldest commits to delete:")
    for c in old[-5:]:
        print(f"  {c.short} - {c.date:%Y-%m-%d %H:%M} - {c.subject}")
    new_head = keep[0]
    print(f"\nNew HEAD will be: {new_head.short} - {new_head.subject}")
    if not args.yes:
        ans = input("This will PERMANENTLY DELETE those commits. Continue? (yes/no): ").strip().lower()
        if ans != "yes":
            print("Cancelled.")
            return 0
    backup = f"backup-{branch}-{datetime.now():%Y%m%d%H%M%S}"
    print(f"\nCreating backup branch: {backup}")
    be.create_branch(backup)
    be.reset(new_head.sha, "hard")
    print(f"\n✓ Deleted {len(old)} commit(s)")
    print(f"Backup branch: {backup}")
    print(f"To restore: git reset --hard {backup}")
    return 0


def cmd_cut(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    be = make_backend(args.backend, repo)
    if not be.is_repo():
        err(f"{repo} is not a git repository")
        return 1
    cutoff = _cutoff(args.days)
    print(f"Cutoff date: {cutoff:%Y-%m-%d %H:%M:%S UTC}")
    commits = be.log(ref="HEAD")
    if not commits:
        print("No commits found.")
        return 0
    keep = [c for c in commits if _parse_commit_ts(c.timestamp) > cutoff]
    old = [c for c in commits if _parse_commit_ts(c.timestamp) <= cutoff]
    print(f"Commits to keep:   {len(keep)}")
    print(f"Commits to remove: {len(old)}")
    if not keep:
        err("No commits to keep — aborting")
        return 1
    if not old:
        print("No old commits to remove.")
        return 0
    oldest_kept = keep[-1]
    print(f"\nOldest kept: {oldest_kept.short} - {oldest_kept.subject}")
    print(f"Date:        {oldest_kept.date}")
    if not args.yes:
        ans = input("\nRewrite history? (yes/no): ").strip().lower()
        if ans != "yes":
            print("Cancelled.")
            return 0
    branch = be.current_branch()
    if args.method == "squash":
        be.reset(oldest_kept.sha, "hard")
        print(f"\n✓ Old commits removed (reset --hard to {oldest_kept.short}).")
        if branch:
            print(f"Force push needed: git push --force origin {branch}")
        return 0
    if branch is None:
        err("Cannot use orphan method on a detached HEAD")
        return 1
    new_branch = f"cleaned_{branch}"
    print(f"\nCreating orphan branch {new_branch}...")
    subprocess.run(["git", "checkout", "--orphan", new_branch], cwd=str(repo), check=True)
    for c in reversed(keep):
        try:
            be.cherry_pick(c.sha, allow_empty=True)
        except subprocess.CalledProcessError as e:
            warn(f"cherry-pick {c.short} failed: {e.stderr or e}")
    print(f"\n✓ New branch {new_branch} with {len(keep)} commit(s) created.")
    print(f"\nTo replace the original:")
    print(f"  git checkout {branch}")
    print(f"  git reset --hard {new_branch}")
    print(f"  git branch -D {new_branch}")
    return 0


def cmd_del_remote_commits(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    be = make_backend(args.backend, repo)
    if not be.is_repo():
        err(f"{repo} is not a git repository")
        return 1
    branch = args.branch
    try:
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).stdout
        if branch == "master" and "origin/main" in refs and "origin/master" not in refs:
            branch = "main"
    except Exception:
        pass
    cutoff = _cutoff(args.days)
    commits = be.log(ref=branch)
    if not commits:
        err(f"No commits on {branch}")
        return 1
    keep = [c for c in commits if _parse_commit_ts(c.timestamp) >= cutoff]
    if not keep:
        err(f"No commits within {args.days} days — nothing to keep, aborting")
        return 1
    new_head = keep[0]
    print(f"Branch: {branch}")
    print(f"Cutoff: {cutoff:%Y-%m-%d %H:%M:%S UTC}")
    print(f"Resetting to: {new_head.short} - {new_head.subject}")
    print(f"              ({new_head.date:%Y-%m-%d %H:%M UTC})")
    if not args.yes:
        ans = input("Proceed with local reset and force-push? (yes/no): ").strip().lower()
        if ans != "yes":
            print("Cancelled.")
            return 0
    if be.current_branch() != branch:
        be.checkout(branch)
    be.reset(new_head.sha, "hard")
    print(f"✓ Local branch {branch} reset to {new_head.short}")
    if args.no_push:
        print("Skipping push (--no-push).")
    else:
        try:
            be.push("origin", f"{branch}:{branch}", force=True)
            print(f"✓ Force-pushed {branch} to origin")
        except Exception as e:
            err(f"push failed: {e}")
            return 1
    return 0


def cmd_list_added(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    be = make_backend(args.backend, repo)
    if not be.is_repo():
        err(f"{repo} is not a git repository")
        return 1
    added = be.files_added_in_last(args.n)
    if not added:
        print(f"No files created in the last {args.n} commit(s).")
        return 0
    for path in added:
        try:
            print(path.relative_to(repo))
        except ValueError:
            print(path)
    return 0


def cmd_restore_deleted(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    be = make_backend(args.backend, repo)
    if not be.is_repo():
        err(f"{repo} is not a git repository")
        return 1
    print("🔍 Analyzing repository history for file deletions...")
    deletions = be.deleted_files_in_history()
    if not deletions:
        print("🎉 No deleted files found in this repository's history.")
        return 0
    missing = [(p, sha) for p, sha in deletions.items() if not (repo / p).exists()]
    if not missing:
        print("ℹ️  All historically deleted files are already present.")
        return 0
    print(f"⚠️  Found {len(missing)} missing file(s).\n")
    restored = 0
    for p, sha in missing:
        print(f"🔄 Restoring: {p} (from commit prior to {sha[:8]})")
        try:
            subprocess.run(
                ["git", "checkout", f"{sha}^", "--", p],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "--", p], cwd=str(repo), check=True, capture_output=True)
            restored += 1
        except subprocess.CalledProcessError:
            try:
                subprocess.run(
                    ["git", "checkout", sha, "--", p],
                    cwd=str(repo),
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "add", "--", p],
                    cwd=str(repo),
                    check=True,
                    capture_output=True,
                )
                restored += 1
            except subprocess.CalledProcessError as e2:
                err(f"   could not restore {p}: {e2}")
    if restored:
        print(f"\n💾 Committing {restored} restored file(s)...")
        try:
            be.commit(args.message)
            print("✓ Commit created.")
        except Exception as e:
            err(f"commit failed: {e}")
            return 1
    else:
        print("❌ No files were successfully restored.")
    return 0


def _pending_deletions_matching_history(be: GitBackend) -> list[str]:
    historical = set(be.deleted_files_in_history().keys())
    if not historical:
        return []
    pending: list[str] = []
    for line in be.status_porcelain():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if "D" in code and path in historical:
            pending.append(path)
    return pending


def cmd_stage_deleted(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    be = make_backend(args.backend, repo)
    if not be.is_repo():
        err(f"{repo} is not a git repository")
        return 1
    pending = _pending_deletions_matching_history(be)
    if not pending:
        print("🎉 No pending historical file deletions need staging.")
        return 0
    print(f"⚠️  Found {len(pending)} deleted file(s) to stage:")
    for p in pending:
        print(f"  {p}")
    print("\n🛠️  Staging...")
    for p in pending:
        be.add(p)
    print(f'💾 Committing: "{args.message}"')
    try:
        be.commit(args.message)
        print("✓ Commit created.")
    except Exception as e:
        err(f"commit failed: {e}")
        return 1
    return 0


def cmd_squash_deletions(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    be = make_backend(args.backend, repo)
    if not be.is_repo():
        err(f"{repo} is not a git repository")
        return 1
    pending = _pending_deletions_matching_history(be)
    if not pending:
        print("🎉 No pending historical file deletions to squash.")
        return 0
    print(f"⚠️  Found {len(pending)} deleted file(s) to squash:")
    for p in pending:
        print(f"  {p}")
    print("\n🛠️  Staging...")
    for p in pending:
        be.add(p)
    print("💾 Amending last commit (--amend --no-edit)...")
    try:
        be.amend_last_commit()
        print("✓ Last commit amended.")
    except Exception as e:
        err(f"amend failed: {e}")
        return 1
    return 0


def cmd_age_filter(args: argparse.Namespace) -> int:
    age_bin = args.age_bin
    if not age_bin:
        default = Path.home() / ".." / "usr" / "bin" / "age"
        age_bin = str(default) if default.exists() else "age"
    if args.mode == "clean":
        pub_path = Path(args.public_key).expanduser()
        if not pub_path.exists():
            err(f"missing public key file: {pub_path}")
            return 1
        recipient = pub_path.read_text(encoding="utf-8").strip()
        if not recipient:
            err("public key file is empty")
            return 1
        data = sys.stdin.buffer.read()
        r = subprocess.run([age_bin, "-r", recipient, "-a"], input=data, capture_output=True)
        if r.returncode != 0:
            err(f"age encrypt failed: {r.stderr.decode(errors='replace')}")
            return 1
        sys.stdout.buffer.write(r.stdout)
        return 0
    data = sys.stdin.buffer.read()
    if not data.lstrip().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"):
        sys.stdout.buffer.write(data)
        return 0
    priv = Path(args.private_key).expanduser()
    if not priv.exists():
        warn("no private key; leaving ciphertext")
        sys.stdout.buffer.write(data)
        return 0
    r = subprocess.run([age_bin, "-d", "-i", str(priv)], input=data, capture_output=True)
    if r.returncode != 0:
        err(f"age decrypt failed: {r.stderr.decode(errors='replace')}")
        return 1
    sys.stdout.buffer.write(r.stdout)
    return 0


def _add_backend_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-b",
        "--backend",
        choices=BACKEND_CHOICES,
        default="subprocess",
        help="Git backend (default: subprocess; other backends fall back to subprocess for unsupported operations)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git_kit.py",
        description="Unified git maintenance CLI with pluggable backends.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Original-script mapping:\n"
            "  checkout_previous.py                ->  checkout-previous\n"
            "  cut30.py                            ->  cut\n"
            "  delcomit.py                         ->  del-remote-commits\n"
            "  git-age-filter.py                   ->  age-filter\n"
            "  lastncommits.py                     ->  list-added\n"
            "  restore_all_historical_deletions.py ->  restore-deleted\n"
            "  rmcommits.py                        ->  rm-commits\n"
            "  squash_deletions.py                 ->  squash-deletions\n"
            "  stage_deleted_files.py              ->  stage-deleted\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "checkout-previous",
        help="Checkout HEAD^ in repos whose HEAD subject matches a prefix",
    )
    p.add_argument(
        "-C",
        "--repo",
        default=".",
        help="Directory containing repositories (default: .)",
    )
    p.add_argument(
        "--prefix",
        default="2026-08-29",
        help="Latest commit subject must start with this prefix (default: 2026-08-29)",
    )
    p.add_argument("-y", "--yes", action="store_true", help="Skip the interactive confirmation")
    _add_backend_arg(p)
    p.set_defaults(func=cmd_checkout_previous)
    p = sub.add_parser(
        "rm-commits",
        help="Reset branch to drop commits older than N days (creates a backup branch)",
    )
    p.add_argument("-C", "--repo", default=".")
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Delete commits older than N days (default: 30)",
    )
    p.add_argument("-y", "--yes", action="store_true")
    _add_backend_arg(p)
    p.set_defaults(func=cmd_rm_commits)
    p = sub.add_parser("cut", help="Rewrite branch dropping commits older than N days")
    p.add_argument("-C", "--repo", default=".")
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Delete commits older than N days (default: 30)",
    )
    p.add_argument(
        "--method",
        choices=("squash", "orphan"),
        default="squash",
        help="Rewrite method (default: squash)",
    )
    p.add_argument("-y", "--yes", action="store_true")
    _add_backend_arg(p)
    p.set_defaults(func=cmd_cut)
    p = sub.add_parser(
        "del-remote-commits",
        help="Reset branch and force-push (deletes commits on remote)",
    )
    p.add_argument("-C", "--repo", default=".")
    p.add_argument(
        "--branch",
        default="master",
        help="Branch to clean (default: master; falls back to main)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=7,
        help="Delete commits older than N days (default: 7)",
    )
    p.add_argument("--no-push", action="store_true", help="Do not force-push; only reset locally")
    p.add_argument("-y", "--yes", action="store_true")
    _add_backend_arg(p)
    p.set_defaults(func=cmd_del_remote_commits)
    p = sub.add_parser("list-added", help="List files added in the last N commits")
    p.add_argument("-C", "--repo", default=".")
    p.add_argument("-n", type=int, default=10, help="Number of commits to look back (default: 10)")
    _add_backend_arg(p)
    p.set_defaults(func=cmd_list_added)
    p = sub.add_parser("restore-deleted", help="Restore every file ever deleted in history")
    p.add_argument("-C", "--repo", default=".")
    p.add_argument(
        "-m",
        "--message",
        default="removed files",
        help='Commit message for the restore commit (default: "removed files")',
    )
    _add_backend_arg(p)
    p.set_defaults(func=cmd_restore_deleted)
    p = sub.add_parser("stage-deleted", help="Stage pending deletions that match history, then commit")
    p.add_argument("-C", "--repo", default=".")
    p.add_argument(
        "-m",
        "--message",
        default="removed files",
        help='Commit message (default: "removed files")',
    )
    _add_backend_arg(p)
    p.set_defaults(func=cmd_stage_deleted)
    p = sub.add_parser("squash-deletions", help="Amend last commit with pending historical deletions")
    p.add_argument("-C", "--repo", default=".")
    _add_backend_arg(p)
    p.set_defaults(func=cmd_squash_deletions)
    p = sub.add_parser(
        "age-filter",
        help="age-based git clean/smudge filter (reads/writes stdin/stdout)",
    )
    p.add_argument("mode", choices=("clean", "smudge"), help='"clean" encrypts; "smudge" decrypts')
    p.add_argument("--age-bin", default=None, help="Path to the age binary (default: auto-detect)")
    p.add_argument(
        "--public-key",
        default=str(Path.home() / ".config" / "age" / "public.key"),
        help="Public key file (default: ~/.config/age/public.key)",
    )
    p.add_argument(
        "--private-key",
        default=str(Path.home() / ".config" / "age" / "keys.txt"),
        help="Private key file (default: ~/.config/age/keys.txt)",
    )
    p.set_defaults(func=cmd_age_filter)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(130)
