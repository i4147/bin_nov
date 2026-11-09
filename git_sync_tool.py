#!/data/data/com.termux/files/usr/bin/env python
"""Bulk git sync: pull/restore every repo, rebase-sync, publish to GitHub.
Analysis / deduplication ------------------------ - ``autopull.py`` (subprocess ``git pull --ff-only``), ``gitpuller.py`` (same via GitPython) and ``gitrestorer.py`` (same walker but it runs ``git restore .`` despite its name) are one ``pull-all`` command; the restore variant is ``pull-all --restore`` (discards local changes, hence ``--yes`` is required).
GitPython was dropped: plain ``git -C`` subprocess calls do the same with no dependency.
- ``gitfix.py`` rebase-onto-upstream + force-with-lease is ``sync-branch`` (the buggy plumbing alternative was removed; the rebase itself is unchanged, guarded by ``--yes`` and a clean tree).
- ``git_pull_newrepo.py`` (create a GitHub repo for local repos without a remote and push) is ``publish``; it uses the GitHub REST API via stdlib ``urllib`` and reads the token from ``$GITHUB_TOKEN`` (the original also loaded ``.env`` via python-dotenv - a tiny built-in ``.env`` reader covers that file).
Nothing network- or history-changing runs without an explicit subcommand; destructive subcommands additionally need ``--yes`` or will only report with ``--dry-run``.
Examples -------- python3 git_sync_tool.py pull-all ~/projects python3 git_sync_tool.py pull-all .
--restore --dry-run python3 git_sync_tool.py sync-branch .
--dry-run python3 git_sync_tool.py publish ~/projects --dry-run"""

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request


GITHUB_API: str = "https://api.github.com"


