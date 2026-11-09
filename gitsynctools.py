#!/data/data/com.termux/files/usr/bin/env python
"""Commit, push and sync a repository in one command.
The ``commit``, ``push`` and ``sync`` subcommands stage the changes (optionally formatting them with black first), build the commit message, create or fork the GitHub repository when asked, configure the token remote from the environment and push the current branch.
Usage: gitsynctools {commit,push,sync} [options] Subcommands: commit push sync Options: --commit --allow-dirty --repo --search-parent --init-if-missing --remote-name --branch --message --message-prefix --add-mode ...
plus 22 more; run ``gitsynctools --help`` for the full list.
Examples: $ gitsynctools sync --message 'update' --set-upstream Command-line script: run ``python gitsynctools.py`` or the ``gitsynctools`` command.
The module is importable as well."""

import argparse
import contextlib
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Optional, Sequence

from dotenv import load_dotenv
from git import GitCommandError, InvalidGitRepositoryError, Repo
from github import Github, GithubException


def load_env() -> None:
    path = Path.home() / ".env"
    if path.exists():
        load_dotenv(path)


def token_names(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_token(names: Sequence[str]) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def open_repo(path: Path, search_parent: bool, init_if_missing: bool) -> Repo:
    try:
        return Repo(path, search_parent_directories=search_parent)
    except InvalidGitRepositoryError:
        if init_if_missing:
            repo = Repo.init(path)
            print("✅ Repository initialized.")
            return repo
        print("Error: Not a git repository.", file=sys.stderr)
        sys.exit(1)


def repo_root(repo: Repo) -> Path:
    return Path(repo.working_tree_dir or ".")


def ensure_gitignore(repo: Repo, mode: str, source: Path, dest: Path) -> None:
    if mode == "none":
        return
    root = repo_root(repo)
    dest_path = dest if dest.is_absolute() else root / dest
    if dest_path.exists():
        print(f"{dest_path} already exists.")
        return
    source_path = source.expanduser()
    if not source_path.exists():
        print(f"{source_path} does not exist. Skipping.")
        return
    if mode == "copy":
        shutil.copy(source_path, dest_path)
        print(f"Copied {source_path} -> {dest_path}")
    elif mode == "symlink":
        dest_path.symlink_to(source_path)
        print(f"Symlinked {source_path} -> {dest_path}")


def find_python_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for path in root.rglob("*.py"):
        if path.is_file():
            files.add(path)
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix:
            continue
        try:
            first = path.open(encoding="utf-8", errors="ignore").readline().strip()
        except OSError:
            continue
        if first.startswith("#!") and "python" in first.lower():
            files.add(path)
    return sorted(files)


def run_cmd(cmd: Sequence[str]) -> bool:
    try:
        subprocess.check_call(cmd)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"Command failed: {' '.join(cmd)}: {exc}", file=sys.stderr)
        return False


def format_black(repo: Repo, black_cmd: str) -> bool:
    root = repo_root(repo)
    files = find_python_files(root)
    if not files:
        print("No Python files found.")
        return True
    print("Formatting Python files with black:")
    for path in files:
        print("->", path)
        if not run_cmd([black_cmd, str(path)]):
            return False
    return True


def current_message(args: argparse.Namespace) -> str:
    if args.message:
        return args.message
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{args.message_prefix}{stamp}"


def active_branch(repo: Repo, branch: Optional[str]) -> str:
    if branch:
        return branch
    try:
        return repo.active_branch.name
    except TypeError:
        print("In detached HEAD state. Skipping push.", file=sys.stderr)
        sys.exit(1)


def stage_and_commit(repo: Repo, message: str, add_mode: str) -> Optional[str]:
    if add_mode == "all":
        repo.git.add("--all")
    else:
        repo.index.add("*")
    try:
        repo.git.commit("-m", message)
        commit = repo.head.commit
        print(f'✅ Committed with message: "{message}"')
        print(f"Commit hash: {commit.hexsha[:7]}")
        return commit.hexsha
    except GitCommandError as exc:
        text = str(exc).lower()
        if "nothing to commit" in text or "no changes" in text:
            print("No changes to commit.")
            return None
        print(f"❌ Commit failed: {exc}", file=sys.stderr)
        sys.exit(1)


