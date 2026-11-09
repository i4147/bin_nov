#!/data/data/com.termux/files/usr/bin/env python
"""Bulk git chores: scan local changes, pull all clones, create repositories, edit ``.gitignore``.
``scan-changes`` reports the status of every repository below a path, ``pull-all`` fast-forwards all of them in parallel, ``newrepo`` creates a GitHub repository through the API and pushes to it (with an optional ``--env-file`` token) and ``gitignore-add`` appends a line to the ``.gitignore`` files it finds.
Usage: gittools {scan-changes,pull-all,newrepo,gitignore-add} [options] Positional arguments: path line paths ...
Subcommands: scan-changes pull-all newrepo gitignore-add Options: --name --private --description --message --branch --env-file Examples: $ gittools pull-all ~/code $ gittools newrepo --name demo --push Command-line script: run ``python gittools.py`` or the ``gittools`` command.
The module is importable as well."""

import argparse
import concurrent.futures
import json
import os
import pathlib
import shutil
import subprocess
from typing import Any, Optional
import urllib.error
import urllib.request

from dh import MAX_WORKERS


_SOURCE_CITATION: str = "[cite: 1]"


def run_git(
    args: list[str], cwd: Optional[pathlib.Path] = None, check: bool = True, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=check, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        msg = f"git command timed out: {' '.join(args)}"
        raise RuntimeError(msg) from e
    except FileNotFoundError as e:
        msg = "git executable not found"
        raise RuntimeError(msg) from e


def get_current_branch(repo_path: pathlib.Path) -> str:
    res = run_git(["branch", "--show-current"], cwd=repo_path, check=False)
    branch = res.stdout.strip()
    if branch:
        return branch
    res2 = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path, check=True)
    branch2 = res2.stdout.strip()
    if branch2 == "HEAD":
        msg = "detached HEAD"
        raise RuntimeError(msg)
    return branch2


def get_github_token(env_file: Optional[pathlib.Path] = None) -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    path = (env_file or pathlib.Path.home() / ".env").expanduser()
    if not path.exists():
        msg = f"GITHUB_TOKEN not found and {path} does not exist"
        raise RuntimeError(msg)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if line.startswith("GITHUB_TOKEN="):
            val = line.split("=", 1)[1].strip().strip("\"'")
            if val:
                return val
    msg = f"GITHUB_TOKEN not found in {path}"
    raise RuntimeError(msg)