def run_git(repo: Path, *git_args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git -C repo ...`` capturing output."""
    proc = subprocess.run(["git", "-C", str(repo), *git_args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(git_args)} failed")
    return proc


def git_out(repo: Path, *git_args: str) -> str | None:
    """Stripped stdout of a git command, or None on failure."""
    proc: subprocess.CompletedProcess[str] = run_git(repo, *git_args, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def find_repos(root: Path) -> list[Path]:
    """Every repo below *root* (the root itself included if it is one)."""
    if (root / ".git").exists():
        return [root]
    return sorted({p.parent for p in root.rglob(".git")})


def is_clean(repo: Path) -> bool:
    """True when the working tree has no changes."""
    return (git_out(repo, "status", "--porcelain") or "") == ""


# -------------------------------------------------------------- pull-all
def cmd_pull_all(args: argparse.Namespace) -> int:
    """Pull (or restore) every repository below a root."""
    root: Path = args.root
    repos: list[Path] = find_repos(root)
    if not repos:
        print("No git repositories found.")
        return 0
    action: str = "restore ." if args.restore else "pull"
    if args.restore and not args.yes and not args.dry_run:
        print("Error: --restore discards local changes; add --yes (or --dry-run).", file=sys.stderr)
        return 2
    print(f"Found {len(repos)} repo(s); action: git {action}")
    ok: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for repo in repos:
        label: object = repo.relative_to(root) if root in repo.parents else repo
        branch: str = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") or "?"
        if args.dry_run:
            print(f"  would run in {label} [{branch}]: git {action}")
            ok.append(repo)
            continue
        if not args.restore and git_out(repo, "remote") in (None, ""):
            failed.append((repo, "no remote configured"))
            print(f"  {label} [{branch}]: skipped (no remote)")
            continue
        command: list[str] = ["restore", "."] if args.restore else ["pull", "--ff-only"]
        proc: subprocess.CompletedProcess[str] = run_git(repo, *command, check=False)
        if proc.returncode == 0:
            ok.append(repo)
            note: str = (proc.stdout.strip().splitlines() or ["done"])[-1]
            print(f"  {label} [{branch}]: {note}")
        else:
            failed.append((repo, proc.stderr.strip()[:200]))
            print(f"  {label} [{branch}]: FAILED {proc.stderr.strip()[:200]}")
    print(f"\nSummary: {len(ok)} ok, {len(failed)} failed/skipped")
    return 1 if failed else 0


# ------------------------------------------------------------ sync-branch
def cmd_sync_branch(args: argparse.Namespace) -> int:
    """Rebase the current branch onto origin's main/master and push."""
    repo: Path = args.repo
    branch: str | None = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        print("Error: not a git repository.", file=sys.stderr)
        return 1
    if not is_clean(repo) and not args.dry_run:
        print("Error: working tree is dirty; commit or stash first.", file=sys.stderr)
        return 1
    steps: list[list[str]] = [
        ["fetch", args.remote],
        ["rebase", "--onto", "@{upstream-target}", "@{head}", "HEAD", "--allow-unrelated-histories"],
        ["push", "--force-with-lease", args.remote, f"{branch}:{branch}"],
    ]
    if args.dry_run:
        print(f"Would sync branch {branch!r} in {repo}:")
        for step in steps:
            print(
                f"  git {' '.join(step)}".replace("@{upstream-target}", f"{args.remote}/main|master").replace(
                    "@{head}", "HEAD"
                )
            )
        return 0
    if not args.yes:
        print("Error: sync-branch rewrites history; add --yes.", file=sys.stderr)
        return 2
    try:
        run_git(repo, "fetch", args.remote)
        upstream: str | None = next(
            (
                ref
                for ref in (f"{args.remote}/master", f"{args.remote}/main")
                if git_out(repo, "rev-parse", "--verify", ref)
            ),
            None,
        )
        if upstream is None:
            print(f"Error: neither {args.remote}/master nor {args.remote}/main exists.", file=sys.stderr)
            return 1
        run_git(repo, "rebase", "--onto", upstream, "HEAD", "HEAD", "--allow-unrelated-histories")
        run_git(repo, "push", "--force-with-lease", args.remote, f"{branch}:{branch}")
    except RuntimeError as exc:
        print(f"Sync failed: {exc}\n(If a rebase is in progress: git -C {repo} rebase --abort)", file=sys.stderr)
        run_git(repo, "rebase", "--abort", check=False)
        return 1
    print(f"Synced {branch} onto {upstream} and pushed.")
    return 0


# ---------------------------------------------------------------- publish
def read_dotenv_token() -> str | None:
    """Minimal .env reader for GITHUB_TOKEN (no python-dotenv needed)."""
    env_path: Path = Path(".env")
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "GITHUB_TOKEN":
            return value.strip().strip('"').strip("'")
    return None


def github_request(token: str, method: str, url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    """Small GitHub REST helper returning the parsed JSON body."""
    data: bytes | None = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"token {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        body: bytes = response.read()
        return json.loads(body.decode()) if body else {}


def cmd_publish(args: argparse.Namespace) -> int:
    """Create GitHub repos for local repos that lack a remote, then push."""
    token: str | None = os.environ.get("GITHUB_TOKEN") or read_dotenv_token()
    repos: list[Path] = [r for r in find_repos(args.root) if git_out(r, "remote") in (None, "")]
    if not repos:
        print("Every repo already has a remote; nothing to publish.")
        return 0
    if token is None and not args.dry_run:
        print("Error: set GITHUB_TOKEN (env or .env).", file=sys.stderr)
        return 2
    username: str = "?"
    if token is not None and not args.dry_run:
        me: dict[str, object] = github_request(token, "GET", f"{GITHUB_API}/user")
        username = str(me.get("login", "?"))
    for repo in repos:
        name: str = repo.resolve().name
        remote_url: str = f"https://github.com/{username}/{name}.git"
        if args.dry_run:
            print(f"  would create github repo {name!r} for {repo} and push to {remote_url}")
            continue
        try:
            github_request(token or "", "POST", f"{GITHUB_API}/user/repos", {"name": name, "private": args.private})
            run_git(repo, "remote", "add", "origin", remote_url)
            branch: str = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") or "main"
            run_git(repo, "push", "-u", "origin", branch)
            print(f"  published {repo} -> {remote_url} ({branch})")
        except (RuntimeError, OSError) as exc:
            print(f"  failed {repo}: {exc}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description="Bulk git pull / sync / publish.")
    sub = parser.add_subparsers(dest="command", required=True)
    pa = sub.add_parser("pull-all", help="git pull --ff-only in every repo below ROOT")
    pa.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    pa.add_argument("--restore", action="store_true", help="git restore . instead of pull")
    pa.add_argument("--yes", action="store_true", help="Confirm --restore")
    pa.add_argument("-n", "--dry-run", action="store_true")
    pa.set_defaults(handler=cmd_pull_all)
    sb = sub.add_parser("sync-branch", help="Rebase onto upstream main/master + force-with-lease")
    sb.add_argument("repo", nargs="?", type=Path, default=Path.cwd())
    sb.add_argument("--remote", default="origin")
    sb.add_argument("--yes", action="store_true", help="Confirm the history rewrite")
    sb.add_argument("-n", "--dry-run", action="store_true")
    sb.set_defaults(handler=cmd_sync_branch)
    pu = sub.add_parser("publish", help="Create GitHub repos for remote-less local repos")
    pu.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    pu.add_argument("--private", action="store_true", help="Create private repos")
    pu.add_argument("-n", "--dry-run", action="store_true")
    pu.set_defaults(handler=cmd_publish)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args: argparse.Namespace = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