def parse_github_url(url: str) -> Optional[tuple[str, str]]:
    patterns = [
        r"https://(?:[^@/]+@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
        r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return match.group(1), match.group(2)
    return None


def build_token_url(url: str, username: str, token: str, fmt: str) -> str:
    if "@github.com" in url and "github.com" in url and "https://" in url:
        return url
    auth = f"oauth2:{token}" if fmt == "oauth2" else f"{username}:{token}"
    if url.startswith("https://github.com/"):
        return url.replace("https://github.com/", f"https://{auth}@github.com/")
    if url.startswith("git@github.com:"):
        base = url.replace("git@github.com:", "https://github.com/")
        return base.replace("https://github.com/", f"https://{auth}@github.com/")
    return url


def github_login(token: str, override: Optional[str]) -> str:
    if override:
        return override
    return Github(token).get_user().login


def create_repo(
    token: str,
    name: str,
    description: Optional[str],
    private: bool,
    auto_init: bool,
    reuse_existing: bool,
) -> str:
    gh = Github(token)
    user = gh.get_user()
    if reuse_existing:
        try:
            repo = user.get_repo(name)
            print(f"Remote repository '{name}' already exists.")
            return repo.clone_url
        except GithubException:
            pass
    print(f"Creating remote repository '{name}'...")
    repo = user.create_repo(
        name=name,
        description=description,
        private=private,
        auto_init=auto_init,
    )
    return repo.clone_url


def fork_repo(token: str, owner: str, name: str) -> str:
    gh = Github(token)
    user = gh.get_user()
    try:
        fork = user.get_repo(name)
        print(f"Fork already exists: {fork.full_name}")
        return fork.clone_url
    except GithubException:
        upstream = gh.get_repo(f"{owner}/{name}")
        print(f"Forking {owner}/{name}...")
        fork = user.create_fork(upstream)
        print(f"Created fork: {fork.full_name}")
        return fork.clone_url


def sanitize_repo_name(name: str) -> str:
    return re.sub(r"[^\w\-\.]", "-", name).lower()


def ensure_remote(
    repo: Repo,
    args: argparse.Namespace,
    token: Optional[str],
    github_user: Optional[str],
) -> bool:
    remote_name = args.remote_name
    names = [remote.name for remote in repo.remotes]
    if remote_name in names:
        if args.fork_origin and token and github_user:
            remote = repo.remote(remote_name)
            parsed = parse_github_url(remote.url)
            if parsed:
                owner, name = parsed
                if owner.lower() != github_user.lower():
                    url = fork_repo(token, owner, name)
                    remote.set_url(url)
                    print(f"Updated remote '{remote_name}' to fork: {url}")
                    return True
        return False
    if args.create_repo:
        if not token:
            print(
                "GITHUB_TOKEN not found. Cannot create remote repository.",
                file=sys.stderr,
            )
            sys.exit(1)
        name = sanitize_repo_name(repo_root(repo).name)
        url = create_repo(
            token,
            name,
            args.description,
            args.private,
            args.auto_init,
            args.reuse_existing_repo,
        )
        repo.create_remote(remote_name, url)
        print(f"Added remote '{remote_name}': {url}")
        return True
    return False


def ensure_fork_remote(
    repo: Repo,
    upstream_owner: str,
    upstream_name: str,
    token: str,
    github_user: Optional[str],
    fork_remote_name: str,
    origin_remote_name: str,
) -> str:
    login = github_login(token, github_user)
    if upstream_owner.lower() == login.lower():
        return origin_remote_name
    url = fork_repo(token, upstream_owner, upstream_name)
    names = [remote.name for remote in repo.remotes]
    if fork_remote_name in names:
        repo.remote(fork_remote_name).set_url(url)
    else:
        repo.create_remote(fork_remote_name, url)
    print(f"Using fork remote '{fork_remote_name}': {url}")
    return fork_remote_name


def push_remote(
    repo: Repo,
    remote_name: str,
    branch: str,
    token_auth: str,
    token: Optional[str],
    github_user: Optional[str],
    token_url_format: str,
    set_upstream: bool,
) -> None:
    try:
        remote = repo.remote(remote_name)
    except ValueError:
        print(f"❌ Remote '{remote_name}' not configured. Skipping push.", file=sys.stderr)
        return
    original_url = remote.url
    auth_applied = False
    if token_auth in ("temporary", "persistent") and token and github_user:
        new_url = build_token_url(original_url, github_user, token, token_url_format)
        if new_url != original_url:
            remote.set_url(new_url)
            auth_applied = True
    try:
        if set_upstream:
            repo.git.push("--set-upstream", remote_name, branch)
        else:
            remote.push(refspec=f"{branch}:{branch}")
        print(f"✅ Successfully pushed to {remote_name}/{branch}")
    except GitCommandError as exc:
        text = str(exc)
        print(f"❌ Push failed: {exc}", file=sys.stderr)
        if "403" in text or "401" in text:
            print("🔐 Authentication failed. Check your GitHub token.", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if auth_applied and token_auth == "temporary":
            remote.set_url(original_url)


def cmd_commit(args: argparse.Namespace) -> int:
    load_env()
    repo = open_repo(args.repo, args.search_parent, args.init_if_missing)
    ensure_gitignore(repo, args.gitignore_mode, args.gitignore_source, args.gitignore_dest)
    if args.format_black and not format_black(repo, args.black_cmd):
        sys.exit(1)
    stage_and_commit(repo, current_message(args), args.add_mode)
    print("Done.")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    load_env()
    names = token_names(args.token_env_vars)
    token = get_token(names)
    if (args.create_repo or args.fork_origin or args.push_to_fork or args.token_auth != "none") and not token:
        print("GITHUB_TOKEN not found.", file=sys.stderr)
        sys.exit(1)
    repo = open_repo(args.repo, args.search_parent, args.init_if_missing)
    ensure_gitignore(repo, args.gitignore_mode, args.gitignore_source, args.gitignore_dest)
    if args.format_black and not format_black(repo, args.black_cmd):
        sys.exit(1)
    branch = active_branch(repo, args.branch)
    commit_hash = stage_and_commit(repo, current_message(args), args.add_mode)
    if commit_hash is None:
        if args.fail_if_no_changes:
            print("No changes to commit.", file=sys.stderr)
            sys.exit(1)
        if not args.push_if_no_changes:
            print("Skipping push because there are no changes.")
            return 0
    github_user: Optional[str] = args.github_user
    if token and (args.create_repo or args.fork_origin or args.push_to_fork or args.token_auth != "none"):
        try:
            github_user = github_login(token, github_user)
        except Exception as exc:
            print(f"Error getting GitHub username: {exc}", file=sys.stderr)
            if args.create_repo or args.fork_origin:
                sys.exit(1)
    changed = ensure_remote(repo, args, token, github_user)
    push_remote_name = args.remote_name
    if args.push_to_fork and token:
        try:
            origin = repo.remote(args.remote_name)
        except ValueError:
            origin = None
        if origin:
            parsed = parse_github_url(origin.url)
            if parsed:
                owner, name = parsed
                login = github_user or github_login(token, None)
                if owner.lower() != login.lower():
                    push_remote_name = ensure_fork_remote(
                        repo,
                        owner,
                        name,
                        token,
                        github_user,
                        args.fork_remote_name,
                        args.remote_name,
                    )
    if push_remote_name not in [remote.name for remote in repo.remotes]:
        print(f"⚠️ Remote '{push_remote_name}' not configured. Changes committed locally only.")
        if args.require_remote:
            sys.exit(1)
        return 0
    set_upstream = args.set_upstream or changed
    auth_mode = args.token_auth if push_remote_name == args.remote_name else args.fork_token_auth
    push_remote(
        repo,
        push_remote_name,
        branch,
        auth_mode,
        token,
        github_user,
        args.token_url_format,
        set_upstream,
    )
    print("🎉 All done! Changes are now on GitHub.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    load_env()
    names = token_names(args.token_env_vars)
    token = get_token(names)
    if not token:
        print("GITHUB_TOKEN not found.", file=sys.stderr)
        sys.exit(1)
    repo = open_repo(args.repo, args.search_parent, args.init_if_missing)
    ensure_gitignore(repo, args.gitignore_mode, args.gitignore_source, args.gitignore_dest)
    if args.commit:
        stage_and_commit(repo, current_message(args), args.add_mode)
    if args.branch:
        repo.git.checkout(args.branch)
    branch = active_branch(repo, args.branch)
    remote_name = args.remote_name
    try:
        remote = repo.remote(remote_name)
    except ValueError:
        print(f"Remote '{remote_name}' not found.", file=sys.stderr)
        sys.exit(1)
    print("Fetching updates...")
    remote.fetch()
    try:
        branch_obj = repo.active_branch
    except TypeError:
        print("In detached HEAD state. Skipping sync.", file=sys.stderr)
        sys.exit(1)
    if not branch_obj.tracking_branch():
        with contextlib.suppress(Exception):
            branch_obj.set_tracking_branch(remote.refs[branch])
    print(f"Pulling latest changes into '{branch}'...")
    remote.pull()
    github_user = args.github_user or github_login(token, None)
    parsed = parse_github_url(remote.url)
    if not parsed:
        print(f"Could not parse GitHub remote URL: {remote.url}", file=sys.stderr)
        sys.exit(1)
    owner, name = parsed
    if owner.lower() == github_user.lower():
        if repo.is_dirty(untracked_files=True) and not args.allow_dirty:
            print(
                "Uncommitted local changes. Please commit before pushing.",
                file=sys.stderr,
            )
            sys.exit(1)
        push_remote(
            repo,
            remote_name,
            branch,
            args.token_auth,
            token,
            github_user,
            args.token_url_format,
            args.set_upstream,
        )
    else:
        if not args.push_to_fork:
            print("You are not the owner and --no-push-to-fork was given.")
            return 0
        fork_remote = ensure_fork_remote(
            repo,
            owner,
            name,
            token,
            github_user,
            args.fork_remote_name,
            remote_name,
        )
        push_remote(
            repo,
            fork_remote,
            branch,
            args.fork_token_auth,
            token,
            github_user,
            args.token_url_format,
            args.set_upstream,
        )
    print("Successfully synced.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merged git commit/push/sync tool")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, push_to_fork_default: bool = False) -> None:
        p.add_argument("--repo", type=Path, default=Path())
        p.add_argument("--search-parent", action=argparse.BooleanOptionalAction, default=True)
        p.add_argument("--init-if-missing", action="store_true")
        p.add_argument("--remote-name", default="origin")
        p.add_argument("--branch", default=None)
        p.add_argument("--message", default=None)
        p.add_argument("--message-prefix", default="")
        p.add_argument("--add-mode", choices=["all", "star"], default="all")
        p.add_argument("--gitignore-mode", choices=["none", "copy", "symlink"], default="none")
        p.add_argument("--gitignore-source", type=Path, default=Path.home() / ".gitignore")
        p.add_argument("--gitignore-dest", type=Path, default=Path(".gitignore"))
        p.add_argument("--format-black", action="store_true")
        p.add_argument("--black-cmd", default="black")
        p.add_argument("--token-env-vars", default="GITHUB_TOKEN,GH_TOKEN,GIT_TOKEN")
        p.add_argument("--github-user", default=None)
        p.add_argument("--create-repo", action="store_true")
        p.add_argument("--fork-origin", action="store_true")
        p.add_argument(
            "--push-to-fork",
            action=argparse.BooleanOptionalAction,
            default=push_to_fork_default,
        )
        p.add_argument("--fork-remote-name", default="fork")
        p.add_argument("--private", action="store_true")
        p.add_argument("--auto-init", action=argparse.BooleanOptionalAction, default=False)
        p.add_argument("--description", default=None)
        p.add_argument("--reuse-existing-repo", action=argparse.BooleanOptionalAction, default=True)
        p.add_argument("--token-auth", choices=["none", "temporary", "persistent"], default="none")
        p.add_argument(
            "--fork-token-auth",
            choices=["none", "temporary", "persistent"],
            default="persistent",
        )
        p.add_argument("--token-url-format", choices=["user", "oauth2"], default="user")
        p.add_argument("--set-upstream", action=argparse.BooleanOptionalAction, default=False)
        p.add_argument("--push-if-no-changes", action=argparse.BooleanOptionalAction, default=True)
        p.add_argument("--fail-if-no-changes", action="store_true")
        p.add_argument("--require-remote", action="store_true")

    commit = sub.add_parser("commit")
    add_common(commit)
    push = sub.add_parser("push")
    add_common(push)
    sync = sub.add_parser("sync")
    add_common(sync, push_to_fork_default=True)
    sync.add_argument("--commit", action="store_true")
    sync.add_argument("--allow-dirty", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "commit":
        return cmd_commit(args)
    if args.command == "push":
        return cmd_push(args)
    if args.command == "sync":
        return cmd_sync(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