def github_api_request(token: str, method: str, url: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "git-helper-tool/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            err_json = json.loads(err_body)
        except Exception:
            err_json = {"message": err_body}
        msg = f"GitHub API error {e.code}: {err_json.get('message', err_body)}"
        raise RuntimeError(msg) from e


def create_github_repo(
    token: str, name: str, private: bool, description: Optional[str], auto_init: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "private": private, "auto_init": auto_init}
    if description:
        payload["description"] = description
    try:
        return github_api_request(token, "POST", "https://api.github.com/user/repos", payload)
    except RuntimeError as e:
        if "422" in str(e):
            user_info = github_api_request(token, "GET", "https://api.github.com/user")
            login = user_info.get("login", "")
            return {
                "clone_url": f"https://github.com/{login}/{name}.git",
                "ssh_url": f"git@github.com:{login}/{name}.git",
                "html_url": f"https://github.com/{login}/{name}",
            }
        raise


def find_git_repos(root_path: pathlib.Path) -> list[pathlib.Path]:
    repos: list[pathlib.Path] = []
    for root, dirs, _ in os.walk(root_path):
        current = pathlib.Path(root)
        if (current / ".git").is_dir():
            repos.append(current)
            dirs[:] = []
    return repos


def cmd_scan_changes(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.path).resolve()
    repos = find_git_repos(root)
    if not repos:
        print("No git repositories found.")
        return 0

    def check_repo(repo: pathlib.Path) -> tuple[pathlib.Path, bool, str]:
        try:
            res = run_git(["status", "--porcelain"], cwd=repo, check=False, timeout=10)
            if res.returncode != 0:
                return (repo, False, f"Git error: {res.stderr.strip()}")
            lines = [line for line in res.stdout.splitlines() if line.strip()]
            if not lines:
                return (repo, False, "Clean")
            detail = ", ".join(lines[:5])
            if len(lines) > 5:
                detail += f" (+{len(lines) - 5} more)"
            return (repo, True, detail)
        except Exception as e:
            return (repo, False, f"Error: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(check_repo, repos))
    dirty_repos = [r for r in results if r[1]]
    for repo, dirty, detail in results:
        if dirty:
            print(f"✗ {repo.name}: CHANGES DETECTED\n  └─ {detail}")
        else:
            print(f"✓ {repo.name}: No changes\n  └─ {detail}")
    if dirty_repos:
        print(f"Found {len(dirty_repos)} repository(ies) with changes")
    else:
        print("All repositories are clean")
    return 0


def cmd_pull_all(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.path).resolve()
    repos = find_git_repos(root)
    if not repos:
        print("No git repositories found.")
        return 0

    def pull_repo(repo: pathlib.Path) -> tuple[pathlib.Path, bool, str]:
        try:
            branch = get_current_branch(repo)
            res = run_git(["remote"], cwd=repo, check=False)
            if res.returncode != 0 or not res.stdout.strip():
                return (repo, False, "No remote configured")
            remote = res.stdout.splitlines()[0]
            run_git(["pull", remote, branch], cwd=repo, check=True, timeout=120)
            return (repo, True, "Pulled successfully")
        except Exception as e:
            return (repo, False, str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(executor.map(pull_repo, repos))
    for repo, success, msg in results:
        if success:
            print(f"✅ {repo.relative_to(root)}: {msg}")
        else:
            print(f"❌ {repo.relative_to(root)}: {msg}")
    return 0


def cmd_new_repo(args: argparse.Namespace) -> int:
    token = get_github_token(pathlib.Path(args.env_file) if args.env_file else None)
    cwd = pathlib.Path.cwd().resolve()
    name = args.name or cwd.name
    user_info = github_api_request(token, "GET", "https://api.github.com/user")
    login = user_info.get("login", "")
    repo_info = create_github_repo(token, name, args.private, args.description, False)
    clone_url = repo_info.get("clone_url") or repo_info.get("ssh_url")
    if not clone_url:
        msg = "Could not determine remote URL"
        raise RuntimeError(msg)
    if not (cwd / ".git").is_dir():
        run_git(["init", "-b", args.branch], cwd=cwd, check=True)
    gitignore_dest = cwd / ".gitignore"
    gitignore_global = pathlib.Path.home() / ".gitignore"
    if not gitignore_dest.exists() and gitignore_global.exists():
        shutil.copy2(gitignore_global, gitignore_dest)
    run_git(["add", "-A"], cwd=cwd, check=True)
    status_res = run_git(["status", "--porcelain"], cwd=cwd, check=True)
    if status_res.stdout.strip():
        run_git(["commit", "-m", args.message], cwd=cwd, check=True)
    run_git(["branch", "-M", args.branch], cwd=cwd, check=True)
    remote_res = run_git(["remote", "get-url", "origin"], cwd=cwd, check=False)
    if remote_res.returncode == 0:
        if remote_res.stdout.strip() != clone_url:
            run_git(["remote", "set-url", "origin", clone_url], cwd=cwd, check=True)
    else:
        run_git(["remote", "add", "origin", clone_url], cwd=cwd, check=True)
    run_git(["push", "-u", "origin", args.branch], cwd=cwd, check=True)
    print(f"https://github.com/{login}/{name}")
    return 0


def cmd_gitignore_add(args: argparse.Namespace) -> int:
    line = args.line.strip()
    if not line:
        msg = "line cannot be empty"
        raise RuntimeError(msg)
    paths = [pathlib.Path(p).resolve() for p in (args.paths or ["."])]
    gi_files: list[pathlib.Path] = []
    for p in paths:
        if p.is_file() and p.name == ".gitignore":
            gi_files.append(p)
        elif p.is_dir():
            gi_files.extend([f for f in p.rglob(".gitignore") if f.is_file()])
    modified = 0
    for gi in gi_files:
        try:
            content = gi.read_text(encoding="utf-8", errors="ignore")
            if line in content.splitlines():
                continue
            if content and not content.endswith("\n"):
                content += "\n"
            content += line + "\n"
            gi.write_text(content, encoding="utf-8")
            modified += 1
            print(f"Updated {gi}")
        except Exception as e:
            print(f"Failed {gi}: {e}", file=sys.stderr)
    print(f"Modified {modified} file(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="git-helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    p_scan = subparsers.add_parser("scan-changes")
    p_scan.add_argument("path", nargs="?", default=".")
    p_scan.set_defaults(func=cmd_scan_changes)
    p_pull = subparsers.add_parser("pull-all")
    p_pull.add_argument("path", nargs="?", default=".")
    p_pull.set_defaults(func=cmd_pull_all)
    p_new = subparsers.add_parser("newrepo")
    p_new.add_argument("--name", default=None)
    p_new.add_argument("--private", action="store_true")
    p_new.add_argument("--description", default=None)
    p_new.add_argument("--message", default="Initial commit")
    p_new.add_argument("--branch", default="main")
    p_new.add_argument("--env-file", default=None)
    p_new.set_defaults(func=cmd_new_repo)
    p_gi = subparsers.add_parser("gitignore-add")
    p_gi.add_argument("line")
    p_gi.add_argument("paths", nargs="*")
    p_gi.set_defaults(func=cmd_gitignore_add)
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
